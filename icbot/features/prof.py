import json
import logging
from typing import Optional, Dict, Any, List
import urllib.request
import urllib.error

import aiohttp
import discord
from discord import app_commands

from icbot.app import ic
from icbot.embeds import system as system_embed

log = logging.getLogger(__name__)

RMP_GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
RMP_AUTH_HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
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

async def fetch_graphql(session: aiohttp.ClientSession, query: str, variables: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = {
        "query": query,
        "variables": variables
    }
    try:
        async with session.post(RMP_GRAPHQL_URL, json=payload, headers=RMP_AUTH_HEADERS, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                log.error(f"[RMP] GraphQL HTTP Error: {resp.status}")
                return None
    except Exception as e:
        log.error(f"[RMP] Exception during GraphQL Fetch: {e}")
        return None


async def get_school_id(session: aiohttp.ClientSession, university_name: str) -> Optional[str]:
    data = await fetch_graphql(session, SCHOOL_SEARCH_QUERY, {"query": {"text": university_name}})
    if not data:
        return None
    
    try:
        edges = data["data"]["newSearch"]["schools"]["edges"]
        if not edges:
            return None
        # Always take the top result 
        return edges[0]["node"]["id"]
    except KeyError as e:
        log.error(f"[RMP] Unexpected payload tracking school ID: {e}")
        return None


async def search_prof(session: aiohttp.ClientSession, name: str, school_id: str) -> List[Dict[str, Any]]:
    data = await fetch_graphql(session, PROF_SEARCH_QUERY, {"query": {"text": name, "schoolID": school_id}})
    if not data:
        return []
    
    try:
        edges = data["data"]["newSearch"]["teachers"]["edges"]
        return [e["node"] for e in edges]
    except KeyError as e:
        log.error(f"[RMP] Unexpected payload tracking professor data: {e}")
        return []


def build_prof_embed(settings, prof_data: Dict[str, Any], university_name: str) -> discord.Embed:
    name = f"{prof_data.get('firstName', '')} {prof_data.get('lastName', '')}".strip()
    department = prof_data.get('department', 'Unknown Department')
    num_ratings = prof_data.get('numRatings', 0)
    
    avg_rating = prof_data.get('avgRating', 0.0)
    avg_diff = prof_data.get('avgDifficulty', 0.0)
    wta_pct = prof_data.get('wouldTakeAgainPercent', -1)
    
    if wta_pct >= 0:
        wta_str = f"{round(wta_pct)}%"
    else:
        wta_str = "N/A"
        
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
    tags = prof_data.get('teacherRatingTags', [])
    if tags:
        # Sort by count descending
        tags_sorted = sorted(tags, key=lambda x: x.get('tagCount', 0), reverse=True)
        top_tags = [t.get('tagName') for t in tags_sorted[:5]]
        fields.append(("🏷️ Top Tags", ", ".join(top_tags), False))
        
    # Extract recent classes
    ratings = prof_data.get('ratings', {}).get('edges', [])
    if ratings:
        recent_comments = []
        for r in ratings:
            node = r.get('node', {})
            course = node.get('class', 'Unknown')
            comment = node.get('comment', '')
            if len(comment) > 100:
                comment = comment[:100] + "..."
            rating = node.get('helpfulRating', '?')
            recent_comments.append(f"**{course}** ({rating}/5): {comment}")
            
        if recent_comments:
            fields.append(("💬 Recent Reviews", "\n\n".join(recent_comments), False))

    embed = system_embed(
        settings,
        title=title,
        summary=summary,
        fields=fields,
        footer_hint="RateMyProfessors"
    )
    # Add a direct link to the RMP page if we know the ID scheme (Base64)
    prof_id = prof_data.get('id', '')
    if prof_id:
        embed.url = f"https://www.ratemyprofessors.com/professor/{prof_id}"

    return embed


@ic.command(name="prof", description="Lookup a professor's rating on RateMyProfessors.")
@app_commands.describe(name="The name of the professor to lookup.")
async def prof_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)

    settings = getattr(interaction.client, "settings", None)
    if settings is None:
        await interaction.followup.send("❌ Bot not initialized properly.", ephemeral=True)
        return
        
    university_name = getattr(settings, "rmp_university", "Toronto Metropolitan University")

    async with aiohttp.ClientSession() as session:
        # 1. Fetch school ID 
        school_id = await get_school_id(session, university_name)
        if not school_id:
             await interaction.followup.send(f"❌ Could not locate the University ID for `{university_name}`.", ephemeral=True)
             return
             
        # 2. Search Professor
        profs = await search_prof(session, name, school_id)
        
    if not profs:
        await interaction.followup.send(f"❌ Could not find a professor matching `{name}` at {university_name}.", ephemeral=True)
        return
        
    if len(profs) > 5:
        names = [f"{p.get('firstName')} {p.get('lastName')} ({p.get('department')})" for p in profs[:5]]
        await interaction.followup.send(
            f"⚠️ Found {len(profs)} results for `{name}`. Please be more specific.\n"
            f"Top matches: {', '.join(names)}",
            ephemeral=True
        )
        return
        
    # If 1-5 results, we'll just take the top exact match or highest rated one if there are conflicts.
    # Usually exact string searches sort exactly the one you want to the top.
    best_match = profs[0]
    
    embed = build_prof_embed(settings, best_match, university_name)
    await interaction.followup.send(embed=embed)
