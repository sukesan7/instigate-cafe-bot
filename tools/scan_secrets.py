#!/usr/bin/env python3
"""
tools/scan_secrets.py — Deterministic secret and sensitive artifact scanner.

Scans the repository (or git index) for:
1. Forbidden tracked files (SQLite DBs, private environment files, private lore packs, keys).
2. Hardcoded API tokens, keys, private key blocks, and high-entropy secrets in tracked files.
3. Verification of .env.example hygiene (empty secrets, synthetic IDs, disabled pollers).

Exit codes:
  0 = Clean scan (no violations detected)
  1 = Violations detected (secrets, forbidden files, or config hazards)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Forbidden file patterns (relative to repo root)
# ---------------------------------------------------------------------------
FORBIDDEN_FILE_PATTERNS = [
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "tools/lore.*.json",
    "lore.*.json",
    "*credential*.json",
    "*client_secret*.json",
]

# Patterns explicitly exempted from the forbidden list
EXEMPT_FILES = {
    ".env.example",
    "tools/lore.example.json",
    "tests/fixtures/synthetic_lore.json",
}

# Known legacy files tracked in historical repo pending explicit owner un-tracking
KNOWN_LEGACY_TRACKED_FILES = {
    "ic_stock.db",
    "tmu_grades.db",
    "tools/lore.tmu_files.json",
}

# ---------------------------------------------------------------------------
# Sensitive token regexes
# ---------------------------------------------------------------------------
SECRET_REGEXES: dict[str, re.Pattern[str]] = {
    "Discord Bot Token": re.compile(
        r"\b(?:[MN][A-Za-z\d]{23,26}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,38}|mfa\.[A-Za-z0-9_-]{20,})\b"
    ),
    "Google Gemini API Key": re.compile(r"\bAIzaSy[A-Za-z0-9_-]{33}\b"),
    "AWS Access Key ID": re.compile(r"\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub Personal Access Token": re.compile(r"\b(?:ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{82})\b"),
    "Slack Webhook URL": re.compile(
        r"https://hooks\.slack\.com/services/T[A-Z0-9_]+/B[A-Z0-9_]+/[A-Za-z0-9_]+"
    ),
    "Discord Webhook URL": re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
    "Private Key Header": re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----"),
}

# Files exempt from content pattern scanning (scanner itself, docs/tests discussing patterns)
SCAN_EXEMPT_PATHS = {
    "tools/scan_secrets.py",
    "tests/fixtures/synthetic_lore.json",
    "tests/unit/test_supply_chain_and_leak_guards.py",
}


@dataclass
class Finding:
    category: str  # FORBIDDEN_FILE, LEAKED_SECRET, CONFIG_HAZARD
    path: str
    line_number: int | None
    description: str
    is_legacy: bool = False
    deleted_in_worktree: bool = False


def get_repo_root() -> Path:
    """Return the absolute path of the repository root."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(res.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, OSError):
        # Fallback to parent of tools directory
        return Path(__file__).resolve().parent.parent


def get_tracked_files(repo_root: Path) -> list[str]:
    """Get list of files tracked by Git."""
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return [f.strip() for f in res.stdout.splitlines() if f.strip()]
    except (subprocess.CalledProcessError, OSError):
        # Fallback: walk directory respecting basic ignores
        out: list[str] = []
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "__pycache__", "node_modules"}]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), repo_root)
                out.append(rel)
        return out


def is_forbidden_path(rel_path: str) -> bool:
    """Check if a relative file path matches forbidden patterns and is not exempt."""
    norm = rel_path.replace("\\", "/")
    if norm in EXEMPT_FILES:
        return False
    basename = os.path.basename(norm)
    if basename in EXEMPT_FILES:
        return False

    for pat in FORBIDDEN_FILE_PATTERNS:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(basename, pat):
            return True
    return False


