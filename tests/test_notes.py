import urllib.request, re

try:
    html = urllib.request.urlopen("https://jsprcrz.github.io/Notes/", timeout=10).read().decode("utf-8")
    js_match = re.search(r'"(/Notes/assets/index-[a-f0-9]+\.js)"', html)
    if not js_match:
        print("No JS file found.")
    else:
        js_url = "https://jsprcrz.github.io" + js_match.group(1)
        print(f"Fetching JS: {js_url}")
        js = urllib.request.urlopen(js_url, timeout=10).read().decode("utf-8")
        
        # Look for course code patterns: something like title:"ELE888", url:"..."
        # We can extract all course-like strings and URLs
        matches = re.findall(r'(\w{3,4}\s*\d{3}).*?(https://jaspercruz\.notion\.site/[^\s\"\'\\]+)', js, re.IGNORECASE)
        if matches:
            print(f"Found {len(matches)} course/notion link pairs")
            for m in list(matches)[:15]:
                print(m)
        else:
            print("No simple regex match. Finding all notion sites:")
            links = re.findall(r'https://jaspercruz\.notion\.site/[^\s\"\'\\\}]+', js)
            print("Found", len(links), "links.")
            for l in list(set(links))[:10]:
                print(l)
except Exception as e:
    print("Error:", e)
