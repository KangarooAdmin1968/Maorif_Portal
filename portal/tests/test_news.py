"""📰 Ахбор ва хабарҳо — news module tests.

Covers: management permissions (superuser/staff/director only), photo
validation + WebP optimization, status transitions, slider ordering and
the dashboard flag fallback, archive visibility.
"""
import datetime
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image

from portal import news as news_mod
from portal.models import News, NewsPhoto

from .helpers import GoldenBase

_TMP_MEDIA = tempfile.mkdtemp(prefix='news_media_')


def _img(name='a.jpg', size=(800, 600), fmt='JPEG'):
    buf = io.BytesIO()
    Image.new('RGB', size, (20, 80, 140)).save(buf, fmt)
    ct = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[fmt]
    return SimpleUploadedFile(name, buf.getvalue(), content_type=ct)


def _bad_file():
    return SimpleUploadedFile('fake.jpg', b'not-an-image-at-all',
                              content_type='image/jpeg')


@override_settings(MEDIA_ROOT=_TMP_MEDIA)
class NewsTests(GoldenBase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TMP_MEDIA, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    def _post_data(self, **kw):
        data = {
            'category': 'visit',
            'title': 'Боздид аз мактаб',
            'school_id': str(self.school_a.id),
            'event_date': '2026-10-20',
            'summary': 'Роҳбари шуъба аз мактаб дидан кард.',
        }
        data.update(kw)
        return data

    def _make_news(self, category='visit', status='draft',
                   published_at=None, title='Хабари озмоишӣ'):
        n = News.objects.create(
            category=category, title=title, school=self.school_a,
            event_date=datetime.date(2026, 10, 20),
            summary='Тавсиф', status=status, author=self.director,
            published_at=published_at)
        return n

    def _publish(self, news, day=1):
        news.status = News.STATUS_PUBLISHED
        news.published_at = datetime.datetime(
            2026, 10, day, 12, 0, tzinfo=datetime.timezone.utc)
        news.save()
        return news

    def _add_photo(self, news, order=0, is_main=True):
        return news_mod.save_photo(news, _img(), order=order, is_main=is_main)

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.delete(news_mod.NEWS_SLIDES_CACHE_KEY)

    # -- permissions ---------------------------------------------------------

    def test_director_can_open_admin_panel(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse('news_admin'))
        self.assertEqual(resp.status_code, 200)

    def test_superuser_and_staff_can_manage(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('news_admin')).status_code, 200)

    def test_teacher_zavuch_principal_cannot_manage(self):
        for user in (self.teacher_a, self.zavuch_a, self.principal_a):
            self.client.force_login(user)
            resp = self.client.get(reverse('news_admin'))
            self.assertRedirects(resp, reverse('dashboard'),
                                 fetch_redirect_response=False)
            resp = self.client.get(reverse('news_create'))
            self.assertRedirects(resp, reverse('dashboard'),
                                 fetch_redirect_response=False)

    def test_anonymous_cannot_manage(self):
        resp = self.client.get(reverse('news_admin'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp.url)

    # -- creation / validation ----------------------------------------------

    def test_create_without_photo_rejected(self):
        self.client.force_login(self.director)
        resp = self.client.post(reverse('news_create'), self._post_data())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.count(), 0)

    def test_create_with_six_photos_rejected(self):
        self.client.force_login(self.director)
        data = self._post_data()
        data['photos'] = [_img(f'p{i}.jpg') for i in range(6)]
        resp = self.client.post(reverse('news_create'), data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.count(), 0)

    def test_create_valid_news_becomes_draft(self):
        self.client.force_login(self.director)
        data = self._post_data()
        data['photos'] = [_img('p1.jpg'), _img('p2.png', fmt='PNG')]
        resp = self.client.post(reverse('news_create'), data)
        self.assertRedirects(resp, reverse('news_admin'),
                             fetch_redirect_response=False)
        n = News.objects.get()
        self.assertEqual(n.status, News.STATUS_DRAFT)
        self.assertEqual(n.author, self.director)
        self.assertEqual(n.photos.count(), 2)
        self.assertEqual(n.photos.filter(is_main=True).count(), 1)

    def test_corrupt_image_rejected(self):
        self.client.force_login(self.director)
        data = self._post_data()
        data['photos'] = [_bad_file()]
        resp = self.client.post(reverse('news_create'), data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.count(), 0)

    # -- image pipeline ------------------------------------------------------

    def test_photo_pipeline_outputs_webp_renditions(self):
        n = self._make_news()
        photo = self._add_photo(n)
        self.assertTrue(photo.image.name.endswith('.webp'))
        self.assertTrue(photo.slider_image.name.endswith('.webp'))
        with Image.open(photo.image) as im:
            self.assertLessEqual(max(im.size), 1600)
        with Image.open(photo.slider_image) as im:
            self.assertEqual(im.size, (1200, 675))

    def test_heic_rejected_with_friendly_message(self):
        f = SimpleUploadedFile('shot.heic', b'\x00' * 2000,
                               content_type='image/heic')
        with self.assertRaises(ValueError):
            news_mod.process_photo(f)

    # -- status transitions --------------------------------------------------

    def test_publish_requires_photo(self):
        n = self._make_news()
        self.client.force_login(self.director)
        self.client.post(reverse('news_action', args=[n.pk]),
                         {'action': 'publish'})
        n.refresh_from_db()
        self.assertEqual(n.status, News.STATUS_DRAFT)

    def test_full_status_cycle(self):
        n = self._make_news()
        self._add_photo(n)
        self.client.force_login(self.director)
        url = reverse('news_action', args=[n.pk])

        self.client.post(url, {'action': 'publish'})
        n.refresh_from_db()
        self.assertEqual(n.status, News.STATUS_PUBLISHED)
        self.assertIsNotNone(n.published_at)

        self.client.post(url, {'action': 'unpublish'})
        n.refresh_from_db()
        self.assertEqual(n.status, News.STATUS_UNPUBLISHED)

        self.client.post(url, {'action': 'archive'})
        n.refresh_from_db()
        self.assertEqual(n.status, News.STATUS_ARCHIVED)
        self.assertTrue(n.photos.exists())  # archive keeps the photos

        self.client.post(url, {'action': 'restore'})
        n.refresh_from_db()
        self.assertEqual(n.status, News.STATUS_DRAFT)

        self.client.post(url, {'action': 'delete'})
        self.assertEqual(News.objects.count(), 0)

    # -- slider ordering -----------------------------------------------------

    def test_slider_orders_by_category_and_caps_five(self):
        # 6 visit items (only 5 newest slide), 2 meeting, 1 renovation.
        for i in range(6):
            n = self._publish(self._make_news(
                'visit', title=f'Боздид {i}'), day=10 - i)
            self._add_photo(n)
        for i in range(2):
            n = self._publish(self._make_news(
                'meeting', title=f'Ҷаласа {i}'), day=5 - i)
            self._add_photo(n)
        n = self._publish(self._make_news('renovation', title='Навгонӣ'), day=1)
        self._add_photo(n, order=0)
        self._add_photo(n, order=1, is_main=False)  # 2 photos -> 2 slides
        # excluded statuses
        for status in (News.STATUS_DRAFT, News.STATUS_UNPUBLISHED,
                       News.STATUS_ARCHIVED):
            x = self._make_news('event', status=status, title='Пинҳонӣ')
            self._add_photo(x)

        slides = news_mod.slider_slides()
        # 5 visit + 2 meeting + 2 photos of the renovation news
        self.assertEqual(len(slides), 9)
        cats = [s['category'] for s in slides]
        self.assertEqual(cats[:5], ['Боздидҳо'] * 5)
        self.assertEqual(cats[5:7], ['Ҷаласаҳо'] * 2)
        # every photo of one news becomes its own consecutive slide
        self.assertEqual(cats[7:9], ['Навгониҳо'] * 2)
        self.assertEqual(slides[7]['title'], 'Навгонӣ')
        self.assertEqual(slides[8]['title'], 'Навгонӣ')
        # event + achievement categories are empty -> skipped, no gaps
        # newest visit first (day=10-i -> i=0 is newest)
        self.assertEqual(slides[0]['title'], 'Боздид 0')
        titles = [s['title'] for s in slides]
        self.assertNotIn('Пинҳонӣ', titles)
        self.assertNotIn('Боздид 5', titles)  # 6th/oldest visit dropped

    def test_slider_cache_invalidated_on_save(self):
        n = self._publish(self._make_news(title='Кэш'), day=1)
        self._add_photo(n)
        self.assertEqual(len(news_mod.slider_slides()), 1)
        # cached now; a new publish must bust it via the signal
        m = self._publish(self._make_news(title='Кэш2'), day=2)
        self._add_photo(m)
        slides = news_mod.slider_slides()
        self.assertEqual(len(slides), 2)

    # -- dashboard / archive -------------------------------------------------

    def test_dashboard_shows_flag_when_no_news(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, 'flag.gif')
        self.assertNotContains(resp, 'id="newsSlider"')

    def test_dashboard_shows_slider_when_published(self):
        n = self._publish(self._make_news(title='Слайд'), day=1)
        self._add_photo(n)
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, 'id="newsSlider"')
        self.assertContains(resp, 'Слайд')
        # the flag stays in the header alongside the slider
        self.assertContains(resp, 'flag.gif')

    def test_archive_shows_only_published(self):
        self._publish(self._make_news(title='Намоён'), day=1)
        hidden = self._make_news('event', status='archived', title='Пинҳон')
        self._add_photo(hidden)
        resp = self.client.get(reverse('news_archive'))
        self.assertContains(resp, 'Намоён')
        self.assertNotContains(resp, 'Пинҳон')
        # category filter works
        resp2 = self.client.get(reverse('news_archive'), {'cat': 'event'})
        self.assertNotContains(resp2, 'Намоён')

    def test_detail_public_and_preview_rules(self):
        n = self._publish(self._make_news(title='Оммавӣ'), day=1)
        self._add_photo(n)
        resp = self.client.get(reverse('news_detail', args=[n.pk]))
        self.assertContains(resp, 'Оммавӣ')
        self.assertContains(resp, 'nd-gallery')

        draft = self._make_news(title='Лоиҳа')
        resp = self.client.get(reverse('news_detail', args=[draft.pk]))
        self.assertRedirects(resp, reverse('news_archive'),
                             fetch_redirect_response=False)
        # manager may preview drafts
        self.client.force_login(self.director)
        resp = self.client.get(reverse('news_detail', args=[draft.pk]))
        self.assertContains(resp, 'Пешнамоиш')
