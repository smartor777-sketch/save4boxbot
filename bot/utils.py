import re

# Полный URL до первых пробелов/знаков переноса
URL_RE = re.compile(r"https?://[^\s]+")

# Извлекаем ID видео из разных форматов YouTube
_SHORTS_RE = re.compile(r"youtube\.com/shorts/([\w\-]+)")
_WATCH_RE = re.compile(r"[?&]v=([\w\-]+)")
_YOUTU_RE = re.compile(r"youtu\.be/([\w\-]+)")
_LIVE_RE = re.compile(r"youtube\.com/live/([\w\-]+)")


def _video_id_from(url: str) -> str | None:
    for pattern in (_SHORTS_RE, _LIVE_RE):
        m = pattern.search(url)
        if m:
            return m.group(1)
    m = _WATCH_RE.search(url)
    if m:
        return m.group(1)
    m = _YOUTU_RE.search(url)
    if m:
        return m.group(1)
    return None


def extract_youtube_url(text: str) -> tuple[str, str] | None:
    """Возвращает (канонический watch-URL, video_id) или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    video_id = _video_id_from(raw)
    if not video_id:
        return None

    return f"https://www.youtube.com/watch?v={video_id}", video_id