"""Manual live diagnostic for RateMyProfessors GraphQL API connectivity.

Not collected by pytest. Run manually when verifying live GraphQL responses:
    python3 scripts/manual/test_rmp.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

URL = "https://www.ratemyprofessors.com/graphql"

# Public frontend basic authorization header expected by RateMyProfessors' GraphQL gateway.
AUTH_HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
}

SCHOOL_QUERY = """
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

PROF_QUERY = """
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
          ratings(first: 5) {
            edges {
              node {
                class
                helpfulRating
                clarityRating
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


def run_query(query: str, variables: dict):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=AUTH_HEADERS,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print("HTTP Error:", exc.code, exc.read().decode("utf-8"))
        return None


def main() -> None:
    print("Searching School...")
    res = run_query(SCHOOL_QUERY, {"query": {"text": "Toronto Metropolitan University"}})
    print(json.dumps(res, indent=2))

    if res and res["data"]["newSearch"]["schools"]["edges"]:
        school_id = res["data"]["newSearch"]["schools"]["edges"][0]["node"]["id"]
        print("\nSchool ID:", school_id)
        print("\nSearching Professor...")
        prof_res = run_query(PROF_QUERY, {"query": {"text": "Alexander", "schoolID": school_id}})
        print(json.dumps(prof_res, indent=2))


if __name__ == "__main__":
    main()
