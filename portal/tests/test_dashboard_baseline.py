"""Baseline regression tests for the main dashboard (GET /).

These tests pin the dashboard's contract BEFORE any performance
optimization lands:

  * a warm ranking cache must serve the page without touching the
    Grade/QuarterGrade aggregation queries (Phase B-2 cache must never
    be bypassed or duplicated);
  * the ``?school=`` filter keeps its current semantics (default to the
    user's school, explicit id filters, '0' = district-wide);
  * the anonymous AJAX endpoints keep their JSON field contract — the
    dashboard JS consumes these exact keys.

All tests run on the disposable in-memory test database. Ranking caches
are process-local and live outside the test transaction; GoldenBase.setUp
already clears every ranking key (main, stale, lock, dirty) and each test
warms what it needs explicitly, so no test depends on execution order.
"""
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from portal.tests.helpers import D_Q1, MATH, GoldenBase, make_grade
from portal.views import (
    calculate_class_rankings,
    calculate_school_rankings,
    calculate_subject_rankings,
    calculate_top_students,
)


class DashboardBaselineBase(GoldenBase):
    """GoldenBase plus a deterministic ranked pool for the honor roll:
    s1 (School A) 9.00 > s3 (School B) 7.00 > s2 (School A) 6.00."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_grade(cls.s1, MATH, 9, D_Q1)
        make_grade(cls.s3, MATH, 7, D_Q1)
        make_grade(cls.s2, MATH, 6, D_Q1)

    def _warm_ranking_caches(self):
        """Populate all four public ranking caches via the real wrappers."""
        calculate_school_rankings()
        calculate_class_rankings()
        calculate_subject_rankings()
        calculate_top_students()


class WarmCacheQueryTests(DashboardBaselineBase):
    """B1/B2: a warm cache serves GET / without ranking aggregation work."""

    # The four ranking engines all emit ``values(...).annotate(
    # Sum(...), Count(...))`` over Grade/QuarterGrade, which Django always
    # compiles to ``SUM(...) ... GROUP BY`` on those tables. Plain row
    # SELECTs — and even scalar COUNT(*) stats — do not match this shape,
    # so a future non-ranking dashboard feature can still read grades.
    # What is intentionally flagged: any grouped SUM over the grade
    # tables is precisely the workload the ranking cache exists to serve.
    RANKING_TABLES = ('"portal_grade"', '"portal_quartergrade"')

    @classmethod
    def _is_ranking_aggregation(cls, sql):
        return (
            any(table in sql for table in cls.RANKING_TABLES)
            and 'SUM(' in sql
            and 'GROUP BY' in sql
        )

    def test_warm_dashboard_runs_no_ranking_aggregation_queries(self):
        self._warm_ranking_caches()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        ranking_queries = [
            q['sql'] for q in ctx.captured_queries
            if self._is_ranking_aggregation(q['sql'])
        ]
        # Pattern-based, not an exact total: unrelated per-request queries
        # (schools, counts, parent-portal map) may legitimately change.
        self.assertEqual(ranking_queries, [])

    def test_warm_dashboard_never_invokes_uncached_calculators(self):
        self._warm_ranking_caches()
        with mock.patch.multiple(
            'portal.views',
            _calculate_school_rankings_uncached=mock.DEFAULT,
            _calculate_class_rankings_uncached=mock.DEFAULT,
            _calculate_subject_rankings_uncached=mock.DEFAULT,
            _calculate_top_students_uncached=mock.DEFAULT,
        ) as patched:
            for m in patched.values():
                m.side_effect = AssertionError(
                    'uncached ranking computation ran during a '
                    'warm-cache dashboard render')
            response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        # Even if a compute error were swallowed by the stale-shadow
        # fallback, an invocation is still a regression — assert on calls.
        for name, m in patched.items():
            m.assert_not_called()


class SchoolFilterTests(DashboardBaselineBase):
    """A2/A3: ``selected_school`` / ``?school=`` filter semantics."""

    def test_selected_school_defaults_to_user_school(self):
        self.client.force_login(self.teacher_a)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['selected_school'], str(self.school_a.id))
        rows = response.context['class_ranking']
        self.assertTrue(rows)
        self.assertTrue(
            all(r['school_id'] == self.school_a.id for r in rows))

    def test_school_param_restricts_class_ranking(self):
        response = self.client.get(
            reverse('dashboard'), {'school': str(self.school_b.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {(r['school_id'], r['class_name'])
             for r in response.context['class_ranking']},
            {(self.school_b.id, '7-Б')})

    def test_school_zero_shows_district_wide_class_ranking(self):
        response = self.client.get(
            reverse('dashboard'), {'school': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_school'], '0')
        self.assertEqual(
            {(r['school_id'], r['class_name'])
             for r in response.context['class_ranking']},
            {(self.school_a.id, '7-А'), (self.school_b.id, '7-Б')})


class AjaxRankingContractTests(DashboardBaselineBase):
    """A6: anonymous JSON endpoints keep their current contract."""

    TOP_STUDENT_FIELDS = {
        'student_id', 'full_name', 'class_name', 'school_id',
        'school_name', 'gpa', 'district_rank', 'school_rank',
    }
    CLASS_RANKING_FIELDS = {
        'school_id', 'school_name', 'class_name', 'gpa',
        'district_rank', 'school_rank',
    }

    def test_top_students_endpoint_returns_engine_output(self):
        response = self.client.get(
            reverse('top_students_ajax'), {'school': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        data = response.json()
        # school=0 means unfiltered: the endpoint must return exactly
        # what the public ranking engine computes.
        self.assertEqual(data, calculate_top_students())
        self.assertEqual(
            [d['student_id'] for d in data],
            [self.s1.id, self.s3.id, self.s2.id])
        for item in data:
            self.assertTrue(self.TOP_STUDENT_FIELDS.issubset(item))

    def test_class_rankings_endpoint_filters_by_school(self):
        response = self.client.get(
            reverse('class_rankings_ajax'),
            {'school': str(self.school_a.id)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data, calculate_class_rankings(str(self.school_a.id)))
        self.assertEqual(
            {(d['school_id'], d['class_name']) for d in data},
            {(self.school_a.id, '7-А')})
        for item in data:
            self.assertTrue(self.CLASS_RANKING_FIELDS.issubset(item))
