from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import downloader

app = FastAPI(title="YT Download Server")


class DownloadRequest(BaseModel):
    url: str
    height: int | None = None
    format_id: str | None = None


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
    }


@app.post("/download")
def download(req: DownloadRequest):
    result = downloader.download(req.url, req.height, req.format_id)
    if result.get("busy"):
        return JSONResponse(status_code=503, content={"ok": False, "error": result["error"]})
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    return {
        "ok": True,
        "title": result.get("title"),
        "duration_min": result.get("duration_min"),
        "filename": result.get("filename"),
    }


@app.get("/file/{filename}")
def get_file(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return {"ok": False, "error": "Некорректное имя файла"}
    path = Path(downloader.DOWNLOAD_DIR) / filename
    if not path.exists():
        return {"ok": False, "error": "Файл не найден"}
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    return {
        "ok": True,
        "concurrency": downloader.MAX_CONCURRENT_DOWNLOADS,
        "active_downloads": downloader.DOWNLOAD_SEM._value is not None
        and downloader.MAX_CONCURRENT_DOWNLOADS - downloader.DOWNLOAD_SEM._value,
    }