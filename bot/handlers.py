import httpx
from aiogram import F, Router, types
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from . import config
from .utils import extract_video

router = Router()

MAX_FILESIZE = 50 * 1024 * 1024  # лимит Telegram
SLOW_SIZE = 30 * 1024 * 1024  # выше этого — помечаем «долго»
HARD_CAP = 45 * 1024 * 1024  # выше этого качество не предлагаем

# key (video_id / hash) -> canonical url (в callback_data не влезает полный URL)
URLS: dict[str, str] = {}


def _mb(size):
    return round(size / 1024 / 1024)


def _format_label(fmt: dict) -> str:
    height = fmt["height"]
    size = fmt.get("filesize")
    if height == 0:
        return "⬇️ Скачать видео"
    if size is None:
        return f"{height}p · ~размер"
    label = f"{height}p · {_mb(size)} МБ"
    if size > SLOW_SIZE:
        label += " (долго)"
    return label


def _build_keyboard(formats: list[dict], key: str) -> InlineKeyboardMarkup:
    rows = []
    for fmt in formats:
        if fmt.get("filesize") is None or fmt["filesize"] <= HARD_CAP:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_format_label(fmt),
                        callback_data=f"fmt:{key}:{fmt['height']}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _allowed(formats: list[dict]) -> list[dict]:
    return [
        f for f in formats
        if f.get("filesize") is None or f["filesize"] <= HARD_CAP
    ]


@router.message(F.text == "/start")
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Пришли ссылку на YouTube (или TikTok), и я скачаю его сюда "
        "(до 1080p, лимит 50 МБ)."
    )


@router.message(F.text)
async def handle_text(message: types.Message):
    parsed = extract_video(message.text)
    if not parsed:
        return
    platform, url, key = parsed

    status = await message.answer("⏳ Проверяю доступные форматы…")
    timeout = httpx.Timeout(120.0, connect=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{config.SERVER_URL}/formats", json={"url": url})
            body = resp.json()
    except httpx.HTTPError as e:
        await status.edit_text(f"❌ Ошибка связи с сервером: {e}")
        return

    if not body.get("ok"):
        await status.edit_text(f"❌ {body.get('error', 'Неизвестная ошибка')}")
        return

    formats = body["formats"]
    available = _allowed(formats)
    if not available:
        await status.edit_text(
            "❌ Видео невозможно скачать — слишком большое (лимит Telegram 50 МБ)."
        )
        return

    URLS[key] = url
    title = body.get("title", "Видео")

    if len(available) == 1 and available[0]["height"] == 0:
        await _download_and_send(status, key, 0)
        return

    kb = _build_keyboard(available, key)
    await status.edit_text(f"🎬 {title}\n\nВыбери качество:", reply_markup=kb)


@router.callback_query(F.data.startswith("fmt:"))
async def handle_format(callback: types.CallbackQuery):
    await callback.answer()
    _, key, height = callback.data.split(":")
    url = URLS.get(key)
    if not url:
        await callback.message.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return

    await _download_and_send(callback.message, key, int(height))


async def _download_and_send(msg: types.Message, key: str, height: int) -> None:
    url = URLS.get(key)
    if not url:
        await msg.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return

    height_label = "видео" if height == 0 else f"{height}p"
    await msg.edit_text(f"⏳ Скачиваю {height_label}…")
    timeout = httpx.Timeout(300.0, connect=10.0)

    payload = {"url": url}
    if height != 0:
        payload["height"] = height

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{config.SERVER_URL}/download", json=payload
            )
            body = resp.json()
    except httpx.HTTPError as e:
        await msg.edit_text(f"❌ Ошибка связи с сервером: {e}")
        return

    if resp.status_code == 503:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Попробовать ещё раз",
                        callback_data=f"fmt:{key}:{height}",
                    )
                ]
            ]
        )
        await msg.edit_text(
            "⚠️ Бот перегружен, пришлите Вашу ссылку позже.",
            reply_markup=kb,
        )
        return

    if not body.get("ok"):
        error = body.get("error", "Неизвестная ошибка")
        if "слишком большое" in error and height != 0:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↩️ Выбрать меньшее качество",
                            callback_data=f"rechoose:{key}",
                        )
                    ]
                ]
            )
            await msg.edit_text(
                "❌ Видео не влезло в 50 МБ.\nПопробуй выбрать меньший формат:",
                reply_markup=kb,
            )
        else:
            await msg.edit_text(f"❌ {error}")
        return

    filename = body["filename"]
    title = body.get("title")
    dur = body.get("duration_min")

    await msg.edit_text("⬆️ Файл готов, отправляю…")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{config.SERVER_URL}/file/{filename}")
            resp.raise_for_status()
            video = resp.content
    except httpx.HTTPError as e:
        await msg.edit_text(f"❌ Ошибка загрузки файла: {e}")
        return

    caption = f"🎬 {title}" if title else "🎬 Видео"
    if dur:
        caption += f"\n⏱ Длительность: ~{dur} мин"

    try:
        await msg.answer_video(BufferedInputFile(video, filename=filename), caption=caption)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось отправить: {e}")


@router.callback_query(F.data.startswith("rechoose:"))
async def rechoose(callback: types.CallbackQuery):
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    url = URLS.get(key)
    if not url:
        await callback.message.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return

    msg = callback.message
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = await client.post(f"{config.SERVER_URL}/formats", json={"url": url})
            body = resp.json()
    except httpx.HTTPError as e:
        await msg.edit_text(f"❌ Ошибка связи с сервером: {e}")
        return

    if not body.get("ok"):
        await msg.edit_text(f"❌ {body.get('error', 'Неизвестная ошибка')}")
        return

    available = _allowed(body["formats"])
    if not available:
        await msg.edit_text(
            "❌ Видео невозможно скачать — слишком большое (лимит Telegram 50 МБ)."
        )
        return

    kb = _build_keyboard(available, key)
    if len(available) == 1 and available[0]["height"] == 0:
        await _download_and_send(msg, key, 0)
        return
    await msg.edit_text(f"🎬 {body.get('title', 'Видео')}\n\nВыбери качество:", reply_markup=kb)