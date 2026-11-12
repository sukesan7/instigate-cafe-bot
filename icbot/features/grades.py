import io
import json
import logging
import re
import urllib.parse

import aiohttp
import aiosqlite
import discord
import matplotlib.pyplot as plt
import numpy as np
from bs4 import BeautifulSoup
from discord import app_commands

from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed
from icbot.errors import format_exception_safe, redact_secrets

log = logging.getLogger(__name__)

DB_PATH = "tmu_grades.db"


async def _init_db(db_path: str | None = None):
    eff_db = db_path or DB_PATH
    async with aiosqlite.connect(eff_db) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_cache (
                course_code TEXT PRIMARY KEY,
                title TEXT,
                parsed_data JSON,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def _fetch_grades_html(url: str, headers: dict[str, str], session: aiohttp.ClientSession) -> str | None:
    async with session.get(url, headers=headers) as resp:
        if resp.status == 404:
            return None  # Course not found
        resp.raise_for_status()
        return await resp.text()


async def fetch_course_data(
    course_code: str,
    *,
    db_path: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    """Fetch from TMU Grades or return cached payload."""
    eff_db = db_path or DB_PATH
    await _init_db(eff_db)  # Ensure DB is ready
    course_code = course_code.lower().strip()

    # Check cache first
    async with (
        aiosqlite.connect(eff_db) as db,
        db.execute(
            "SELECT title, parsed_data FROM course_cache WHERE course_code = ?", (course_code,)
        ) as cursor,
    ):
        row = await cursor.fetchone()
        if row:
            log.info("Serving %s from local SQLite cache.", course_code)
            return {"title": row[0], "professors": json.loads(row[1])}

    # Not in cache, scrape live
    url = f"https://tmu.grades.zone/course/{urllib.parse.quote(course_code)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    if session is not None and not session.closed:
        html = await _fetch_grades_html(url, headers, session)
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as tmp_session:
            html = await _fetch_grades_html(url, headers, tmp_session)

    if html is None:
        return None

    # Parse HTML
    clean_html = html.replace('\\"', '"')
    soup = BeautifulSoup(clean_html, "html.parser")

    title_el = soup.find("h2")
    if not title_el:
        return None  # Invalid page block

    course_title = title_el.text.strip()

    distributions = []
    for match in re.finditer(r'"distribution":(\[.*?\])', clean_html):
        try:
            distributions.append(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError) as exc:
            log.debug("Skipping malformed distribution JSON for %s: %s", course_code, exc)

    prof_blocks = soup.find_all(
        "div",
        class_="flex h-full w-full flex-wrap items-center justify-center gap-2 lg:flex-row lg:items-start",
    )

    results = []
    for i, block in enumerate(prof_blocks):
        name_div = block.select_one(".font-bold")
        if not name_div:
            continue
        name = name_div.text.strip()

        # Skip the aggregate 'All Professors' row as per user request
        if name.lower() == "all professors":
            continue

        term_p = block.find("p")
        term = term_p.text.strip() if term_p else ""

        badges = block.find_all("span", attrs={"data-slot": "badge"})
        avg = badges[0].text if len(badges) > 0 else "N/A"
        common = badges[1].text if len(badges) > 1 else "N/A"

        student_span = block.select_one(".bg-accent")
        students = student_span.text.replace("student", "").strip() if student_span else ("N/A")

        dist = distributions[i] if i < len(distributions) else []

        results.append(
            {"name": name, "term": term, "avg": avg, "common": common, "students": students, "dist": dist}
        )

    # Save to Cache
    async with aiosqlite.connect(eff_db) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO course_cache (course_code, title, parsed_data)
            VALUES (?, ?, ?)
        """,
            (course_code, course_title, json.dumps(results)),
        )
        await db.commit()

    return {"title": course_title, "professors": results}


def generate_chart(course_title: str, professors: list) -> io.BytesIO:
    """Generate a grouped bar chart using Matplotlib for the given professors distributions."""
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10, 6))

    # Static X-axis letter grades
    letters = ["F-S", "F", "FNA", "D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+"]

    # Prepare data map
    x = np.arange(len(letters))
    num_profs = len(professors)

    if num_profs == 0:
        # Fallback empty chart
        ax.text(0.5, 0.5, "No Distribution Data", ha="center", va="center", transform=ax.transAxes)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        return buf

    # Calculate dynamic bar width
    total_width = 0.8
    width = total_width / num_profs

    cmap = plt.get_cmap("Set2")  # Nice distinct colors

    max_y = 0
    for i, prof in enumerate(professors):
        dist = prof.get("dist", [])

        # Map the json payload counts to the letter array
        counts_map = {item["letterGrade"]: item["count"] for item in dist}

        y_vals = []
        total_students = sum(counts_map.values())
        for letter in letters:
            count = counts_map.get(letter, 0)
            # We plot percentages to match the reference image's Y-axis
            pct = (count / total_students * 100) if total_students > 0 else 0
            y_vals.append(pct)
            if pct > max_y:
                max_y = pct

        offset = (i - num_profs / 2) * width + width / 2
        ax.bar(x + offset, y_vals, width, label=prof["name"], color=cmap(i % 8))

    ax.set_ylabel("Percentage of Students")
    ax.set_title(f"{course_title} - Grade Distribution")
    ax.set_xticks(x)
    ax.set_xticklabels(letters)
    ax.legend()

    # Minor grid lines
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.set_axisbelow(True)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf


@ic.command(name="grades", description="Fetch TMU grade distributions by course code")
@app_commands.describe(course="The course code (e.g., ele888, cps109)")
async def grades_command(interaction: discord.Interaction, course: str):
    await interaction.response.defer(thinking=True)

    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    eff_db_path = DB_PATH if DB_PATH != "tmu_grades.db" else settings.grades_db_path
    shared_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    try:
        data = await fetch_course_data(course, db_path=eff_db_path, session=shared_session)
        if not data:
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="Course Not Found",
                    summary=f"Could not locate grade distributions for `{course}`. Ensure the course code is valid on `tmu.grades.zone`.",
                )
            )
            return

        if not data["professors"]:
            await interaction.followup.send(
                embed=system_embed(
                    settings,
                    title="No Professor Data",
                    summary=f"`{data['title']}` exists, but has no discrete professor distributions available.",
                )
            )
            return

        # Build Graph
        chart_bytes = generate_chart(data["title"], data["professors"])
        file = discord.File(chart_bytes, filename="distribution.png")

        embed = system_embed(
            settings,
            title=data["title"],
            summary="[View on TMU Grades Zone](https://tmu.grades.zone/course/"
            + urllib.parse.quote(course.lower().strip())
            + ")",
        )
        embed.set_image(url="attachment://distribution.png")

        for prof in data["professors"]:
            val = f"**{prof['students']} students** | {prof['avg']} | {prof['common']}\n*{prof['term']}*"
            embed.add_field(name=prof["name"], value=val, inline=False)

        await interaction.followup.send(embed=embed, file=file)

    except (
        aiohttp.ClientError,
        TimeoutError,
        aiosqlite.Error,
        discord.DiscordException,
        OSError,
        LookupError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        secrets = settings.secret_values()
        log.warning(
            "Error fetching grades for %s: %s",
            redact_secrets(course, secrets),
            format_exception_safe(exc, secrets),
        )
        await interaction.followup.send(
            embed=system_embed(
                settings,
                title="Grades Error",
                summary="An unexpected internal error occurred while fetching course grades.",
            )
        )
