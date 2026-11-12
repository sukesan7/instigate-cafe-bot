"""
tools/check_syntax.py — Offline syntax validator for staged/selected Python, JSON, TOML, and YAML files.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import sys
from typing import Any


def _load_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def main(argv: list[str] | None = None) -> int:
    paths = list(sys.argv[1:] if argv is None else argv)
    tomllib_mod = _load_optional("tomllib")
    yaml_mod = _load_optional("yaml")

    for raw in paths:
        path = pathlib.Path(raw)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        if suffix == ".py":
            ast.parse(text, filename=raw)
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml" and tomllib_mod is not None:
            tomllib_mod.loads(text)
        elif suffix in (".yml", ".yaml") and yaml_mod is not None:
            yaml_mod.safe_load(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
