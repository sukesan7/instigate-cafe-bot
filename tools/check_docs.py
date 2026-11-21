"""Offline Markdown link, image, heading-anchor, external-URL allowlist, and placeholder validator.

Usage:
    uv run python tools/check_docs.py [--no-anchors] [--print-external] [paths...]

Validates tracked Markdown documents completely offline without network access:
1. Rejects workstation `file:///` URIs.
2. Verifies every relative link and image reference resolves inside the repository root.
3. Optionally verifies `#heading-anchor` fragments on local `.md` targets using GitHub slug rules.
4. Checks external `http(s)://` URLs against an intentional domain allowlist and inventories all
   external URLs that cannot be HTTP-checked offline.
5. Rejects explicit forbidden placeholders and removed script/module references in publication docs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Intentional external domains permitted in repository documentation (not HTTP-fetched offline)
DEFAULT_ALLOWED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "img.shields.io",
        "www.python.org",
        "www.sqlite.org",
        "docs.astral.sh",
        "keepachangelog.com",
        "semver.org",
        "discord.com",
        "ai.google.dev",
        "www.metrolinx.com",
        "example.invalid",
    }
)

# Forbidden placeholders / dead artifact references in publication docs
FORBIDDEN_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("placeholder GitHub clone URL", re.compile(r"github\.com/your-username/", re.IGNORECASE)),
    ("stale repository URL placeholder", re.compile(r"github\.com/sukesan/campus-bot\b", re.IGNORECASE)),
    ("explicit placeholder label", re.compile(r"\(Repository link placeholder\)", re.IGNORECASE)),
    ("nonexistent restore_db.py script", re.compile(r"\btools/restore_db\.py\b")),
    ("nonexistent icbot/storage/user.py module", re.compile(r"\bicbot/storage/user\.py\b")),
    ("unresolved TBD marker", re.compile(r"\bTBD\b")),
    ("unresolved TODO marker", re.compile(r"\bTODO\s*:")),
    ("unresolved FIXME marker", re.compile(r"\bFIXME\s*:")),
)

_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\r\n]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class DocViolation:
    """Single documentation validation violation."""

    file: str
    line: int
    kind: str
    message: str


@dataclass
class DocValidationReport:
    """Aggregated offline validation output across scanned Markdown files."""

    files_scanned: int = 0
    local_links_checked: int = 0
    local_images_checked: int = 0
    anchors_checked: int = 0
    violations: list[DocViolation] = field(default_factory=list)
    external_urls: dict[str, set[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0


def github_heading_slug(heading_text: str) -> str:
    """Compute a GitHub-flavored Markdown heading anchor slug."""
    # Strip inline markdown formatting (code backticks, bold/italic markers, links)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading_text)
    text = text.replace("`", "").replace("*", "").replace("~", "")
    text = text.strip().lower()
    # Keep alphanumeric, unicode word chars, spaces, hyphens, and underscores; drop punctuation/emoji
    cleaned_chars: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in (" ", "-", "_"):
            cleaned_chars.append(ch)
    slug = "".join(cleaned_chars).strip()
    slug = re.sub(r"\s+", "-", slug)
    return slug


def extract_markdown_anchors(content: str) -> set[str]:
    """Extract all GitHub-style heading anchors from a Markdown document."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in content.splitlines():
        m_fence = _FENCE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue
        if in_fence:
            continue

        m_head = _HEADING_RE.match(line)
        if m_head:
            base = github_heading_slug(m_head.group(1))
            if not base:
                continue
            idx = counts.get(base, 0)
            counts[base] = idx + 1
            anchors.add(base if idx == 0 else f"{base}-{idx}")
    return anchors


