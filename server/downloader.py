import os
import re
import shutil
import tempfile
import threading
import time

from dotenv import load_dotenv

load_dotenv()

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_MB", "50")) * 1024 * 1024

COOKIE_FILE = os.getenv("COOKIE_FILE", "")

CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_MIN", "15")) * 60
FILE_MAX_AGE_SEC = int(os.getenv("FILE_MAX_AGE_MIN", "15")) * 60

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)")
TIKTOK_RE = re.compile(r"tiktok\.com")
INSTAGRAM_RE = re.compile(r"instagram\.com")

INSTAGRAM_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "4"))
DOWNLOAD_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

SOCKET_TIMEOUT_SEC = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "30"))
DOWNLOAD_TIMEOUT_SEC = int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "300"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


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
    """Возвращает платформу ('youtube'/'tiktok'/'instagram') или None."""
    if YOUTUBE_RE.search(url or ""):
        return "youtube"
    if TIKTOK_RE.search(url or ""):
        return "tiktok"
    if INSTAGRAM_RE.search(url or ""):
        return "instagram"
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


def _base_opts(output_template: str) -> dict:
    opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "overwrites": True,
        "socket_timeout": SOCKET_TIMEOUT_SEC,
        "retries": 2,
        "fragment_retries": 2,
    }
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts


def _instagram_opts(output_template: str) -> dict:
    """Опции для Instagram: фото-посты не должны ронять экстракцию."""
    opts = _base_opts(output_template)
    opts["ignore_no_formats_error"] = True
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


def _timeout_hook_builder():
    start_time = time.time()

    def _hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
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


def _codec_rank(vcodec: str | None) -> int:
    """Приоритет кодеков, совпадающий с yt-dlp при выборе bestvideo."""
    v = (vcodec or "").lower()
    if v.startswith("av01") or v.startswith("av1"):
        return 3
    if v.startswith("vp9") or v.startswith("hevc") or v.startswith("h265"):
        return 2
    if v.startswith("h264") or v.startswith("avc"):
        return 1
    return 0


