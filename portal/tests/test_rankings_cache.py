"""Focused tests for the 60-second ranking-result cache (Phase B-2).

Covers: miss->set, hit (zero queries), cached/uncached equivalence,
top-student PRE-LIMIT caching semantics, per-model signal invalidation,
TTL=60, cache-failure fallback, journal/grade read freshness, and the
bounded-refresh behaviour (dirty stamp, stale shadow, single-flight
lock, refresh-interval rate limit).
"""
import time
from unittest import mock

from django.core.cache import cache
from django.db import transaction

from portal.models import Grade
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_grade,
    make_quarter_grade, make_school, make_student,
)
from portal.views import (
    RANKING_ALL_CACHE_KEYS, RANKING_CACHE_KEYS, RANKING_CACHE_TTL,
    RANKING_DIRTY_KEY, RANKING_STALE_TTL,
    _calculate_school_rankings_uncached,
    calculate_class_rankings, calculate_school_rankings,
    calculate_subject_rankings, calculate_top_students,
)


def _clear_ranking_cache():
    cache.delete_many(RANKING_ALL_CACHE_KEYS)


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
            # Interval=0 makes each warm a real recompute so the
            # assertions stay about invalidation, not rate limiting.
            with self.subTest(model=name), \
                    mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
                self._warm_all()
                obj = create()
                self._assert_all_keys_cleared()
                self.assertIsNotNone(cache.get(RANKING_DIRTY_KEY))
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
        # Each refresh stores the main entry (60s) plus the shadow copy
        # that survives invalidation for rate-limited serving.
        self.assertEqual(spy.call_count, 8)
        for call in spy.call_args_list:
            key = call.args[0]
            expected = (RANKING_CACHE_TTL if key in RANKING_CACHE_KEYS
                        else RANKING_STALE_TTL)
            self.assertEqual(call.kwargs['timeout'], expected)
            self.assertEqual(
                call.kwargs['timeout'],
                60 if key in RANKING_CACHE_KEYS else 600)

    # --- G: journal/grade freshness ----------------------------------------

    def test_grade_save_reflects_within_refresh_window(self):
        calculate_school_rankings()  # warm cache
        g = make_grade(self.s1, MATH, 10, D_Q1)
        # Direct read path used by journal/student pages sees the row now.
        self.assertEqual(Grade.objects.get(pk=g.pk).score, 10)
        # The normal ORM save still drops the main key instantly.
        self.assertIsNone(cache.get('rank:v1:school'))
        # Inside the refresh window the read is rate-limited to the
        # shadow; once the window elapses (patched to 0 here) the
        # recomputed ranking includes the new score with no extra write:
        # School A pool = 9 + 6 + 10 = 25 / 3 = 8.33.
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.33)

    def test_stale_shadow_served_during_refresh_interval(self):
        first = calculate_school_rankings()  # warm cache
        make_grade(self.s1, MATH, 10, D_Q1)  # deletes main + stamps dirty
        self.assertIsNone(cache.get('rank:v1:school'))
        # Readers inside the window get the pre-change shadow — the
        # recompute is rate-limited, not duplicated per save.
        self.assertEqual(calculate_school_rankings(), first)

    def test_burst_saves_do_not_recompute_inside_interval(self):
        calculate_school_rankings()  # warm cache
        with mock.patch(
                'portal.views._calculate_school_rankings_uncached',
                wraps=_calculate_school_rankings_uncached) as spy:
            for _ in range(5):
                make_grade(self.s1, MATH, 8, D_Q1)
                calculate_school_rankings()
            # 5 saves, 5 reads, zero recomputes while the window is open.
            self.assertEqual(spy.call_count, 0)
            # After the window the very next read refreshes once.
            with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
                calculate_school_rankings()
            self.assertEqual(spy.call_count, 1)

    def test_single_flight_lock_serves_shadow_instead_of_duplicating(self):
        first = calculate_school_rankings()  # warm cache
        make_grade(self.s1, MATH, 10, D_Q1)
        # Age the shadow past the interval so a refresh WOULD run, then
        # hold the single-flight lock: readers still get the shadow.
        cache.add('rank:v1:school:computing', 1, 60)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0), \
                mock.patch(
                    'portal.views._calculate_school_rankings_uncached',
                    side_effect=AssertionError('duplicate compute')):
            self.assertEqual(calculate_school_rankings(), first)
        cache.delete('rank:v1:school:computing')
        # Once released, the pending change is picked up.
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.33)

    def test_entry_predating_dirty_stamp_is_not_served_fresh(self):
        # Simulates a save committing mid-refresh: the stored entry's
        # start stamp predates the dirty mark, so it cannot look fresh.
        cache.set('rank:v1:school', (time.time() - 5, ['sentinel']), 60)
        make_grade(self.s1, MATH, 10, D_Q1)
        data = calculate_school_rankings()
        self.assertNotEqual(data, ['sentinel'])
        by_school = {d['school'].id: d for d in data}
        self.assertAlmostEqual(by_school[self.school_a.id]['gpa'], 8.33)

    def test_refresh_failure_serves_shadow_instead_of_error(self):
        first = calculate_school_rankings()  # warm cache
        make_grade(self.s1, MATH, 10, D_Q1)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0), \
                mock.patch(
                    'portal.views._calculate_school_rankings_uncached',
                    side_effect=Exception('db down')):
            self.assertEqual(calculate_school_rankings(), first)

    def test_quarter_grade_and_student_changes_reflect_within_window(self):
        calculate_top_students()
        make_quarter_grade(self.s2, '7-А', MATH, 1, grade=10)  # 6 -> 8.00
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['student_id']: d for d in calculate_top_students()}
        self.assertAlmostEqual(data[self.s2.id]['gpa'], 8.0)
        self.assertEqual(data[self.s2.id]['district_rank'], 2)

        calculate_class_rankings()
        make_student(self.school_b, '8-А', 'Нав Учен')
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            keys = {(d['school_id'], d['class_name'])
                    for d in calculate_class_rankings()}
        self.assertIn((self.school_b.id, '8-А'), keys)

        new_school = make_school('Мактаб №50')
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            ids = {d['school'].id for d in calculate_school_rankings()}
        self.assertIn(new_school.id, ids)

    # --- H2: hardening — non-tuple shadows + commit-time dirty stamp ------

    def test_corrupt_shadow_not_served_while_refresh_runs(self):
        calculate_school_rankings()  # warm -> shadow exists
        make_grade(self.s1, MATH, 10, D_Q1)
        # A malformed shadow must never be indexed/served: with the
        # interval elapsed and the single-flight lock held, the reader
        # falls through to a direct compute instead of returning it.
        cache.set('rank:v1:school:stale', 'corrupt', 60)
        cache.add('rank:v1:school:computing', 1, 60)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0), \
                mock.patch('time.sleep'):  # skip the cold-start spin
            data = {d['school'].id: d for d in calculate_school_rankings()}
        cache.delete('rank:v1:school:computing')
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.33)

    def test_refresh_failure_with_corrupt_shadow_raises_original(self):
        calculate_school_rankings()  # warm
        cache.set('rank:v1:school:stale', 'corrupt', 60)
        make_grade(self.s1, MATH, 10, D_Q1)
        # With a non-tuple shadow the original compute error must
        # propagate instead of returning 'corrupt'[1].
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0), \
                mock.patch(
                    'portal.views._calculate_school_rankings_uncached',
                    side_effect=Exception('db down')):
            with self.assertRaises(Exception) as ctx:
                calculate_school_rankings()
        self.assertEqual(str(ctx.exception), 'db down')

    def test_dirty_stamp_restamped_on_transaction_commit(self):
        # A save inside an atomic block stamps dirty immediately AND
        # queues a commit-time re-stamp, so a refresh that started
        # mid-transaction (pre-commit reads) cannot mark its result fresh.
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                make_grade(self.s1, MATH, 10, D_Q1)
                in_txn_dirty = cache.get(RANKING_DIRTY_KEY)
                # Simulate a refresh entry stamped while the txn was open.
                mid_txn_at = time.time()
                cache.set(
                    'rank:v1:school', (mid_txn_at, ['sentinel']), 60)
        self.assertIsNotNone(in_txn_dirty)
        # The commit-time callback was queued and executed.
        self.assertGreaterEqual(len(callbacks), 1)
        commit_dirty = cache.get(RANKING_DIRTY_KEY)
        self.assertIsNotNone(commit_dirty)
        self.assertGreaterEqual(commit_dirty, mid_txn_at)
        # The mid-transaction entry is not fresh: the next read recomputes
        # and sees the committed row (pool 9+6+10 = 25/3 = 8.33).
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.33)

    def test_rollback_does_not_run_commit_restamp(self):
        with self.captureOnCommitCallbacks() as callbacks:
            try:
                with transaction.atomic():
                    make_grade(self.s1, MATH, 10, D_Q1)
                    raise RuntimeError('force rollback')
            except RuntimeError:
                pass
        # Callbacks queued inside the rolled-back savepoint are discarded.
        self.assertEqual(callbacks, [])

    # --- cache-failure fallback (optional-safe) ----------------------------

    def test_cache_failure_falls_back_to_direct_calculation(self):
        with mock.patch.object(cache, 'get', side_effect=Exception('down')), \
                mock.patch.object(cache, 'set', side_effect=Exception('down')):
            data = calculate_school_rankings()
        self.assertIn(self.school_a.id, [d['school'].id for d in data])
