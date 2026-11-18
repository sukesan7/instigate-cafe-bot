import logging
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import discord
from discord import app_commands

from icbot.academics.models import ProfessorRecord, ProviderProvenance, campus_from_settings
from icbot.app import ic
from icbot.container import get_container, resolve_guild_settings
from icbot.embeds import system as system_embed

log = logging.getLogger(__name__)

RMP_GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
RMP_AUTH_HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
}

SCHOOL_SEARCH_QUERY = """
query ($query: SchoolSearchQuery!) {
  newSearch {
    schools(query: $query) {
      edges {
        node {
          id
          name
        }
      }
    }
  }
}
"""

PROF_SEARCH_QUERY = """
query ($query: TeacherSearchQuery!) {
  newSearch {
    teachers(query: $query) {
      edges {
        node {
          id
          firstName
          lastName
          avgRating
          numRatings
          wouldTakeAgainPercent
          avgDifficulty
          department
          teacherRatingTags {
            tagName
            tagCount
          }
          ratings(first: 3) {
            edges {
              node {
                class
                helpfulRating
                difficultyRating
                comment
              }
            }
          }
        }
      }
    }
  }
}
"""


async def fetch_graphql(
    session: aiohttp.ClientSession, query: str, variables: dict[str, Any]
) -> dict[str, Any] | None:
    payload = {"query": query, "variables": variables}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.post(
            RMP_GRAPHQL_URL, json=payload, headers=RMP_AUTH_HEADERS, timeout=timeout
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, dict) else None
            else:
                log.error("[RMP] GraphQL HTTP Error: %s", resp.status)
                return None
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        log.error("[RMP] Exception during GraphQL Fetch: %s", e)
        return None


async def get_school_id(session: aiohttp.ClientSession, university_name: str) -> str | None:
    data = await fetch_graphql(session, SCHOOL_SEARCH_QUERY, {"query": {"text": university_name}})
    if not data:
        return None

    try:
        edges = data["data"]["newSearch"]["schools"]["edges"]
        if not edges:
            return None
        # Always take the top result
        node_id = edges[0]["node"]["id"]
        return str(node_id) if node_id is not None else None
    except KeyError as e:
        log.error("[RMP] Unexpected payload tracking school ID: %s", e)
        return None


async def search_prof(session: aiohttp.ClientSession, name: str, school_id: str) -> list[dict[str, Any]]:
    data = await fetch_graphql(session, PROF_SEARCH_QUERY, {"query": {"text": name, "schoolID": school_id}})
    if not data:
        return []

    try:
        edges = data["data"]["newSearch"]["teachers"]["edges"]
        return [e["node"] for e in edges]
    except KeyError as e:
        log.error("[RMP] Unexpected payload tracking professor data: %s", e)
        return []


def build_prof_embed(settings, prof_data: dict[str, Any], university_name: str) -> discord.Embed:
    name = f"{prof_data.get('firstName', '')} {prof_data.get('lastName', '')}".strip()
    department = prof_data.get("department", "Unknown Department")
    num_ratings = prof_data.get("numRatings", 0)

    avg_rating = prof_data.get("avgRating", 0.0)
    avg_diff = prof_data.get("avgDifficulty", 0.0)
    wta_pct = prof_data.get("wouldTakeAgainPercent", -1)

    wta_str = f"{round(wta_pct)}%" if wta_pct >= 0 else "N/A"

    title = f"🍎 {name} — {avg_rating}/5.0"
    if avg_rating == 0 and num_ratings == 0:
        title = f"🍎 {name} — No Ratings"

    summary = (
        f"**{university_name}** • {department}\n"
        f"Total Ratings: **{num_ratings}**\n"
        f"Difficulty: **{round(avg_diff, 1)} / 5.0**\n"
        f"Would Take Again: **{wta_str}**"
    )

    fields = []

    # Extract tags
    tags = prof_data.get("teacherRatingTags", [])
    if tags:
        # Sort by count descending
        tags_sorted = sorted(tags, key=lambda x: x.get("tagCount", 0), reverse=True)
        top_tags = [t.get("tagName") for t in tags_sorted[:5]]
        fields.append(("🏷️ Top Tags", ", ".join(top_tags), False))

    # Extract recent classes
    ratings = prof_data.get("ratings", {}).get("edges", [])
    if ratings:
        recent_comments = []
        for r in ratings:
            node = r.get("node", {})
            course = node.get("class", "Unknown")
            comment = node.get("comment", "")
            if len(comment) > 100:
                comment = comment[:100] + "..."
            rating = node.get("helpfulRating", "?")
            recent_comments.append(f"**{course}** ({rating}/5): {comment}")

        if recent_comments:
            fields.append(("💬 Recent Reviews", "\n\n".join(recent_comments), False))

    embed = system_embed(
        settings, title=title, summary=summary, fields=fields, footer_hint="RateMyProfessors"
    )
    # Add a direct link to the RMP page if we know the ID scheme (Base64)
    prof_id = prof_data.get("id", "")
    if prof_id:
        embed.url = f"https://www.ratemyprofessors.com/professor/{prof_id}"

    return embed


