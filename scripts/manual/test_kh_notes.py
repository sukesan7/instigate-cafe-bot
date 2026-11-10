"""Manual live diagnostic for checking HTML/Notion links on notes.krishadmin.com.

Not collected by pytest. Run manually when validating live site markup:
    python3 scripts/manual/test_kh_notes.py
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request


def main() -> None:
    url = "https://notes.krishadmin.com/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")

        matches = re.findall(
            r"(\w{3,4}\s*\d{3}).*?(https://[^\.]+\.notion\.site/[^\s\"\'\\]+)",
            html,
            re.IGNORECASE,
        )
        print(f"Regex 1 Found: {len(matches)}")
        if matches:
            print(matches[:5])

        raw_links = set(re.findall(r"https://[^\.]+\.notion\.site/[^\s\"\'\\>]+", html))
        print(f"Raw notion links found: {len(raw_links)}")
        for link in list(raw_links)[:5]:
            print(link)
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        print(f"Error: {exc}")


if __name__ == "__main__":
    main()