def list_tracked_markdown_files(repo_root: Path = PROJECT_ROOT) -> list[Path]:
    """Return sorted paths of all git-tracked Markdown files under `repo_root`."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "*.md"],
            cwd=str(repo_root),
            text=True,
        )
        return sorted(repo_root / line.strip() for line in out.splitlines() if line.strip())
    except (subprocess.SubprocessError, OSError):
        return sorted(p for p in repo_root.rglob("*.md") if ".venv" not in p.parts and ".git" not in p.parts)


def validate_markdown_files(
    files: Iterable[Path],
    *,
    repo_root: Path = PROJECT_ROOT,
    check_anchors: bool = True,
    allowed_external_hosts: frozenset[str] = DEFAULT_ALLOWED_EXTERNAL_HOSTS,
    check_placeholders: bool = True,
) -> DocValidationReport:
    """Validate local links, images, heading anchors, external hosts, and placeholders offline."""
    report = DocValidationReport()
    root_resolved = repo_root.resolve()
    anchor_cache: dict[Path, set[str]] = {}

    def _get_anchors(md_path: Path) -> set[str]:
        if md_path not in anchor_cache:
            try:
                anchor_cache[md_path] = extract_markdown_anchors(md_path.read_text(encoding="utf-8"))
            except OSError:
                anchor_cache[md_path] = set()
        return anchor_cache[md_path]

    for md_path in files:
        resolved_md = md_path.resolve()
        if not resolved_md.is_file():
            report.violations.append(
                DocViolation(
                    file=str(md_path),
                    line=1,
                    kind="missing_file",
                    message=f"Markdown file does not exist: {md_path}",
                )
            )
            continue

        report.files_scanned += 1
        try:
            rel_display = resolved_md.relative_to(root_resolved).as_posix()
        except ValueError:
            rel_display = str(resolved_md)

        content = resolved_md.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        in_fence = False
        fence_char = ""
        fence_len = 0

        for lineno, line in enumerate(lines, start=1):
            # 1. Always reject workstation file:/// URIs (even inside code blocks)
            if "file:///" in line:
                report.violations.append(
                    DocViolation(
                        file=rel_display,
                        line=lineno,
                        kind="file_uri",
                        message="Workstation 'file:///' URI is forbidden; use a repository-relative path.",
                    )
                )

            # 2. Check forbidden placeholders across publication docs (outside code blocks except URLs)
            if check_placeholders:
                for label, pat in FORBIDDEN_PLACEHOLDER_PATTERNS:
                    if pat.search(line):
                        report.violations.append(
                            DocViolation(
                                file=rel_display,
                                line=lineno,
                                kind="forbidden_placeholder",
                                message=f"Detected forbidden placeholder or removed artifact ({label}).",
                            )
                        )

            m_fence = _FENCE_RE.match(line)
            if m_fence:
                marker = m_fence.group(1)
                if not in_fence:
                    in_fence = True
                    fence_char = marker[0]
                    fence_len = len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_len:
                    in_fence = False
                continue

            if in_fence:
                continue

            # 3. Parse Markdown links and image references
            for match in _LINK_RE.finditer(line):
                is_image = match.group(1) == "!"
                raw_target = match.group(3).strip()
                # Strip optional Markdown title syntax: `path "title"`
                if raw_target.startswith("<") and ">" in raw_target:
                    raw_target = raw_target[1 : raw_target.index(">")].strip()
                else:
                    # Split only if whitespace precedes a quoted title
                    title_split = re.split(r'\s+["\']', raw_target, maxsplit=1)
                    raw_target = title_split[0].strip()

                if not raw_target or raw_target.startswith("mailto:"):
                    continue

                if raw_target.startswith("file:///"):
                    # Already recorded above
                    continue

                if raw_target.startswith(("http://", "https://")):
                    parsed = urlparse(raw_target)
                    host = (parsed.hostname or "").lower()
                    report.external_urls.setdefault(raw_target, set()).add(rel_display)
                    if allowed_external_hosts and host not in allowed_external_hosts:
                        report.violations.append(
                            DocViolation(
                                file=rel_display,
                                line=lineno,
                                kind="unallowlisted_external_url",
                                message=f"External URL host '{host}' is not in the intentional allowlist: {raw_target}",
                            )
                        )
                    continue

                # Local link or image reference
                if "#" in raw_target:
                    path_part, anchor_part = raw_target.split("#", 1)
                else:
                    path_part, anchor_part = raw_target, ""

                path_part = unquote(path_part).strip()
                anchor_part = unquote(anchor_part).strip()

                if not path_part:
                    # Same-file anchor link (#section)
                    if check_anchors and anchor_part:
                        report.anchors_checked += 1
                        anchors = _get_anchors(resolved_md)
                        if anchor_part.lower() not in anchors:
                            report.violations.append(
                                DocViolation(
                                    file=rel_display,
                                    line=lineno,
                                    kind="missing_anchor",
                                    message=f"Heading anchor '#{anchor_part}' not found in {rel_display}.",
                                )
                            )
                    continue

                if is_image:
                    report.local_images_checked += 1
                else:
                    report.local_links_checked += 1

                target_path = (resolved_md.parent / path_part).resolve()
                try:
                    target_path.relative_to(root_resolved)
                except ValueError:
                    report.violations.append(
                        DocViolation(
                            file=rel_display,
                            line=lineno,
                            kind="path_escape",
                            message=f"Local reference '{raw_target}' escapes repository root.",
                        )
                    )
                    continue

                if not target_path.exists():
                    report.violations.append(
                        DocViolation(
                            file=rel_display,
                            line=lineno,
                            kind="missing_image" if is_image else "missing_local_target",
                            message=(
                                f"Missing local {'image' if is_image else 'link'} target "
                                f"'{path_part}' (resolved from {rel_display})."
                            ),
                        )
                    )
                    continue

                if (
                    check_anchors
                    and anchor_part
                    and target_path.is_file()
                    and target_path.suffix.lower() == ".md"
                ):
                    report.anchors_checked += 1
                    target_anchors = _get_anchors(target_path)
                    if anchor_part.lower() not in target_anchors:
                        report.violations.append(
                            DocViolation(
                                file=rel_display,
                                line=lineno,
                                kind="missing_anchor",
                                message=f"Heading anchor '#{anchor_part}' not found in '{path_part}'.",
                            )
                        )

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline Markdown local-link, image, anchor, and placeholder validator."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional Markdown file paths to validate (defaults to all git-tracked *.md files).",
    )
    parser.add_argument(
        "--no-anchors",
        action="store_true",
        help="Skip heading anchor verification on local .md#anchor links.",
    )
    parser.add_argument(
        "--print-external",
        action="store_true",
        help="Print every external URL encountered (not HTTP-validated offline).",
    )
    args = parser.parse_args(argv)

    target_files = [Path(p) for p in args.paths] if args.paths else list_tracked_markdown_files(PROJECT_ROOT)
    report = validate_markdown_files(
        target_files,
        repo_root=PROJECT_ROOT,
        check_anchors=not args.no_anchors,
    )

    print(
        f"Docs check: {report.files_scanned} files, "
        f"{report.local_links_checked} local links, "
        f"{report.local_images_checked} local images, "
        f"{report.anchors_checked} anchors, "
        f"{len(report.external_urls)} unique external URLs (unverified offline)."
    )

    if args.print_external and report.external_urls:
        print("External URLs (not fetched offline):")
        for url in sorted(report.external_urls):
            refs = ", ".join(sorted(report.external_urls[url]))
            print(f"  - {url}  [{refs}]")

    if not report.ok:
        print(f"\n[FAIL] {len(report.violations)} documentation violation(s) found:")
        for v in report.violations:
            print(f"  - {v.file}:{v.line} [{v.kind}] {v.message}")
        return 1

    print("[PASS] All local links, images, anchors, and publication checks passed offline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
