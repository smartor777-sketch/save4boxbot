from playwright.sync_api import sync_playwright
import json

pw = sync_playwright().start()
browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'])
context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

cookies = []
with open('/opt/yt-bot/reddit_cookies.txt') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        cookies.append({'name': name, 'value': value, 'domain': domain, 'path': path, 'secure': secure.upper() == 'TRUE', 'httpOnly': False})
context.add_cookies(cookies)

page = context.new_page()
page.goto('https://www.reddit.com/r/LoneStarAllStars/s/KicrFU49Be', wait_until='domcontentloaded', timeout=30000)
page.wait_for_timeout(8000)

js1 = """() => {
    const el = document.querySelector('nsfw-blocking-container, [data-testid=nsfw-overlay], shreddit-nsfw-overlay');
    if (el) return el.outerHTML.substring(0, 2000);
    return null;
}"""
nsfw = page.evaluate(js1)
print('NSFW element:', nsfw)

js2 = """() => {
    return Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim()).filter(t => t);
}"""
buttons = page.evaluate(js2)
print('Buttons:', buttons)

js3 = """() => {
    const p = document.querySelector('shreddit-post');
    if (!p) return null;
    var result = {};
    for (var i = 0; i < p.attributes.length; i++) {
        var a = p.attributes[i];
        result[a.name] = a.value.substring(0, 200);
    }
    return result;
}"""
post = page.evaluate(js3)
print('Post:', json.dumps(post, indent=2) if post else 'None')

js4 = """() => {
    return Array.from(document.querySelectorAll('img')).map(i => i.src).filter(s => s && s.includes('redd.it'));
}"""
imgs = page.evaluate(js4)
print('Reddit imgs:', imgs)

browser.close()
pw.stop()
