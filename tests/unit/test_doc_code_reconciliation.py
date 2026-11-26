"""Contract tests reconciling docs, packaging/service units, .env.example, feature flags, and code."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import pytest

import icbot
from icbot.bot import resolve_intent_policy
from icbot.config import (
    FEATURE_PROFILE_PRESETS,
    KNOWN_MODULE_IDS,
    Settings,
    resolve_ai_access,
)
from icbot.guild_config import (
    GUILD_OVERRIDABLE_KEYS,
    PROCESS_ONLY_KEYS,
    SECRET_SETTING_KEYS,
    GuildSettings,
    resolve_effective_settings,
)
from icbot.registry import FEATURE_MANIFESTS
from tools import check_command_docs, check_docs

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _parse_env_example() -> dict[str, str]:
    kv: dict[str, str] = {}
    for raw in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip()
    return kv


def test_systemd_unit_documentation_uri_resolves_to_existing_repo_doc() -> None:
    """Verify systemd Documentation= is a valid HTTPS URI targeting an existing repository doc."""
    unit_text = (REPO_ROOT / "systemd" / "campus-bot.service").read_text(encoding="utf-8")
    doc_lines = [line.strip() for line in unit_text.splitlines() if line.strip().startswith("Documentation=")]
    assert len(doc_lines) == 1, "Expected exactly one Documentation= directive in campus-bot.service"

    uri = doc_lines[0].split("=", 1)[1].strip()
    parsed = urlparse(uri)
    assert parsed.scheme == "https", f"systemd Documentation= must be a full URI, got {uri!r}"
    assert "your-username" not in uri
    assert "production-hosting-and-recovery.md" not in uri

    canonical_prefix = "https://github.com/sukesan7/instigate-cafe-bot/blob/main/"
    assert uri.startswith(canonical_prefix), f"Unexpected repository Documentation= URI: {uri!r}"
    rel_doc = uri[len(canonical_prefix) :]
    assert (REPO_ROOT / rel_doc).is_file(), f"systemd Documentation= target does not exist: {rel_doc}"

    assert "Environment=DB_PATH=/opt/campus-bot/data/campus.db" in unit_text
    assert "Environment=GRADES_DB_PATH=/opt/campus-bot/data/tmu_grades.db" in unit_text
    assert "Environment=ICBOT_ALLOW_GATEWAY=true" in unit_text
    assert "main.py --allow-gateway" in unit_text


def test_security_policy_versions_contact_and_intent_match_code() -> None:
    """Verify SECURITY.md accurately lists the active release version and valid contact channels."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject["project"]["version"]
    assert expected_version == "1.0.0"
    assert icbot.__version__ == expected_version

    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert expected_version in security
    assert "1.0.0-rc1" not in security
    assert "1.0.0rc1" not in security
    assert "imsukesan@gmail.com" in security
    assert "https://github.com/sukesan7/instigate-cafe-bot/security/advisories/new" in security
    for fake in ("example.com", "security@campus-bot", "your-username"):
        assert fake not in security.lower()
    assert "complete deletion of their profile and economy balance" not in security.lower()
    assert "backup" in security.lower()


