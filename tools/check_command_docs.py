"""Offline-safe command-tree introspection and documentation drift validator.

Installs an outbound IPv4/IPv6 socket guard before importing ``icbot.registry`` so
command-tree introspection is guaranteed offline with zero Discord or HTTP traffic.

Checks:
1. Every leaf command registered under ``/ic`` is documented in ``docs/features.md``.
2. Every ``/ic ...`` command route published in ``docs/features.md`` (and ``README.md``)
   exists in the live command tree (catching typos like ``/ic exams`` or ``/ic daily``).
3. Parameter names documented in ``docs/features.md`` command rows match the live
   ``discord.app_commands.Parameter`` names for that command.
4. Documented theme presets in ``docs/configuration.md`` and ``docs/features.md`` match
   ``icbot.theme.THEME_PRESETS`` and ``DEFAULT_THEME_NAME``.

Usage:
    uv run python tools/check_command_docs.py [--print-matrix]
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ensure repo root is importable when executed as `python tools/check_command_docs.py`
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Parent groups permitted as shorthand mentions in prose (never leaf typos like `/ic exams`)
ALLOWED_PARENT_GROUPS: frozenset[str] = frozenset(
    {
        "ic",
        "ic admin",
        "ic casino",
        "ic exam",
        "ic icse",
        "ic sports",
    }
)

# Regex for backticked command invocations like `/ic exam list` or `/ic notes course:CPS109`
_BACKTICK_CMD_RE = re.compile(r"`/(ic(?:\s+[a-z0-9_-]+)*)(?:\s+[^`]*)?`")
# Regex for command table rows in docs/features.md: | `/ic ...` | options | ...
_TABLE_ROW_RE = re.compile(r"^\|\s*`/(ic(?:\s+[a-z0-9_-]+)+)`\s*\|\s*([^|]*)\|")
_BACKTICK_OPT_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")
# Explicit historical typos that must never reappear in public docs
FORBIDDEN_COMMAND_TYPOS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("/ic exams (typo for /ic exam)", re.compile(r"`/ic\s+exams(?:\b|[^a-z0-9_-])")),
    ("/ic daily (typo for /ic dailyclaim)", re.compile(r"`/ic\s+daily(?:\s|`|$)")),
)


@dataclass(frozen=True)
class CommandSpec:
    """Introspected slash-command metadata from the live `/ic` command tree."""

    qualified_name: str
    module_id: str
    description: str
    parameters: tuple[str, ...]
    required_parameters: tuple[str, ...]


@dataclass
class CommandDocsReport:
    """Aggregated drift report between live command registry and Markdown docs."""

    registered_count: int = 0
    documented_count: int = 0
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0


def _install_offline_guard() -> None:
    """Clear token environment variables and block outbound IPv4/IPv6 sockets."""
    for env_key in ("DISCORD_TOKEN", "GEMINI_API_KEY", "GO_API_KEY", "ODDS_API_KEY"):
        os.environ[env_key] = ""
    os.environ["ICBOT_ALLOW_GATEWAY"] = "false"

    orig_connect = socket.socket.connect

    def _blocked_connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"Offline introspection guard blocked outbound socket to {address!r}")
        return orig_connect(self, address)

    socket.socket.connect = _blocked_connect  # type: ignore[method-assign,assignment]


def introspect_command_tree() -> list[CommandSpec]:
    """Safely load feature modules offline and extract all leaf commands under `/ic`."""
    from icbot.app import ic
    from icbot.registry import FEATURE_MANIFESTS, load_feature_modules

    load_feature_modules()

    cmd_to_module: dict[str, str] = {}
    for module_id, manifest in FEATURE_MANIFESTS.items():
        for cmd_name in manifest.commands_supplied:
            cmd_to_module[cmd_name] = module_id

    specs: list[CommandSpec] = []

    def _walk(node: Any, prefix: str = "ic") -> None:
        for child in sorted(node.commands, key=lambda c: c.name):
            qname = f"{prefix} {child.name}"
            if hasattr(child, "commands"):
                _walk(child, qname)
            else:
                params = tuple(p.name for p in child.parameters)
                req_params = tuple(p.name for p in child.parameters if p.required)
                specs.append(
                    CommandSpec(
                        qualified_name=qname,
                        module_id=cmd_to_module.get(qname, "unknown"),
                        description=getattr(child, "description", ""),
                        parameters=params,
                        required_parameters=req_params,
                    )
                )

    _walk(ic)
    return specs


def _extract_table_commands(markdown_text: str) -> dict[str, tuple[str, ...]]:
    """Extract `{qualified_name: (option_names...)}` from Markdown command tables."""
    found: dict[str, tuple[str, ...]] = {}
    in_fence = False
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _TABLE_ROW_RE.match(stripped)
        if not m:
            continue
        qname = " ".join(m.group(1).split())
        opt_cell = m.group(2).strip()
        if opt_cell.lower() in ("none", "—", "-", ""):
            opts: tuple[str, ...] = ()
        else:
            # Ignore parenthetical type/choice/default notes like `fmt` (`ics`/`csv`, default `ics`)
            without_parens = re.sub(r"\([^)]*\)", "", opt_cell)
            opts = tuple(_BACKTICK_OPT_RE.findall(without_parens))
        found[qname] = opts
    return found


def _extract_all_backtick_command_mentions(markdown_text: str) -> list[tuple[int, str]]:
    """Return `(line_number, normalized_command)` for every backticked `/ic ...` reference."""
    mentions: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(markdown_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _BACKTICK_CMD_RE.finditer(line):
            raw = " ".join(m.group(1).split())
            # Strip trailing argument key:value tokens or ellipses when someone writes `/ic notes course:CPS109`
            tokens = [tok for tok in raw.split() if ":" not in tok and tok != "..."]
            if tokens:
                mentions.append((lineno, " ".join(tokens)))
    return mentions


def validate_command_docs(
    specs: list[CommandSpec],
    *,
    features_text: str,
    readme_text: str | None = None,
    config_text: str | None = None,
    all_docs_texts: dict[str, str] | None = None,
) -> CommandDocsReport:
    """Cross-check live command specs against `docs/features.md` and related docs."""
    from icbot.theme import DEFAULT_THEME_NAME, THEME_PRESETS

    report = CommandDocsReport(registered_count=len(specs))
    spec_by_name = {s.qualified_name: s for s in specs}
    valid_routes = set(spec_by_name.keys()) | set(ALLOWED_PARENT_GROUPS)

    table_cmds = _extract_table_commands(features_text)
    report.documented_count = len(table_cmds)

    # 1. Missing commands from docs/features.md tables
    for qname in sorted(spec_by_name):
        if qname not in table_cmds:
            report.violations.append(
                f"docs/features.md: missing registered command '/{qname}' from command tables."
            )

    # 2. Phantom or typo commands in docs/features.md tables
    for qname, doc_opts in sorted(table_cmds.items()):
        if qname not in spec_by_name:
            report.violations.append(
                f"docs/features.md: documented command '/{qname}' does not exist in live registry."
            )
            continue
        actual_params = spec_by_name[qname].parameters
        if set(doc_opts) != set(actual_params):
            missing_opts = sorted(set(actual_params) - set(doc_opts))
            extra_opts = sorted(set(doc_opts) - set(actual_params))
            report.violations.append(
                f"docs/features.md: option mismatch for '/{qname}' "
                f"(missing={missing_opts}, extra={extra_opts}, actual={list(actual_params)})."
            )

    # 3. Validate all backticked `/ic ...` mentions in features.md and README.md
    sources_to_check: list[tuple[str, str]] = [("docs/features.md", features_text)]
    if readme_text is not None:
        sources_to_check.append(("README.md", readme_text))

    for label, text in sources_to_check:
        for lineno, mention in _extract_all_backtick_command_mentions(text):
            if mention in valid_routes:
                continue
            # Handle inline examples like `/ic admin features enable:casino` -> match longest valid prefix
            tokens = mention.split()
            matched_prefix = any(" ".join(tokens[:k]) in spec_by_name for k in range(len(tokens), 1, -1))
            if not matched_prefix:
                report.violations.append(
                    f"{label}:{lineno}: unrecognized command route '/{mention}' (not in live registry)."
                )

    # 4. Forbidden historical command typos across all checked docs
    typo_targets = all_docs_texts if all_docs_texts is not None else dict(sources_to_check)
    for label, text in sorted(typo_targets.items()):
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for typo_desc, pattern in FORBIDDEN_COMMAND_TYPOS:
                if pattern.search(line):
                    report.violations.append(f"{label}:{lineno}: forbidden command typo {typo_desc}.")

    # 5. Theme preset parity
    if config_text is not None:
        for preset_key in THEME_PRESETS:
            if preset_key not in config_text:
                report.violations.append(
                    f"docs/configuration.md: theme preset '{preset_key}' missing from configuration docs."
                )
        if DEFAULT_THEME_NAME not in features_text:
            report.violations.append(
                f"docs/features.md: default theme '{DEFAULT_THEME_NAME}' is not documented."
            )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline command-tree introspection and documentation drift validator."
    )
    parser.add_argument(
        "--print-matrix",
        action="store_true",
        help="Print the introspected command matrix in Markdown format.",
    )
    args = parser.parse_args(argv)

    _install_offline_guard()
    specs = introspect_command_tree()

    if args.print_matrix:
        print("| Command | Module | Parameters |")
        print("| :--- | :--- | :--- |")
        for s in specs:
            params = (
                ", ".join(f"`{p}`*" if p in s.required_parameters else f"`{p}`" for p in s.parameters)
                or "None"
            )
            print(f"| `/{s.qualified_name}` | `{s.module_id}` | {params} |")
        return 0

    features_text = (PROJECT_ROOT / "docs/features.md").read_text(encoding="utf-8")
    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    config_text = (PROJECT_ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    all_docs: dict[str, str] = {"README.md": readme_text}
    for md_path in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        all_docs[f"docs/{md_path.name}"] = md_path.read_text(encoding="utf-8")

    report = validate_command_docs(
        specs,
        features_text=features_text,
        readme_text=readme_text,
        config_text=config_text,
        all_docs_texts=all_docs,
    )

    print(
        f"Command docs check: {report.registered_count} live commands, "
        f"{report.documented_count} documented table rows."
    )
    if not report.ok:
        print(f"[FAIL] {len(report.violations)} command documentation drift issue(s):")
        for v in report.violations:
            print(f"  - {v}")
        return 1

    print("[PASS] Published command routes, options, and theme presets match the live registry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
