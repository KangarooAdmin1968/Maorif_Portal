"""Regression tests for the duplicate-today-grades fix and subject-specific
conduct (Тарбия / Хулқ-атвор) isolation.

Issue 1 coverage:
- student_detail 'БАҲОҲОИ ИМРӮЗА' shows one line per logical slot;
  accidental identical duplicate rows collapse for DISPLAY only (DB rows
  are never touched by the read path); legitimate D1/D2, lesson-less and
  assessment slots stay distinctly labelled.
- Today behavior stats dedupe accidental identical rows per slot while
  legitimate different slots keep their own infraction contribution.
- Repeated saves reuse a single Grade row; a later save still merges
  pre-existing duplicates (merge semantics of get_or_create_grade).

Issue 2 coverage:
- grade_entry conduct carry-over only ever prefills from the SAME
  subject — a foreign subject's conduct can never appear in this
  journal (display) or be persisted (batch save).
- Batch save never materializes untouched behavior fields (no phantom
  behavior_score rows, stored values preserved when the key is absent).
- Student.behavior_status keeps the subject-equal aggregate design.
"""
import datetime

from django.urls import reverse

from portal.models import Assessment, Grade, Lesson
from portal.tests.helpers import (
    MATH, PERIOD, TAJIK, GoldenBase, make_grade, make_student,
)
from portal.views import get_date_quarter, get_or_create_grade

AJAX_URL = reverse('save_grade_ajax')
TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)
PHYSICS = 'ФИЗИКА'
HISTORY = 'ТАЪРИХ'


def make_lesson(class_subject, date=TODAY, lesson_number=1, topic=''):
    return Lesson.objects.create(
        class_subject=class_subject, date=date,
        lesson_number=lesson_number, topic=topic,
    )


def make_control(class_subject, date=TODAY, number=None, lesson=None):
    return Assessment.objects.create(
        class_subject=class_subject, date=date,
        quarter=get_date_quarter(date), category='control',
        number=number, lesson=lesson,
    )


def post_daily(client, student, subject=MATH, date=TODAY, extra=None):
    data = {
        'student_id': student.id,
        'subject': subject,
        'date': date.isoformat(),
        'type': 'daily',
    }
    if extra:
        data.update(extra)
    return client.post(AJAX_URL, data)


def grade_in_slot(student, subject=MATH, date=TODAY, score=None,
                  behavior=None, lesson=None, assessment=None):
    g = make_grade(student, subject=subject, score=score, date=date,
                   behavior=behavior)
    if lesson is not None:
        g.lesson = lesson
    if assessment is not None:
        g.assessment = assessment
    if lesson is not None or assessment is not None:
        g.save()
    return g


# ---------------------------------------------------------------------------
# Issue 1 — today grades display
# ---------------------------------------------------------------------------

class TodayGradesDisplayTests(GoldenBase):
    """student_detail is public — no login needed for context assertions."""

    def _ctx(self, student=None):
        resp = self.client.get(
            reverse('student_detail', args=[(student or self.s1).id]))
        return resp.context

    def test_single_grade_shown_once(self):
        make_grade(self.s1, score=10, date=TODAY)
        self.assertEqual(
            self._ctx()['today_grades'],
            [{'subject': MATH, 'score': 10}],
        )

    def test_identical_duplicate_rows_display_once(self):
        # Accidental identical duplicates (same slot, same value) collapse
        # for display only — the rows themselves are never modified.
        make_grade(self.s1, score=10, date=TODAY)
        make_grade(self.s1, score=10, date=TODAY)
        self.assertEqual(
            self._ctx()['today_grades'],
            [{'subject': MATH, 'score': 10}],
        )
        self.assertEqual(
            Grade.objects.filter(student=self.s1, date=TODAY).count(), 2)

    def test_same_slot_different_scores_both_shown(self):
        # Differing values are never silently hidden.
        make_grade(self.s1, score=9, date=TODAY)
        make_grade(self.s1, score=10, date=TODAY)
        self.assertEqual(len(self._ctx()['today_grades']), 2)

    def test_lesson_slot_labelled(self):
        l1 = make_lesson(self.cs_a, TODAY, 1)
        grade_in_slot(self.s1, score=9, lesson=l1)
        self.assertEqual(
            self._ctx()['today_grades'],
            [{'subject': f'{MATH} (дарси 1)', 'score': 9}],
        )

    def test_two_lessons_same_score_not_collapsed(self):
        l1 = make_lesson(self.cs_a, TODAY, 1)
        l2 = make_lesson(self.cs_a, TODAY, 2)
        grade_in_slot(self.s1, score=10, lesson=l1)
        grade_in_slot(self.s1, score=10, lesson=l2)
        entries = self._ctx()['today_grades']
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            {e['subject'] for e in entries},
            {f'{MATH} (дарси 1)', f'{MATH} (дарси 2)'},
        )

    def test_lessonless_and_lesson_slot_distinct(self):
        l1 = make_lesson(self.cs_a, TODAY, 1)
        make_grade(self.s1, score=10, date=TODAY)
        grade_in_slot(self.s1, score=10, lesson=l1)
        labels = [e['subject'] for e in self._ctx()['today_grades']]
        self.assertIn(MATH, labels)
        self.assertIn(f'{MATH} (дарси 1)', labels)

    def test_assessment_slot_labelled(self):
        a = make_control(self.cs_a, TODAY, number=1)
        grade_in_slot(self.s1, score=8, assessment=a)
        self.assertEqual(
            self._ctx()['today_grades'],
            [{'subject': f'{MATH} (Назоратӣ №1)', 'score': 8}],
        )


