"""student_detail per-subject chart — quarter-grade consistency.

Regression coverage for the fix that made the personal page show the same
quarter grade as the journal / class report instead of a lifetime blended
average of Grade.score + QuarterGrade rows:

- Daily grades without АҶМ  -> projection uses АТ unchanged (never halved).
- Daily grades + АҶМ       -> АҶЧ = (АТ + АҶМ) / 2.
- Multi-component/row АҶМ  -> each assessment contributes one result.
- Stored QuarterGrade      -> official value wins over the live projection
                              and is not flagged as "Ҷорӣ".
- Quarter isolation        -> rows of other quarters never leak in; a
                              `?quarter=` selection switches the display.
- Empty state              -> no rows for the quarter -> empty chart data,
                              page still renders.
- Read-only                -> opening the page never writes QuarterGrade.
"""
from django.urls import reverse

from portal.models import Assessment, Grade, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, D_Q2, MATH, PERIOD, GoldenBase, lock_quarter,
    make_quarter_grade,
)

GEOGRAPHY = 'ҶУҒРОФИЯ'


def make_ajm_assessment(class_subject, date=D_Q1, quarter=1):
    return Assessment.objects.create(
        class_subject=class_subject, date=date, quarter=quarter,
        category='control', is_ajm=True,
    )


def mk_grade(student, score, date=D_Q1, subject=MATH, components=None,
             assessment=None):
    return Grade.objects.create(
        student=student, subject=subject, score=score, period=PERIOD,
        date=date, components=components, assessment=assessment,
    )


