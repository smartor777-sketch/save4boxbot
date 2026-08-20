import datetime
import json
import os
import threading

STATS_FILE = os.getenv("STATS_FILE", "./stats.json")
_lock = threading.Lock()


def _read() -> dict:
    if not os.path.exists(STATS_FILE):
        return {}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATS_FILE)
    except OSError:
        pass


def record_download(platform: str) -> None:
    day = datetime.date.today().isoformat()
    with _lock:
        data = _read()
        day_data = data.setdefault(day, {})
        day_data[platform] = day_data.get(platform, 0) + 1
        _write(data)


def _counts(day_data: dict) -> dict:
    counts = {p: day_data.get(p, 0) for p in ("youtube", "tiktok", "instagram", "vk", "rutube", "yandex", "coub")}
    counts["total"] = sum(counts.values())
    return counts


def period_stats() -> dict:
    today = datetime.date.today()
    with _lock:
        data = _read()
        today_data = data.get(today.isoformat(), {})
        month_prefix = today.strftime("%Y-%m")
        month_data: dict = {}
        for day, counts in data.items():
            if day.startswith(month_prefix):
                for p, n in counts.items():
                    month_data[p] = month_data.get(p, 0) + n
    return {
        "today": _counts(today_data),
        "month": _counts(month_data),
    }


def prune() -> None:
    month_prefix = datetime.date.today().strftime("%Y-%m")
    with _lock:
        data = _read()
        old = [k for k in data if not k.startswith(month_prefix)]
        if not old:
            return
        for k in old:
            del data[k]
        _write(data)

