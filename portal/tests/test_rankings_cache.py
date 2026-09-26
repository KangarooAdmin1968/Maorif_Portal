"""Focused tests for the 60-second ranking-result cache (Phase B-2).

Covers: miss->set, hit (zero queries), cached/uncached equivalence,
top-student PRE-LIMIT caching semantics, per-model signal invalidation,
TTL=60, cache-failure fallback, and journal/grade read freshness.
"""
from unittest import mock

from django.core.cache import cache

from portal.models import Grade
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_grade,
    make_quarter_grade, make_school, make_student,
)
from portal.views import (
    RANKING_CACHE_KEYS, RANKING_CACHE_TTL,
    calculate_class_rankings, calculate_school_rankings,
    calculate_subject_rankings, calculate_top_students,
)


def _clear_ranking_cache():
    cache.delete_many(RANKING_CACHE_KEYS)


class RankingCacheTests(GoldenBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Deterministic student pool for the top-students tests:
        # s1 (A, 9.00) > s3 (B, 7.00) > s2 (A, 6.00)
        make_grade(cls.s1, MATH, 9, D_Q1)
        make_grade(cls.s3, MATH, 7, D_Q1)
        make_grade(cls.s2, MATH, 6, D_Q1)

    def setUp(self):
        super().setUp()
        _clear_ranking_cache()

    def tearDown(self):
        _clear_ranking_cache()

    # --- A/B/C: miss -> set -> hit, cached == uncached --------------------

    def test_school_ranking_miss_then_hit_zero_queries(self):
        self.assertIsNone(cache.get('rank:v1:school'))
        first = calculate_school_rankings()
        self.assertIsNotNone(cache.get('rank:v1:school'))
        with self.assertNumQueries(0):
            second = calculate_school_rankings()
        self.assertEqual(first, second)

    def test_class_ranking_miss_then_hit_zero_queries(self):
        self.assertIsNone(cache.get('rank:v1:class:unfiltered'))
        first = calculate_class_rankings()
        self.assertIsNotNone(cache.get('rank:v1:class:unfiltered'))
        with self.assertNumQueries(0):
            second = calculate_class_rankings()
        self.assertEqual(first, second)

    def test_class_ranking_filter_applied_after_cache(self):
        # Cache stores the COMPLETE unfiltered list; school_filter is
        # applied to the cached data without new queries.
        calculate_class_rankings()
        expected = [
            item for item in calculate_class_rankings()
            if item['school_id'] == self.school_a.id
        ]
        with self.assertNumQueries(0):
            filtered = calculate_class_rankings(str(self.school_a.id))
        self.assertEqual(filtered, expected)

    def test_subject_ranking_miss_then_hit_zero_queries(self):
        self.assertIsNone(cache.get('rank:v1:subject'))
        first = calculate_subject_rankings()
        self.assertIsNotNone(cache.get('rank:v1:subject'))
        with self.assertNumQueries(0):
            second = calculate_subject_rankings()
        self.assertEqual(first, second)

    def test_top_students_miss_then_hit_zero_queries(self):
        self.assertIsNone(cache.get('rank:v1:top:prelimit'))
        first = calculate_top_students()
        self.assertIsNotNone(cache.get('rank:v1:top:prelimit'))
        with self.assertNumQueries(0):
            second = calculate_top_students()
        self.assertEqual(first, second)

    # --- D: top-students PRE-FILTER / PRE-LIMIT trap -----------------------

    def test_top_students_prelimit_cache_serves_school_filters(self):
        # Global order: s1 (9.0) > s3 (7.0) > s2 (6.0). B's best (s3) is
        # district rank 2 — if the cache stored the limit=1-truncated list,
        # filtering it by school B would wrongly return nothing.
        calculate_top_students()  # warm the pre-limit cache
        self.assertIsNotNone(cache.get('rank:v1:top:prelimit'))
        with self.assertNumQueries(0):
            only_a = calculate_top_students(str(self.school_a.id), limit=1)
            only_b = calculate_top_students(str(self.school_b.id), limit=1)
        self.assertEqual([d['student_id'] for d in only_a], [self.s1.id])
        self.assertEqual([d['student_id'] for d in only_b], [self.s3.id])

    def test_top_students_limit_applied_after_cache(self):
        calculate_top_students()
        with self.assertNumQueries(0):
            top1 = calculate_top_students(limit=1)
            top2 = calculate_top_students(limit=2)
        self.assertEqual([d['student_id'] for d in top1], [self.s1.id])
        self.assertEqual(
            [d['student_id'] for d in top2], [self.s1.id, self.s3.id])

    # --- E: signal invalidation on all five ranking-input models -----------

    def _warm_all(self):
        calculate_school_rankings()
        calculate_class_rankings()
        calculate_subject_rankings()
        calculate_top_students()
        self.assertTrue(
            all(cache.get(k) is not None for k in RANKING_CACHE_KEYS))

    def _assert_all_keys_cleared(self):
        self.assertTrue(
            all(cache.get(k) is None for k in RANKING_CACHE_KEYS))

    def test_invalidation_on_model_save_and_delete(self):
        writers = [
            ('Grade', lambda: make_grade(self.s1, MATH, 8, D_Q1)),
            ('QuarterGrade',
             lambda: make_quarter_grade(self.s1, '7-А', MATH, 1, grade=8)),
            ('Student',
             lambda: make_student(self.school_a, '8-В', 'Тест Ученик')),
            ('School', lambda: make_school('Мактаб №77')),
            ('ClassSubject',
             lambda: make_class_subject(self.school_a, '8-В', MATH)),
        ]
        for name, create in writers:
            with self.subTest(model=name):
                self._warm_all()
                obj = create()
                self._assert_all_keys_cleared()
                self._warm_all()
                obj.delete()
                self._assert_all_keys_cleared()

    # --- F: TTL ------------------------------------------------------------

    def test_ttl_is_60_seconds(self):
        with mock.patch.object(cache, 'set', wraps=cache.set) as spy:
            calculate_school_rankings()
            calculate_class_rankings()
            calculate_subject_rankings()
            calculate_top_students()
        self.assertEqual(spy.call_count, 4)
        for call in spy.call_args_list:
            self.assertEqual(call.args[2], 60)
            self.assertEqual(call.args[2], RANKING_CACHE_TTL)

    # --- G: journal/grade freshness ----------------------------------------

    def test_grade_save_stays_fresh_and_invalidates_immediately(self):
        calculate_school_rankings()  # warm cache
        g = make_grade(self.s1, MATH, 10, D_Q1)
        # Direct read path used by journal/student pages sees the row now.
        self.assertEqual(Grade.objects.get(pk=g.pk).score, 10)
        # The normal ORM save invalidated the ranking cache instantly.
        self.assertIsNone(cache.get('rank:v1:school'))
        # Recomputed ranking already includes the new score:
        # School A pool = 9 + 6 + 10 = 25 / 3 = 8.33.
        data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.33)

    # --- cache-failure fallback (optional-safe) ----------------------------

    def test_cache_failure_falls_back_to_direct_calculation(self):
        with mock.patch.object(cache, 'get', side_effect=Exception('down')), \
                mock.patch.object(cache, 'set', side_effect=Exception('down')):
            data = calculate_school_rankings()
        self.assertIn(self.school_a.id, [d['school'].id for d in data])
