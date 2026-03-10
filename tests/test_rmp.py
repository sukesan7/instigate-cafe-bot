import urllib.request, json, urllib.error

# Public GraphQL endpoint for RateMyProfessors
URL = "https://www.ratemyprofessors.com/graphql"

# This basic auth token is used publicly by their frontend
AUTH_HEADERS = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
}

def run_query(query: str, variables: dict):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=AUTH_HEADERS,
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("HTTP Error:", e.code, e.read().decode("utf-8"))
        return None

# 1. Search for the School to get its ID
school_query = """
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

print("Searching School...")
res = run_query(school_query, {"query": {"text": "Toronto Metropolitan University"}})
print(json.dumps(res, indent=2))

if res and res["data"]["newSearch"]["schools"]["edges"]:
    school_id = res["data"]["newSearch"]["schools"]["edges"][0]["node"]["id"]
    print("\nSchool ID:", school_id)
    
    # 2. Search for a professor within that school
    prof_query = """
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
    print("\nSearching Professor...")
    prof_res = run_query(prof_query, {"query": {"text": "Alexander", "schoolID": school_id}})
    print(json.dumps(prof_res, indent=2))
