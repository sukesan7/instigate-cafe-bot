#!/usr/bin/env python3
"""Gated runner and JUnit status classifier for `.github/workflows/live-smoke.yml`.

Safety guarantees:
- Inert by default (`DISABLED`, exit 0, zero pytest execution) unless explicit opt-in
  flags/env are active (both `--enable-live-network` / `ICBOT_ALLOW_LIVE_SMOKE=1` and
  `OPT_IN_LIVE_SMOKE=true` / `ICBOT_LIVE_SMOKE_OPT_IN=true`).
- Mere presence of `GO_API_KEY` or other provider keys NEVER triggers live network requests.
- Strictly clears `DISCORD_TOKEN`, `GEMINI_API_KEY`, `ICBOT_ALLOW_GATEWAY`, pollers, and
  production DB paths before launching pytest.
- Distinguishes `DISABLED`, `PASSED` (requires >= 1 passed, 0 failed/errored),
  `SKIPPED_UNCONFIGURED` (0 passed, > 0 skipped -> non-zero), `NO_TESTS_SELECTED`
  (exit 5 or 0 collected -> non-zero), and `FAILED` (never masks real failures).
- Redacts configured credential values before printing or writing `$GITHUB_STEP_SUMMARY`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from icbot.errors import redact_secrets  # noqa: E402

SmokeStatus = Literal["DISABLED", "PASSED", "SKIPPED_UNCONFIGURED", "NO_TESTS_SELECTED", "FAILED"]
VALID_PROVIDERS: tuple[str, ...] = ("go_transit", "ttc_mastodon", "sports_odds", "crypto_fng", "all")

SECRET_ENV_VARS: tuple[str, ...] = (
    "DISCORD_TOKEN",
    "GEMINI_API_KEY",
    "GO_API_KEY",
    "ODDS_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "OPT_IN_LIVE_SMOKE",
)


@dataclass(frozen=True)
class SmokeOutcome:
    status: SmokeStatus
    provider: str
    total: int
    passed: int
    skipped: int
    failures: int
    errors: int
    duration_seconds: float
    exit_code: int
    reason: str = ""


def _parse_junit_counts(junit_path: Path) -> tuple[int, int, int, int, float, str]:
    """Return `(total, skipped, failures, errors, time_sec, first_message)` from pytest JUnit XML."""
    if not junit_path.is_file():
        return 0, 0, 0, 0, 0.0, "Missing JUnit XML report."
    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError as exc:
        return 0, 0, 0, 1, 0.0, f"Malformed JUnit XML: {exc}"

    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    total = skipped = failures = errors = 0
    duration = 0.0
    messages: list[str] = []

    for suite in suites:
        total += int(suite.attrib.get("tests", 0) or 0)
        skipped += int(suite.attrib.get("skipped", 0) or 0)
        failures += int(suite.attrib.get("failures", 0) or 0)
        errors += int(suite.attrib.get("errors", 0) or 0)
        duration += float(suite.attrib.get("time", 0.0) or 0.0)

    for tag in ("failure", "error", "skipped"):
        for elem in root.iter(tag):
            msg = (elem.attrib.get("message") or elem.text or "").strip()
            if msg:
                messages.append(msg[:240])
                break

    return total, skipped, failures, errors, duration, "; ".join(messages[:2])


def classify_smoke_result(
    *,
    returncode: int,
    junit_path: Path,
    provider: str,
) -> SmokeOutcome:
    """Classify pytest exit code and JUnit counts into an honest SmokeOutcome."""
    total, skipped, failures, errors, duration, detail = _parse_junit_counts(junit_path)
    passed = max(0, total - skipped - failures - errors)

    if returncode == 5 or total == 0:
        return SmokeOutcome(
            status="NO_TESTS_SELECTED",
            provider=provider,
            total=total,
            passed=passed,
            skipped=skipped,
            failures=failures,
            errors=errors,
            duration_seconds=duration,
            exit_code=1,
            reason=detail or "No live_smoke tests were selected (pytest exit status 5 / 0 collected).",
        )

    if failures > 0 or errors > 0 or returncode != 0:
        return SmokeOutcome(
            status="FAILED",
            provider=provider,
            total=total,
            passed=passed,
            skipped=skipped,
            failures=failures,
            errors=errors,
            duration_seconds=duration,
            exit_code=1,
            reason=detail or f"Live smoke failed with pytest exit code {returncode}.",
        )

    if passed == 0:
        return SmokeOutcome(
            status="SKIPPED_UNCONFIGURED",
            provider=provider,
            total=total,
            passed=0,
            skipped=skipped,
            failures=0,
            errors=0,
            duration_seconds=duration,
            exit_code=1,
            reason=detail or "All selected live_smoke checks were skipped (credentials/config missing).",
        )

    return SmokeOutcome(
        status="PASSED",
        provider=provider,
        total=total,
        passed=passed,
        skipped=skipped,
        failures=failures,
        errors=errors,
        duration_seconds=duration,
        exit_code=0,
        reason="At least one opted-in live provider check passed with zero failures.",
    )


def format_smoke_summary(outcome: SmokeOutcome, *, secrets: Iterable[str] = ()) -> str:
    """Format a sanitized Markdown/CLI summary with zero leaked secrets."""
    safe_reason = redact_secrets(outcome.reason, secrets)
    lines = [
        f"## Live Provider Smoke Summary: `{outcome.status}`",
        f"- **Provider filter:** `{outcome.provider}`",
        f"- **Counts:** total={outcome.total}, passed={outcome.passed}, skipped={outcome.skipped}, "
        f"failures={outcome.failures}, errors={outcome.errors}",
        f"- **Duration:** {outcome.duration_seconds:.2f}s",
    ]
    if safe_reason:
        lines.append(f"- **Detail:** {safe_reason}")
    return "\n".join(lines)


def _write_github_summary(summary_text: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_file:
        return
    try:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(summary_text + "\n")
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gated runner for opt-in live_smoke provider diagnostics.")
    parser.add_argument(
        "--provider",
        default="all",
        choices=VALID_PROVIDERS,
        help="Provider smoke group to execute (default: all configured).",
    )
    parser.add_argument(
        "--enable-live-network",
        action="store_true",
        default=False,
        help="Explicit operator opt-in required alongside OPT_IN_LIVE_SMOKE=true.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    provider = str(args.provider)

    secret_values = tuple(
        os.environ[k].strip() for k in SECRET_ENV_VARS if k in os.environ and os.environ[k].strip()
    )

    opt_in_secret = os.environ.get("OPT_IN_LIVE_SMOKE", "").strip().lower() == "true"
    explicit_opt_in = bool(args.enable_live_network) or (
        os.environ.get("ICBOT_ALLOW_LIVE_SMOKE", "") == "1"
        and os.environ.get("ICBOT_LIVE_SMOKE_OPT_IN", "").strip().lower() == "true"
    )

    if not (opt_in_secret and explicit_opt_in):
        outcome = SmokeOutcome(
            status="DISABLED",
            provider=provider,
            total=0,
            passed=0,
            skipped=0,
            failures=0,
            errors=0,
            duration_seconds=0.0,
            exit_code=0,
            reason=(
                "Inert: requires BOTH OPT_IN_LIVE_SMOKE=true and explicit operator opt-in "
                "(--enable-live-network). Mere presence of provider keys never executes network checks."
            ),
        )
        summary = format_smoke_summary(outcome, secrets=secret_values)
        print(summary)
        _write_github_summary(summary)
        return 0

    child_env = {
        **os.environ,
        "DISCORD_TOKEN": "",
        "GEMINI_API_KEY": "",
        "ICBOT_ALLOW_GATEWAY": "false",
        "LLM_ENABLED": "false",
        "MARKET_WATCH_ENABLED": "false",
        "MARKET_DAILY_REPORT_ENABLED": "false",
        "BTC_WATCH_ENABLED": "false",
        "JOBS_POLL_SECONDS": "0",
        "WORKDAY_POLL_SECONDS": "0",
        "DB_PATH": ":memory:",
        "ICBOT_ALLOW_LIVE_SMOKE": "1",
        "ICBOT_LIVE_SMOKE_OPT_IN": "true",
        "ICBOT_LIVE_SMOKE_PROVIDER": provider,
    }

    with tempfile.TemporaryDirectory(prefix="icbot_live_smoke_") as tmpdir:
        junit_path = Path(tmpdir) / "live_smoke_junit.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-markers",
            "-m",
            "live_smoke",
            f"--junitxml={junit_path}",
            "--tb=short",
            "-rs",
        ]
        res = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=child_env, check=False)
        outcome = classify_smoke_result(
            returncode=res.returncode,
            junit_path=junit_path,
            provider=provider,
        )

    summary = format_smoke_summary(outcome, secrets=secret_values)
    print(summary)
    _write_github_summary(summary)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