class TodayBehaviorStatsTests(GoldenBase):

    def _ctx(self):
        resp = self.client.get(reverse('student_detail', args=[self.s1.id]))
        return resp.context

    def test_identical_duplicate_behavior_counted_once(self):
        make_grade(self.s1, behavior=3, date=TODAY)
        make_grade(self.s1, behavior=3, date=TODAY)
        ctx = self._ctx()
        self.assertEqual(ctx['today_total_infractions'], 2)
        self.assertEqual(
            ctx['today_behavior_breakdown'],
            {MATH: {'score': 3, 'infractions': 2}},
        )

    def test_two_lessons_behavior_each_counted(self):
        l1 = make_lesson(self.cs_a, TODAY, 1)
        l2 = make_lesson(self.cs_a, TODAY, 2)
        grade_in_slot(self.s1, behavior=3, lesson=l1)
        grade_in_slot(self.s1, behavior=5, lesson=l2)
        ctx = self._ctx()
        self.assertEqual(ctx['today_total_infractions'], 2)
        self.assertEqual(
            ctx['today_behavior_breakdown'][f'{MATH} (дарси 1)'],
            {'score': 3, 'infractions': 2},
        )
        self.assertEqual(
            ctx['today_behavior_breakdown'][f'{MATH} (дарси 2)'],
            {'score': 5, 'infractions': 0},
        )

    def test_mixed_duplicate_and_other_subjects(self):
        # Identical dup in MATH counts once; a different subject counts on
        # its own — total is 2 infractions, not 4 and not 0.
        make_grade(self.s1, behavior=3, date=TODAY)
        make_grade(self.s1, behavior=3, date=TODAY)
        make_grade(self.s1, subject=TAJIK, behavior=5, date=TODAY)
        ctx = self._ctx()
        self.assertEqual(ctx['today_total_infractions'], 2)
        self.assertEqual(ctx['today_behavior_status'], 'Нигаронкунанда')


# ---------------------------------------------------------------------------
# Issue 1 — save-path deduplication
# ---------------------------------------------------------------------------

class DuplicateSavePreventionTests(GoldenBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_single_save_creates_one_row(self):
        resp = post_daily(self.client, self.s1, extra={'score': '10'})
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Grade.objects.filter(
                student=self.s1, subject=MATH, date=TODAY).count(), 1)

    def test_repeated_identical_save_reuses_row(self):
        for _ in range(2):
            resp = post_daily(self.client, self.s1, extra={'score': '10'})
            self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Grade.objects.filter(
                student=self.s1, subject=MATH, date=TODAY).count(), 1)

    def test_next_save_merges_existing_duplicates(self):
        # Legacy duplicates left by a past race are merged on the next
        # write with the same logical key — merge semantics preserved.
        make_grade(self.s1, score=10, date=TODAY)
        make_grade(self.s1, score=10, date=TODAY)
        resp = post_daily(self.client, self.s1, extra={'score': '9'})
        self.assertTrue(resp.json()['success'])
        rows = Grade.objects.filter(
            student=self.s1, subject=MATH, date=TODAY)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().score, 9)

    def test_get_or_create_repeated_calls_single_row(self):
        g1, created1 = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=TODAY)
        g2, created2 = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=TODAY)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(g1.pk, g2.pk)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 1)

    def test_lesson_and_lessonless_saves_stay_two_rows(self):
        # Intended multi-slot support must not collapse: the same logical
        # key dedupes, different keys never merge.
        l1 = make_lesson(self.cs_a, TODAY, 1)
        post_daily(self.client, self.s1, extra={'score': '10'})
        post_daily(
            self.client, self.s1,
            extra={'score': '9', 'lesson_id': l1.id})
        rows = Grade.objects.filter(student=self.s1, date=TODAY)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(lesson__isnull=True).score, 10)
        self.assertEqual(rows.get(lesson=l1).score, 9)