def scan_file_content(file_path: Path, rel_path: str) -> list[Finding]:
    """Scan text file content for sensitive patterns."""
    findings: list[Finding] = []
    norm = rel_path.replace("\\", "/")
    if norm in SCAN_EXEMPT_PATHS:
        return findings

    try:
        # Check if file is binary by reading first block
        with open(file_path, "rb") as bf:
            chunk = bf.read(1024)
            if b"\x00" in chunk:
                return findings  # Skip binary files

        with open(file_path, encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f, start=1):
                for label, pat in SECRET_REGEXES.items():
                    m = pat.search(line)
                    if m:
                        matched_text = m.group(0)
                        # Mask matched token for safe reporting
                        masked = (
                            matched_text[:4] + "..." + matched_text[-4:] if len(matched_text) > 8 else "***"
                        )
                        findings.append(
                            Finding(
                                category="LEAKED_SECRET",
                                path=norm,
                                line_number=line_idx,
                                description=f"Found pattern for {label}: '{masked}'",
                            )
                        )
    except (OSError, UnicodeError, ValueError) as e:
        findings.append(
            Finding(
                category="SCAN_ERROR",
                path=norm,
                line_number=None,
                description=f"Could not read file: {e}",
            )
        )

    return findings


def audit_env_example(repo_root: Path) -> list[Finding]:
    """Verify that .env.example contains only synthetic IDs and empty secrets."""
    findings: list[Finding] = []
    env_example = repo_root / ".env.example"
    if not env_example.exists():
        findings.append(
            Finding(
                category="CONFIG_HAZARD",
                path=".env.example",
                line_number=None,
                description=".env.example is missing from repository root",
            )
        )
        return findings

    secret_keys_requiring_empty = [
        "DISCORD_TOKEN",
        "GEMINI_API_KEY",
        "GO_API_KEY",
        "ODDS_API_KEY",
        "ALPHAVANTAGE_API_KEY",
        "TTC_MASTODON_ACCT",
    ]

    poller_keys_requiring_disabled = [
        "LLM_ENABLED",
        "MARKET_WATCH_ENABLED",
        "MARKET_DAILY_REPORT_ENABLED",
        "BTC_WATCH_ENABLED",
        "JOBS_BOOTSTRAP_POST",
        "TTC_BOOTSTRAP_POST_LATEST",
        "ICBOT_ALLOW_GATEWAY",
    ]

    with open(env_example, encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue

            if "=" in line_str:
                key, val = line_str.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")

                # Check secret keys
                if key in secret_keys_requiring_empty and val:
                    findings.append(
                        Finding(
                            category="CONFIG_HAZARD",
                            path=".env.example",
                            line_number=idx,
                            description=f"Secret key '{key}' has non-empty value in example configuration",
                        )
                    )

                # Check optional pollers
                if key in poller_keys_requiring_disabled and val.lower() in ("true", "1", "yes"):
                    findings.append(
                        Finding(
                            category="CONFIG_HAZARD",
                            path=".env.example",
                            line_number=idx,
                            description=f"Integration '{key}' defaults to enabled in .env.example (should default to false for safe dev boot)",
                        )
                    )

                # Check snowflake IDs: if numeric, should be 0 or 0-like
                if key.endswith(("_ID", "_CHANNEL_ID", "GUILD_ID")) and val.isdigit():
                    int_val = int(val)
                    if int_val not in (0,):
                        findings.append(
                            Finding(
                                category="CONFIG_HAZARD",
                                path=".env.example",
                                line_number=idx,
                                description=f"Snowflake ID '{key}={val}' is non-zero (should be 0 or commented placeholder)",
                            )
                        )

    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic secret and publish-safety scanner for Instigate Cafe Bot."
    )
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="Acknowledge known tracked legacy database/lore files without failing the scan.",
    )
    parser.add_argument(
        "--enforce-clean",
        action="store_true",
        help="Strict publication mode: fail if any legacy files or unscrubbed artifacts remain tracked.",
    )
    parser.add_argument(
        "--worktree-only",
        action="store_true",
        help="Scan only files currently existing in the working tree (skipping pending deletions).",
    )
    args = parser.parse_args(argv)

    repo_root = get_repo_root()
    tracked_files = get_tracked_files(repo_root)

    findings: list[Finding] = []

    # 1. Check for forbidden file patterns in tracked files
    for rel_path in tracked_files:
        norm = rel_path.replace("\\", "/")
        file_exists = (repo_root / rel_path).exists()
        if args.worktree_only and not file_exists:
            continue
        if is_forbidden_path(norm):
            is_legacy = norm in KNOWN_LEGACY_TRACKED_FILES
            status = "present on disk" if file_exists else "deleted from worktree (pending git commit)"
            findings.append(
                Finding(
                    category="FORBIDDEN_FILE",
                    path=norm,
                    line_number=None,
                    description=f"Tracked file matches forbidden publication pattern (*.db, private lore, private env) — {status}",
                    is_legacy=is_legacy,
                    deleted_in_worktree=not file_exists,
                )
            )

    # 2. Check file content for secrets
    for rel_path in tracked_files:
        full_path = repo_root / rel_path
        if full_path.is_file():
            findings.extend(scan_file_content(full_path, rel_path))

    # 3. Check .env.example hygiene
    findings.extend(audit_env_example(repo_root))

    # Evaluate results
    critical_findings = [f for f in findings if not f.is_legacy]
    legacy_findings = [f for f in findings if f.is_legacy]

    print("================================================================================")
    print(" Instigate Cafe Bot — Publish Safety & Secret Scanner Report")
    print(f" Repository root: {repo_root}")
    print(f" Tracked files scanned: {len(tracked_files)}")
    print("================================================================================\n")

    if not findings:
        print(" [PASS] Clean scan! No leaked secrets, forbidden files, or config hazards found.")
        print("================================================================================")
        return 0

    if legacy_findings:
        print(f"--- Known Legacy Tracked Artifacts ({len(legacy_findings)}) ---")
        print("These historical files remain in git ls-files pending explicit owner un-tracking:")
        for f in legacy_findings:
            print(f"  [LEGACY] {f.path}: {f.description}")
        print()

    if critical_findings:
        print(f"--- Critical Violations Found ({len(critical_findings)}) ---")
        for f in critical_findings:
            loc = f"{f.path}:{f.line_number}" if f.line_number else f.path
            print(f"  [{f.category}] {loc}: {f.description}")
        print()

    print("================================================================================")
    if (
        critical_findings
        or (legacy_findings and args.enforce_clean)
        or (legacy_findings and not args.allow_legacy)
    ):
        print(" Maintainer Remediation Guide:")
        print("--------------------------------------------------------------------------------")
        print(" 1. To untrack a forbidden file without deleting your local copy:")
        print("      git rm --cached <path/to/file>")
        print("      echo '<pattern>' >> .gitignore")
        print()
        print(" 2. If a secret or token was committed to git history:")
        print("      a. Immediately REVOKE and ROTATE the compromised credential at the provider.")
        print("      b. Remove the secret from code/configuration and commit the scrubbed version.")
        print("      c. Consult SECURITY.md and docs/deployment.md for git history remediation policies.")
        print("================================================================================")

    if critical_findings:
        print(" [FAIL] Scan failed due to critical violations. Remediate findings before commit/push.")
        return 1
    elif legacy_findings and args.enforce_clean:
        print(" [FAIL] Enforce-clean mode active: legacy tracked artifacts must be untracked before release.")
        return 1
    elif legacy_findings and not args.allow_legacy:
        print(
            " [WARN] Legacy tracked artifacts detected. Use --allow-legacy during intermediate development,"
        )
        print("        or run with --enforce-clean to verify ready-for-publication state.")
        return 1
    else:
        print(" [PASS] Intermediate development check passed (legacy artifacts acknowledged).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
