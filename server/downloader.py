import os
import queue
import re
import shutil
import tempfile
import threading
import time

import httpx

from dotenv import load_dotenv

from . import stats

load_dotenv()

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_MB", "50")) * 1024 * 1024

COOKIE_FILE = os.getenv("COOKIE_FILE", "")
VK_COOKIE_FILE = os.getenv("VK_COOKIE_FILE", "")

CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_MIN", "15")) * 60
FILE_MAX_AGE_SEC = int(os.getenv("FILE_MAX_AGE_MIN", "15")) * 60

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)")
TIKTOK_RE = re.compile(r"tiktok\.com")
INSTAGRAM_RE = re.compile(r"instagram\.com")
VK_RE = re.compile(
    r"\bvk(?:video)?\.(?:com|ru)/(?:video|clip)(-?\d+_\d+)"
    r"|\bvk(?:video)?\.(?:com|ru)/[^?\s]*\?.*?z=(?:video|clip)(-?\d+_\d+)"
)
RUTUBE_RE = re.compile(r"\brutube\.ru")
COUB_RE = re.compile(r"\bcoub\.com")
YANDEX_VIDEO_RE = re.compile(r"\byandex\.\w{2,3}(?:\.(?:am|ge|il|tr))?/video/(?:touch/)?preview")
DZEN_RE = re.compile(r"\bdzen\.ru")
REDDIT_RE = re.compile(r"(?:www\.)?reddit\.com|r\.redd\.it|reddit\.com")

INSTAGRAM_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "4"))
DOWNLOAD_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

_PROGRESS: dict[tuple[str, int | None, str | None], dict] = {}
_PROGRESS_LOCK = threading.Lock()


def _register_progress(key: tuple[str, int | None, str | None], **kw) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS[key] = kw


def _unregister_progress(key: tuple[str, int | None, str | None]) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.pop(key, None)


def get_progress(url: str, height: int | None, codec: str | None = None) -> dict | None:
    with _PROGRESS_LOCK:
        return _PROGRESS.get((url, height, codec))

SOCKET_TIMEOUT_SEC = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "30"))
DOWNLOAD_TIMEOUT_SEC = int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "300"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

THUMB_TIMEOUT_SEC = int(os.getenv("THUMB_TIMEOUT_SEC", "10"))
THUMB_MAX_BYTES = int(os.getenv("THUMB_MAX_MB", "5")) * 1024 * 1024

_CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _save_thumbnail(info: dict) -> str | None:
    """Скачивает превью видео и кладёт его в DOWNLOAD_DIR.

    Возвращает имя файла (для /file/…) или None, если превью недоступно.
    """
    url = (info or {}).get("thumbnail")
    if not url or not url.startswith(("http://", "https://")):
        return None
    vid = re.sub(r"[^A-Za-z0-9_\-]+", "", (info or {}).get("id") or "video") or "video"
    filename = f"thumb_{vid}.jpg"
    try:
        with httpx.Client(timeout=THUMB_TIMEOUT_SEC, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not content_type.startswith("image/"):
                    return None
                chunks = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > THUMB_MAX_BYTES:
                        return None
                    chunks.append(chunk)
                if not chunks:
                    return None
                ext = _CTYPE_EXT.get(content_type, ".jpg")
                if ext != ".jpg":
                    filename = f"thumb_{vid}{ext}"
        path = os.path.join(DOWNLOAD_DIR, filename)
        with open(path, "wb") as fh:
            fh.write(b"".join(chunks))
        return filename
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except (OSError, NameError):
            pass
        return None


class DownloadTimeoutError(Exception):
    pass


class FileTooBigError(Exception):
    pass


class ExtractRetryError(Exception):
    pass


RETRY_ATTEMPTS = int(os.getenv("EXTRACT_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("EXTRACT_RETRY_BACKOFF_SEC", "2"))


def _extract_info_with_retry(ydl, url: str, download: bool) -> dict:
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return ydl.extract_info(url, download=download)
        except (DownloadTimeoutError, FileTooBigError):
            raise
        except Exception as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
    raise last_err


def is_youtube(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url or ""))


def is_instagram(url: str) -> bool:
    return bool(INSTAGRAM_RE.search(url or ""))


def is_supported(url: str) -> str | None:
    """Возвращает платформу ('youtube'/'tiktok'/'instagram'/'vk'/'rutube'/'coub'/'yandex'/'dzen') или None."""
    u = url or ""
    if YOUTUBE_RE.search(u):
        return "youtube"
    if TIKTOK_RE.search(u):
        return "tiktok"
    if INSTAGRAM_RE.search(u):
        return "instagram"
    if VK_RE.search(u):
        return "vk"
    if RUTUBE_RE.search(u):
        return "rutube"
    if COUB_RE.search(u):
        return "coub"
    if YANDEX_VIDEO_RE.search(u):
        return "yandex"
    if DZEN_RE.search(u):
        return "dzen"
    if REDDIT_RE.search(u):
        return "reddit"
    return None


def _media_kind(info: dict) -> str:
    """'video' или 'image' — определяем по наличию видеоформатов."""
    for f in info.get("formats") or []:
        if f.get("vcodec") and f["vcodec"] != "none":
            return "video"
    return "image"


def _instagram_title(username: str | None, kinds: set[str]) -> str:
    """Человеческий заголовок поста по типам медиа (не 'Video by')."""
    if "video" in kinds:
        label = "Видео"
    elif kinds == {"image"}:
        label = "Фото"
    else:
        label = "Пост"
    return f"{label} от {username}" if username else label


def _clean_instagram_name(name: str, kind: str) -> str:
    """Заменяем 'Video by X' в имени файла на 'Видео/Фото от X'."""
    label = "Видео" if kind == "video" else "Фото"
    ext = os.path.splitext(name)[1]
    base = os.path.splitext(name)[0]
    for old in ("Video by", "Photo by", "Post by"):
        if base.startswith(old):
            base = f"{label} от {base[len(old):].lstrip()}"
            break
    return base + ext


CONCURRENT_FRAGMENTS = int(os.getenv("CONCURRENT_FRAGMENTS", "4"))
# Чанки применяются только к YouTube (см. _base_opts), остальные платформы качают без них.
HTTP_CHUNK_SIZE = int(os.getenv("HTTP_CHUNK_SIZE", "10485760"))

# YouTube-клиенты в порядке приоритета. С датацентровых IP стоковый `web`
# отдаёт "Sign in to confirm you're not a bot", а web_embedded/android/web_music
# его обходят. Форматы всех клиентов сливаются yt-dlp в один список.
YT_PLAYER_CLIENTS = [c.strip() for c in os.getenv(
    "YT_PLAYER_CLIENTS", "web_embedded,android,web_music"
).split(",") if c.strip()]

# Внешний JS-рантайм для решения подписей/n-challenge YouTube (EJS).
# Пустой список = не использовать. С Debian-пакета Node 20 ejs не работает,
# нужен node >= 22 или deno >= 2.3.
JS_RUNTIMES = [r.strip() for r in os.getenv(
    "JS_RUNTIMES", "node"
).split(",") if r.strip()]


def _base_opts(output_template: str, http_chunk_size: int | None = None, platform: str | None = None) -> dict:
    opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "overwrites": True,
        "socket_timeout": SOCKET_TIMEOUT_SEC,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
    }
    # Чанки (range-запросы) задаём только там, где они полезны: у YouTube большие
    # single-file форматы, где чанк переживает обрыв соединения. У VK/Яндекса они
    # ломают скачивание (Conflicting range), у Rutube/Instagram/TikTok бесполезны.
    if http_chunk_size:
        opts["http_chunk_size"] = http_chunk_size
    if YT_PLAYER_CLIENTS:
        opts["extractor_args"] = {"youtube": {"player_client": YT_PLAYER_CLIENTS}}
    if JS_RUNTIMES:
        opts["js_runtimes"] = {r: {} for r in JS_RUNTIMES}
    impersonate = os.getenv("IMPERSONATE", "chrome").strip()
    if impersonate:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        opts["impersonate"] = ImpersonateTarget.from_str(impersonate)
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    if platform == "vk" and VK_COOKIE_FILE and os.path.exists(VK_COOKIE_FILE):
        opts["cookiefile"] = VK_COOKIE_FILE
    return opts


def _tiktok_opts(output_template: str, http_chunk_size: int | None = None) -> dict:
    """Опции для TikTok: глобальная имперсонация (curl_cffi) триггерит WAF-капчу."""
    opts = _base_opts(output_template, http_chunk_size=http_chunk_size)
    opts.pop("impersonate", None)
    return opts


_INSTAGRAM_STORY_RE = re.compile(r"instagram\.com/stories/([^/]+)/(\d+)")
_INSTAGRAM_HIGHLIGHT_RE = re.compile(r"instagram\.com/stories/highlights/(\d+)")

def _is_instagram_story(url: str) -> bool:
    """Stories и highlights — плейлисты, нужен noplaylist=False."""
    if _INSTAGRAM_STORY_RE.search(url) or _INSTAGRAM_HIGHLIGHT_RE.search(url):
        return True
    return False


def _instagram_opts(output_template: str, *, is_story: bool = False) -> dict:
    """Опции для Instagram: фото-посты не должны ронять экстракцию.
    is_story=True — stories (плейлисты, нужен noplaylist=False)."""
    opts = _base_opts(output_template)
    opts["ignore_no_formats_error"] = True
    opts["noplaylist"] = not is_story  # Stories — плейлисты, noplaylist=False
    cookie_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cookies.txt")
    if os.path.isfile(cookie_path) and is_story:
        opts["cookiefile"] = cookie_path
    return opts


def _instagram_entry_format(entry: dict) -> str:
    """Видео-элемент качаем 'best', фото — по format_id (img-best)."""
    formats = entry.get("formats") or []
    if any(f.get("vcodec") and f["vcodec"] != "none" for f in formats):
        return "best"
    return "img-best/best"



def _first_downloaded_path(info: dict) -> str | None:
    """Путь первого скачанного файла из result_info."""
    dl = []
    for e in info.get("entries") or []:
        dl.extend((e or {}).get("requested_downloads") or [])
    if not dl:
        dl = info.get("requested_downloads") or []
    if dl and dl[0].get("filepath"):
        return dl[0]["filepath"]
    return None


def _timeout_hook_builder(task_key: tuple[str, int | None, str | None] | None = None):
    start_time = time.time()

    def _hook(d: dict) -> None:
        if d.get("status") == "downloading":
            if task_key is not None:
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                # Определяем, какой поток качается: видео / аудио / оба сразу
                # (прогрессивный формат). DASH на YouTube качает видео и аудио
                # отдельно — бот показывает каждый поток своим баром.
                info = d.get("info_dict") or {}
                vcodec = info.get("vcodec") or "none"
                acodec = info.get("acodec") or "none"
                if vcodec != "none" and acodec != "none":
                    stream = "combined"
                elif vcodec != "none":
                    stream = "video"
                else:
                    stream = "audio"
                _register_progress(
                    task_key,
                    downloaded=downloaded,
                    total=total,
                    percent=round(downloaded / total * 100) if total else None,
                    status="downloading",
                    stream=stream,
                    started_at=start_time,
                )
            if time.time() - start_time > DOWNLOAD_TIMEOUT_SEC:
                raise DownloadTimeoutError(
                    f"Превышен лимит времени скачивания ({DOWNLOAD_TIMEOUT_SEC} сек)"
                )
            downloaded = d.get("downloaded_bytes") or 0
            if downloaded > MAX_FILESIZE_BYTES:
                raise FileTooBigError(
                    f"Видео слишком большое ({downloaded / 1024 / 1024:.0f} МБ), "
                    f"лимит {MAX_FILESIZE_BYTES / 1024 / 1024:.0f} МБ — скачивание прервано"
                )

    return _hook


def _codec_key(vcodec: str | None) -> str:
    """Группа кодека для выбора формата: h264 / vp9 / hevc / av1 / other."""
    v = (vcodec or "").lower()
    if v.startswith("av01") or v.startswith("av1"):
        return "av1"
    if v.startswith("vp09") or v.startswith("vp9"):
        return "vp9"
    if v.startswith("h264") or v.startswith("avc"):
        return "h264"
    if v.startswith("hvc1") or v.startswith("hev1") or v.startswith("hevc") or v.startswith("h265"):
        return "hevc"
    return "other"


def _codec_rank(vcodec: str | None) -> int:
    """Приоритет кодеков.

    H.264/avc1 — на первом месте: только его Telegram стримит нативно
    (supports_streaming), остальные кодеки требуют полной загрузки файла.
    AV1/VP9 по размеру меньше, поэтому идут как фолбэк.
    """
    v = (vcodec or "").lower()
    if v.startswith("h264") or v.startswith("avc"):
        return 3
    if v.startswith("vp09") or v.startswith("vp9") or v.startswith("hevc") or v.startswith("h265") or v.startswith("hvc1") or v.startswith("hev1"):
        return 2
    if v.startswith("av01") or v.startswith("av1"):
        return 1
    return 0


def _list_formats_reddit(url: str) -> dict:
    """Получает информацию о Reddit-посте через Playwright."""
    url = _resolve_reddit_share(url)

    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_reddit_playwright, url)
            title, media = future.result(timeout=60)

        if not media:
            return {"error": "Не удалось извлечь медиа из Reddit-поста"}

        has_video = any(m["type"] == "video" for m in media)
        has_image = any(m["type"] == "image" for m in media)

        if has_video and has_image:
            kind = "mixed"
        elif has_video:
            kind = "video"
        else:
            kind = "image"

        return {
            "ok": True,
            "platform": "reddit",
            "title": title,
            "media_count": len(media),
            "media": [{"index": i, "type": m["type"]} for i, m in enumerate(media)],
            "kind": kind,
        }

    except Exception as e:
        return {"error": f"Reddit: {e}"}