# ---------------------------------------------------------------------------
# Issue 2 — subject-specific conduct isolation
# ---------------------------------------------------------------------------

class ConductPrefillIsolationTests(GoldenBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])

    def _behavior_default(self):
        resp = self.client.get(self.url, {'date': TODAY.isoformat()})
        return resp.context['behavior_default']

    def test_foreign_subject_never_prefills(self):
        # Physics conduct 3 must not appear in the Algebra journal.
        make_grade(self.s1, subject=PHYSICS, behavior=3, date=YESTERDAY)
        self.assertNotEqual(self._behavior_default()[self.s1.id], 3)
        self.assertEqual(self._behavior_default()[self.s1.id], 5)  # fallback

    def test_same_subject_history_still_carries(self):
        make_grade(self.s1, subject=MATH, behavior=4, date=YESTERDAY)
        make_grade(self.s1, subject=PHYSICS, behavior=3, date=YESTERDAY)
        self.assertEqual(self._behavior_default()[self.s1.id], 4)

    def test_today_same_subject_value_wins(self):
        make_grade(self.s1, subject=MATH, behavior=2, date=TODAY)
        make_grade(self.s1, subject=PHYSICS, behavior=1, date=YESTERDAY)
        self.assertEqual(self._behavior_default()[self.s1.id], 2)

    def test_sticker_prefill_scoped_to_pseudo_subject(self):
        st = make_student(self.school_a, '1-А', 'Синчалак Солеҳов')
        make_grade(st, subject=PHYSICS, behavior=3, date=YESTERDAY)
        make_grade(st, subject='Стикерҳо', behavior=4, date=YESTERDAY)
        resp = self.client.get(
            reverse('sticker_entry', args=[self.school_a.id, '1-А']))
        self.assertEqual(resp.context['behavior_default'][st.id], 4)


class ConductBatchSaveTests(GoldenBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])

    def test_score_only_post_no_phantom_behavior(self):
        # A batch save must not invent a conduct value for edited students.
        resp = self.client.post(self.url, {
            'date': TODAY.isoformat(),
            f'score_{self.s1.id}': '8',
        })
        self.assertEqual(resp.status_code, 302)
        g = Grade.objects.get(student=self.s1, subject=MATH, date=TODAY)
        self.assertEqual(g.score, 8)
        self.assertIsNone(g.behavior_score)

    def test_plain_submit_creates_no_rows(self):
        # Untouched students get no phantom behavior=5 rows.
        self.client.post(self.url, {'date': TODAY.isoformat()})
        self.assertFalse(Grade.objects.filter(date=TODAY).exists())

    def test_absent_behavior_key_preserves_stored_value(self):
        make_grade(self.s1, subject=MATH, behavior=4, date=TODAY)
        self.client.post(self.url, {
            'date': TODAY.isoformat(),
            f'score_{self.s1.id}': '8',
        })
        g = Grade.objects.get(student=self.s1, date=TODAY)
        self.assertEqual(g.score, 8)
        self.assertEqual(g.behavior_score, 4)

    def test_explicit_behavior_written(self):
        self.client.post(self.url, {
            'date': TODAY.isoformat(),
            f'score_{self.s1.id}': '8',
            f'behavior_{self.s1.id}': '3',
        })
        g = Grade.objects.get(student=self.s1, date=TODAY)
        self.assertEqual(g.behavior_score, 3)

    def test_explicit_empty_behavior_clears(self):
        make_grade(self.s1, subject=MATH, score=7, behavior=4, date=TODAY)
        self.client.post(self.url, {
            'date': TODAY.isoformat(),
            f'score_{self.s1.id}': '7',
            f'behavior_{self.s1.id}': '',
        })
        g = Grade.objects.get(student=self.s1, date=TODAY)
        self.assertIsNone(g.behavior_score)


