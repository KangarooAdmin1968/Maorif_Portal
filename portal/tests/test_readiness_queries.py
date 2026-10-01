"""Focused regression tests for the readiness-page zavuch lookup.

The Tab-1 school loop used to issue one ``User`` query per academic school
(``User.objects.filter(username='zavuch_<n>').first()``). It now fetches all
zavuch accounts in a single ``username__in`` query. These tests pin both the
behavior (same ``zavuch_last_login`` semantics) and the query count.
"""
import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .helpers import GoldenBase


class ReadinessZavuchLookupTests(GoldenBase):
    def _get_stats(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('school_readiness_rating'))
        self.assertEqual(resp.status_code, 200)
        return {item['school'].id: item for item in resp.context['stats']}

    def test_zavuch_last_login_present_and_missing(self):
        last_login = timezone.make_aware(
            datetime.datetime(2026, 9, 30, 8, 0, 0))
        self.zavuch_a.last_login = last_login
        self.zavuch_a.save(update_fields=['last_login'])

        stats = self._get_stats()

        # School A ("Мактаб №1") -> username 'zavuch_1' exists -> last_login shown.
        self.assertEqual(stats[self.school_a.id]['zavuch_username'], 'zavuch_1')
        self.assertEqual(stats[self.school_a.id]['zavuch_last_login'], last_login)
        # School B ("Мактаб №2") -> no 'zavuch_2' account -> None, as before.
        self.assertEqual(stats[self.school_b.id]['zavuch_username'], 'zavuch_2')
        self.assertIsNone(stats[self.school_b.id]['zavuch_last_login'])

    def test_zavuch_lookup_is_one_query(self):
        """All zavuch_<n> lookups are served by a single username__in query."""
        with CaptureQueriesContext(connection) as ctx:
            self.client.force_login(self.admin)
            resp = self.client.get(reverse('school_readiness_rating'))
        self.assertEqual(resp.status_code, 200)

        zavuch_queries = [
            q['sql'] for q in ctx.captured_queries
            if '"auth_user"' in q['sql'] and 'zavuch_' in q['sql']
        ]
        self.assertEqual(len(zavuch_queries), 1)
        self.assertIn('IN', zavuch_queries[0].upper())
