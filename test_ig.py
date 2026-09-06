import urllib.request, json

cookies = {}
with open("/opt/yt-bot/cookies.txt") as f:
    for line in f:
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]

cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

shortcode = "DKfSLtfy99c"
url = f"https://www.instagram.com/reel/{shortcode}/"

req = urllib.request.Request(url, headers={
    "Cookie": cookie_str,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
})
resp = urllib.request.urlopen(req, timeout=15)
html = resp.read().decode()

if "loginPage" in html or '"Login"' in html or "accounts/login" in html:
    print("LOGIN PAGE - cookies invalid")
else:
    print("Page OK, length:", len(html))
    import re
    m = re.search(r'"video_url"\s*:\s*"([^"]+)"', html)
    if m:
        print("video_url found:", m.group(1)[:200])
    else:
        print("No video_url in page")
