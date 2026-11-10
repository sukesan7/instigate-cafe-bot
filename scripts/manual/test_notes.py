"""Manual live diagnostic for checking Vite SPA JS bundle links on jsprcrz.github.io/Notes/.

Not collected by pytest. Run manually when validating live site markup:
    python3 scripts/manual/test_notes.py
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request


def main() -> None:
    try:
        html = (
            urllib.request.urlopen("https://jsprcrz.github.io/Notes/", timeout=10)
            .read()
            .decode("utf-8")
        )
        js_match = re.search(r'"(/Notes/assets/index-[a-f0-9]+\.js)"', html)
        if not js_match:
            print("No JS file found.")
            return

        js_url = "https://jsprcrz.github.io" + js_match.group(1)
        print(f"Fetching JS: {js_url}")
        js = urllib.request.urlopen(js_url, timeout=10).read().decode("utf-8")

        matches = re.findall(
            r"(\w{3,4}\s*\d{3}).*?(https://jaspercruz\.notion\.site/[^\s\"\'\\]+)",
            js,
            re.IGNORECASE,
        )
        if matches:
            print(f"Found {len(matches)} course/notion link pairs")
            for m in list(matches)[:15]:
                print(m)
        else:
            print("No simple regex match. Finding all notion sites:")
            links = re.findall(r"https://jaspercruz\.notion\.site/[^\s\"\'\\\}]+", js)
            print("Found", len(links), "links.")
            for link in list(set(links))[:10]:
                print(link)
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        print("Error:", exc)


if __name__ == "__main__":
    main()