def list_formats(url: str) -> dict:
    import yt_dlp

    platform = is_supported(url)
    if not platform:
        return {"error": "Ссылка не поддерживается (YouTube / TikTok / Instagram)"}

    if platform == "instagram":
        try:
            with yt_dlp.YoutubeDL(_instagram_opts("")) as ydl:
                info = _extract_info_with_retry(ydl, url, download=False)
        except Exception as e:
            return {"error": f"Не удалось получить информацию: {e}"}

        entries = info.get("entries")
        if entries:
            media = [
                {"index": i, "kind": _media_kind(e)}
                for i, e in enumerate(entries)
                if e
            ]
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
            "is_carousel": bool(entries),
        }

    if platform == "tiktok":
        try:
            with yt_dlp.YoutubeDL(_base_opts("")) as ydl:
                info = _extract_info_with_retry(ydl, url, download=False)
        except Exception as e:
            return {"error": f"Не удалось получить информацию: {e}"}

        return {
            "ok": True,
            "platform": "tiktok",
            "title": info.get("title"),
            "duration_sec": info.get("duration"),
            "formats": [{"height": 0, "format_id": "best", "filesize": None}],
        }

    try:
        with yt_dlp.YoutubeDL(_base_opts("")) as ydl:
            info = _extract_info_with_retry(ydl, url, download=False)
    except Exception as e:
        return {"error": f"Не удалось получить информацию: {e}"}

    by_height = {}
    audio_sizes = []
    for f in info.get("formats", []):
        height = f.get("height")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        if height and vcodec != "none":
            size = f.get("filesize") or f.get("filesize_approx")
            tbr = f.get("tbr") or 0
            progressive = bool(acodec and acodec != "none")
            rank = _codec_rank(vcodec)
            cur = by_height.get(height)
            if cur is None or (rank, tbr) > (cur.get("rank", 0), cur.get("tbr", 0)):
                by_height[height] = {
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

    formats = []
    for h in sorted(by_height):
        fmt = by_height[h]
        vid_size = fmt["size"]
        if fmt["progressive"] or vid_size is None:
            total = vid_size
        else:
            total = (vid_size + best_audio_size) if best_audio_size is not None else None
        codec = codec_names.get(fmt["vcodec"].split(".")[0], fmt["vcodec"].upper())
        formats.append(
            {
                "height": h,
                "format_id": fmt["format_id"],
                "filesize": total,
                "codec": codec,
            }
        )

    return {
        "ok": True,
        "platform": "youtube",
        "title": info.get("title"),
        "duration_sec": info.get("duration"),
        "formats": formats,
    }


def download(url: str, height: int | None = None, format_id: str | None = None) -> dict:
    import yt_dlp

    if not is_supported(url):
        return {"error": "Ссылка не поддерживается (YouTube / TikTok / Instagram)"}

    if not DOWNLOAD_SEM.acquire(blocking=False):
        return {
            "error": "Бот перегружен, пришлите Вашу ссылку позже",
            "busy": True,
        }

    try:
        return _do_download(url, height, format_id)
    finally:
        DOWNLOAD_SEM.release()


def _do_download_instagram(url: str) -> dict:
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:
        try:
            with yt_dlp.YoutubeDL(_instagram_opts("")) as ydl:
                meta = _extract_info_with_retry(ydl, url, download=False)
        except Exception as e:
            return {"error": f"Не удалось получить информацию: {e}"}

        entries = meta.get("entries") or []
        items = [e for e in entries if e] if entries else [meta]
        if not items:
            return {"error": "Файл не был создан"}

        tmp_dir = os.path.join(tmp, "dl")
        os.makedirs(tmp_dir, exist_ok=True)

        results = []
        for idx, entry in enumerate(items, start=1):
            fmt_sel = _instagram_entry_format(entry)
            opts = _instagram_opts(
                os.path.join(tmp_dir, f"%(title).100B [%(id)s]_{idx}.%(ext)s")
            )
            opts["format"] = fmt_sel
            opts["playlist_items"] = str(idx)
            opts["progress_hooks"] = [_timeout_hook_builder()]
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = _extract_info_with_retry(ydl, url, download=True)
            except DownloadTimeoutError as e:
                return {"error": str(e)}
            except FileTooBigError as e:
                return {"error": str(e)}
            except Exception as e:
                return {"error": f"Не удалось скачать: {e}"}

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
        if total > MAX_FILESIZE_BYTES:
            return {
                "error": (
                    f"Пост слишком большой ({total / 1024 / 1024:.0f} МБ), "
                    f"лимит {MAX_FILESIZE_BYTES / 1024 / 1024:.0f} МБ"
                )
            }

        username = meta.get("channel") or meta.get("uploader")
        title = _instagram_title(username, {r["kind"] for r in results})

        return {
            "ok": True,
            "title": title,
            "files": results,
        }


def _do_download(url: str, height: int | None = None, format_id: str | None = None) -> dict:
    import yt_dlp

    platform = is_supported(url)

    if platform == "instagram":
        return _do_download_instagram(url)

    if platform == "tiktok" or not height:
        if platform == "tiktok":
            fmt_sel = "best"
            suffix = ""
        else:
            fmt_sel = "best"
            suffix = "_720p"
    else:
        fmt_sel = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        suffix = f"_{height}p"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = os.path.join(tmp, "dl")
        os.makedirs(tmp_dir, exist_ok=True)

        opts = _base_opts(
            os.path.join(tmp_dir, f"%(title).100B [%(id)s]{suffix}.%(ext)s")
        )
        opts["format"] = fmt_sel
        opts["merge_output_format"] = "mp4"
        opts["progress_hooks"] = [_timeout_hook_builder()]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = _extract_info_with_retry(ydl, url, download=True)
        except DownloadTimeoutError as e:
            return {"error": str(e)}
        except FileTooBigError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Не удалось скачать: {e}"}

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