def list_formats(url: str) -> dict:
    import yt_dlp

    platform = is_supported(url)
    if not platform:
        return {"error": "Ссылка не поддерживается (YouTube / TikTok / Instagram / VK / Rutube / Coub / Яндекс Видео / Dzen)"}

    if platform == "instagram":
        story = _is_instagram_story(url)
        try:
            with yt_dlp.YoutubeDL(_instagram_opts("", is_story=story)) as ydl:
                info = _extract_info_with_retry(ydl, url, download=False)
        except Exception as e:
            return {"error": f"Не удалось получить информацию: {e}"}

        entries = info.get("entries")
        # Stories: yt-dlp возвращает плейлист (n_entries/playlist_count), но entries
        # может быть генератором или отсутствовать. Обрабатываем оба случая.
        n_entries = info.get("n_entries") or info.get("playlist_count")
        if entries or n_entries:
            if entries and not isinstance(entries, list):
                entries = list(entries)
            media = [
                {"index": i, "kind": _media_kind(e)}
                for i, e in enumerate(entries or [])
                if e
            ] if entries else [{"index": i, "kind": "video"} for i in range(n_entries or 0)]
        else:
            media = [{"index": 0, "kind": _media_kind(info)}]

        username = info.get("channel") or info.get("uploader")
        title = _instagram_title(username, {m["kind"] for m in media})

        return {
            "ok": True,
            "platform": "instagram",
            "title": title,
            "duration_sec": info.get("duration"),
            "media_count": len(media),
            "media": media,
            "is_carousel": bool(entries) or bool(n_entries),
        }

    if platform == "tiktok":
        try:
            with yt_dlp.YoutubeDL(_tiktok_opts("")) as ydl:
                info = _extract_info_with_retry(ydl, url, download=False)
        except Exception as e:
            return {"error": f"Не удалось получить информацию: {e}"}

        return {
            "ok": True,
            "platform": "tiktok",
            "title": info.get("title"),
            "duration_sec": info.get("duration"),
            "formats": _group_formats(info, "tiktok"),
            "thumbnail": _save_thumbnail(info),
        }

    if platform == "reddit":
        return _list_formats_reddit(url)

    try:
        with yt_dlp.YoutubeDL(_base_opts("", platform=platform)) as ydl:
            info = _extract_info_with_retry(ydl, url, download=False)
    except Exception as e:
        return {"error": f"Не удалось получить информацию: {e}"}

    return {
        "ok": True,
        "platform": platform,
        "title": info.get("title"),
        "duration_sec": info.get("duration"),
        "formats": _group_formats(info, platform),
        "thumbnail": _save_thumbnail(info),
    }


def _group_formats(info: dict, platform: str) -> list[dict]:
    """Группирует форматы по высоте и коду (h264/vp9/hevc/av1): одна высота
    может давать несколько кодеков с разным размером."""
    # Coub: видео (h264, без height/vcodec в метаданных) и аудио (mp3) отдаются
    # отдельными файлами. Группируем вручную по качеству med/high. Финальный
    # MP4 собирает ffmpeg, поэтому оцениваем его итоговый размер для обоих
    # режимов: «как есть» (видео зациклено до конца музыки) и «без повторов»
    # (один проход цикла, аудио обрезано под длину видео).
    if platform == "coub":
        by_name = {}
        for f in info.get("formats", []):
            by_name[f["format_id"]] = f
        quality = {"med": 360, "high": 720}
        vid_dur = info.get("duration") or 0
        # Длительность аудио по размеру mp3: med стабильно ~128 кбит/с (CBR),
        # med и high — одна и та же песня, длительность одинаковая.
        a_med = by_name.get("html5-audio-med")
        audio_dur = None
        if a_med:
            am = a_med.get("filesize") or a_med.get("filesize_approx")
            if am:
                audio_dur = am * 8 / 128000
        formats = []
        for q, height in quality.items():
            v = by_name.get(f"html5-video-{q}")
            a = by_name.get(f"html5-audio-{q}")
            if not v:
                continue
            v_size = v.get("filesize") or v.get("filesize_approx")
            a_size = a.get("filesize") or a.get("filesize_approx") if a else None
            # «Без повторов»: один проход видео + аудио AAC 128k (~16 КБ/с),
            # обрезанное под длину видео-цикла.
            size_1x = None
            if v_size and vid_dur:
                size_1x = v_size + vid_dur * 16000
            # «Как есть»: видео зациклено до конца аудио (размер растёт
            # пропорционально длительности) + полное аудио (AAC 128k).
            size_loop = None
            if v_size and vid_dur and audio_dur:
                size_loop = v_size * (audio_dur / vid_dur) + audio_dur * 16000
            formats.append(
                {
                    "height": height,
                    "format_id": v["format_id"],
                    "filesize": size_loop,
                    "filesize_1x": size_1x,
                    "codec": "H.264",
                    "codec_key": "h264",
                }
            )
        return formats

    by_height: dict[int, dict[str, dict]] = {}
    audio_sizes = []
    for f in info.get("formats", []):
        # VK/Яндекс/Dzen: HLS (m3u8) виснет с серверных IP, предлагаем только DASH.
        if platform in ("vk", "yandex", "dzen") and "m3u8" in (f.get("protocol") or ""):
            continue
        height = f.get("height")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        if height and vcodec and vcodec != "none":
            ck = _codec_key(vcodec)
            size = f.get("filesize") or f.get("filesize_approx")
            tbr = f.get("tbr") or 0
            progressive = bool(acodec and acodec != "none")
            rank = _codec_rank(vcodec)
            cur = by_height.setdefault(height, {}).get(ck)
            if cur is None or (rank, tbr) > (cur.get("rank", 0), cur.get("tbr", 0)):
                by_height[height][ck] = {
                    "height": height,
                    "format_id": f["format_id"],
                    "size": size,
                    "tbr": tbr,
                    "rank": rank,
                    "vcodec": vcodec,
                    "progressive": progressive,
                }
        elif vcodec == "none" and acodec and acodec != "none":
            audio_sizes.append(f.get("filesize") or f.get("filesize_approx"))

    known_audio = [s for s in audio_sizes if s]
    best_audio_size = max(known_audio) if known_audio else None

    duration = info.get("duration")
    audio_fmt = _chosen_audio(info.get("formats", []), platform)
    audio_tbr = audio_fmt.get("tbr") if audio_fmt else None

    codec_names = {
        "av01": "AV1",
        "av1": "AV1",
        "vp9": "VP9",
        "h265": "H.265",
        "hevc": "H.265",
        "h264": "H.264",
        "avc1": "H.264",
        "avc": "H.264",
    }

    codec_order = {"h264": 0, "vp9": 1, "hevc": 2, "av1": 3, "other": 4}

    formats = []
    for h in sorted(by_height):
        for ck in sorted(by_height[h], key=lambda c: codec_order.get(c, 9)):
            fmt = by_height[h][ck]
            vid_size = fmt["size"]
            if fmt["progressive"]:
                total = vid_size or _estimate_size(fmt["tbr"], None, duration)
            elif vid_size is not None:
                total = vid_size + (best_audio_size or 0)
            else:
                total = _estimate_size(fmt["tbr"], audio_tbr, duration)
            vc = fmt["vcodec"] or ""
            codec = codec_names.get(vc.split(".")[0], vc.upper())
            formats.append(
                {
                    "height": h,
                    "format_id": fmt["format_id"],
                    "filesize": total,
                    "codec": codec,
                    "codec_key": ck,
                }
            )

    return formats


