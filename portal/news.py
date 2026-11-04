"""Ахбор ва хабарҳо — helpers for the news module.

Photo pipeline (Pillow only, no new deps):
  * content-validated upload (Image.verify — the name/extension is ignored)
  * EXIF orientation fixed via ImageOps.exif_transpose
  * two WebP renditions: slider 1200x675 (cover crop) + gallery <=1600px
  * the uploaded original is discarded — only renditions are kept

Slider feed (dashboard):
  * category order: Боздидҳо → Ҷаласаҳо → Чорабиниҳо → Дастовардҳо →
    Навгониҳо; per category the 5 newest published items, each ONE slide
    built from its main photo. Empty categories are skipped entirely.
  * The whole slide list is one cached queryset — invalidated on any
    News/NewsPhoto save/delete (signals.py).
"""
import io
import json
import logging
import re
import urllib.request
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageOps

from .models import News, NewsPhoto

logger = logging.getLogger(__name__)

NEWS_SLIDES_CACHE_KEY = 'news:slides:v3'
NEWS_SLIDES_CACHE_SECONDS = 300          # 5 min; invalidated on writes
NEWS_PER_CATEGORY = 5                    # spec: max 5 latest per category
MAX_UPLOAD_BYTES = 15 * 1024 * 1024      # 15 MB per file
MAX_PHOTOS = 5

SLIDER_SIZE = (1200, 675)
GALLERY_MAX = 1600
WEBP_QUALITY = 82

ALLOWED_CONTENT_TYPES = {
    'image/jpeg', 'image/png', 'image/webp',
}
# HEIC/HEIF is not decoded by Pillow — rejected with a friendly message.
HEIC_TYPES = {'image/heic', 'image/heif'}


