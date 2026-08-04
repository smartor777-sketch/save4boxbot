import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot.utils import extract_youtube_url

cases = [
    # (вход, ожидаемый video_id или None)
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
    ("https://m.youtube.com/shorts/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/v/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", "dQw4w9WgXcQ"),
    ("смотри вот https://www.youtube.com/watch?v=dQw4w9WgXcQ класс!", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts?cbrd=1", None),
    ("https://vk.com/video123", None),
    ("просто текст без ссылки", None),
    ("https://youtube.com/shorts/LpdiocCvzY8", "LpdiocCvzY8"),
]

failed = 0
for text, expected in cases:
    result = extract_youtube_url(text)
    got = result[1] if result else None
    status = "OK " if got == expected else "FAIL"
    if got != expected:
        failed += 1
    print(f"{status} | {text} -> {got} (expected {expected})")

print(f"\n{len(cases) - failed}/{len(cases)} passed")
sys.exit(1 if failed else 0)