def download(
    url: str,
    height: int | None = None,
    format_id: str | None = None,
    codec: str | None = None,
    loop: bool = True,
) -> dict:
    import yt_dlp

    if not is_supported(url):
        return {"error": "Ссылка не поддерживается (YouTube / TikTok / Instagram / VK / Rutube / Coub / Яндекс Видео / Dzen)"}

    if not DOWNLOAD_SEM.acquire(blocking=False):
        return {
            "error": "Бот перегружен, пришлите Вашу ссылку позже",
            "busy": True,
        }

    try:
        result = _do_download(
            url, height, format_id, codec, task_key=(url, height, codec), loop=loop
        )
        if "error" not in result:
            stats.record_download(is_supported(url))
        return result
    finally:
        DOWNLOAD_SEM.release()


def _do_download_instagram(
    url: str, task_key: tuple[str, int | None, str | None] | None = None
) -> dict:
    import yt_dlp

    if task_key is not None:
        _register_progress(task_key, status="extracting", started_at=time.time())

    try:
        with tempfile.TemporaryDirectory() as tmp:
            story = _is_instagram_story(url)
            try:
                with yt_dlp.YoutubeDL(_instagram_opts("", is_story=story)) as ydl:
                    meta = _extract_info_with_retry(ydl, url, download=False)
            except Exception as e:
                return {"error": f"Не удалось получить информацию: {e}"}

            entries = meta.get("entries") or []
            # Stories: entries может быть генератором или пустым, но n_entries > 0
            n_entries = meta.get("n_entries") or meta.get("playlist_count")
            if entries:
                items = [e for e in entries if e]
            elif n_entries:
                # Stories — плейлист без entries в metadata, скачиваем по playlist_items
                items = [{"index": i} for i in range(1, n_entries + 1)]
            else:
                items = [meta]
            if not items:
                return {"error": "Файл не был создан"}

            tmp_dir = os.path.join(tmp, "dl")
            os.makedirs(tmp_dir, exist_ok=True)

            results = []
            for idx, entry in enumerate(items, start=1):
                fmt_sel = _instagram_entry_format(entry)
                opts = _instagram_opts(
                    os.path.join(tmp_dir, f"%(title).100B [%(id)s]_{idx}.%(ext)s"),
                    is_story=story,
                )
                opts["format"] = fmt_sel
                opts["playlist_items"] = str(idx)
                opts["progress_hooks"] = [_timeout_hook_builder(task_key)]
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = _extract_info_with_retry(ydl, url, download=True)
                except DownloadTimeoutError as e:
                    return {"error": str(e)}
                except FileTooBigError:
                    continue  # Слишком большой файл — пропускаем (stories/carousel)
                except Exception as e:
                    continue  # Ошибка скачивания — пропускаем

                src = _first_downloaded_path(info)
                if not src or not os.path.exists(src):
                    return {"error": f"Файл не был создан ({idx})"}

                target = os.path.join(DOWNLOAD_DIR, os.path.basename(src))
                shutil.move(src, target)
                ext = os.path.splitext(target)[1].lower()
                kind = "image" if ext in INSTAGRAM_IMAGE_EXTS else "video"
                clean = _clean_instagram_name(os.path.basename(target), kind)
                if clean != os.path.basename(target):
                    clean_target = os.path.join(DOWNLOAD_DIR, clean)
                    os.rename(target, clean_target)
                    target = clean_target
                results.append({"filename": os.path.basename(target), "kind": kind})

            total = sum(
                os.path.getsize(os.path.join(DOWNLOAD_DIR, r["filename"])) for r in results
            )

            if not results:
                return {"error": "Файл не был создан (все файлы пропущены — слишком большие)"}

            username = meta.get("channel") or meta.get("uploader")
            title = _instagram_title(username, {r["kind"] for r in results})

            return {
                "ok": True,
                "title": title,
                "files": results,
            }
    finally:
        if task_key is not None:
            _unregister_progress(task_key)



def _fmt_selector(platform: str, height: int | None, codec: str | None = None) -> tuple[str, str]:
    """Селектор формата и суффикс имени файла.

    codec — предпочтительный кодек (h264/vp9/hevc/av1). Если передан,
    сначала пробуем этот кодек с mp4a, затем этот кодек с любым аудио,
    затем любой кодек — как фолбэк.
    """
    # HLS (m3u8) у VK/Яндекса с серверных IP виснет на скачивании фрагментов,
    # поэтому исключаем его и берём single-file DASH-форматы. Аудио капаем
    # ~134 кбит/с (ближайшая ступенька VK), чтобы длинные ролики не раздувались
    # сверх лимита 50 МБ.
    no_hls = "[protocol!*=m3u8]" if platform in ("vk", "yandex", "dzen") else ""
    audio_cap = "[tbr<=136]" if platform in ("vk", "yandex", "dzen") else ""

    vcodec_filter = {
        "h264": "[vcodec^=avc1]",
        "av1": "[vcodec^=av01]",
        "vp9": "[vcodec^=vp09]",
        # hvc1/hev1 — оба префикса HEVC, ^= в одном условии даёт AND,
        # поэтому regex через ~=.
        "hevc": "[vcodec~=^(hvc1|hev1)]",
        "other": "",
    }.get(codec or "", "")

    # Coub: видео и аудио отдельными файлами (html5-video-med/high + html5-audio-*),
    # quality: 360p→med, 720p→high. Форматы без height/vcodec, поэтому селектор
    # по format_id; итог склеивается в mp4 (merge_output_format).
    if platform == "coub":
        q = "med" if height == 360 else "high"
        if height == 360 or height == 720:
            return (
                f"html5-video-{q}+html5-audio-{q}/best",
                f"_{height}p",
            )
        return "best", "_720p"

    # TikTok отдаёт комбинированные mp4 (видео+аудио в одном файле), отдельных
    # аудио-потоков нет — селектор по best с учётом высоты и кодека.
    if platform == "tiktok":
        vf = vcodec_filter or "[vcodec^=avc1]"
        if not height:
            return f"best{vf}/best", "_720p"
        return (
            f"best[height<={height}]{vf}/best[height<={height}]/best",
            f"_{height}p",
        )

    # Telegram стримит только H.264/AAC в MP4 (supports_streaming), поэтому
    # сначала пробуем предпочтительный кодек + mp4a, при недоступности — любой.
    if not height:
        if vcodec_filter:
            return (
                f"best{no_hls}{vcodec_filter}[acodec^=mp4a]"
                f"/best{no_hls}{vcodec_filter}/best{no_hls}/best",
                "_720p",
            )
        return (
            f"best{no_hls}[vcodec^=avc1][acodec^=mp4a]/best{no_hls}/best",
            "_720p",
        )
    codec_audio = f"bestvideo[height<={height}]{no_hls}{vcodec_filter}+bestaudio{no_hls}[acodec^=mp4a]"
    codec_no_audio = f"bestvideo[height<={height}]{no_hls}{vcodec_filter}+bestaudio{no_hls}{audio_cap}"
    h264_audio = f"bestvideo[height<={height}]{no_hls}[vcodec^=avc1]+bestaudio{no_hls}[acodec^=mp4a]"
    h264_no_audio = f"bestvideo[height<={height}]{no_hls}[vcodec^=avc1]+bestaudio{no_hls}{audio_cap}"
    any_codec = f"bestvideo[height<={height}]{no_hls}+bestaudio{no_hls}{audio_cap}"
    fallback = f"bestvideo[height<={height}]{no_hls}+bestaudio{no_hls}"
    if vcodec_filter:
        return (
            f"{codec_audio}/{codec_no_audio}/{h264_audio}/{h264_no_audio}"
            f"/{any_codec}/{fallback}"
            f"/best[height<={height}]{no_hls}/best",
            f"_{height}p",
        )
    return (
        f"{h264_audio}/{h264_no_audio}/{any_codec}/{fallback}"
        f"/best[height<={height}]{no_hls}/best",
        f"_{height}p",
    )