def test_env_defaults_and_workday_poll_behavior_match_between_code_env_and_docs() -> None:
    """Verify .env.example, README.md, docs/configuration.md, and icbot/config.py agree on defaults."""
    env_kv = _parse_env_example()
    base = Settings()
    empty_loaded = Settings.from_env(environ={})

    # Raw dataclass vs from_env Workday inheritance rule
    assert base.jobs_poll_seconds == 600
    assert base.workday_poll_seconds == 0
    assert empty_loaded.jobs_poll_seconds == 600
    assert empty_loaded.workday_poll_seconds == 600
    assert empty_loaded.effective_workday_poll_seconds == 600

    assert env_kv["JOBS_POLL_SECONDS"] == "600"
    assert env_kv["WORKDAY_POLL_SECONDS"] == "0"
    assert env_kv["WORKDAY_SITES"] == ""
    assert env_kv["JOB_ALERTS_CHANNEL_ID"] == "0"
    assert env_kv["LLM_ENABLED"] == "false"
    assert env_kv["LLM_CHANNEL_HISTORY_ENABLED"] == "false"
    assert env_kv["GEMINI_API_KEY"] == ""
    assert env_kv["ICBOT_ALLOW_GATEWAY"] == "false"
    assert env_kv["DB_PATH"] == base.db_path
    assert env_kv["GRADES_DB_PATH"] == base.grades_db_path

    # Setting GEMINI_API_KEY without LLM_ENABLED=true never enables AI calls or MESSAGE_CONTENT
    key_only = Settings.from_env(environ={"GEMINI_API_KEY": "synthetic-key"})
    assert resolve_ai_access(key_only).allowed is False
    assert resolve_intent_policy(key_only).privileged_intents == ()

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    config_doc = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    # Reconciled descriptions (no false "WORKDAY_POLL_SECONDS=0 disables by default" claim)
    assert "0 disables by default" not in config_doc
    assert "WORKDAY_POLL_SECONDS=0) default to off" not in readme
    assert "inherits `JOBS_POLL_SECONDS`" in config_doc
    assert "Title|https://" in config_doc
    assert "(defaults to `https://mstdn.ca`)" not in config_doc
    assert "1 latest matching alert" in config_doc


def test_feature_flags_process_vs_guild_scope_and_deployment_alignment() -> None:
    """Verify feature profiles/modules, guild overrides, intents, and hosting paths stay aligned."""
    # Code partition invariants
    assert SECRET_SETTING_KEYS.isdisjoint(GUILD_OVERRIDABLE_KEYS)
    assert PROCESS_ONLY_KEYS.isdisjoint(GUILD_OVERRIDABLE_KEYS)

    # Deny-first host guard: per-guild llm_enabled=True cannot bypass process llm_enabled=False
    host_disabled = Settings(llm_enabled=False, gemini_api_key="synthetic-key")
    guild_enable_attempt = GuildSettings.from_overrides(900000000000000001, {"llm_enabled": True})
    resolved = resolve_effective_settings(host_disabled, guild_enable_attempt)
    assert resolved.llm_enabled is False
    assert resolve_ai_access(resolved).allowed is False

    config_doc = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    features_doc = (REPO_ROOT / "docs" / "features.md").read_text(encoding="utf-8")
    deploy_doc = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    privacy_doc = (REPO_ROOT / "docs" / "privacy.md").read_text(encoding="utf-8")
    dev_doc = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")

    for profile in FEATURE_PROFILE_PRESETS:
        assert f"`{profile}`" in config_doc
        assert f"`{profile}`" in features_doc
    for mod_id in KNOWN_MODULE_IDS:
        assert f"`{mod_id}`" in config_doc
        assert f"`{mod_id}`" in features_doc
        assert mod_id in FEATURE_MANIFESTS

    # Settings fields stay documented
    internal_fields = {"guild_id_raw", "guild_id_invalid"}
    for f in dataclasses.fields(Settings):
        if f.name not in internal_fields:
            assert f.name in config_doc

    # Hosting paths & gateway opt-in parity
    for text in (deploy_doc, privacy_doc):
        assert "/data/campus.db" in text
    assert "ICBOT_ALLOW_GATEWAY=true" in deploy_doc
    assert "--allow-gateway" in deploy_doc
    assert "277025507392" in deploy_doc
    assert "Offline Test Suite" in dev_doc


def test_all_local_markdown_links_and_command_parity_pass() -> None:
    """Run check_docs and check_command_docs programmatically as part of the doc-code contract."""
    tracked_md = check_docs.list_tracked_markdown_files(REPO_ROOT)
    doc_report = check_docs.validate_markdown_files(tracked_md, repo_root=REPO_ROOT, check_anchors=True)
    assert doc_report.ok, f"Markdown validation failed: {doc_report.violations}"

    specs = check_command_docs.introspect_command_tree()
    cmd_report = check_command_docs.validate_command_docs(
        specs,
        features_text=(REPO_ROOT / "docs" / "features.md").read_text(encoding="utf-8"),
        readme_text=(REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        config_text=(REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8"),
    )
    assert cmd_report.ok, f"Command doc parity failed: {cmd_report.violations}"
