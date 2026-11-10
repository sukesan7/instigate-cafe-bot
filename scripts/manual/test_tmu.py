"""Manual live diagnostic for TMU Grades scraping and matplotlib chart generation.

Not collected by pytest. Run manually inside an activated environment when
debugging live tmu.grades.zone parsing:
    python3 scripts/manual/test_tmu.py
"""

from __future__ import annotations

import asyncio

from icbot.features.grades import fetch_course_data, generate_chart


async def main() -> None:
    course = "ele888"
    data = await fetch_course_data(course)
    print("Fetched:", data["title"] if data else "None")
    if data:
        buf = generate_chart(data["title"], data["professors"])
        print("Chart generated, bytes:", buf.getbuffer().nbytes)
        print("Fetching from cache...")
        data2 = await fetch_course_data(course)
        print("Cache returned:", len(data2["professors"]) if data2 else "None", "professors")


if __name__ == "__main__":
    asyncio.run(main())