@asynccontextmanager
async def _scoped_http_session(session: aiohttp.ClientSession | None):
    if session is not None and not session.closed:
        yield session
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as temp_session:
        yield temp_session


def build_prof_record_embed(
    settings,
    record: ProfessorRecord,
    provenance: ProviderProvenance | None = None,
) -> discord.Embed:
    """Build a Discord embed from a typed ``ProfessorRecord`` with source attribution."""
    wta_str = f"{round(record.would_take_again_pct)}%" if record.would_take_again_pct >= 0 else "N/A"

    title = f"🍎 {record.full_name} — {record.avg_rating}/5.0"
    if record.avg_rating == 0 and record.num_ratings == 0:
        title = f"🍎 {record.full_name} — No Ratings"

    summary = (
        f"**{record.institution}** • {record.department}\n"
        f"Total Ratings: **{record.num_ratings}**\n"
        f"Difficulty: **{round(record.avg_difficulty, 1)} / 5.0**\n"
        f"Would Take Again: **{wta_str}**"
    )

    fields: list[tuple[str, str, bool]] = []
    if record.top_tags:
        fields.append(("🏷️ Top Tags", ", ".join(record.top_tags[:5]), False))

    if record.recent_reviews:
        recent_comments = []
        for rev in record.recent_reviews[:3]:
            comment = rev.comment
            if len(comment) > 100:
                comment = comment[:100] + "..."
            recent_comments.append(f"**{rev.course_code}** ({rev.helpful_rating}/5): {comment}")
        if recent_comments:
            fields.append(("💬 Recent Reviews", "\n\n".join(recent_comments), False))

    footer_label = provenance.provider_name if provenance is not None else "RateMyProfessors"
    embed = system_embed(
        settings,
        title=title,
        summary=summary,
        fields=fields,
        footer_hint=footer_label,
    )
    if record.profile_url:
        embed.url = record.profile_url
    return embed


@ic.command(name="prof", description="Lookup a professor's rating via the campus professor provider.")
@app_commands.describe(name="The name of the professor to lookup.")
async def prof_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)

    if getattr(interaction.client, "settings", None) is None:
        await interaction.followup.send("❌ Bot not initialized properly.", ephemeral=True)
        return

    settings = await resolve_guild_settings(interaction.client, interaction)
    container = get_container(interaction.client)
    campus = campus_from_settings(settings)
    open_session = (
        container.http_session
        if container.http_session is not None and not container.http_session.closed
        else None
    )

    result = await container.academic_registry.search_professors(
        name,
        campus=campus,
        settings=settings,
        session=open_session,
        clock=container.clock,
    )

    if result.status == "unsupported_campus":
        await interaction.followup.send(
            f"ℹ️ {result.error_message or f'No professor provider is configured for {campus.name}.'}",
            ephemeral=True,
        )
        return

    if result.status in ("error", "unsafe_url"):
        await interaction.followup.send(
            f"❌ Professor lookup provider (`{result.provenance.provider_name}`) encountered an error.",
            ephemeral=True,
        )
        return

    if not result.ok or not result.data:
        if result.error_code == "institution_not_found":
            await interaction.followup.send(
                f"❌ Could not locate the University ID for `{campus.name}`.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"❌ Could not find a professor matching `{name}` at {campus.name}.", ephemeral=True
        )
        return

    profs = result.data
    if len(profs) > 5:
        names = [f"{p.full_name} ({p.department})" for p in profs[:5]]
        await interaction.followup.send(
            f"⚠️ Found {len(profs)} results for `{name}`. Please be more specific.\n"
            f"Top matches: {', '.join(names)}",
            ephemeral=True,
        )
        return

    embed = build_prof_record_embed(settings, profs[0], result.provenance)
    await interaction.followup.send(embed=embed)
