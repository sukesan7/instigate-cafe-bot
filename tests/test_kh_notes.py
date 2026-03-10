import urllib.request, re

try:
    url = "https://notes.krishadmin.com/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
    
    # E.g. find <a href="https://*.notion.site/...">...</a> or something
    matches = re.findall(r'(\w{3,4}\s*\d{3}).*?(https://[^\.]+\.notion\.site/[^\s\"\'\\]+)', html, re.IGNORECASE)
    print(f"Regex 1 Found: {len(matches)}")
    if matches:
        print(matches[:5])
        
    # fallback
    raw_links = set(re.findall(r'https://[^\.]+\.notion\.site/[^\s"\'\\>]+', html))
    print(f"Raw notion links found: {len(raw_links)}")
    
    for l in list(raw_links)[:5]:
        print(l)
except Exception as e:
    print(e)
