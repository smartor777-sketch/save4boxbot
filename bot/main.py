import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import config
from .handlers import START_TEXT, router

logging.basicConfig(level=logging.INFO)


BOT_DESCRIPTION = (
    "Скачивает видео и фото из YouTube, Instagram, TikTok, VK, Rutube, "
    "Яндекс Видео, Coub и Dzen прямо в Telegram.\n\n"
    "Что умеет:\n"
    "• YouTube — видео в лучшем качестве\n"
    "• Instagram — фото, видео и карусели\n"
    "• TikTok — видео\n"
    "• VK — видео и клипы\n"
    "• Rutube — видео\n"
    "• Coub — видео с аудио\n"
    "• Dzen — видео\n"
    "• Яндекс Видео — превью из поиска\n\n"
    "Как пользоваться: просто пришли ссылку на пост — и файл появится здесь.\n"
    "Максимальный размер файла — 50 МБ."
)


async def setup_bot_profile(bot: Bot) -> None:
    await bot.set_my_description(BOT_DESCRIPTION)
    await bot.set_my_short_description(
        "Скачиваю видео и фото из YouTube, Instagram, TikTok, VK, Rutube, "
        "Яндекс Видео, Coub и Dzen."
    )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Приветствие и помощь"),
            BotCommand(command="stats", description="Статистика за сегодня и за месяц"),
        ]
    )


async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await setup_bot_profile(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