class ConductSubjectIndependenceTests(GoldenBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_changing_physics_never_touches_algebra(self):
        alg = make_grade(self.s1, subject=MATH, behavior=5, date=TODAY)
        make_grade(self.s1, subject=PHYSICS, behavior=3, date=TODAY)
        resp = post_daily(
            self.client, self.s1, subject=PHYSICS,
            extra={'behavior_score': '4'})
        self.assertTrue(resp.json()['success'])
        alg.refresh_from_db()
        self.assertEqual(alg.behavior_score, 5)
        self.assertEqual(
            Grade.objects.get(
                student=self.s1, subject=PHYSICS).behavior_score, 4)

    def test_each_subject_keeps_own_conduct(self):
        make_grade(self.s1, subject=MATH, behavior=5)
        make_grade(self.s1, subject=PHYSICS, behavior=3)
        self.assertEqual(
            Grade.objects.get(
                student=self.s1, subject=MATH).behavior_score, 5)
        self.assertEqual(
            Grade.objects.get(
                student=self.s1, subject=PHYSICS).behavior_score, 3)


class OverallBehaviorTests(GoldenBase):
    """Student.behavior_status: per-subject mean -> mean across subjects."""

    def test_subject_equal_average(self):
        make_grade(self.s1, subject=MATH, behavior=5)
        make_grade(self.s1, subject=PHYSICS, behavior=3)
        make_grade(self.s1, subject=TAJIK, behavior=5)
        make_grade(self.s1, subject=HISTORY, behavior=5)
        # (5 + 3 + 5 + 5) / 4 = 4.5 -> Намунавӣ
        self.assertEqual(self.s1.behavior_status, 'Намунавӣ')

    def test_per_subject_mean_then_across_subjects(self):
        # Physics gets two marks (3 and 5 -> mean 4): the subject still
        # contributes exactly once — (5 + 4) / 2 = 4.5, not (5+3+5)/3.
        make_grade(self.s1, subject=MATH, behavior=5)
        make_grade(self.s1, subject=PHYSICS, behavior=3)
        make_grade(self.s1, subject=PHYSICS, behavior=5)
        self.assertEqual(self.s1.behavior_status, 'Намунавӣ')

    def test_low_average_status(self):
        make_grade(self.s1, subject=MATH, behavior=5)
        make_grade(self.s1, subject=PHYSICS, behavior=1)
        # (5 + 1) / 2 = 3.0 -> Қаноатбахш
        self.assertEqual(self.s1.behavior_status, 'Қаноатбахш')

    def test_no_behavior_placeholder(self):
        self.assertEqual(self.s1.behavior_status, 'Маълумот нест')


# ---------------------------------------------------------------------------
# Frontend pins — the template-side guards this fix depends on
# ---------------------------------------------------------------------------

class JournalFrontendPinTests(GoldenBase):

    def _src(self, name):
        from pathlib import Path
        from django.conf import settings
        return (
            Path(settings.BASE_DIR) / 'portal' / 'templates' / 'portal' / name
        ).read_text(encoding='utf-8')

    def test_inflight_guard_in_grade_entry(self):
        # One edit -> one request: change+blur double-fire is suppressed
        # by the in-flight marker, not by removing the blur handler.
        src = self._src('grade_entry.html')
        self.assertIn("input.dataset.inflight === value", src)
        self.assertIn("input.dataset.inflight = value", src)
        self.assertIn("delete input.dataset.inflight", src)
        self.assertIn("'blur', () => saveDaily(el)", src)

    def test_untouched_behavior_excluded_from_batch_submit(self):
        src = self._src('grade_entry.html')
        # Untouched behavior selects are disabled before the form POST so
        # the carry-over/default prefill is never materialized.
        self.assertIn("'.daily-behavior:not(:disabled)'", src)
        self.assertIn("el.dataset.touched", src)
        self.assertIn("!el.dataset.touched", src)

    def test_inflight_guard_in_sticker_entry(self):
        src = self._src('sticker_entry.html')
        self.assertIn("input.dataset.inflight === value", src)
        self.assertIn("input.dataset.inflight = value", src)