def _chosen_audio(formats: list[dict], platform: str) -> dict | None:
    """Аудио-формат, который выберет селектор скачивания (для оценки размера)."""
    audio = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") and f["acodec"] != "none"
    ]
    if platform in ("vk", "yandex", "dzen"):
        audio = [f for f in audio if not str(f.get("protocol") or "").startswith("m3u8")]
        capped = [f for f in audio if (f.get("tbr") or 0) <= 136]
        if capped:
            audio = capped
    return max(audio, key=lambda f: f.get("tbr") or 0) if audio else None


def _estimate_size(v_tbr: float | None, a_tbr: float | None, duration: float | None) -> int | None:
    """Оценка размера по битрейту, когда CDN не отдаёт filesize."""
    if not duration or (v_tbr or 0) <= 0:
        return None
    kbps = v_tbr + (a_tbr or 0)
    return int(kbps / 8 * duration * 1024)


def _do_download_coub(
    url: str,
    height: int | None = None,
    task_key: tuple[str, int | None, str | None] | None = None,
    loop: bool = True,
) -> dict:
    """Coub: видео и аудио отдаются отдельными файлами. Скачиваем оба и
    склеиваем через ffmpeg.

    loop=True (по умолчанию): видео повторяется (-stream_loop -1) до конца
    музыки (-shortest) — файл длиной с аудио, цикл повторён несколько раз.

    loop=False: один проход цикла. Аудио обрезается под точную длину видео
    (длина одного цикла), повторов нет. Аудио перекодируем в AAC — файл
    стримится в Telegram."""
    import subprocess
    import yt_dlp

    if task_key is not None:
        _register_progress(task_key, status="extracting", started_at=time.time())

    q = "med" if height == 360 else "high"

    try:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = os.path.join(tmp, "video.%(ext)s")
            audio_path = os.path.join(tmp, "audio.%(ext)s")

            vopts = _base_opts(video_path)
            vopts["format"] = f"html5-video-{q}"
            vopts["progress_hooks"] = [_timeout_hook_builder(task_key)]
            with yt_dlp.YoutubeDL(vopts) as ydl:
                vmeta = _extract_info_with_retry(ydl, url, download=True)

            aopts = _base_opts(audio_path)
            aopts["format"] = f"html5-audio-{q}"
            aopts["progress_hooks"] = [_timeout_hook_builder(task_key)]
            with yt_dlp.YoutubeDL(aopts) as ydl:
                _extract_info_with_retry(ydl, url, download=True)

            vid = _first_downloaded_path(vmeta)
            if not vid or not os.path.exists(vid):
                return {"error": "Файл не был создан"}
            audio = os.path.join(tmp, "audio.mp3")
            if not os.path.exists(audio):
                return {"error": "Аудио не было создано"}

            out = os.path.join(tmp, f"out_{q}.mp4")
            # Точная длина видео — длина одного цикла (нужна и для обрезки
            # аудио, и чтобы видео с -stream_loop -1 не вылезло за конец
            # музыки: -shortest режет по потоку, но повтор цикла завершается
            # на границе кадра и может удлинить файл).
            probe_v = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", vid,
                ],
                capture_output=True, text=True, check=True,
            )
            vid_dur = float(probe_v.stdout.strip())

            if loop:
                # Полная длина аудио — жёсткая обрезка через -t.
                probe_a = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", audio,
                    ],
                    capture_output=True, text=True, check=True,
                )
                audio_dur = float(probe_a.stdout.strip())
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-stream_loop", "-1", "-i", vid,
                    "-i", audio,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-t", f"{audio_dur:.3f}", "-movflags", "+faststart", out,
                ]
            else:
                # Один проход: аудио обрезаем под длину видео-цикла.
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", vid,
                    "-i", audio,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-t", f"{vid_dur:.3f}", "-movflags", "+faststart", out,
                ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)

            size = os.path.getsize(out)
            if size > MAX_FILESIZE_BYTES:
                return {
                    "error": (
                        f"Видео слишком большое ({size / 1024 / 1024:.0f} МБ), "
                        f"лимит {MAX_FILESIZE_BYTES / 1024 / 1024:.0f} МБ"
                    )
                }

            title = vmeta.get("title") or ""
            tag = "_1x" if not loop else ""
            target = os.path.join(DOWNLOAD_DIR, f"{_safe_name(title)} [coub]{tag}{'_360p' if q == 'med' else '_720p'}.mp4")
            shutil.move(out, target)

            return {
                "filename": os.path.basename(target),
                "path": target,
                "title": title,
                "duration_sec": vid_dur if not loop else vmeta.get("duration"),
                "duration_min": round((vid_dur if not loop else vmeta.get("duration") or 0) / 60, 1),
                "filesize": size,
            }
    except subprocess.CalledProcessError as e:
        return {"error": f"Не удалось склеить видео и аудио: {e.stderr[:200]}"}
    except DownloadTimeoutError as e:
        return {"error": str(e)}
    except FileTooBigError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Не удалось скачать: {e}"}
    finally:
        if task_key is not None:
            _unregister_progress(task_key)


