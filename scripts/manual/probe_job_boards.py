"""Manual opt-in live ATS job board verifier (Greenhouse, Lever, Workday).

This script is strictly for manual, owner-run live network verification outside CI
and offline tests. Importing this module performs zero network or filesystem I/O.

Usage:
    # Preview which ~100 curated major tech/semiconductor/AI/quant/Canadian boards return live jobs
    uv run python scripts/manual/probe_job_boards.py

    # Also re-check any boards currently configured in .env
    uv run python scripts/manual/probe_job_boards.py --include-existing

    # Replace GREENHOUSE_BOARDS, LEVER_SITES, and WORKDAY_SITES in .env with verified working boards
    uv run python scripts/manual/probe_job_boards.py --write-env
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from icbot.services.jobs.adapters import GreenhouseAdapter, LeverAdapter, WorkdayAdapter  # noqa: E402
from icbot.services.jobs_workday_client import parse_workday_sites  # noqa: E402

# Curated candidate boards (~100) across semiconductors, AI labs, cloud/SaaS,
# quant finance, fintech, aerospace/robotics, and Canadian employers.
CANDIDATE_GREENHOUSE_BOARDS: tuple[str, ...] = (
    # AI Labs & Cloud / Data Infrastructure
    "anthropic",
    "databricks",
    "cloudflare",
    "scaleai",
    "cohere",
    "figma",
    "mongodb",
    "elastic",
    "gitlab",
    "confluent",
    "cockroachlabs",
    "vercel",
    "notion",
    "linear",
    "okta",
    "twilio",
    "dropbox",
    "asana",
    "braze",
    "benchling",
    "purestorage",
    # Fintech & Consumer Tech
    "stripe",
    "coinbase",
    "robinhood",
    "plaid",
    "ramp",
    "chime",
    "sofi",
    "brex",
    "gusto",
    "faire",
    "airbnb",
    "doordashusa",
    "instacart",
    "discord",
    "lyft",
    "pinterest",
    "reddit",
    "flexport",
    "7shifts",
    "remotecom",
    "verkada",
    "nexhealth",
    "ridgeline",
    # Autonomous, Aerospace, Defense & Robotics
    "spacex",
    "waymo",
    "andurilindustries",
    "appliedintuition",
    "aurorainnovation",
    "relativityspace",
    "astranis",
    "neuralink",
    "slingshotaerospace",
    "ursamajor",
    "bedrockrobotics",
    # Quant Trading & Finance
    "janestreet",
    "citadel",
    "hudsonrivertrading",
    "jumptrading",
    "point72",
    "twosigma",
    "imc",
    "drweng",
    "akunacapital",
)

CANDIDATE_LEVER_SITES: tuple[str, ...] = (
    "palantir",
    "wealthsimple",
    "zoox",
    "waabi",
    "sanctuary",
    "kepler",
    "loftorbital",
    "saronic",
    "field-ai",
    "MachinaLabs",
    "policyme",
    "getmaple",
    "newton",
    "gauntlet",
    "mechanicalorchard",
    "spotify",
    "mistral",
)

CANDIDATE_WORKDAY_SITES: tuple[str, ...] = (
    # Semiconductors & Hardware
    "NVIDIA|https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "AMD|https://amd.wd1.myworkdayjobs.com/AMD",
    "Intel|https://intel.wd1.myworkdayjobs.com/External",
    "Qualcomm|https://qualcomm.wd5.myworkdayjobs.com/External",
    "Micron|https://micron.wd1.myworkdayjobs.com/External",
    "Cadence|https://cadence.wd1.myworkdayjobs.com/External_Careers",
    "Synopsys|https://synopsys.wd1.myworkdayjobs.com/External_Careers",
    "TexasInstruments|https://ti.wd1.myworkdayjobs.com/External",
    "AppliedMaterials|https://amat.wd1.myworkdayjobs.com/External",
    "LamResearch|https://lamresearch.wd1.myworkdayjobs.com/External",
    # Enterprise Software & Cloud
    "Salesforce|https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
    "Adobe|https://adobe.wd5.myworkdayjobs.com/external_experienced",
    "Autodesk|https://autodesk.wd1.myworkdayjobs.com/Ext",
    "Workday|https://workday.wd5.myworkdayjobs.com/Workday",
    "CrowdStrike|https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers",
    "Cisco|https://cisco.wd5.myworkdayjobs.com/Cisco_Careers",
    "Dell|https://dell.wd1.myworkdayjobs.com/External",
    "HPE|https://hpe.wd5.myworkdayjobs.com/Jobsathpe",
    "PayPal|https://paypal.wd1.myworkdayjobs.com/jobs",
    "Mastercard|https://mastercard.wd1.myworkdayjobs.com/CorporateCareers",
    # Aerospace & Automotive
    "LockheedMartin|https://lmco.wd1.myworkdayjobs.com/External",
    "Boeing|https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS",
    "GeneralMotors|https://generalmotors.wd5.myworkdayjobs.com/Careers_GM",
    # Canadian Banks & Institutions
    "TD|https://td.wd3.myworkdayjobs.com/TD_Bank_Careers",
    "BMO|https://bmo.wd3.myworkdayjobs.com/External",
    "CIBC|https://cibc.wd3.myworkdayjobs.com/search",
    "Scotiabank|https://scotiabank.wd3.myworkdayjobs.com/Scotiabank",
    "RBC|https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1",
)


@dataclass(frozen=True)
class BoardProbeResult:
    provider: str
    target: str
    ok: bool
    job_count: int = 0
    error: str = ""


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        s = raw.strip()
        if not s:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def update_env_job_boards(
    env_path: Path,
    *,
    greenhouse_boards: Sequence[str],
    lever_sites: Sequence[str],
    workday_sites: Sequence[str],
) -> None:
    """Update ONLY GREENHOUSE_BOARDS, LEVER_SITES, and WORKDAY_SITES in an existing .env file."""
    replacements = {
        "GREENHOUSE_BOARDS": ",".join(_dedupe_preserve_order(greenhouse_boards)),
        "LEVER_SITES": ",".join(_dedupe_preserve_order(lever_sites)),
        "WORKDAY_SITES": ",".join(_dedupe_preserve_order(workday_sites)),
    }
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    seen_keys: set[str] = set()
    kv_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

    for line in lines:
        m = kv_re.match(line)
        if m and m.group(1) in replacements:
            key = m.group(1)
            out_lines.append(f"{key}={replacements[key]}")
            seen_keys.add(key)
        else:
            out_lines.append(line)

    for key, val in replacements.items():
        if key not in seen_keys:
            out_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


async def probe_all_boards(
    *,
    greenhouse_boards: Sequence[str],
    lever_sites: Sequence[str],
    workday_sites: Sequence[str],
    concurrency: int = 10,
    timeout_seconds: int = 12,
    min_jobs: int = 1,
) -> list[BoardProbeResult]:
    """Probe candidate ATS boards concurrently using the bot's own adapters."""
    sem = asyncio.Semaphore(max(1, concurrency))
    gh_adapter = GreenhouseAdapter()
    lv_adapter = LeverAdapter()
    wd_adapter = WorkdayAdapter()

    async def _probe_gh(session: aiohttp.ClientSession, slug: str) -> BoardProbeResult:
        async with sem:
            try:
                jobs = await gh_adapter.fetch_postings(
                    session, slug, timeout_seconds=timeout_seconds, limit=25
                )
                ok = len(jobs) >= min_jobs
                return BoardProbeResult("greenhouse", slug, ok, len(jobs), "" if ok else "0 jobs")
            except Exception as exc:  # noqa: BLE001
                return BoardProbeResult("greenhouse", slug, False, 0, str(exc)[:120])

    async def _probe_lv(session: aiohttp.ClientSession, site: str) -> BoardProbeResult:
        async with sem:
            try:
                jobs = await lv_adapter.fetch_postings(
                    session, site, timeout_seconds=timeout_seconds, limit=25
                )
                ok = len(jobs) >= min_jobs
                return BoardProbeResult("lever", site, ok, len(jobs), "" if ok else "0 jobs")
            except Exception as exc:  # noqa: BLE001
                return BoardProbeResult("lever", site, False, 0, str(exc)[:120])

    async def _probe_wd(session: aiohttp.ClientSession, spec: str) -> BoardProbeResult:
        parsed = parse_workday_sites([spec])
        if not parsed:
            return BoardProbeResult("workday", spec, False, 0, "Invalid Label|URL format")
        ws = parsed[0]
        async with sem:
            try:
                jobs, _api, _t, _s = await wd_adapter.fetch_postings(
                    session,
                    ws.careers_url,
                    employer_label=ws.label,
                    api_url_override=ws.api_url_override,
                    locale=ws.locale,
                    timeout_seconds=timeout_seconds,
                    limit=20,
                )
                ok = len(jobs) >= min_jobs
                return BoardProbeResult("workday", spec, ok, len(jobs), "" if ok else "0 jobs")
            except Exception as exc:  # noqa: BLE001
                return BoardProbeResult("workday", spec, False, 0, str(exc)[:120])

    headers = {"User-Agent": "InstigateCafeBot/1.0"}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds + 5),
        headers=headers,
    ) as session:
        tasks = [
            *(_probe_gh(session, s) for s in _dedupe_preserve_order(greenhouse_boards)),
            *(_probe_lv(session, s) for s in _dedupe_preserve_order(lever_sites)),
            *(_probe_wd(session, s) for s in _dedupe_preserve_order(workday_sites)),
        ]
        return list(await asyncio.gather(*tasks))


