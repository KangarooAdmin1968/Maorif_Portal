"""Phase 1 PWA support: manifest, icons, and the inert service worker.

The service worker must stay inert (no caching, no fetch interception, no
offline data). These tests also pin the manifest contract needed for the
browser's native Install / Add-to-Home-Screen flow.
"""
import json

from django.contrib.staticfiles.finders import find
from django.test import TestCase
from django.urls import reverse

MANIFEST_STATIC = 'portal/manifest.webmanifest'
SW_STATIC = 'portal/sw.js'
ICON_DIR = 'portal/icons'


class ManifestTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        path = find(MANIFEST_STATIC)
        assert path, f'{MANIFEST_STATIC} not found by staticfiles finders'
        with open(path, encoding='utf-8') as f:
            cls.manifest = json.load(f)

    def test_manifest_is_valid_json_and_reachable_via_static(self):
        self.assertTrue(find(MANIFEST_STATIC))

    def test_manifest_identity(self):
        m = self.manifest
        self.assertEqual(m['name'], 'Маориф')
        self.assertEqual(m['short_name'], 'Маориф')
        self.assertEqual(m['start_url'], '/')
        self.assertEqual(m['scope'], '/')
        self.assertEqual(m['display'], 'standalone')
        self.assertEqual(m['orientation'], 'portrait-primary')
        self.assertEqual(m['lang'], 'tg')
        self.assertEqual(m['dir'], 'ltr')
        self.assertRegex(m['theme_color'], r'^#[0-9a-fA-F]{6}$')
        self.assertRegex(m['background_color'], r'^#[0-9a-fA-F]{6}$')

    def test_manifest_icons_exist_on_disk(self):
        icons = self.manifest['icons']
        sizes = {i['sizes'] for i in icons}
        self.assertIn('192x192', sizes)
        self.assertIn('512x512', sizes)
        purposes = {i['purpose'] for i in icons}
        self.assertIn('maskable', purposes)
        for icon in icons:
            # "src" is an absolute /static/ URL — map it back to the
            # staticfiles-relative path and confirm the file exists.
            self.assertTrue(icon['src'].startswith('/static/'))
            rel = icon['src'][len('/static/'):]
            self.assertTrue(find(rel), f'missing icon file: {rel}')


class ServiceWorkerTests(TestCase):

    def test_sw_url_reverse(self):
        self.assertEqual(reverse('service_worker'), '/sw.js')

    def test_sw_served_at_root_scope(self):
        r = self.client.get('/sw.js')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/javascript')
        self.assertEqual(r['Service-Worker-Allowed'], '/')
        self.assertEqual(r['Cache-Control'], 'no-cache')

    def test_sw_is_inert_no_caching_no_interception(self):
        with open(find(SW_STATIC), encoding='utf-8') as f:
            src = f.read()
        # No CacheStorage, no offline stores, no respondWith interception.
        self.assertNotIn('caches.', src)
        self.assertNotIn('indexedDB', src)
        self.assertNotIn('respondWith', src)
        self.assertNotIn('localStorage', src)


class BaseTemplatePwaTests(TestCase):
    """The canonical base template carries the PWA wiring exactly once."""

    def setUp(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        self.html = r.content.decode()

    def test_manifest_linked(self):
        self.assertIn('rel="manifest"', self.html)
        self.assertIn('portal/manifest.webmanifest', self.html)

    def test_theme_color_and_app_meta(self):
        self.assertIn('name="theme-color" content="#003366"', self.html)
        self.assertIn('mobile-web-app-capable', self.html)
        self.assertIn('apple-mobile-web-app-capable', self.html)
        self.assertIn('apple-mobile-web-app-title', self.html)

    def test_icons_linked(self):
        self.assertIn('apple-touch-icon', self.html)
        self.assertIn('portal/icons/apple-touch-icon.png', self.html)
        self.assertIn('portal/icons/favicon-32.png', self.html)

    def test_sw_registration_guarded_and_lazy(self):
        self.assertIn("'serviceWorker' in navigator", self.html)
        self.assertIn("serviceWorker.register('/sw.js')", self.html)
        # Registered only after page load — never blocks rendering/forms.
        self.assertIn("addEventListener('load'", self.html)
