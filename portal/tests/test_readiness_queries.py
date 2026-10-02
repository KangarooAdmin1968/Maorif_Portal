"""Focused regression tests for the readiness-page zavuch lookup.

The Tab-1 school loop used to issue one ``User`` query per academic school
(``User.objects.filter(username='zavuch_<n>').first()``). It now fetches all
zavuch accounts in a single ``username__in`` query. These tests pin both the
behavior (same ``zavuch_last_login`` semantics) and the query count.
"""
import datetime
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .helpers import GoldenBase, make_school, make_teacher_profile, make_user


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


class ReadinessZavuchScopeTests(GoldenBase):
    """Zavuch requests are server-side school-scoped; admin/staff stay
    district-wide. The scope is applied via ``schools``/``school_ids``
    before the expensive downstream queries run.
    """

    def _get(self, user):
        self.client.force_login(user)
        resp = self.client.get(reverse('school_readiness_rating'))
        self.assertEqual(resp.status_code, 200)
        return resp

    def _stats_ids(self, resp):
        return {item['school'].id for item in resp.context['stats']}

    def _activity_ids(self, resp):
        return {item['school'].id for item in resp.context['activity']}

    def test_zavuch_sees_only_own_school(self):
        resp = self._get(self.zavuch_a)
        self.assertEqual(self._stats_ids(resp), {self.school_a.id})
        self.assertEqual(self._activity_ids(resp), {self.school_a.id})

    def test_zavuch_response_omits_other_school_rows(self):
        """Only the own-school row is rendered.

        (The global school *name* still appears inside the shared
        parent-portal modal JSON on every page — so the check targets the
        readiness table rows, not the whole HTML body.)
        """
        resp = self._get(self.zavuch_a)
        body = resp.content.decode('utf-8')
        # Tab 1 renders one "Завуч" cell per stats row; tab 2 one
        # "school-row" per activity row.
        self.assertEqual(body.count('data-label="Завуч"'), 1)
        self.assertEqual(body.count('class="school-row'), 1)
        self.assertContains(resp, self.school_a.name)

    def test_zavuch_own_school_detail_fields_preserved(self):
        """The scoped view keeps the full school detail surface: teacher
        rows, subjects, hours/norm, grades, fulfillment %, journal status."""
        make_teacher_profile(self.teacher_a, self.school_a,
                             full_name='Сангинов Аҳмад')
        resp = self._get(self.zavuch_a)
        row = next(i for i in resp.context['activity']
                   if i['school'].id == self.school_a.id)

        for key in ('students', 'total_grades', 'fulfillment', 'gpa',
                    'rank', 'is_mine', 'teachers'):
            self.assertIn(key, row)
        self.assertTrue(row['is_mine'])

        teacher = row['teachers'][0]
        self.assertEqual(teacher['name'], 'Сангинов Аҳмад')
        for key in ('subjects', 'hours', 'min_grades', 'grades_done',
                    'fulfillment', 'journal'):
            self.assertIn(key, teacher)
        # 7-А МАТЕМАТИКА is norm-bearing -> a journal row exists even
        # without entered grades (status 'empty').
        self.assertEqual(len(teacher['journal']), 1)
        self.assertEqual(teacher['journal'][0]['subject'], 'МАТЕМАТИКА')
        self.assertEqual(teacher['journal'][0]['status'], 'empty')

    def test_zavuch_queries_never_reference_other_schools(self):
        """Every school_id IN (...) filter must contain only own school."""
        with CaptureQueriesContext(connection) as ctx:
            resp = self._get(self.zavuch_a)
        leaks = []
        for q in ctx.captured_queries:
            for m in re.finditer(r'school_id" IN \(([^)]*)\)', q['sql']):
                ids = {x.strip() for x in m.group(1).split(',')}
                if str(self.school_b.id) in ids:
                    leaks.append(q['sql'])
        self.assertEqual(leaks, [])

    def test_zavuch_scoped_response_is_smaller_than_admin(self):
        admin_resp = self._get(self.admin)
        zavuch_resp = self._get(self.zavuch_a)
        self.assertEqual(len(admin_resp.context['stats']), 2)
        self.assertEqual(len(zavuch_resp.context['stats']), 1)
        self.assertLess(len(zavuch_resp.content), len(admin_resp.content))

    def test_admin_sees_all_schools(self):
        resp = self._get(self.admin)
        self.assertEqual(self._stats_ids(resp),
                         {self.school_a.id, self.school_b.id})
        self.assertEqual(self._activity_ids(resp),
                         {self.school_a.id, self.school_b.id})

    def test_staff_sees_all_schools(self):
        rais = make_user('rais_1', role='director', staff=True)
        resp = self._get(rais)
        self.assertEqual(self._stats_ids(resp),
                         {self.school_a.id, self.school_b.id})

    def test_zavuch_without_school_gets_empty_result(self):
        orphan = make_user('zavuch_9', role='teacher', school=None)
        resp = self._get(orphan)
        self.assertEqual(list(resp.context['stats']), [])
        self.assertEqual(list(resp.context['activity']), [])

    def test_zavuch_bound_to_nonacademic_school_gets_empty(self):
        kindergarten = make_school(
            'Кӯдакистони Гулбарг',
            type='Муассисаи давлатии таълимии томактабӣ')
        zavuch_kg = make_user('zavuch_8', role='teacher', school=kindergarten)
        resp = self._get(zavuch_kg)
        self.assertEqual(list(resp.context['stats']), [])
        self.assertEqual(list(resp.context['activity']), [])

    def test_zavuch_label_is_singular(self):
        resp = self._get(self.zavuch_a)
        self.assertContains(resp, '🏆 Рейтинг<')
        self.assertNotContains(resp, 'Рейтинги муассисаҳо')

    def test_admin_label_is_plural(self):
        resp = self._get(self.admin)
        self.assertContains(resp, 'Рейтинги муассисаҳо')

    def test_staff_label_is_plural(self):
        rais = make_user('rais_1', role='director', staff=True)
        resp = self._get(rais)
        self.assertContains(resp, 'Рейтинги муассисаҳо')
