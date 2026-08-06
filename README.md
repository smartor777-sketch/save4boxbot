# Telegram-бот для скачивания видео (@Save4Box)

Бот скачивает видео по ссылке: YouTube, Instagram, TikTok и другие
платформы, поддерживаемые [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Архитектура

Проект состоит из двух частей:

- `bot/` — Telegram-бот на `aiogram`, принимает ссылки и отдаёт файлы в чат.
- `server/` — FastAPI-сервис (`yt-server`), обёртка над yt-dlp:
  - `POST /formats` — список доступных форматов для ссылки;
  - `POST /download` — скачивание с ограничением высоты/размера;
  - `GET /file/<name>` — отдача файла в чат;
  - `GET /stats` — статистика скачиваний за день/месяц.

Бот и сервер запускаются отдельными systemd-юнитами (`yt-bot.service`,
`yt-server.service`), сервер слушает только `127.0.0.1`.

## Установка

```bash
python3 -m venv .venv
./.venv/bin/pip install -U "yt-dlp[default]"   # + yt-dlp-ejs для YouTube
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN и при необходимости COOKIE_FILE
```

### JS-рантайм для YouTube (обязательно)

Современный YouTube требует решения JavaScript-челленджей (n-challenge).
Для этого yt-dlp нужен внешний JS-рантайм:

- **Deno (рекомендуется):** включён по умолчанию, достаточно положить бинарник
  `deno` (>= 2.3.0) в `PATH`, например `/usr/local/bin/deno`.
- **Node.js (>= 22.0.0):** включить флагом `--js-runtimes node`.

Вместе с рантаймом поставляется пакет `yt-dlp-ejs` (ставится через
`pip install "yt-dlp[default]"`) — без него YouTube отдаёт
`n challenge solving failed` и недоступны видеоформаты.

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Токен бота из BotFather |
| `SERVER_URL` | `http://127.0.0.1:8000` | URL внутреннего FastAPI-сервиса |
| `DOWNLOAD_DIR` | `./downloads` | Каталог для скачанных файлов |
| `MAX_FILESIZE_MB` | `50` | Макс. размер файла, который бот отдаёт в Telegram |
| `CLEANUP_INTERVAL_MIN` | `15` | Период чистки старых файлов |
| `FILE_MAX_AGE_MIN` | `15` | Сколько минут хранить файлы перед удалением |
| `MAX_CONCURRENT_DOWNLOADS` | `4` | Одновременные загрузки на сервер |
| `YTDLP_SOCKET_TIMEOUT` | `30` | Таймаут yt-dlp на сетевые операции |
| `DOWNLOAD_TIMEOUT_SEC` | `300` | Лимит времени на одно скачивание |
| `EXTRACT_RETRY_ATTEMPTS` | `3` | Ретраи при ошибках извлечения |
| `EXTRACT_RETRY_BACKOFF_SEC` | `2` | Пауза между ретраями |
| `IMPERSONATE` | `chrome` | Имитация браузера для yt-dlp (chrome/safari/...) |
| `COOKIE_FILE` | — | Путь к cookies-файлу (Netscape) |

## Cookies: зачем и как

### Зачем нужны cookies

yt-dlp может работать и без них, но с cookies:

- скачиваются **возрастные (18+) YouTube-видео** (`Sign in to confirm your age`);
- меньше шансов попасть под бот-детект (`Sign in to confirm you're not a bot`);
- у YouTube часто возвращается более полный список форматов;
- Instagram/TikTok обычно **требуют** авторизации для прямых ссылок
  на скачивание.

Cookies подключаются **глобально** (`cookiefile` в `server/downloader.py`),
т.е. отправляются при **каждом** запросе к YouTube, Instagram, TikTok — не только
для 18+.

### Как получить cookies

1. Открыть нужный сайт (например, `youtube.com`) в Chrome/Edge/Firefox
   **будучи залогиненным**.
2. Установить расширение **«Get cookies.txt LOCALLY»** (Chrome/Edge)
   или аналог для Firefox.
3. Экспортировать cookies **только для нужного домена**
   (например, `youtube.com`) — на выходе будет файл в **Netscape-формате**.
4. Положить файл на сервер и указать путь в `.env`:

   ```
   COOKIE_FILE=./cookies.txt
   ```

5. Перезапустить сервер: `systemctl restart yt-server`.

В одном файле можно держать cookies сразу нескольких доменов
(YouTube + Instagram + TikTok) — yt-dlp сам выберет нужные по домену.

### Формат файла

Netscape HTTP Cookie File:

```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	0	YSC	RWWXYMHok0M
...
```

Каждая строка: `domain  includeSubdomains  path  secure  expires  name  value`.

### Безопасность и риск бана

> ⚠️ **Cookies = ваша живая сессия.** Это фактически пароль от аккаунта.

- **Никогда не коммитьте `cookies.txt` в git.** Файл добавлен в `.gitignore` —
  но перед публикацией репозитория обязательно проверьте `git status` и
  `git ls-files`.
- Не выкладывайте cookies в публичные чаты/репозитории — ими можно
  залогиниться под вас.
- Использование cookies в автоматическом скачивании — **потенциальный риск
  бана аккаунта** при аномальной активности. При разумном объёме
  (десятки-сотни видео в день, а не тысячи) риск минимален.
- Если cookies протухли или YouTube запросил re-auth — бот просто перестанет
  скачивать age-restricted, обычные видео продолжат работать без cookies.

### Переменные cookies

| Переменная | Описание |
|---|---|
| `COOKIE_FILE` | Путь к cookies-файлу (Netscape). Пусто = без cookies. |

## Deployment

Пример systemd-юнитов смотри в `yt-bot.service`. Сервер (`yt-server`) и бот
(`yt-bot`) обычно запускаются через:

```bash
systemctl enable --now yt-server
systemctl enable --now yt-bot
```