def can_manage_news(user):
    """Only the district chief (ROLE_DIRECTOR) and superusers manage news."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    from .views import get_user_role          # local import avoids cycles
    from django.conf import settings
    return get_user_role(user) == settings.ROLE_DIRECTOR


def invalidate_news_cache():
    cache.delete(NEWS_SLIDES_CACHE_KEY)


def slider_slides():
    """Ordered slide list for the dashboard, cached.

    Order = category order (5 news per category, newest first). Each
    news contributes ALL its photos (main first, then by order) as
    consecutive slides. Empty categories are skipped. Returns [] when
    nothing is published — the caller then shows the flag instead.
    """
    data = cache.get(NEWS_SLIDES_CACHE_KEY)
    if data is not None:
        return data

    by_cat = {}
    items = (
        News.objects.filter(status=News.STATUS_PUBLISHED)
        .select_related('school')
        .prefetch_related('photos')
        .order_by('-published_at', '-created_at')
    )
    for n in items:
        if len(by_cat.get(n.category, ())) >= NEWS_PER_CATEGORY:
            continue
        photos = [p for p in n.photos.all() if p.slider_image]
        has_video = (n.media_type == News.MEDIA_VIDEO and n.video_code)
        if not photos and not has_video:
            continue
        base = {
            'id': n.pk,
            'category': n.get_category_display(),
            'title': n.title,
            'school_name': n.school_name,
            'event_date': n.event_date.strftime('%d.%m.%Y'),
            'summary': n.summary,
            'url': f'/news/{n.pk}/',
        }
        slides_for_news = []
        if has_video:
            # One click-to-play video slide first — the Instagram iframe is
            # only created in the browser after the visitor presses play.
            slides_for_news.append(dict(
                base, type='video', embed=n.video_embed_url,
                video_url=n.video_url,
                thumb=n.video_thumb.url if n.video_thumb else ''))
        # Main photo first, the rest follow in their stored order.
        photos.sort(key=lambda p: (not p.is_main, p.order, p.id))
        slides_for_news.extend(
            dict(base, type='photo', image=p.slider_image.url)
            for p in photos)
        by_cat.setdefault(n.category, []).append(slides_for_news)

    slides = []
    for key, _label in News.CATEGORY_CHOICES:
        for news_slides in by_cat.get(key, ()):
            slides.extend(news_slides)

    cache.set(NEWS_SLIDES_CACHE_KEY, slides, NEWS_SLIDES_CACHE_SECONDS)
    return slides


def _webp_bytes(img, quality=WEBP_QUALITY):
    buf = io.BytesIO()
    img.save(buf, 'WEBP', quality=quality, method=6)
    return buf.getvalue()


def process_photo(django_file):
    """Validate + convert one upload. Returns (gallery_bytes, slider_bytes).

    Raises ValueError with a user-facing Tajik message on any problem:
    wrong type, corrupt file, or unsupported HEIC.
    """
    ctype = (getattr(django_file, 'content_type', '') or '').lower()
    if ctype in HEIC_TYPES or (django_file.name or '').lower().endswith(
            ('.heic', '.heif')):
        raise ValueError(
            'Формати HEIC дастгирӣ намешавад. Аксро пеш аз боркунӣ ба '
            'JPG табдил диҳед.')
    if django_file.size > MAX_UPLOAD_BYTES:
        raise ValueError('Акс аз 15 МБ калон аст — ҳаҷмашро кам кунед.')

    try:
        img = Image.open(django_file)
        img.verify()                       # content check, not the name
    except Exception:
        raise ValueError('Файл акси дуруст нест ё вайрон аст.')

    django_file.seek(0)
    try:
        img = Image.open(django_file)
        img = ImageOps.exif_transpose(img)  # fix phone orientation
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        elif img.mode == 'L':
            img = img.convert('RGB')
    except Exception:
        raise ValueError('Акс кушода нашуд — файли дигар интихоб кунед.')

    gallery = img.copy()
    gallery.thumbnail((GALLERY_MAX, GALLERY_MAX), Image.LANCZOS)
    slider = ImageOps.fit(img, SLIDER_SIZE, Image.LANCZOS)
    return _webp_bytes(gallery), _webp_bytes(slider)


def save_photo(news, django_file, order, is_main=False):
    """Process one upload and persist a NewsPhoto."""
    gallery, slider = process_photo(django_file)
    photo = NewsPhoto(news=news, order=order, is_main=is_main)
    photo.image.save(
        f'{news.pk}_{order}_g.webp', ContentFile(gallery), save=False)
    photo.slider_image.save(
        f'{news.pk}_{order}_s.webp', ContentFile(slider), save=False)
    photo.save()
    return photo


def replace_main_photo(photo):
    """Mark `photo` as the single main photo of its news item."""
    NewsPhoto.objects.filter(news_id=photo.news_id, is_main=True) \
        .exclude(pk=photo.pk).update(is_main=False)
    if not photo.is_main:
        photo.is_main = True
        photo.save(update_fields=['is_main'])
    invalidate_news_cache()


def publish(news):
    news.status = News.STATUS_PUBLISHED
    news.published_at = timezone.now()
    news.save(update_fields=['status', 'published_at', 'updated_at'])


def unpublish(news):
    news.status = News.STATUS_UNPUBLISHED
    news.save(update_fields=['status', 'updated_at'])


def archive(news):
    news.status = News.STATUS_ARCHIVED
    news.save(update_fields=['status', 'updated_at'])


def restore(news):
    """Restore from archive/unpublished back to draft — author republishes."""
    news.status = News.STATUS_DRAFT
    news.published_at = None
    news.save(update_fields=['status', 'published_at', 'updated_at'])


# --- Instagram video (Reels embed) -------------------------------------------
#
# Only official instagram.com post/reel/tv links are accepted. The stored
# shortcode (A–Z, a–z, 0–9, -, _) is the single source for building the
# official /embed/ iframe URL — raw HTML from Instagram never reaches the
# page, so injected markup cannot sneak in through a crafted link.

VIDEO_URL_RE = re.compile(
    r'^https?://(?:www\.)?instagram\.com/'
    r'(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,30})')

VIDEO_OEMBED_URL = 'https://www.instagram.com/api/v1/oembed/'
VIDEO_FETCH_TIMEOUT = 8                      # seconds per outbound request
VIDEO_THUMB_MAX_BYTES = 6 * 1024 * 1024


def extract_video_code(url):
    """Instagram shortcode from a reel/post/tv URL, or None when the URL
    is not an Instagram media link. Query strings (?igsh=…) are ignored."""
    if not url:
        return None
    m = VIDEO_URL_RE.match(url.strip())
    return m.group(1) if m else None


def canonical_video_url(code):
    return 'https://www.instagram.com/reel/' + code + '/'


def video_enabled():
    """Feature switch for Instagram video news.

    To disable the feature instantly — e.g. if the dashboard slows down or
    Instagram embeds break — set NEWS_VIDEO_ENABLED = False in
    maorif_portal/settings.py. Stored video links and photos are kept;
    photo news, the slider and journals are unaffected.
    """
    return getattr(settings, 'NEWS_VIDEO_ENABLED', True)


def fetch_video_thumb(news):
    """Best-effort: download the reel's cover once via the public oEmbed
    endpoint and store it on news.video_thumb.

    Runs only when a director/superuser saves a video news item — never on
    the public request path. Any failure is logged and silently skipped:
    the page then shows a styled placeholder cover instead. The fetched
    thumbnail is an image file verified by Pillow, not HTML.
    """
    if not news.video_code:
        return
    url = (VIDEO_OEMBED_URL + '?url='
           + quote(canonical_video_url(news.video_code), safe=''))
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'MaorifPortal/1.0'})
        with urllib.request.urlopen(req, timeout=VIDEO_FETCH_TIMEOUT) as r:
            data = json.loads(r.read(1 << 20).decode('utf-8', 'replace'))
        thumb_url = data.get('thumbnail_url') or ''
        if not thumb_url.startswith('https://'):
            return
        req = urllib.request.Request(
            thumb_url, headers={'User-Agent': 'MaorifPortal/1.0'})
        with urllib.request.urlopen(req, timeout=VIDEO_FETCH_TIMEOUT) as r:
            blob = r.read(VIDEO_THUMB_MAX_BYTES + 1)
        if len(blob) > VIDEO_THUMB_MAX_BYTES:
            return
        Image.open(io.BytesIO(blob)).verify()          # content check
        img = Image.open(io.BytesIO(blob))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        news.video_thumb.save(
            news.video_code + '.webp',
            ContentFile(_webp_bytes(img)), save=False)
    except Exception:
        logger.exception(
            'Failed to fetch Instagram thumbnail for %s', news.video_code)
