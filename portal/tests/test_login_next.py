"""Login redirect / next-parameter contract.

Covers the bug found in manual verification: an unauthenticated visit to a
monthly-journal URL containing Cyrillic class/subject names must land on
login, and a successful login must return to the exact original URL —
without the next= parameter being mangled.
"""
import urllib.parse

from django.test import Client
from django.urls import reverse

from portal.tests.helpers import MATH, GoldenBase

LOGIN_URL = reverse('login')
PW = 'pw_test_123'


class LoginNextTests(GoldenBase):
    def setUp(self):
        super().setUp()
        # reverse() already returns a URI-escaped path (Cyrillic -> %XX).
        self.journal_url = reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH,
        }) + '?month=2025-10'

    def _login(self, next_url, username='teacher_1'):
        return self.client.post(LOGIN_URL, {
            'username': username,
            'password': PW,
            'next': next_url,
        })

    # -- unauthenticated -----------------------------------------------------

    def test_unauthenticated_redirects_to_login_with_next(self):
        resp = self.client.get(self.journal_url)
        self.assertEqual(resp.status_code, 302)
        loc = resp['Location']
        self.assertTrue(loc.startswith(LOGIN_URL))
        parsed = urllib.parse.urlparse(loc)
        next_vals = urllib.parse.parse_qs(parsed.query)['next']
        # Query-decoding once must yield the exact original URL — no extra
        # escaping layers may be baked in.
        self.assertEqual(next_vals[0], self.journal_url)

    def test_next_value_is_not_double_escaped(self):
        resp = self.client.get(self.journal_url)
        loc = resp['Location']
        next_val = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)['next'][0]
        # '%25' inside the decoded value would mean the escape was applied
        # twice (a real % escape should survive exactly once).
        self.assertNotIn('%25', next_val)
        self.assertIn('%D0', next_val)

    # -- login round-trip -----------------------------------------------------

    def test_login_returns_to_monthly_journal(self):
        resp = self._login(self.journal_url)
        self.assertEqual(resp.status_code, 302)
        loc = resp['Location']
        # The Location may be escaped or raw Unicode — both decode to the
        # same path.
        self.assertEqual(urllib.parse.unquote(loc), urllib.parse.unquote(self.journal_url))
        follow = self.client.get(loc)
        self.assertEqual(follow.status_code, 200)
        self.assertTemplateUsed(follow, 'portal/monthly_journal.html')

    def test_login_via_get_next_also_works(self):
        # Browser form posts to /login/?next=... — the query param survives.
        resp = self.client.post(
            LOGIN_URL + '?' + urllib.parse.urlencode({'next': self.journal_url}),
            {'username': 'teacher_1', 'password': PW},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            urllib.parse.unquote(resp['Location']),
            urllib.parse.unquote(self.journal_url),
        )

    def test_login_next_double_escaped_normalized(self):
        # If a proxy delivered next with a second escape layer, the login
        # view must still land on the canonical single-escaped URL.
        double = urllib.parse.quote(self.journal_url, safe='')
        resp = self._login(double)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            urllib.parse.unquote(resp['Location']),
            urllib.parse.unquote(self.journal_url),
        )
        follow = self.client.get(resp['Location'])
        self.assertEqual(follow.status_code, 200)

    # -- safety ----------------------------------------------------------------

    def test_external_next_rejected(self):
        resp = self._login('https://evil.example.com/x')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('dashboard'))

    def test_protocol_relative_next_rejected(self):
        resp = self._login('//evil.example.com/x')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('dashboard'))

    def test_missing_next_goes_to_dashboard(self):
        resp = self.client.post(LOGIN_URL, {
            'username': 'teacher_1', 'password': PW,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('dashboard'))

    def test_wrong_password_stays_on_login(self):
        resp = self._login(self.journal_url)
        resp = self.client.post(LOGIN_URL, {
            'username': 'teacher_1', 'password': 'wrong', 'next': self.journal_url,
        })
        self.assertEqual(resp.status_code, 200)

    # -- authenticated ---------------------------------------------------------

    def test_authenticated_user_opens_journal_directly(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(self.journal_url)
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'portal/monthly_journal.html')

    def test_grade_entry_cyrillic_login_roundtrip(self):
        url = reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH,
        }) + '?date=2025-10-15'
        c = Client()
        resp = c.get(url)
        self.assertEqual(resp.status_code, 302)
        next_val = urllib.parse.parse_qs(
            urllib.parse.urlparse(resp['Location']).query
        )['next'][0]
        resp = c.post(LOGIN_URL, {
            'username': 'teacher_1', 'password': PW, 'next': next_val,
        })
        self.assertEqual(resp.status_code, 302)
        follow = c.get(resp['Location'])
        self.assertEqual(follow.status_code, 200)
        self.assertTemplateUsed(follow, 'portal/grade_entry.html')