class StudentQuarterChartTests(GoldenBase):
    """student_detail is public — context assertions need no login."""

    def _ctx(self, quarter=1, student=None):
        resp = self.client.get(
            reverse('student_detail', args=[(student or self.s1).id])
            + f'?quarter={quarter}')
        self.assertEqual(resp.status_code, 200)
        return resp.context

    def _subject_scores(self, ctx):
        return dict(zip(ctx['subjects'], ctx['scores']))

    # --- АТ only: no assessment -> quarter value is АТ unchanged ----------

    def test_daily_only_uses_at_unchanged(self):
        for score in (8, 9, 10):
            mk_grade(self.s1, score=score)
        ctx = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx)[MATH], 9)

    # --- АТ + АҶМ -> (АТ + АҶМ) / 2 ---------------------------------------

    def test_daily_plus_assessment_halves_at_ajm(self):
        for score in (9, 9, 9):
            mk_grade(self.s1, score=score)
        a = make_ajm_assessment(self.cs_a)
        mk_grade(self.s1, score=3, assessment=a)
        ctx = self._ctx(quarter=1)
        # АТ = 9, АҶМ = 3 -> АҶЧ = 6. A naive mean of all rows would be 7.5.
        self.assertEqual(self._subject_scores(ctx)[MATH], 6)

    # --- Components / duplicate rows count once per assessment -----------

    def test_multi_component_assessment_counts_once(self):
        mk_grade(self.s1, score=10)
        a = make_ajm_assessment(self.cs_a)
        mk_grade(self.s1, score=8.5, components=[8, 9], assessment=a)
        ctx = self._ctx(quarter=1)
        # АҶМ = 8.5 (one result, not two) -> АҶЧ = (10 + 8.5) / 2 = 9.25 -> 9
        self.assertEqual(self._subject_scores(ctx)[MATH], 9)

    def test_two_rows_same_assessment_count_once(self):
        mk_grade(self.s1, score=10)
        a = make_ajm_assessment(self.cs_a, date=D_Q1_B)
        mk_grade(self.s1, score=8, assessment=a)
        mk_grade(self.s1, score=10, assessment=a)
        ctx = self._ctx(quarter=1)
        # One assessment result: mean(8, 10) = 9 -> АҶЧ = (10 + 9) / 2 = 9.5 -> 10
        self.assertEqual(self._subject_scores(ctx)[MATH], 10)

    # --- Missing assessment is never a zero -------------------------------

    def test_missing_assessment_not_halved_to_zero(self):
        mk_grade(self.s1, score=9)
        mk_grade(self.s1, score=9, date=D_Q1_B)
        ctx = self._ctx(quarter=1)
        value = self._subject_scores(ctx)[MATH]
        self.assertEqual(value, 9)
        self.assertNotEqual(value, 5)  # (9 + 0) / 2 rounded would be 5

    # --- Official QuarterGrade precedence ----------------------------------

    def test_official_quarter_grade_wins_over_projection(self):
        mk_grade(self.s1, score=10)
        mk_grade(self.s1, score=10, date=D_Q1_B)
        # Stored official value deliberately differs from the projection.
        make_quarter_grade(self.s1, quarter=1, grade=4)
        ctx = self._ctx(quarter=1)
        subject_scores = self._subject_scores(ctx)
        self.assertEqual(subject_scores[MATH], 4)
        self.assertNotIn(MATH, {
            s for s, live in zip(ctx['subjects'], ctx['score_live']) if live
        })
        self.assertFalse(ctx['has_live_scores'])

    def test_official_grade_found_for_non_normalized_class_name(self):
        mk_grade(self.s1, score=10)
        make_quarter_grade(self.s1, class_name='7-А', quarter=1, grade=4)
        # Legacy row: bypass Student.save() normalization.
        type(self.s1).objects.filter(pk=self.s1.pk).update(class_name='7-а')
        ctx = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx)[MATH], 4)
        self.assertFalse(ctx['has_live_scores'])

    # --- Live projection is marked when no stored row exists ----------------

    def test_locked_quarter_projects_live_and_flags_it(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        mk_grade(self.s1, score=8)
        mk_grade(self.s1, score=10, date=D_Q1_B)
        ctx = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx)[MATH], 9)
        live = dict(zip(ctx['subjects'], ctx['score_live']))
        self.assertTrue(live[MATH])
        self.assertTrue(ctx['has_live_scores'])

    def test_page_never_writes_quarter_grade(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        mk_grade(self.s1, score=9)
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).exists())
        self._ctx(quarter=1)
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).exists())

    # --- Quarter isolation --------------------------------------------------

    def test_other_quarter_grades_do_not_leak(self):
        mk_grade(self.s1, score=10)                      # Q1
        mk_grade(self.s1, score=2, date=D_Q2)            # Q2
        ctx_q1 = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx_q1)[MATH], 10)
        ctx_q2 = self._ctx(quarter=2)
        self.assertEqual(self._subject_scores(ctx_q2)[MATH], 2)

    def test_assessment_quarter_field_scopes_its_rows(self):
        # A Q2-dated Grade linked to a Q1 assessment belongs to Q1, like
        # the journal's split; the Q2 display stays empty for the subject.
        a = make_ajm_assessment(self.cs_a, date=D_Q2, quarter=1)
        mk_grade(self.s1, score=6, assessment=a)
        ctx_q1 = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx_q1)[MATH], 6)
        ctx_q2 = self._ctx(quarter=2)
        self.assertNotIn(MATH, self._subject_scores(ctx_q2))

    def test_invalid_quarter_param_falls_back_to_current(self):
        mk_grade(self.s1, score=9)
        resp = self.client.get(
            reverse('student_detail', args=[self.s1.id]) + '?quarter=x')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.context['selected_quarter'], (1, 2, 3, 4))

    # --- Empty state ---------------------------------------------------------

    def test_no_grades_renders_empty_state(self):
        ctx = self._ctx(quarter=1)
        self.assertEqual(ctx['subjects'], [])
        self.assertEqual(ctx['scores'], [])
        resp = self.client.get(
            reverse('student_detail', args=[self.s1.id]) + '?quarter=1')
        self.assertContains(resp, 'Дар ин чорак баҳо ворид нашудааст.')

    # --- Confirmed discrepancy scenario -------------------------------------

    def test_matches_journal_value_not_lifetime_average(self):
        # Geography, the reported case: strong daily marks but a very weak
        # control assessment. The journal quarter cell (and the class
        # report) show 3 = std_round((АТ 5 + АҶМ 1) / 2); the old lifetime
        # blend (~4.7) must no longer appear.
        for score in (5, 5, 5, 5, 5):
            mk_grade(self.s1, score=score, subject=GEOGRAPHY)
        a = make_ajm_assessment(self.cs_a)
        mk_grade(self.s1, score=1, subject=GEOGRAPHY, assessment=a)
        ctx = self._ctx(quarter=1)
        self.assertEqual(self._subject_scores(ctx)[GEOGRAPHY], 3)
