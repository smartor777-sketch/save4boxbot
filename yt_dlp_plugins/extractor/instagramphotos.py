from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.utils import traverse_obj


class _InstagramPhotosIE(InstagramIE, plugin_name="instagramphotos"):
    """Замена InstagramIE с поддержкой фотографий.

    Стоковый экстрактор строит форматы только из video_versions и
    отклоняет фото-посты ('There is no video in this post').
    Добавляем лучшее изображение из image_versions2.candidates как
    отдельный формат (vcodec=none), поэтому фото качаются как .jpg.
    """

    def _extract_product_media(self, product_media):
        result = super()._extract_product_media(product_media)
        formats = result.get("formats") or []
        existing_urls = {f.get("url") for f in formats}

        candidates = traverse_obj(
            product_media, ("image_versions2", "candidates", lambda _, v: v)
        ) or []

        def _area(c):
            return (c.get("width") or 0) * (c.get("height") or 0)

        best = None
        seen = set()
        for idx, cand in enumerate(candidates):
            url = cand.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            key = (_area(cand), idx)
            if best is None or key > best[0]:
                best = (key, cand)

        if best and best[1].get("url") not in existing_urls:
            cand = best[1]
            formats.append(
                {
                    "url": cand["url"],
                    "format_id": "img-best",
                    "width": cand.get("width"),
                    "height": cand.get("height"),
                    "vcodec": "none",
                    "acodec": "none",
                    "ext": "jpg",
                    "protocol": "https",
                }
            )
        result["formats"] = formats
        return result
