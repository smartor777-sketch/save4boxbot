import json, urllib.request, time

# Request 1
t1 = time.time()
data = json.dumps({"url": "https://www.reddit.com/r/mildlyinfuriating/s/pk25C5XFrV"}).encode()
req = urllib.request.Request("http://127.0.0.1:8002/formats", data=data, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
body = json.loads(resp.read().decode())
print("Request 1: %.1fs, items: %d" % (time.time()-t1, body.get("media_count", 0)))

# Request 2 (should reuse browser)
t2 = time.time()
data = json.dumps({"url": "https://www.reddit.com/r/LoneStarAllStars/s/KicrFU49Be"}).encode()
req = urllib.request.Request("http://127.0.0.1:8002/formats", data=data, headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
body = json.loads(resp.read().decode())
print("Request 2: %.1fs, items: %d" % (time.time()-t2, body.get("media_count", 0)))