def _load_existing_env_boards(env_path: Path) -> tuple[list[str], list[str], list[str]]:
    if not env_path.is_file():
        return [], [], []
    from dotenv import dotenv_values

    vals = dotenv_values(env_path)

    def _split(key: str) -> list[str]:
        return [tok.strip() for tok in (vals.get(key) or "").split(",") if tok.strip()]

    return _split("GREENHOUSE_BOARDS"), _split("LEVER_SITES"), _split("WORKDAY_SITES")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual live ATS job board verifier for Greenhouse, Lever, and Workday."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to local .env file (default: .env).",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Merge boards already listed in --env-file into the candidate scan.",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Overwrite GREENHOUSE_BOARDS, LEVER_SITES, and WORKDAY_SITES in --env-file with verified working boards.",
    )
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent probes (default: 10).")
    parser.add_argument("--timeout", type=int, default=12, help="Per-board timeout in seconds (default: 12).")
    parser.add_argument(
        "--min-jobs", type=int, default=1, help="Minimum live postings required (default: 1)."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gh = list(CANDIDATE_GREENHOUSE_BOARDS)
    lv = list(CANDIDATE_LEVER_SITES)
    wd = list(CANDIDATE_WORKDAY_SITES)

    if args.include_existing:
        e_gh, e_lv, e_wd = _load_existing_env_boards(args.env_file)
        gh.extend(e_gh)
        lv.extend(e_lv)
        wd.extend(e_wd)

    total_candidates = (
        len(_dedupe_preserve_order(gh)) + len(_dedupe_preserve_order(lv)) + len(_dedupe_preserve_order(wd))
    )
    print(
        f"Probing {total_candidates} ATS job boards (concurrency={args.concurrency}, timeout={args.timeout}s)..."
    )

    results = asyncio.run(
        probe_all_boards(
            greenhouse_boards=gh,
            lever_sites=lv,
            workday_sites=wd,
            concurrency=args.concurrency,
            timeout_seconds=args.timeout,
            min_jobs=args.min_jobs,
        )
    )

    ok_gh = [r.target for r in results if r.provider == "greenhouse" and r.ok]
    ok_lv = [r.target for r in results if r.provider == "lever" and r.ok]
    ok_wd = [r.target for r in results if r.provider == "workday" and r.ok]
    failed = [r for r in results if not r.ok]

    print(
        f"\nVerified working: {len(ok_gh)} Greenhouse, {len(ok_lv)} Lever, {len(ok_wd)} Workday "
        f"(total {len(ok_gh) + len(ok_lv) + len(ok_wd)}/{len(results)}; dropped {len(failed)})."
    )
    if failed:
        print("\nDropped / Failed boards:")
        for r in failed:
            label = r.target.split("|", 1)[0] if r.provider == "workday" else r.target
            print(f"  [SKIP] {r.provider:10s} {label:24s} -> {r.error}")

    print("\n=== Verified .env lines ===")
    print(f"GREENHOUSE_BOARDS={','.join(ok_gh)}")
    print(f"LEVER_SITES={','.join(ok_lv)}")
    print(f"WORKDAY_SITES={','.join(ok_wd)}")

    if args.write_env:
        if not args.env_file.is_file():
            print(
                f"\n[ERROR] {args.env_file} does not exist; refusing to create a partial .env.",
                file=sys.stderr,
            )
            return 1
        update_env_job_boards(
            args.env_file,
            greenhouse_boards=ok_gh,
            lever_sites=ok_lv,
            workday_sites=ok_wd,
        )
        print(
            f"\n[OK] Updated GREENHOUSE_BOARDS, LEVER_SITES, and WORKDAY_SITES in {args.env_file} (mode 0600)."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
