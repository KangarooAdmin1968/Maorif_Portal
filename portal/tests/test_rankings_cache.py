"""Focused tests for the 60-second ranking-result cache (Phase B-2).

Covers: miss->set, hit (zero queries), cached/uncached equivalence,
top-student PRE-LIMIT caching semantics, per-model signal invalidation,
TTL=60, cache-failure fallback, journal/grade read freshness, and the
bounded-refresh behaviour (dirty stamp, stale shadow, single-flight
lock, refresh-interval rate limit).
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from portal.models import Assessment, Grade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, MATH, GoldenBase, make_class_subject, make_grade,
    make_quarter_grade, make_school, make_student,
)
from portal.views import (
    RANKING_ALL_CACHE_KEYS, RANKING_CACHE_KEYS, RANKING_CACHE_TTL,
    RANKING_GPAS_CACHE_KEY,
    RANKING_DIRTY_KEY, RANKING_LOCK_SUFFIX, RANKING_STALE_SUFFIX,
    RANKING_STALE_TTL, RankingRefreshBusy,
    _all_student_gpas, _calculate_school_rankings_uncached,
    _ranking_cached, _ranking_flock_release, _ranking_flock_try,
    _ranking_lock_path,
    all_student_gpas,
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
        all_student_gpas()
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

    def test_invalidation_on_assessment_save_and_delete(self):
        """Assessment edits re-derive QuarterGrade rows via the bulk recalc,
        whose bulk_create/bulk_update bypass model signals — the Assessment
        signal itself must drop the ranking keys and stamp dirty."""
        a = Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1, quarter=1,
            category='control', is_ajm=True)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            self._warm_all()
            a.is_ajm = False
            a.save()
            self._assert_all_keys_cleared()
            self.assertIsNotNone(cache.get(RANKING_DIRTY_KEY))
            self._warm_all()
            a.delete()
            self._assert_all_keys_cleared()

    # --- F: TTL ------------------------------------------------------------

    def test_ttl_is_60_seconds(self):
        with mock.patch.object(cache, 'set', wraps=cache.set) as spy:
            calculate_school_rankings()
            calculate_class_rankings()
            calculate_subject_rankings()
            calculate_top_students()
        # Each refresh stores the main entry (60s) plus the shadow copy
        # that survives invalidation for rate-limited serving. Lock writes
        # (key:computing, via cache.add -> backend-internal set on
        # FileBasedCache) are a separate mechanism — filtered out.
        data_keys = set(RANKING_CACHE_KEYS) | {
            k + ':stale' for k in RANKING_CACHE_KEYS}
        calls = [c for c in spy.call_args_list if c.args[0] in data_keys]
        self.assertEqual(len(calls), 8)
        for call in calls:
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
        g = make_grade(self.s1, MATH, 10, D_Q1_B)
        # Direct read path used by journal/student pages sees the row now.
        self.assertEqual(Grade.objects.get(pk=g.pk).score, 10)
        # The normal ORM save still drops the main key instantly.
        self.assertIsNone(cache.get('rank:v1:school'))
        # Inside the refresh window the read is rate-limited to the
        # shadow; once the window elapses (patched to 0 here) the
        # recomputed ranking includes the new score with no extra write:
        # School A pool = s1 {9, 10, derived q1=10} + s2 {6, derived q1=6}
        # = 41 / 5 = 8.20.
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.20)

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
        make_grade(self.s1, MATH, 10, D_Q1_B)
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
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.20)

    def test_entry_predating_dirty_stamp_is_not_served_fresh(self):
        # Simulates a save committing mid-refresh: the stored entry's
        # start stamp predates the dirty mark, so it cannot look fresh.
        cache.set('rank:v1:school', (time.time() - 5, ['sentinel']), 60)
        make_grade(self.s1, MATH, 10, D_Q1_B)
        data = calculate_school_rankings()
        self.assertNotEqual(data, ['sentinel'])
        by_school = {d['school'].id: d for d in data}
        self.assertAlmostEqual(by_school[self.school_a.id]['gpa'], 8.20)

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
        make_grade(self.s1, MATH, 10, D_Q1_B)
        # A malformed shadow must never be indexed/served. With the
        # interval elapsed and the refresh blocked, the reader neither
        # returns it nor computes without the lock: it waits, then raises.
        cache.set('rank:v1:school:stale', 'corrupt', 60)
        cache.add('rank:v1:school:computing', 1, 60)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0), \
                mock.patch('portal.views.RANKING_LOCK_WAIT', 0.2), \
                mock.patch(
                    'portal.views._calculate_school_rankings_uncached',
                    side_effect=AssertionError('unlocked compute')):
            with self.assertRaises(RankingRefreshBusy):
                calculate_school_rankings()
        cache.delete('rank:v1:school:computing')
        # Once unblocked, the next reader takes the lock and recomputes.
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.20)

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
                make_grade(self.s1, MATH, 10, D_Q1_B)
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
        # and sees the committed row (pool 9+10+10+6+6 = 41/5 = 8.20).
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            data = {d['school'].id: d for d in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.20)

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

    # --- H3: dashboard while a cold refresh runs in another worker -------

    def test_dashboard_renders_empty_block_when_refresh_busy(self):
        # Cold school ranking, OS lock held elsewhere past the wait: the
        # dashboard renders that block with its {% empty %} row; it never
        # computes without the lock, and the other rankings still render.
        handle = _ranking_flock_try('rank:v1:school')
        self.assertIsInstance(handle, int)
        try:
            with mock.patch('portal.views.RANKING_LOCK_WAIT', 0.1), \
                    mock.patch(
                        'portal.views._calculate_school_rankings_uncached',
                        side_effect=AssertionError('unlocked compute')):
                resp = self.client.get(reverse('dashboard'))
        finally:
            _ranking_flock_release(handle)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['school_ranking'], [])
        self.assertContains(resp, 'Маълумот мавҷуд нест')
        self.assertTrue(resp.context['subject_ranking'])
        self.assertTrue(resp.context['top_students'])

    def test_ranking_ajax_returns_empty_list_when_refresh_busy(self):
        handle = _ranking_flock_try('rank:v1:top:prelimit')
        self.assertIsInstance(handle, int)
        try:
            with mock.patch('portal.views.RANKING_LOCK_WAIT', 0.1), \
                    mock.patch(
                        'portal.views._calculate_top_students_uncached',
                        side_effect=AssertionError('unlocked compute')):
                resp = self.client.get(reverse('top_students_ajax'))
        finally:
            _ranking_flock_release(handle)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_student_detail_shows_placeholders_when_gpa_refresh_busy(self):
        handle = _ranking_flock_try(RANKING_GPAS_CACHE_KEY)
        self.assertIsInstance(handle, int)
        try:
            with mock.patch('portal.views.RANKING_LOCK_WAIT', 0.1), \
                    mock.patch('portal.views._all_student_gpas',
                               side_effect=AssertionError('unlocked compute')):
                resp = self.client.get(
                    reverse('student_detail', args=[self.s1.id]))
        finally:
            _ranking_flock_release(handle)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['gpa_pending'])
        for name in ('gpa', 'class_rank', 'school_rank', 'district_rank'):
            self.assertEqual(resp.context[name], '—')
        # No performance badge is claimed while the GPA is unknown.
        self.assertNotContains(resp, 'class="dynamic-badge')
        self.assertNotContains(resp, 'None')

    def test_readiness_rating_blank_gpa_when_school_refresh_busy(self):
        self.client.force_login(self.admin)
        handle = _ranking_flock_try('rank:v1:school')
        self.assertIsInstance(handle, int)
        try:
            with mock.patch('portal.views.RANKING_LOCK_WAIT', 0.1), \
                    mock.patch(
                        'portal.views._calculate_school_rankings_uncached',
                        side_effect=AssertionError('unlocked compute')):
                resp = self.client.get(reverse('school_readiness_rating'))
        finally:
            _ranking_flock_release(handle)
        self.assertEqual(resp.status_code, 200)
        activity = resp.context['activity']
        self.assertTrue(activity)
        self.assertTrue(all(a['gpa'] is None for a in activity))

    # --- I: student_detail GPA map -----------------------------------------
    # 'rank:v1:gpas' caches the district-wide {student_id: gpa} map that
    # student_detail needs per request. Same bounded-refresh scheme as the
    # dashboard rankings: signal invalidation drops the main key, readers
    # serve the shadow copy inside the refresh window, and one worker
    # recomputes under the single-flight lock.

    def test_gpas_miss_then_hit_zero_queries(self):
        self.assertIsNone(cache.get('rank:v1:gpas'))
        first = all_student_gpas()
        self.assertIsNotNone(cache.get('rank:v1:gpas'))
        with self.assertNumQueries(0):
            second = all_student_gpas()
        self.assertEqual(first, second)

    def test_gpas_cached_matches_uncached(self):
        self.assertEqual(all_student_gpas(), _all_student_gpas())

    def test_gpas_reflect_within_refresh_window(self):
        first = all_student_gpas()  # warm cache
        make_grade(self.s1, MATH, 10, D_Q1_B)
        # The save drops the main key instantly; inside the window the
        # rate-limited shadow is served, after the window the recompute
        # picks up the new row.
        self.assertIsNone(cache.get('rank:v1:gpas'))
        self.assertEqual(all_student_gpas(), first)
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            self.assertEqual(all_student_gpas(), _all_student_gpas())

    def test_student_detail_serves_gpas_from_cache(self):
        url = reverse('student_detail', args=[self.s1.id])
        first = self.client.get(url)  # cold: computes once
        self.assertEqual(first.status_code, 200)
        # Warm cache: the repeat request must not recompute the map.
        with mock.patch(
                'portal.views._all_student_gpas',
                wraps=_all_student_gpas) as spy:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(spy.call_count, 0)


class SchoolRankingMergedQueryTests(GoldenBase):
    """Coverage for the merged QuarterGrade aggregation inside
    _calculate_school_rankings_uncached: one scan producing both the
    quarter (1-4) and attestation (0) totals, preserving the NULL
    semantics of the two former queries."""

    def test_uncached_runs_three_queries(self):
        # Grade scan + merged QuarterGrade scan + School list.
        with self.assertNumQueries(3):
            _calculate_school_rankings_uncached()

    def test_quarter0_row_with_grade_only_is_ignored(self):
        # A quarter=0 row carrying a non-null `grade` (rather than
        # att_grade) matched neither former query and must contribute
        # nothing to the merged one. School B pool: daily 7 + the
        # signal-derived q1=7 -> 14/2 = 7.0.
        make_grade(self.s3, MATH, 7, D_Q1)
        make_quarter_grade(self.s3, '7-Б', MATH, 0, grade=10, att=None)
        data = {d['school'].id: d
                for d in _calculate_school_rankings_uncached()}
        self.assertAlmostEqual(data[self.school_b.id]['gpa'], 7.0)

    def test_null_grade_rows_are_ignored(self):
        # quarter 1-4 rows with grade=NULL matched neither former query;
        # a group holding only such rows produces a NULL aggregate row
        # that _add_score must drop — no contribution, no crash.
        make_grade(self.s3, MATH, 7, D_Q1)
        s_new = make_student(self.school_b, '8-А', 'Нав Хонанда')
        make_quarter_grade(s_new, '8-А', MATH, 1, grade=None)
        make_quarter_grade(self.s3, '7-Б', MATH, 2, grade=None)
        data = {d['school'].id: d
                for d in _calculate_school_rankings_uncached()}
        self.assertAlmostEqual(data[self.school_b.id]['gpa'], 7.0)


class SharedRankingScanTests(GoldenBase):
    """The dashboard refreshes four ranking keys per request; inside
    sharing_ranking_grade_scan() they share one current-year Grade scan
    (504 follow-up) without changing any result or freshness rule."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_grade(cls.s1, MATH, 9, D_Q1)
        make_grade(cls.s1, MATH, 4, D_Q1)  # same cell: canonical dedup
        make_grade(cls.s2, MATH, 7.5, D_Q1_B)
        make_grade(cls.s3, MATH, 6, D_Q1)
        # Excluded populations: non-graded class, non-academic school.
        junior = make_student(cls.school_a, '1-А', 'Хурд Хонанда')
        make_grade(junior, MATH, 10, D_Q1)
        office = make_school('Идораи маориф', type='Идора')
        clerk = make_student(office, '7-А', 'Идора Хонанда')
        make_grade(clerk, MATH, 3, D_Q1)

    def _uncached_all(self):
        from portal.views import (
            _calculate_class_rankings_uncached,
            _calculate_subject_rankings_uncached,
            _calculate_top_students_uncached,
        )
        return [
            _calculate_school_rankings_uncached(),
            _calculate_class_rankings_uncached(),
            _calculate_subject_rankings_uncached(),
            _calculate_top_students_uncached(),
        ]

    def test_shared_scan_results_match_standalone(self):
        from portal import views
        from portal.views import sharing_ranking_grade_scan
        standalone = self._uncached_all()
        with mock.patch('portal.views.canonical_grade_values',
                        wraps=views.canonical_grade_values) as spy:
            with sharing_ranking_grade_scan():
                shared = self._uncached_all()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(shared, standalone)

    def test_dashboard_cold_load_scans_grades_once(self):
        from portal import views
        with mock.patch('portal.views.canonical_grade_values',
                        wraps=views.canonical_grade_values) as spy:
            resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        # Formerly one full scan per ranking key (4).
        self.assertEqual(spy.call_count, 1)
        for key in RANKING_CACHE_KEYS[:4]:
            self.assertIsNotNone(cache.get(key))
        standalone = self._uncached_all()
        self.assertEqual(resp.context['school_ranking'], standalone[0])
        self.assertEqual(resp.context['subject_ranking'], standalone[2])
        self.assertEqual(resp.context['top_students'],
                         calculate_top_students())

    def test_reused_scan_stamps_entry_at_scan_time(self):
        # A save landing after the shared scan must not be masked: the
        # entry computed from the reused rows is stamped no later than
        # the scan, so it predates the dirty stamp and is not fresh.
        from portal.views import sharing_ranking_grade_scan
        with sharing_ranking_grade_scan():
            calculate_school_rankings()  # performs the scan
            make_grade(self.s3, MATH, 10, D_Q1_B)
            calculate_subject_rankings()  # cold: reuses the scan
        entry = cache.get('rank:v1:subject')
        self.assertLess(entry[0], cache.get(RANKING_DIRTY_KEY))
        with mock.patch('portal.views.RANKING_REFRESH_INTERVAL', 0):
            self.assertEqual(
                calculate_subject_rankings(), self._uncached_all()[2])

    def test_no_sharing_outside_scope(self):
        from portal.views import (
            _shared_ranking_grade_rows, sharing_ranking_grade_scan,
        )
        self.assertIsNone(_shared_ranking_grade_rows())
        with sharing_ranking_grade_scan():
            self.assertIsNotNone(_shared_ranking_grade_rows())
        self.assertIsNone(_shared_ranking_grade_rows())


