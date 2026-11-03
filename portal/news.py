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

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, ImageOps

from .models import News, NewsPhoto

NEWS_SLIDES_CACHE_KEY = 'news:slides:v2'
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
        if not photos:
            continue
        # Main photo first, the rest follow in their stored order.
        photos.sort(key=lambda p: (not p.is_main, p.order, p.id))
        slides_for_news = [{
            'id': n.pk,
            'category': n.get_category_display(),
            'title': n.title,
            'school_name': n.school_name,
            'event_date': n.event_date.strftime('%d.%m.%Y'),
            'summary': n.summary,
            'image': p.slider_image.url,
            'url': f'/news/{n.pk}/',
        } for p in photos]
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