def _safe_name(name: str) -> str:
    """Имя файла без символов, недопустимых в файловой системе."""
    keep = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return keep or "coub"


REDDIT_COOKIES_PATH = os.getenv("REDDIT_COOKIES_PATH", "/opt/yt-bot/reddit_cookies.txt")
REDDIT_BROWSER_TTL_SEC = 300  # 5 минут — авто-закрытие

_reddit_browser = None
_reddit_playwright = None
_reddit_browser_lock = threading.Lock()
_reddit_browser_ts: float = 0


def _load_reddit_cookies_sync():
    """Загружает cookies.txt (Netscape) в список для Playwright."""
    cookies = []
    if not os.path.isfile(REDDIT_COOKIES_PATH):
        return cookies
    with open(REDDIT_COOKIES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,
            })
    return cookies


_REDDIT_COOKIES = _load_reddit_cookies_sync()


def _resolve_reddit_share(url: str) -> str:
    """Превращает /s/ share-ссылку в /comments/ формат."""
    m = re.search(r"reddit\.com/r/(\w+)/s/(\w+)", url)
    if not m:
        return url
    sub, share_id = m.group(1), m.group(2)
    api_url = f"https://www.reddit.com/r/{sub}/s/{share_id}.json"
    try:
        resp = httpx.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                post_data = data[0]["data"]["children"][0]["data"]
                permalink = post_data.get("permalink", "")
                if permalink:
                    return f"https://www.reddit.com{permalink}"
    except Exception:
        pass
    return url


