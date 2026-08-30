import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
