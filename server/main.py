from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import downloader, stats

app = FastAPI(title="YT Download Server")

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


class DownloadRequest(BaseModel):
    url: str
    height: int | None = None


class FormatsRequest(BaseModel):
    url: str


@app.post("/formats")
def formats(req: FormatsRequest):
    result = downloader.list_formats(req.url)
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    return {
        "ok": True,
        "platform": result.get("platform"),
        "title": result.get("title"),
        "duration_sec": result.get("duration_sec"),
        "formats": result.get("formats", []),
        "thumbnail": result.get("thumbnail"),
    }


@app.post("/download")
def download(req: DownloadRequest):
    result = downloader.download(req.url, req.height)
    if result.get("busy"):
        return JSONResponse(status_code=503, content={"ok": False, "error": result["error"]})
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    return {
        "ok": True,
        "title": result.get("title"),
        "duration_min": result.get("duration_min"),
        "filename": result.get("filename"),
        "files": result.get("files"),
    }


@app.get("/file/{filename}")
def get_file(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Некорректное имя файла"})
    path = Path(downloader.DOWNLOAD_DIR) / filename
    if not path.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": "Файл не найден"})
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/stats")
def get_stats():
    return {"ok": True, **stats.period_stats()}


@app.get("/status")
def status():
    return {
        "ok": True,
        "concurrency": downloader.MAX_CONCURRENT_DOWNLOADS,
        "active_downloads": downloader.DOWNLOAD_SEM._value is not None
        and downloader.MAX_CONCURRENT_DOWNLOADS - downloader.DOWNLOAD_SEM._value,
    }