def _extract_reddit_media_sync(page) -> list[dict]:
    """Извлекает медиа-URL из Reddit-страницы (sync evaluate)."""
    media = []

    # Пробуем извлечь из Reddit JSON API (надёжнее для галерей)
    try:
        page_url = page.url
        # Используем fetch из браузера (с cookies)
        json_url = page_url.rstrip('/') + '.json'
        print(f"[reddit] Trying JSON API via browser: {json_url}")
        api_result = page.evaluate("""async (url) => {
            try {
                const resp = await fetch(url, {credentials: 'include'});
                if (!resp.ok) return {error: resp.status};
                const data = await resp.json();
                const results = [];
                if (Array.isArray(data) && data.length > 0) {
                    const post = data[0]?.data?.children?.[0]?.data;
                    if (post) {
                        // Галерея
                        const meta = post.media_metadata || {};
                        for (const [id, item] of Object.entries(meta)) {
                            if (item.status === 'valid') {
                                const s = item.s || {};
                                const u = s.u || s.gif || '';
                                if (u) results.push(u.replace(/&amp;/g, '&'));
                            }
                        }
                        // Одиночная картинка
                        if (results.length === 0) {
                            const imgs = post.preview?.images || [];
                            for (const img of imgs) {
                                const src = img.source?.url || '';
                                if (src) results.push(src.replace(/&amp;/g, '&'));
                            }
                        }
                    }
                }
                return {images: results};
            } catch(e) {
                return {error: e.message};
            }
        }""", json_url)
        print(f"[reddit] JSON API result: {api_result}")
        if api_result and not api_result.get("error"):
            for img_url in api_result.get("images", []):
                media.append({"url": img_url, "type": "image"})
            if media:
                print(f"[reddit] JSON API: found {len(media)} images")
    except Exception as e:
        print(f"[reddit] JSON API failed: {e}")

    # Fallback: DOM img extraction — собираем preview.redd.it текущего поста (только основной пост, не комментарии)
    if not media:
        try:
            post_prefix = page.evaluate("""() => {
                const post = document.querySelector('shreddit-post');
                if (post) {
                    const perm = post.getAttribute('permalink') || '';
                    return perm.split('/').filter(Boolean).pop() || '';
                }
                return '';
            }""")
            post_prefix_hyphen = post_prefix.replace("_", "-") if post_prefix else ""

            all_imgs = page.evaluate("""() => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('img').forEach(el => {
                    const src = el.getAttribute('src') || '';
                    if (!src.includes('preview.redd.it') || seen.has(src)) return;
                    // Only images inside the main post (not comments, not ads)
                    if (!el.closest('shreddit-post')) return;
                    seen.add(src);
                    results.push(src);
                });
                return results;
            }""")

            existing_urls = set()
            for img_url in all_imgs:
                base = img_url.split("?")[0]
                if base in existing_urls:
                    continue
                if post_prefix and (post_prefix in img_url or post_prefix_hyphen in img_url):
                    media.append({"url": img_url, "type": "image"})
                    existing_urls.add(base)
                elif not post_prefix:
                    media.append({"url": img_url, "type": "image"})
                    existing_urls.add(base)

            if media:
                print(f"[reddit] DOM extraction: {len(media)} images")
        except Exception as e:
            print(f"[reddit] DOM extraction error: {e}")

    if media:
        return media

    # Fallback: DOM extraction
    videos = page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('shreddit-video, video-player, video').forEach(el => {
            const src = el.getAttribute('src') || el.querySelector('source')?.getAttribute('src');
            if (src && src.includes('v.redd.it')) results.push({url: src, type: 'video'});
        });
        document.querySelectorAll('video source').forEach(el => {
            const src = el.getAttribute('src');
            if (src && src.includes('v.redd.it')) results.push({url: src, type: 'video'});
        });
        document.querySelectorAll('[data-hls-url], [data-dash-url]').forEach(el => {
            const hls = el.getAttribute('data-hls-url');
            const dash = el.getAttribute('data-dash-url');
            if (hls) results.push({url: hls, type: 'video'});
            if (dash) results.push({url: dash, type: 'video'});
        });
        return results;
    }""")
    media.extend(videos)

    images = page.evaluate("""() => {
        const results = [];
        // shreddit-post: content-href может быть ссылкой на внешний хост (redgifs, imgur, etc.)
        document.querySelectorAll('shreddit-post').forEach(el => {
            const href = el.getAttribute('content-href');
            if (href) {
                if (href.includes('i.redd.it')) {
                    results.push({url: href, type: 'image'});
                } else if (href.includes('v.redd.it') || href.includes('.mp4')) {
                    results.push({url: href, type: 'video'});
                } else if (href.includes('reddit.com/gallery/')) {
                    // Галерею пропускаем — картинки извлекаем из carousel ниже
                } else {
                    // Внешний хост (redgifs, imgur, etc.) — помечаем как video (большинство NSFW = видео)
                    results.push({url: href, type: 'video', external: true});
                }
            }
            // Также проверяем preview-href
            const preview = el.getAttribute('preview-href');
            if (preview && preview.includes('i.redd.it')) {
                results.push({url: preview, type: 'image'});
            }
        });
        // Галерея Reddit — извлекаем ВСЕ картинки из carousel (только внутри shreddit-post)
        document.querySelectorAll('shreddit-post gallery-carousel img').forEach(el => {
            let src = el.getAttribute('src');
            if (src) {
                results.push({url: src, type: 'image'});
            }
        });
        // data-testid="gallery-container" (только внутри shreddit-post)
        document.querySelectorAll('shreddit-post [data-testid="gallery-container"] img').forEach(el => {
            let src = el.getAttribute('src');
            if (src) {
                results.push({url: src, type: 'image'});
            }
        });
        // figure/media контейнер с оригинальным изображением (только внутри shreddit-post)
        document.querySelectorAll('shreddit-post figure img, shreddit-post [data-testid="post-container"] img').forEach(el => {
            let src = el.getAttribute('src');
            if (src && (src.includes('i.redd.it') || src.includes('preview.redd.it')))
                results.push({url: src, type: 'image'});
        });
        // Все i.redd.it картинки внутри shreddit-post (исключая preview и thumb)
        document.querySelectorAll('shreddit-post img').forEach(el => {
            let src = el.getAttribute('src');
            if (src && src.includes('i.redd.it') && !src.includes('preview') && !src.includes('thumb'))
                results.push({url: src, type: 'image'});
        });
        return results;
    }""")
    media.extend(images)

    seen = set()
    unique = []
    for m in media:
        if m["url"] not in seen:
            seen.add(m["url"])
            unique.append(m)
    # Если есть внешний контент (redgifs, imgur) — убираем reddit preview
    has_external = any(m.get("external") for m in unique)
    if has_external:
        unique = [m for m in unique if m.get("external") or "i.redd.it" not in m["url"]]
    # Если есть видео — убираем статичные картинки (заглушки)
    has_video = any(m["type"] == "video" for m in unique)
    if has_video:
        unique = [m for m in unique if m["type"] == "video"]
    # Убираемreddit placeholder icons (gtqlqg7tb6nh1.png и подобные)
    unique = [m for m in unique if "gtqlqg7tb6nh1" not in m["url"]]
    return unique


class _RedditBrowserPool:
    """Shared Playwright browser for Reddit (single-threaded, TTL 5 min).

    All Playwright operations run in a dedicated worker thread to avoid
    cross-thread issues. Requests are queued and processed sequentially.
    """

    def __init__(self, ttl_sec: int = 300):
        self._ttl = ttl_sec
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def _run_worker(self):
        """Dedicated thread: keeps browser alive, processes requests."""
        from playwright.sync_api import sync_playwright
        import queue

        pw = None
        browser = None
        last_used = 0.0

        while True:
            try:
                item = self._queue.get(timeout=self._ttl)
            except queue.Empty:
                # TTL expired — close browser
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = None
                if pw:
                    try:
                        pw.stop()
                    except Exception:
                        pass
                    pw = None
                print("[reddit] Browser closed (idle TTL)")
                continue

            # Parse item: extract or download
            if item[0] == "__download__":
                _, image_url, out_path, result_event, result_box = item
                task_type = "download"
            else:
                url, result_event, result_box = item
                task_type = "extract"

            try:
                # Start browser if needed
                if browser is None or not browser.is_connected():
                    if browser:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    if pw:
                        try:
                            pw.stop()
                        except Exception:
                            pass
                    pw = sync_playwright().start()
                    browser = pw.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-blink-features=AutomationControlled",
                        ],
                    )
                    print("[reddit] Browser started")

                last_used = time.time()

                # Create context with cookies
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                )
                if _REDDIT_COOKIES:
                    context.add_cookies(_REDDIT_COOKIES)

                try:
                    if task_type == "download":
                        # Download image through browser page
                        page = context.new_page()
                        try:
                            resp = page.goto(image_url, wait_until="commit", timeout=15000)
                            if resp and resp.ok:
                                body = resp.body()
                                with open(out_path, "wb") as f:
                                    f.write(body)
                                result_box[0] = True
                                print(f"[reddit] Image downloaded: {len(body)} bytes")
                            else:
                                status = resp.status if resp else "no response"
                                print(f"[reddit] Image download failed: {status}")
                                result_box[0] = False
                        except Exception as e:
                            print(f"[reddit] Image download error: {e}")
                            result_box[0] = False
                        finally:
                            page.close()
                    else:
                        # Extract media from page, then download images via ctx.request.get()
                        page = context.new_page()
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(3000)

                            try:
                                age_btn = page.locator('button:has-text("Yes"), button:has-text("yes"), [data-testid="nsfw-overlay"] button')
                                if age_btn.count() > 0:
                                    age_btn.first.click()
                                    page.wait_for_timeout(2000)
                            except Exception:
                                pass

                            page.wait_for_timeout(5000)

                            # Scroll to trigger lazy loading
                            for _ in range(3):
                                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                                page.wait_for_timeout(1500)

                            # Click gallery next buttons to trigger lazy loading of all images
                            try:
                                for _ in range(15):
                                    next_btn = page.locator('[aria-label="Next"], [data-testid="gallery-carousel-next"], button[aria-label*="next" i]')
                                    if next_btn.count() > 0:
                                        next_btn.first.click(timeout=1000)
                                        page.wait_for_timeout(500)
                                    else:
                                        break
                                page.evaluate('window.scrollTo(0, 0)')
                                page.wait_for_timeout(1000)
                            except Exception:
                                pass

                            title = page.title() or "Reddit"
                            media = _extract_reddit_media_sync(page)

                            # Download each image via browser context (bypasses Cloudflare)
                            PLACEHOLDER_HASHES = {"gtqlqg7tb6nh1", "qih5sp46ikoh1"}
                            downloaded_media = []
                            for item in media:
                                if item["type"] != "image":
                                    downloaded_media.append(item)
                                    continue
                                img_url = item["url"]
                                # Skip placeholders
                                if any(h in img_url for h in PLACEHOLDER_HASHES):
                                    continue
                                try:
                                    resp = context.request.get(img_url)
                                    if resp.status == 200:
                                        ct = resp.headers.get("content-type", "")
                                        if "image" in ct:
                                            body = resp.body()
                                            item["_bytes"] = body
                                            print(f"[reddit] Downloaded: {len(body)} bytes - {img_url[:100]}")
                                        else:
                                            print(f"[reddit] Skipped (ct={ct}): {img_url[:80]}")
                                    else:
                                        print(f"[reddit] Failed ({resp.status}): {img_url[:80]}")
                                except Exception as e:
                                    print(f"[reddit] Download error: {e}")
                                downloaded_media.append(item)

                            print(f"[reddit] {url} -> {len(downloaded_media)} items")
                            stats.record_download("reddit")
                            result_box[0] = (title, downloaded_media)
                        finally:
                            page.close()
                finally:
                    context.close()

            except Exception as e:
                result_box[0] = Exception(f"Reddit: {e}")
            finally:
                result_event.set()

    def extract(self, url: str, timeout: float = 90) -> tuple[str, list[dict]]:
        """Submit URL and wait for result (blocks caller thread)."""
        result_event = threading.Event()
        result_box = [None]
        self._queue.put((url, result_event, result_box))
        result_event.wait(timeout=timeout)
        if result_box[0] is None:
            raise TimeoutError("Reddit Playwright timed out")
        if isinstance(result_box[0], Exception):
            raise result_box[0]
        return result_box[0]

    def download_image(self, image_url: str, out_path: str, timeout: float = 30) -> bool:
        """Download image through browser (bypasses 403)."""
        result_event = threading.Event()
        result_box = [None]
        self._queue.put(("__download__", image_url, out_path, result_event, result_box))
        result_event.wait(timeout=timeout)
        return result_box[0] if result_box[0] is not None else False


_reddit_pool = _RedditBrowserPool(ttl_sec=REDDIT_BROWSER_TTL_SEC)


def _run_reddit_playwright(url: str) -> tuple[str, list[dict], object]:
    """Extract media from Reddit using shared browser pool (thread-safe).
    Returns (title, media_list, page_object) for in-browser downloads.
    """
    return _reddit_pool.extract(url)


def _do_download_reddit(url: str, height: int | None = None,
                        task_key: tuple[str, int | None, str | None] = None) -> dict:
    """Скачивание Reddit-постов через Playwright (обход 403) + yt-dlp."""
    import yt_dlp

    url = _resolve_reddit_share(url)

    if task_key:
        _register_progress(task_key, status="extracting", started_at=time.time())

    try:
        # Запускаем Playwright в отдельном потоке чтобы не блокировать asyncio
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_reddit_playwright, url)
            title, media = future.result(timeout=60)

        if not media:
            return {"error": "Не удалось извлечь медиа из Reddit-поста"}

        if task_key:
            _register_progress(task_key, status="downloading", started_at=time.time())

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = os.path.join(tmp, "dl")
            os.makedirs(tmp_dir, exist_ok=True)

            downloaded = []
            for i, item in enumerate(media):
                media_url = item["url"]
                ext = ".mp4" if item["type"] == "video" else ".jpg"
                out_path = os.path.join(tmp_dir, f"reddit_{i}{ext}")

                try:
                    if item["type"] == "video":
                        opts = _base_opts(out_path)
                        opts["format"] = "bestvideo+bestaudio/best"
                        opts["merge_output_format"] = "mp4"
                        if "v.redd.it" in media_url and os.path.isfile(REDDIT_COOKIES_PATH):
                            opts["cookiefile"] = REDDIT_COOKIES_PATH
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            ydl.download([media_url])
                        for f in os.listdir(tmp_dir):
                            if f.startswith(f"reddit_{i}") and f.endswith((".mp4", ".webm")):
                                actual = os.path.join(tmp_dir, f)
                                if actual != out_path:
                                    os.rename(actual, out_path)
                                    break
                    else:
                        # Используем перехваченные байты из браузера
                        captured = item.get("_bytes")
                        if captured:
                            with open(out_path, "wb") as f:
                                f.write(captured)
                            print(f"[reddit] Image from browser cache: {len(captured)} bytes")
                        else:
                            # Fallback: скачиваем через pool
                            print(f"[reddit] Downloading image via browser: {media_url}")
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                                ok = ex.submit(_reddit_pool.download_image, media_url, out_path).result(timeout=30)
                            if not ok:
                                print(f"[reddit] Browser download failed for {media_url}")
                                continue

                    if os.path.isfile(out_path):
                        downloaded.append(out_path)
                except Exception as e:
                    print(f"[reddit] Failed to download {media_url}: {e}")

            if not downloaded:
                return {"error": "Не удалось скачать медиа из Reddit"}

            results = []
            for path in downloaded:
                sz = os.path.getsize(path)
                kind = "video" if path.endswith(".mp4") else "image"
                dst = os.path.join(DOWNLOAD_DIR, os.path.basename(path))
                shutil.copy2(path, dst)
                results.append({"path": dst, "size": sz, "kind": kind})

            return {
                "ok": True,
                "title": title,
                "files": results,
            }

    except Exception as e:
        return {"error": f"Reddit: {e}"}
    finally:
        if task_key:
            _unregister_progress(task_key)


def _do_download(url: str, height: int | None = None, format_id: str | None = None,
                 codec: str | None = None,
                 task_key: tuple[str, int | None, str | None] | None = None,
                 loop: bool = True) -> dict:
    import yt_dlp

    platform = is_supported(url)

    if platform == "instagram":
        return _do_download_instagram(url, task_key)

    if platform == "coub":
        return _do_download_coub(url, height, task_key, loop=loop)

    if platform == "reddit":
        return _do_download_reddit(url, height, task_key)

    fmt_sel, suffix = _fmt_selector(platform, height, codec)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = os.path.join(tmp, "dl")
        os.makedirs(tmp_dir, exist_ok=True)

        opts_fn = _tiktok_opts if platform == "tiktok" else _base_opts
        opts = opts_fn(
            os.path.join(tmp_dir, f"%(title).100B [%(id)s]{suffix}.%(ext)s"),
            http_chunk_size=HTTP_CHUNK_SIZE if platform == "youtube" else None,
            platform=platform,
        )
        opts["format"] = fmt_sel
        opts["merge_output_format"] = "mp4"
        opts["progress_hooks"] = [_timeout_hook_builder(task_key)]
        if task_key is not None:
            _register_progress(task_key, status="extracting", started_at=time.time())

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = _extract_info_with_retry(ydl, url, download=True)
        except DownloadTimeoutError as e:
            return {"error": str(e)}
        except FileTooBigError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Не удалось скачать: {e}"}
        finally:
            if task_key is not None:
                _unregister_progress(task_key)

        src = None
        dl = (meta or {}).get("requested_downloads") or []
        if dl:
            src = dl[0].get("filepath")
        if not src or not os.path.exists(src):
            return {"error": "Файл не был создан"}

        size = os.path.getsize(src)
        if size > MAX_FILESIZE_BYTES:
            return {
                "error": (
                    f"Видео слишком большое ({size / 1024 / 1024:.0f} МБ), "
                    f"лимит {MAX_FILESIZE_BYTES / 1024 / 1024:.0f} МБ"
                )
            }

        target = os.path.join(DOWNLOAD_DIR, os.path.basename(src))
        shutil.move(src, target)

        return {
            "filename": os.path.basename(target),
            "path": target,
            "title": meta.get("title") if meta else None,
            "duration_sec": meta.get("duration") if meta else None,
            "duration_min": round((meta.get("duration") or 0) / 60, 1),
            "filesize": size,
        }


def cleanup_loop() -> None:
    while True:
        now = time.time()
        for name in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(p) and now - os.path.getmtime(p) > FILE_MAX_AGE_SEC:
                    os.remove(p)
            except OSError:
                pass
        time.sleep(CLEANUP_INTERVAL_SEC)


threading.Thread(target=cleanup_loop, daemon=True).start()
