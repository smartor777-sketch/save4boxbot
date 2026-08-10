"""Обход WAF-капчи TikTok.

Триггер капчи — browser impersonation (curl_cffi). TikTok детектит по
TLS/HTTP2-отпечатку ВСЕ impersonate-цели (chrome/firefox/edge/safari и
старые версии) и отдаёт challenge-страницу вместо данных. Обычный TLS
(без impersonation) проходит. Поэтому отключаем impersonation для всех
веб-запросов TikTok.

Сопутствующее требование: глобальная имперсонация в опциях тоже не должна
применяться к TikTok — она подхватывается хендлером как fallback
(_get_request_target: extensions.get('impersonate') or self.impersonate).
В downloader.py для TikTok используется _tiktok_opts, который её снимает.
"""

from yt_dlp.extractor.tiktok import TikTokIE


class _NoImpersonateTikTokIE(TikTokIE, plugin_name="tiktok"):
    def _download_webpage_handle(self, *args, **kwargs):
        kwargs["impersonate"] = False
        return super()._download_webpage_handle(*args, **kwargs)

    def _download_webpage(self, *args, **kwargs):
        kwargs["impersonate"] = False
        return super()._download_webpage(*args, **kwargs)

