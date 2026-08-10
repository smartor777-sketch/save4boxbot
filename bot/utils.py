import hashlib
import re

# Полный URL до первых пробелов/знаков переноса
URL_RE = re.compile(r"https?://[^\s]+")

# Извлекаем ID видео из разных форматов YouTube
_SHORTS_RE = re.compile(r"(?:www\.|m\.)?youtube\.com/shorts/([\w\-]+)")
_LIVE_RE = re.compile(r"(?:www\.|m\.)?youtube\.com/live/([\w\-]+)")
_EMBED_RE = re.compile(r"(?:www\.|m\.)?youtube\.com/embed/([\w\-]+)")
_PATH_RE = re.compile(r"(?:www\.|m\.)?youtube\.com/v/([\w\-]+)")
_WATCH_RE = re.compile(r"[?&]v=([\w\-]+)")
_YOUTU_RE = re.compile(r"youtu\.be/([\w\-]+)")

# Instagram ссылки (пост / рилс / видео)
_INSTAGRAM_RE = re.compile(
    r"(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories)/([\w\-]+)"
)

# TikTok ссылки
_TIKTOK_VIDEO_RE = re.compile(
    r"(?:www\.|vm\.|vt\.)?tiktok\.com/.*?/video/(\d+)"
)
_TIKTOK_PHOTO_RE = re.compile(
    r"(?:www\.|vm\.|vt\.)?tiktok\.com/.*?/photo/(\d+)"
)
_TIKTOK_SHORT_RE = re.compile(r"(?:vm\.|vt\.)tiktok\.com/\S+")

# VK / VK Video ссылки (domains: vk.com, vk.ru, vkvideo.ru)
_VK_DOMAIN_RE = re.compile(r"\bvk(?:video)?\.(?:com|ru)")
_VK_VIDEO_RE = re.compile(r"/(?:video|clip)(-?\d+_\d+)")
_VK_Z_RE = re.compile(r"[?&]z=(?:video|clip)(-?\d+_\d+)")

# Яндекс Видео — ссылки на «превью» из поиска (filmId), в т.ч. мобильные /touch/
_YANDEX_PREVIEW_RE = re.compile(
    r"https?://(?:www\.)?yandex\.\w{2,3}(?:\.(?:am|ge|il|tr))?/video/(?:touch/)?preview"
)

# Rutube — одиночные видео и встраиваемые
_RUTUBE_RE = re.compile(
    r"rutube\.ru/(?:(?:live/)?video(?:/private)?|(?:play/)?embed)/([\da-z]{32})"
)


def _video_id_from(url: str) -> str | None:
    for pattern in (_SHORTS_RE, _LIVE_RE, _EMBED_RE, _PATH_RE):
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


def extract_instagram_url(text: str) -> tuple[str, str] | None:
    """Возвращает (канонический пост-URL, shortcode) для Instagram или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    if "instagram.com" not in raw:
        return None
    m = _INSTAGRAM_RE.search(raw)
    if not m:
        return None

    shortcode = m.group(1)
    return f"https://www.instagram.com/p/{shortcode}/", shortcode


def extract_tiktok_url(text: str) -> tuple[str, str] | None:
    """Возвращает (URL, короткий ключ) для TikTok или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    is_tiktok = ".tiktok.com" in raw
    if not is_tiktok:
        return None
    if not (
        _TIKTOK_VIDEO_RE.search(raw)
        or _TIKTOK_PHOTO_RE.search(raw)
        or _TIKTOK_SHORT_RE.search(raw)
    ):
        return None

    key = hashlib.md5(raw.encode()).hexdigest()[:10]
    return raw, key


def extract_vk_url(text: str) -> tuple[str, str] | None:
    """Возвращает (канонический VK-URL, video_id) или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    if not _VK_DOMAIN_RE.search(raw):
        return None
    m = _VK_VIDEO_RE.search(raw) or _VK_Z_RE.search(raw)
    if not m:
        return None

    video_id = m.group(1)
    return f"https://vk.com/video{video_id}", video_id


def extract_yandex_url(text: str) -> tuple[str, str] | None:
    """Возвращает (URL превью, ключ) для Яндекс Видео или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    if not _YANDEX_PREVIEW_RE.search(raw):
        return None

    key = hashlib.md5(raw.encode()).hexdigest()[:10]
    return raw, key


def extract_rutube_url(text: str) -> tuple[str, str] | None:
    """Возвращает (URL видео, video_id) для Rutube или None."""
    raw_match = URL_RE.search(text.strip())
    if not raw_match:
        return None

    raw = raw_match.group(0).rstrip(".,!?)")
    m = _RUTUBE_RE.search(raw)
    if not m:
        return None

    return raw, m.group(1)


def extract_video(text: str) -> tuple[str, str, str] | None:
    """Возвращает (platform, url, key) или None."""
    parsed = extract_youtube_url(text)
    if parsed:
        url, key = parsed
        return "youtube", url, key
    parsed = extract_instagram_url(text)
    if parsed:
        url, key = parsed
        return "instagram", url, key
    parsed = extract_tiktok_url(text)
    if parsed:
        url, key = parsed
        return "tiktok", url, key
    parsed = extract_vk_url(text)
    if parsed:
        url, key = parsed
        return "vk", url, key
    parsed = extract_yandex_url(text)
    if parsed:
        url, key = parsed
        return "yandex", url, key
    parsed = extract_rutube_url(text)
    if parsed:
        url, key = parsed
        return "rutube", url, key
    return None
