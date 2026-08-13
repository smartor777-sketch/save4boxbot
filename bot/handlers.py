import os
import asyncio
import time

import httpx
import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from urllib.parse import quote

from . import config
from .utils import extract_video

router = Router()

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")  # сюда сервер сохраняет файлы
MAX_FILESIZE = 50 * 1024 * 1024  # лимит Telegram
SLOW_SIZE = 30 * 1024 * 1024  # выше этого — помечаем «долго»
HARD_CAP = 45 * 1024 * 1024  # выше этого качество не предлагаем

def _local_file_input(filename: str) -> FSInputFile | None:
    """Файл уже лежит на диске сервера — отдаём путь вместо буфера в памяти."""
    path = os.path.abspath(os.path.join(DOWNLOAD_DIR, os.path.basename(filename)))
    if os.path.exists(path):
        return FSInputFile(path, filename=filename)
    return None


def _is_photo_message(msg: types.Message) -> bool:
    """Сообщение с постером (фотография), которое потом подменим на видео."""
    return bool(getattr(msg, "photo", None))


async def _edit_status(msg: types.Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Правит текст/подпись в зависимости от типа сообщения.

    «message is not modified» и rate-limit не роняют обработчик — статус-сообщение
    вторично, а падение тут теряет уже скачанный файл.
    """
    try:
        if _is_photo_message(msg):
            await msg.edit_caption(text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    except TelegramRetryAfter as e:
        logger.warning("status edit rate-limited, skip: %s", e)


async def _poll_progress(
    status_msg: types.Message,
    label: str,
    url: str,
    height: int | None,
    post_task: asyncio.Task,
) -> None:
    """Показывает стадию и процент скачивания, пока висит POST /download."""
    frames = ("🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛")
    idx = 0
    last_text = None
    last_edit = 0.0
    while not post_task.done():
        text = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                r = await client.post(
                    f"{config.SERVER_URL}/progress",
                    json={"url": url, "height": height},
                )
                body = r.json()
            st = body.get("status")
            pct = body.get("percent")
            if st == "downloading" and isinstance(pct, (int, float)):
                pct = min(99, max(0, int(pct)))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                text = f"⏳ Скачиваю {label}… {pct}%\n{bar}"
            elif st == "extracting":
                # Точного % на этапе извлечения нет — сервер отдаёт прошедшие
                # секунды, рисуем плавную полоску с капом 88%, чтобы не выглядела
                # завершённой до старта скачивания.
                elapsed = int(body.get("elapsed") or 0)
                ep = min(88, elapsed * 88 // 15)
                bar = "█" * (ep // 10) + "░" * (10 - ep // 10)
                text = (
                    f"{frames[idx % len(frames)]} Получаю информацию… {label} "
                    f"({elapsed}с)\n{bar}"
                )
        except (httpx.HTTPError, ValueError, TypeError):
            text = None

        now = time.monotonic()
        if text and text != last_text and now - last_edit >= 3:
            last_text = text
            last_edit = now
            try:
                await _edit_status(status_msg, text)
            except Exception as e:
                logger.warning("progress status edit failed: %s", e)
        idx += 1
        await asyncio.sleep(2)


# key (video_id / hash) -> canonical url (в callback_data не влезает полный URL)
URLS: dict[str, str] = {}

# скачивания в процессе («конкурентный двойной клик» по одной кнопке)
_IN_FLIGHT: set[str] = set()


def _size_label(size):
    if size >= 1024 * 1024:
        return f"{round(size / 1024 / 1024)} МБ"
    if size >= 1024:
        return f"{round(size / 1024)} КБ"
    return f"{size} Б"


_AGE_RESTRICTED_MARKS = (
    "sign in to confirm your age",
    "age-restricted",
    "inappropriate for some users",
    "age restricted",
)
_COOKIE_INVALID_MARKS = (
    "no longer valid",
    "cookie is invalid",
    "cookie_invalid",
)
_YANDEX_FAILURE_MARKS = (
    "не удалось определить источник видео на странице превью",
    "yandexvideo preview",
    "yandexvideo",
    "unable to extract data_raw",
)
_YT_BOT_CHECK_MARKS = (
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "not a bot",
    "bot-check",
    "n challenge solving failed",
    "unable to solve n challenge",
)


def _friendly_error(error) -> str:
    """Превращает сырые ошибки yt-dlp в осмысленные сообщения для пользователя."""
    if not error:
        return "Неизвестная ошибка"
    text = str(error).lower()
    if any(m in text for m in _AGE_RESTRICTED_MARKS):
        return (
            "🚫 Это видео с возрастным ограничением (18+).\n"
            "Скачивание таких видео недоступно без подтверждённого аккаунта YouTube."
        )
    if any(m in text for m in _COOKIE_INVALID_MARKS):
        return (
            "⚠️ Куки YouTube устарели (YouTube ротирует сессии).\n"
            "Возрастные видео временно недоступны, обычные работают без ограничений."
        )
    if any(m in text for m in _YANDEX_FAILURE_MARKS):
        return (
            "🤖 Не удалось разобрать превью Яндекс Видео — Яндекс меняет разметку страницы.\n"
            "Попробуйте отправить прямую ссылку на ролик с Rutube / ВК / YouTube."
        )
    if any(m in text for m in _YT_BOT_CHECK_MARKS):
        return (
            "🔒 YouTube требует подтвердить, что вы не бот (наш сервер попал под защиту).\n"
            "Ссылки на короткие видео (Shorts) обычно работают. Ошибка временная — "
            "скоро починим."
        )
    return str(error)


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
    "👋 Привет! Я скачиваю видео и фото из YouTube, Instagram, TikTok, VK, "
    "Rutube и Яндекс Видео прямо в Telegram.\n\n"
    "Просто пришли ссылку на видео — и файл появится здесь "
    "(размер до 50 МБ, при необходимости предложу выбрать качество)."
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
            f"• VK — {c.get('vk', 0)}\n"
            f"• Rutube — {c.get('rutube', 0)}\n"
            f"• Яндекс Видео — {c.get('yandex', 0)}\n"
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
            "Пришли ссылку на видео из YouTube, Instagram, TikTok, "
            "VK, Rutube или Яндекс Видео."
        )
        return
    platform, url, key = parsed
    logger.info("Request: platform=%s key=%s url=%s", platform, key, url)

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
        await status.edit_text(f"❌ {_friendly_error(body.get('error'))}")
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
    kb = _build_keyboard(available, key)

    thumb_name = body.get("thumbnail")
    if thumb_name:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                r = await client.get(f"{config.SERVER_URL}/file/{quote(thumb_name)}")
                r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Thumb fetch failed thumb=%s: %s", thumb_name, e)
            r = None
        if r is not None and r.content and len(r.content) < 5 * 1024 * 1024:
            poster = BufferedInputFile(r.content, filename=thumb_name)
            try:
                await status.answer_photo(poster, caption=f"🎬 {title}", reply_markup=kb)
            except Exception as e:
                logger.warning("answer_photo failed for %s: %s", key, e)
            else:
                try:
                    await status.delete()
                except Exception:
                    pass
                return
    logger.info("No poster for key=%s (thumb=%s): fallback to text flow", key, thumb_name)

    if len(available) == 1 and available[0]["height"] == 0:
        await _download_and_send(status, key, 0)
        return

    await status.edit_text(f"🎬 {title}\n\nВыбери качество:", reply_markup=kb)


@router.callback_query(F.data.startswith("fmt:"))
async def handle_format(callback: types.CallbackQuery):
    await callback.answer()
    _, key, height = callback.data.split(":")
    url = URLS.get(key)
    if not url:
        await callback.message.edit_text("❌ Ссылка устарела, пришли её ещё раз.")
        return

    marker = f"fmt:{key}:{height}"
    if marker in _IN_FLIGHT:
        await callback.answer("⏳ Уже скачиваю, чуть позже…", show_alert=False)
        return
    _IN_FLIGHT.add(marker)
    try:
        await _download_and_send(callback.message, key, int(height))
    finally:
        _IN_FLIGHT.discard(marker)


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
    marker = f"ig:{key}"
    if marker in _IN_FLIGHT:
        await callback.answer("⏳ Уже скачиваю, чуть позже…", show_alert=False)
        return
    _IN_FLIGHT.add(marker)
    try:
        await _download_instagram_and_send(callback.message, key)
    finally:
        _IN_FLIGHT.discard(marker)


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

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{config.SERVER_URL}/download", json={"url": url}
            )

    post_task = asyncio.create_task(_post())
    poll_task = asyncio.create_task(
        _poll_progress(msg, "пост", url, None, post_task)
    )
    try:
        try:
            resp = await post_task
        except httpx.HTTPError as e:
            await msg.edit_text(f"❌ Ошибка связи с сервером: {e}")
            return
        body = resp.json()
    finally:
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)

    if resp.status_code == 503:
        await msg.edit_text("⚠️ Бот перегружен, пришлите Вашу ссылку позже.", reply_markup=retry_kb)
        return

    if not body.get("ok"):
        error = body.get("error", "Неизвестная ошибка")
        await msg.edit_text(f"❌ {_friendly_error(error)}", reply_markup=retry_kb if "перегружен" in error else None)
        return

    files = body.get("files") or []
    if not files:
        await msg.edit_text("❌ Файл не был создан")
        return

    first_kind = files[0].get("kind", "video")
    emoji = "🎬" if first_kind == "video" else "📸"
    caption = f"{emoji} {body.get('title')}" if body.get("title") else "🎬 Видео"
    media_items = []
    missing = []
    for f in files:
        local = _local_file_input(f["filename"])
        if local is not None:
            media_items.append((f["kind"], local))
        else:
            missing.append(f)
    if missing:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for f in missing:
                    r = await client.get(f"{config.SERVER_URL}/file/{quote(f['filename'])}")
                    r.raise_for_status()
                    media_items.append((f["kind"], BufferedInputFile(r.content, filename=f["filename"])))
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
        await _edit_status(msg, "❌ Ссылка устарела, пришли её ещё раз.")
        return

    height_label = "видео" if height == 0 else f"{height}p"
    timeout = httpx.Timeout(300.0, connect=10.0)

    # Прогресс выводим в отдельное заметное текстовое сообщение: подпись под
    # фото-постером слишком мелкая. Постер остаётся видимым во время скачивания.
    if _is_photo_message(msg):
        try:
            progress_msg = await msg.answer(f"⏳ Скачиваю {height_label}…")
        except Exception:
            progress_msg = msg
    else:
        progress_msg = msg
        await _edit_status(progress_msg, f"⏳ Скачиваю {height_label}…")

    payload = {"url": url}
    if height != 0:
        payload["height"] = height

    async def _post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{config.SERVER_URL}/download", json=payload
            )

    post_task = asyncio.create_task(_post())
    poll_task = asyncio.create_task(
        _poll_progress(progress_msg, height_label, url, None if height == 0 else height, post_task)
    )
    try:
        try:
            resp = await post_task
        except httpx.HTTPError as e:
            await _edit_status(progress_msg, f"❌ Ошибка связи с сервером: {e}")
            return
        body = resp.json()
    finally:
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)

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
        await _edit_status(
            progress_msg,
            "⚠️ Бот перегружен, пришлите Вашу ссылку позже.",
            kb,
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
            await _edit_status(
                progress_msg,
                "❌ Видео не влезло в 50 МБ.\nПопробуй выбрать меньший формат:",
                kb,
            )
        else:
            await _edit_status(progress_msg, f"❌ {_friendly_error(error)}")
        return

    filename = body["filename"]
    title = body.get("title")
    dur = body.get("duration_min")

    height_label = f" ({height}p)" if height else ""
    caption_parts = [f"🎬 {title}{height_label}"]
    if dur:
        caption_parts.append(f"⏱️ Длительность: ~{dur} мин")
    caption_parts.append(url)
    caption = "\n".join(caption_parts)

    try:
        await _edit_status(progress_msg, "⬆️ Файл готов, отправляю…")
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{config.SERVER_URL}/file/{quote(filename)}")
            resp.raise_for_status()
            video = BufferedInputFile(resp.content, filename=filename)
        await msg.answer_video(video, caption=caption)
    except httpx.HTTPError as e:
        await _edit_status(progress_msg, f"❌ Ошибка загрузки файла: {e}")
        return
    except Exception as e:
        await _edit_status(progress_msg, f"❌ Не удалось отправить: {e}")
        return

    # Видео отправлено — постер и статус больше не нужны.
    try:
        await msg.delete()
    except Exception:
        pass
    if progress_msg is not msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass


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
        await msg.edit_text(f"❌ {_friendly_error(body.get('error'))}")
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
    await _edit_status(msg, f"🎬 {body.get('title', 'Видео')}\n\nВыбери качество:", kb)


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(callback: types.CallbackQuery):
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    URLS.pop(key, None)
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("❌ Отменено")
