import httpx
from aiogram import F, Router, types
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from urllib.parse import quote

from . import config
from .utils import extract_video

router = Router()

MAX_FILESIZE = 50 * 1024 * 1024  # лимит Telegram
SLOW_SIZE = 30 * 1024 * 1024  # выше этого — помечаем «долго»
HARD_CAP = 45 * 1024 * 1024  # выше этого качество не предлагаем

# key (video_id / hash) -> canonical url (в callback_data не влезает полный URL)
URLS: dict[str, str] = {}


def _size_label(size):
    if size >= 1024 * 1024:
        return f"{round(size / 1024 / 1024)} МБ"
    if size >= 1024:
        return f"{round(size / 1024)} КБ"
    return f"{size} Б"


def _format_label(fmt: dict) -> str:
    height = fmt["height"]
    size = fmt.get("filesize")
    codec = fmt.get("codec")
    codec_txt = f" · {codec}" if codec else ""
    if height == 0:
        return "⬇️ Скачать видео"
    if size is None:
        return f"{height}p{codec_txt} · ~размер"
    label = f"{height}p{codec_txt} · {_size_label(size)}"
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
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"cancel:{key}",
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _allowed(formats: list[dict]) -> list[dict]:
    return [
        f for f in formats
        if f.get("filesize") is None or f["filesize"] <= HARD_CAP
    ]


START_TEXT = (
    "👋 Привет! Пришли ссылку на YouTube, Instagram или TikTok, "
    "и я скачаю его сюда (размер файла до 50 МБ)."
)


@router.message(F.text == "/start")
async def start(message: types.Message):
    await message.answer(START_TEXT)


@router.message(F.text == "/stats")
async def stats(message: types.Message):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=10.0)) as client:
            resp = await client.get(f"{config.SERVER_URL}/stats")
            body = resp.json()
    except httpx.HTTPError:
        await message.answer("❌ Не удалось получить статистику. Попробуйте позже.")
        return

    if not body.get("ok"):
        await message.answer("❌ Не удалось получить статистику.")
        return

    today = body.get("today", {})
    month = body.get("month", {})


    def fmt(c):
        return (
            f"• YouTube — {c.get('youtube', 0)}\n"
            f"• TikTok — {c.get('tiktok', 0)}\n"
            f"• Instagram — {c.get('instagram', 0)}\n"
            f"Всего: {c.get('total', 0)}"
        )

    text = (
        "📊 Статистика за сегодня и за месяц\n\n"
        "За сегодня:\n"
        f"{fmt(today)}\n\n"
        "За этот месяц:\n"
        f"{fmt(month)}"
    )
    await message.answer(text)


@router.message(F.text)
async def handle_text(message: types.Message):
    if message.edit_date:
        return
    parsed = extract_video(message.text)
    if not parsed:
        if message.text.startswith("/"):
            return
        await message.reply(
            "ℹ️ Это не похоже на ссылку для скачивания.\n"
            "Пришли ссылку на видео или фото из YouTube, Instagram или TikTok."
        )
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

    URLS[key] = url

    if body.get("platform") == "instagram":
        await _handle_instagram(status, key, body)
        return

    formats = body["formats"]
    available = _allowed(formats)
    if not available:
        await status.edit_text(
            "❌ Видео невозможно скачать — слишком большое (лимит Telegram 50 МБ)."
        )
        return

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


async def _handle_instagram(msg: types.Message, key: str, body: dict) -> None:
    count = body.get("media_count", 1)
    label = "⬇️ Скачать" if count == 1 else f"📦 Скачать пост ({count} файлов)"
    media = body.get("media") or []
    first_kind = media[0].get("kind") if media else None
    emoji = "🎬" if first_kind == "video" else "📸"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"ig:{key}")],
            [
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"cancel:{key}", style="danger"
                )
            ],
        ]
    )
    title = body.get("title", "Пост")
    await msg.edit_text(f"{emoji} {title}\n\nГотово к скачиванию:", reply_markup=kb)


@router.callback_query(F.data.startswith("ig:"))
async def handle_instagram_post(callback: types.CallbackQuery):
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    url = URLS.get(key)
    if not url:
        await callback.message.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return
    await _download_instagram_and_send(callback.message, key)


async def _download_instagram_and_send(msg: types.Message, key: str) -> None:
    url = URLS.get(key)
    if not url:
        await msg.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return

    await msg.edit_text("⏳ Скачиваю…")
    timeout = httpx.Timeout(300.0, connect=10.0)

    retry_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Попробовать ещё раз", callback_data=f"ig:{key}"
                )
            ]
        ]
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{config.SERVER_URL}/download", json={"url": url})
            body = resp.json()
    except httpx.HTTPError as e:
        await msg.edit_text(f"❌ Ошибка связи с сервером: {e}")
        return

    if resp.status_code == 503:
        await msg.edit_text("⚠️ Бот перегружен, пришлите Вашу ссылку позже.", reply_markup=retry_kb)
        return

    if not body.get("ok"):
        error = body.get("error", "Неизвестная ошибка")
        await msg.edit_text(f"❌ {error}", reply_markup=retry_kb if "перегружен" in error else None)
        return

    files = body.get("files") or []
    if not files:
        await msg.edit_text("❌ Файл не был создан")
        return

    first_kind = files[0].get("kind", "video")
    emoji = "🎬" if first_kind == "video" else "📸"
    caption = f"{emoji} {body.get('title')}" if body.get("title") else "🎬 Видео"
    media_items = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for f in files:
                r = await client.get(f"{config.SERVER_URL}/file/{quote(f['filename'])}")
                r.raise_for_status()
                bfile = BufferedInputFile(r.content, filename=f["filename"])
                media_items.append((f["kind"], bfile))
    except httpx.HTTPError as e:
        await msg.edit_text(f"❌ Ошибка загрузки файла: {e}")
        return

    try:
        if len(media_items) == 1:
            kind, bf = media_items[0]
            if kind == "video":
                await msg.answer_video(bf, caption=caption)
            else:
                await msg.answer_photo(bf, caption=caption)
        else:
            group = []
            for i, (kind, bf) in enumerate(media_items):
                cap = caption if i == 0 else None
                if kind == "video":
                    group.append(InputMediaVideo(media=bf, caption=cap))
                else:
                    group.append(InputMediaPhoto(media=bf, caption=cap))
            for start in range(0, len(group), 10):
                await msg.answer_media_group(group[start : start + 10])
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Не удалось отправить: {e}")


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
            resp = await client.get(f"{config.SERVER_URL}/file/{quote(filename)}")
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


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(callback: types.CallbackQuery):
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    URLS.pop(key, None)
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("❌ Отменено")