SF_KEY = 'rank:v1:test:singleflight'
SF_KEYS = (SF_KEY, SF_KEY + RANKING_STALE_SUFFIX, SF_KEY + RANKING_LOCK_SUFFIX)

# Child process: try the same non-blocking OS lock the view uses and
# print LOCKED (held elsewhere) or FREE. No Django import needed.
_CHILD_LOCK_PROBE = r'''
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
try:
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    print("FREE")
except OSError:
    print("LOCKED")
'''

# Child process: take the OS lock, report HELD, then sleep until killed
# (process death without an explicit unlock, like a crashed worker).
_CHILD_LOCK_HOLD = r'''
import os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
try:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except ImportError:
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("HELD", flush=True)
time.sleep(1.0)
os._exit(0)
'''


class RankingSingleFlightTests(SimpleTestCase):
    """Cross-worker single-flight for _ranking_cached (504 follow-up).

    Each thread opens its own lock descriptor, exactly like separate
    gunicorn workers, so these exercise the OS lock rather than an
    in-process mutex."""

    def setUp(self):
        cache.delete_many(SF_KEYS)
        cache.delete(RANKING_DIRTY_KEY)

    def tearDown(self):
        cache.delete_many(SF_KEYS)

    def _counting_compute(self, duration=0.0, result='fresh'):
        calls = []
        guard = threading.Lock()

        def compute():
            with guard:
                calls.append(1)
            time.sleep(duration)
            return result
        return compute, calls

    def test_concurrent_cold_misses_compute_once(self):
        # 2.5s > the former 2s cold-start wait, after which every loser
        # used to call compute() itself.
        compute, calls = self._counting_compute(duration=2.5)
        barrier = threading.Barrier(6)
        results, errors = [], []

        def worker():
            try:
                barrier.wait()
                results.append(_ranking_cached(SF_KEY, compute))
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(results, ['fresh'] * 6)
        self.assertEqual(len(calls), 1)

    def test_lock_holder_elsewhere_serves_stale_without_compute(self):
        cache.set(SF_KEY + RANKING_STALE_SUFFIX,
                  (time.time() - 100, 'stale'), RANKING_STALE_TTL)
        handle = _ranking_flock_try(SF_KEY)
        self.assertIsInstance(handle, int)
        try:
            compute, calls = self._counting_compute()
            self.assertEqual(_ranking_cached(SF_KEY, compute), 'stale')
            self.assertEqual(calls, [])
        finally:
            _ranking_flock_release(handle)

    def test_stale_within_interval_served_without_lock(self):
        cache.set(SF_KEY + RANKING_STALE_SUFFIX,
                  (time.time(), 'stale'), RANKING_STALE_TTL)
        compute, calls = self._counting_compute()
        with mock.patch('portal.views._ranking_lock_acquire') as acquire:
            self.assertEqual(_ranking_cached(SF_KEY, compute), 'stale')
        acquire.assert_not_called()
        self.assertEqual(calls, [])

    def test_lock_released_after_exception_without_stale(self):
        def boom():
            raise RuntimeError('db down')
        with self.assertRaises(RuntimeError):
            _ranking_cached(SF_KEY, boom)
        # OS lock is free again and no backoff marker was left behind.
        handle = _ranking_flock_try(SF_KEY)
        self.assertIsInstance(handle, int)
        _ranking_flock_release(handle)
        self.assertIsNone(cache.get(SF_KEY + RANKING_LOCK_SUFFIX))
        compute, calls = self._counting_compute()
        self.assertEqual(_ranking_cached(SF_KEY, compute), 'fresh')
        self.assertEqual(len(calls), 1)

    def test_exception_with_stale_backs_off_and_releases_os_lock(self):
        cache.set(SF_KEY + RANKING_STALE_SUFFIX,
                  (time.time() - 100, 'stale'), RANKING_STALE_TTL)

        def boom():
            raise RuntimeError('db down')
        self.assertEqual(_ranking_cached(SF_KEY, boom), 'stale')
        self.assertIsNotNone(cache.get(SF_KEY + RANKING_LOCK_SUFFIX))
        handle = _ranking_flock_try(SF_KEY)
        self.assertIsInstance(handle, int)
        _ranking_flock_release(handle)
        # During the backoff readers keep the shadow; no retry runs.
        compute, calls = self._counting_compute()
        self.assertEqual(_ranking_cached(SF_KEY, compute), 'stale')
        self.assertEqual(calls, [])

    def test_waiter_takes_over_when_owner_fails(self):
        # Cold start: the owner raises; a waiting reader must take the
        # lock and compute exactly once instead of erroring.
        started = threading.Event()

        def failing():
            started.set()
            time.sleep(0.5)
            raise RuntimeError('owner failed')
        compute, calls = self._counting_compute()
        owner_err, waiter_res = [], []

        def owner():
            try:
                _ranking_cached(SF_KEY, failing)
            except RuntimeError as exc:
                owner_err.append(exc)
        t = threading.Thread(target=owner)
        t.start()
        started.wait(5)
        waiter_res.append(_ranking_cached(SF_KEY, compute))
        t.join(10)
        self.assertEqual(len(owner_err), 1)
        self.assertEqual(waiter_res, ['fresh'])
        self.assertEqual(len(calls), 1)

    def test_waiter_never_duplicates_when_holder_exceeds_wait(self):
        # Holder computes for 1.5s; the waiter's threshold is 0.3s. The
        # waiter must give up with RankingRefreshBusy — never call
        # compute() itself — and the holder's single result still lands.
        compute, calls = self._counting_compute(duration=1.5)
        holder_started = threading.Event()

        def holder_compute():
            holder_started.set()
            return compute()
        holder_res = []
        t = threading.Thread(
            target=lambda: holder_res.append(
                _ranking_cached(SF_KEY, holder_compute)))
        t.start()
        self.assertTrue(holder_started.wait(5))
        with mock.patch('portal.views.RANKING_LOCK_WAIT', 0.3):
            with self.assertRaises(RankingRefreshBusy):
                _ranking_cached(SF_KEY, compute)
        t.join(10)
        self.assertEqual(holder_res, ['fresh'])
        self.assertEqual(len(calls), 1)
        # After the holder finished, readers get its entry, no recompute.
        self.assertEqual(_ranking_cached(SF_KEY, compute), 'fresh')
        self.assertEqual(len(calls), 1)

    def test_waiter_takes_over_after_holder_process_dies(self):
        # A child process takes the OS lock and exits without releasing
        # it (simulated worker death); the kernel frees the lock and the
        # waiting reader computes exactly once.
        path = _ranking_lock_path(SF_KEY)
        child = subprocess.Popen(
            [sys.executable, '-c', _CHILD_LOCK_HOLD, path],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'HELD')
            compute, calls = self._counting_compute()
            with mock.patch('portal.views.RANKING_LOCK_WAIT', 10):
                self.assertEqual(_ranking_cached(SF_KEY, compute), 'fresh')
            self.assertEqual(len(calls), 1)
        finally:
            child.kill()
            child.wait(10)
            child.stdout.close()

    def test_unopenable_lock_file_never_computes(self):
        compute, calls = self._counting_compute()
        with mock.patch('portal.views.os.makedirs',
                        side_effect=PermissionError('read-only')), \
                mock.patch('portal.views.RANKING_LOCK_WAIT', 0.2):
            with self.assertRaises(RankingRefreshBusy):
                _ranking_cached(SF_KEY, compute)
            cache.set(SF_KEY + RANKING_STALE_SUFFIX,
                      (time.time() - 100, 'stale'), RANKING_STALE_TTL)
            self.assertEqual(_ranking_cached(SF_KEY, compute), 'stale')
        self.assertEqual(calls, [])

    def test_sibling_result_reused_after_acquiring_lock(self):
        # A sibling stored a fresh entry between our lookup and lock
        # acquisition: it must be reused, not recomputed.
        from portal import views
        real_acquire = views._ranking_lock_acquire

        def acquire_after_sibling(key):
            cache.set(SF_KEY, (time.time(), 'sibling'), 60)
            return real_acquire(key)
        compute, calls = self._counting_compute()
        with mock.patch('portal.views._ranking_lock_acquire',
                        side_effect=acquire_after_sibling):
            self.assertEqual(_ranking_cached(SF_KEY, compute), 'sibling')
        self.assertEqual(calls, [])

    def test_os_lock_is_exclusive_across_processes(self):
        path = _ranking_lock_path(SF_KEY)

        def probe():
            return subprocess.run(
                [sys.executable, '-c', _CHILD_LOCK_PROBE, path],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        handle = _ranking_flock_try(SF_KEY)
        self.assertIsInstance(handle, int)
        try:
            self.assertEqual(probe(), 'LOCKED')
        finally:
            _ranking_flock_release(handle)
        self.assertEqual(probe(), 'FREE')


class SharedCacheBackendTests(TestCase):
    """Phase-1 504 mitigation: the ranking cache must live in a backend
    shared by every gunicorn worker, in a location that is never served
    publicly and never committed.

    FileBasedCache.add() is a get+set pair and is therefore NOT used as
    the refresh lock; cross-process single-flight is an OS file lock
    (views._ranking_flock_try, covered by RankingSingleFlightTests).
    """

    def test_default_cache_is_file_based(self):
        # Per-process LocMemCache is the regression this phase removes.
        self.assertIn(
            'FileBasedCache', settings.CACHES['default']['BACKEND'])

    def test_cache_location_is_private_runtime_dir(self):
        loc = Path(settings.CACHES['default']['LOCATION']).resolve()
        self.assertIn(settings.BASE_DIR, loc.parents)
        public_roots = [
            Path(settings.STATIC_ROOT).resolve(),
            Path(settings.MEDIA_ROOT).resolve(),
            *[Path(d).resolve() for d in settings.STATICFILES_DIRS],
        ]
        for root in public_roots:
            self.assertFalse(
                loc == root or root in loc.parents,
                f'cache dir must not be under {root}')
        # And it must never be a public root itself.
        self.assertNotIn(loc, public_roots)

    def test_expiry_is_honored(self):
        cache.set('phase1:expiry', 'x', timeout=0)
        self.assertIsNone(cache.get('phase1:expiry'))
        cache.set('phase1:expiry', 'x', timeout=60)
        self.assertEqual(cache.get('phase1:expiry'), 'x')
        cache.delete('phase1:expiry')

    def test_add_semantics_still_work(self):
        # In-process add() is atomic; the cross-process caveat above only
        # weakens the dedup guarantee, never the stored data.
        self.assertTrue(cache.add('phase1:lock', 1, 60))
        self.assertFalse(cache.add('phase1:lock', 2, 60))
        cache.delete('phase1:lock')

    def test_delete_many_clears_every_ranking_key(self):
        _clear_ranking_cache()
        for k in RANKING_ALL_CACHE_KEYS:
            self.assertIsNone(cache.get(k))
