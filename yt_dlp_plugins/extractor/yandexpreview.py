import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class YandexVideoPreviewIE(InfoExtractor):
    """Фикс сломанного встроенного YandexVideoPreview.

    Стоковый экстрактор ищет в странице window.Ya.__inline_params__,
    которого на новой версии страницы больше нет (страница стала SPA).
    Вместо этого берём ссылку на источник из блока VideoViewer-Source
    и отдаём её обратно в yt-dlp (rutube / dzen / vk / ok / youtube / ...).
    """

    _VALID_URL = (
        r"https?://(?:www\.)?yandex\.\w{2,3}(?:\.(?:am|ge|il|tr))?/video/(?:touch/)?preview"
        r"(?:/?\?.*?filmId=|/)(?P<id>\d+)"
    )

    _SOURCE_LINK_RE = re.compile(
        r'class="Link VideoViewer-Source(?:PathLink|IconLink)"[^>]+href=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)

        source_url = self._search_regex(
            self._SOURCE_LINK_RE, webpage, "source link", default=None
        )
        if not source_url:
            raise ExtractorError(
                "Не удалось определить источник видео на странице превью",
                expected=True,
            )

        if source_url.startswith("//"):
            source_url = "https:" + source_url
        elif source_url.startswith("http://"):
            source_url = "https://" + source_url[len("http://") :]

        if re.search(
            r"yandex\.\w{2,3}(?:\.(?:am|ge|il|tr))?/video/(?:touch/)?preview", source_url
        ):
            raise ExtractorError(
                "Источник видео недоступен для скачивания (DRM / приватный ролик)",
                expected=True,
            )

        return self.url_result(source_url)

