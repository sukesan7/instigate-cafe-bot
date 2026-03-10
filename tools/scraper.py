import re
import json
from bs4 import BeautifulSoup

with open("tmu_raw.html", "r", encoding="utf-8") as f:
    html = f.read()

clean_html = html.replace('\\"', '"')
soup = BeautifulSoup(clean_html, "html.parser")

title_el = soup.find("h2")
course_title = title_el.text.strip() if title_el else "Unknown"
print(f"Course: {course_title}")

distributions = []
for match in re.finditer(r'"distribution":(\[.*?\])', clean_html):
    try:
        dist_str = match.group(1)
        # some payloads might still have escaped slashes depending on how deeply it's nested
        distributions.append(json.loads(dist_str))
    except Exception as e:
        print("err parsing json:", e)

prof_blocks = soup.find_all("div", class_="flex h-full w-full flex-wrap items-center justify-center gap-2 lg:flex-row lg:items-start")

results = []
for i, block in enumerate(prof_blocks):
    name_div = block.select_one(".font-bold")
    if not name_div: continue
    name = name_div.text.strip()
    
    term_p = block.find("p")
    term = term_p.text.strip() if term_p else ""
    
    badges = block.find_all("span", attrs={"data-slot": "badge"})
    avg = badges[0].text if len(badges) > 0 else "N/A"
    common = badges[1].text if len(badges) > 1 else "N/A"
    
    student_span = block.select_one(".bg-accent")
    students = student_span.text.replace("student", "").strip() if student_span else "N/A"
    
    dist = distributions[i] if i < len(distributions) else []
    
    results.append({
        "name": name,
        "term": term,
        "avg": avg,
        "common": common,
        "students": students,
        "dist": dist
    })

for r in results:
    print(f"{r['name']} | {r['students']} students | {r['avg']} | {r['common']}")
    print(" ", r['term'])
    print("  Dist size:", len(r['dist']))
    if r['dist']:
        print("  Sample Dist:", r['dist'][1])

