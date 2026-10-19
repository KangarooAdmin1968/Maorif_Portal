"""Automatic derived quarter grades — regression tests.

QuarterGrade rows for quarters 1-4 are a derived aggregate of the source
Grade rows (daily + control-assessment results). portal.signals recalculates
the affected student/subject/quarter whenever a source row is added, edited,
or deleted — no manual recalculation step is needed. The same signals protect
the data: quarter grades cannot be created, overwritten, or deleted through
any teacher grade-entry endpoint, while the attestation mark (quarter 0)
stays teacher-entered and feeds the derived Ҷамъбастӣ.
"""
import datetime
import io
import json
import re
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse

from portal.models import Assessment, Grade, QuarterGrade, QuarterLock
from portal.tests.helpers import (
    D_PREV_Q1, D_Q1, D_Q1_B, D_Q2, MATH, PERIOD, TAJIK, GoldenBase,
    lock_quarter, make_class_subject, make_grade, make_quarter_grade,
)
from portal.tests.test_phase4_lessons import (
    make_control, post_assessment, post_grade,
)
from portal.views import _live_quarter_payload, academic_year_start

AJAX_URL = reverse('save_grade_ajax')
DAILY_SAVE_URL = reverse('daily_save')
MONTHLY_SAVE_URL = reverse('monthly_save')
DELETE_URL = reverse('assessment_delete')


def qg(student, quarter, subject=MATH, year=None):
    return QuarterGrade.objects.filter(
        student=student, subject=subject, quarter=quarter,
        academic_year=year if year is not None else academic_year_start(),
    ).first()


# ---------------------------------------------------------------------------
# Automatic recalculation on source writes
# ---------------------------------------------------------------------------


class AutoQuarterWriteTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_daily_save_writes_quarter_grade(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 9)  # mean(8, 10)

    def test_daily_edit_recalculates(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        post_grade(self.client, self.s1, score='10', date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 10)

    def test_deleting_last_source_removes_quarter_row(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertIsNotNone(qg(self.s1, 1))
        post_grade(self.client, self.s1, score='', date=D_Q1)
        self.assertIsNone(qg(self.s1, 1))

    def test_half_up_rounding_at_write(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 10)  # 9.5 -> 10

    def test_ajm_plus_at_halved_mean(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        a = make_control(self.cs_a, is_ajm=True)
        post_grade(self.client, self.s1, score='8/9', assessment=a)
        # AT = 9, АҶМ = 8.5 -> АҶЧ = 8.75 -> 9
        self.assertEqual(qg(self.s1, 1).grade, 9)

    def test_missing_control_mark_not_zero(self):
        # No АҶМ at all: the quarter grade is the daily average alone —
        # missing control results are never treated as zero.
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        # A non-АҶМ control grade counts as a daily result, not a second pool.
        a = make_control(self.cs_a, is_ajm=False)
        post_grade(self.client, self.s1, score='7', assessment=a)
        self.assertEqual(qg(self.s1, 1).grade, 8)  # mean(8, 10, 7)

    def test_ajm_edit_updates_quarter(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        a = make_control(self.cs_a, is_ajm=True)
        post_grade(self.client, self.s1, score='10', assessment=a)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        post_grade(self.client, self.s1, score='4', assessment=a)
        # AT = 8, АҶМ = 4 -> 6
        self.assertEqual(qg(self.s1, 1).grade, 6)

    def test_ajm_toggle_recalculates_without_grade_writes(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        post_grade(self.client, self.s1, score='9', date=D_Q1_B)
        a = make_control(self.cs_a, is_ajm=False)
        post_grade(self.client, self.s1, score='5', assessment=a)
        # All-current pool: mean(9, 9, 5) = 7.67 -> 8
        self.assertEqual(qg(self.s1, 1).grade, 8)
        count = Grade.objects.count()
        resp = post_assessment(self.client, self.cs_a, assessment=a,
                               is_ajm=True)
        self.assertTrue(resp.json()['success'])
        self.assertEqual(Grade.objects.count(), count)
        # Pool split without any Grade write: AT = 9, АҶМ = 5 -> 7
        self.assertEqual(qg(self.s1, 1).grade, 7)

    def test_assessment_delete_recalculates(self):
        a = make_control(self.cs_a, is_ajm=True)
        post_grade(self.client, self.s1, score='8', date=D_Q1_B)
        post_grade(self.client, self.s1, score='10', assessment=a)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        self.client.post(DELETE_URL, {'assessment_id': a.id})
        self.assertEqual(qg(self.s1, 1).grade, 8)

    def test_monthly_save_writes_quarter_grade(self):
        resp = self.client.post(MONTHLY_SAVE_URL, {'payload': json.dumps([{
            'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'score': '8',
        }])})
        self.assertTrue(resp.json()['success'])
        self.assertEqual(qg(self.s1, 1).grade, 8)

    def test_daily_save_batch_writes_and_reports_quarter(self):
        resp = self.client.post(DAILY_SAVE_URL, {'payload': json.dumps([
            {'key': 'a', 'student_id': self.s1.id, 'subject': MATH,
             'date': D_Q1.isoformat(), 'score': '8'},
            {'key': 'b', 'student_id': self.s1.id, 'subject': MATH,
             'date': D_Q1_B.isoformat(), 'score': '10'},
        ])})
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(qg(self.s1, 1).grade, 9)
        # The response carries the recalculated derived cells so the page
        # refreshes read-only quarter cells without a reload.
        entry = data['students'][str(self.s1.id)]
        self.assertEqual(
            entry['quarters'], [{'quarter': 1, 'value': 9, 'live': False}])
        self.assertEqual(entry['semi_annual_1'], 9)

    def test_other_students_subjects_quarters_untouched(self):
        make_quarter_grade(self.s2, quarter=1, grade=10)
        make_quarter_grade(self.s1, quarter=2, grade=7)
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        # teacher_a is only assigned MATH — the other-subject source row is
        # created via ORM; the signal derives its quarter independently.
        make_grade(self.s1, subject=TAJIK, score=8, date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        self.assertEqual(qg(self.s1, 1, subject=TAJIK).grade, 8)
        self.assertEqual(qg(self.s2, 1).grade, 10)
        self.assertEqual(qg(self.s1, 2).grade, 7)
        self.assertIsNone(qg(self.s1, 3))
        self.assertIsNone(qg(self.s3, 1))

    def test_school_isolation(self):
        self.client.force_login(self.teacher_b)
        post_grade(self.client, self.s3, score='9', date=D_Q1)
        self.assertEqual(qg(self.s3, 1).grade, 9)
        self.assertIsNone(qg(self.s1, 1))

    def test_locked_quarter_not_recalculated(self):
        # A locked quarter keeps its stored value frozen even when source
        # rows change underneath (lock-first ordering is the realistic flow).
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        make_grade(self.s1, score=9, date=D_Q1)
        self.assertIsNone(qg(self.s1, 1))
        make_quarter_grade(self.s1, quarter=1, grade=10)
        make_grade(self.s1, score=5, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 10)

    def test_locked_quarter_still_blocks_source_writes(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertFalse(resp.json()['success'])
        self.assertIsNone(qg(self.s1, 1))


# ---------------------------------------------------------------------------
# Manual writes to derived grades are rejected everywhere
# ---------------------------------------------------------------------------


class DerivedGradeProtectionTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.entry_url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])

    def _quarterly(self, quarter, score):
        return self.client.post(AJAX_URL, {
            'student_id': self.s1.id, 'subject': MATH,
            'type': 'quarterly', 'quarter': str(quarter), 'score': score,
        })

    def test_teacher_cannot_write_quarter_grade(self):
        for q in (1, 2, 3, 4):
            resp = self._quarterly(q, '10')
            self.assertFalse(resp.json()['success'], f'quarter {q}')
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1).exists())

    def test_admin_cannot_write_quarter_grade_via_teacher_endpoint(self):
        # The ordinary journal endpoints reject derived-grade writes for
        # every role — there is no manual path to a stored quarter value.
        self.client.force_login(self.admin)
        resp = self._quarterly(1, '10')
        self.assertFalse(resp.json()['success'])
        self.assertFalse(qg(self.s1, 1))

    def test_teacher_cannot_delete_quarter_grade(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertIsNotNone(qg(self.s1, 1))
        resp = self._quarterly(1, '')
        self.assertFalse(resp.json()['success'])
        self.assertEqual(qg(self.s1, 1).grade, 8)

    def test_batch_post_quarter_keys_ignored(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.client.post(self.entry_url, {
            'date': D_Q1.isoformat(),
            f'score_{self.s1.id}': '7',
            f'q_1_{self.s1.id}': '3',
            f'q_2_{self.s1.id}': '2',
        })
        # The forged q_* values never land — the batch edit flows through
        # signals so q1 re-derives to 7, and q2 is not created.
        self.assertEqual(qg(self.s1, 1).grade, 7)
        self.assertIsNone(qg(self.s1, 2))

    def test_attestation_still_teacher_writable(self):
        # The attestation mark is a source input to Ҷамъбастӣ, not a derived
        # grade — both write paths keep working.
        resp = self._quarterly(0, '9')
        self.assertTrue(resp.json()['success'])
        self.assertEqual(qg(self.s1, 0).att_grade, 9)
        self.client.post(self.entry_url, {
            'date': D_Q1.isoformat(),
            f'att_{self.s1.id}': '7',
        })
        self.assertEqual(qg(self.s1, 0).att_grade, 7)

    def test_journal_renders_quarter_cells_disabled(self):
        resp = self.client.get(self.entry_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for name in (f'q_1_{self.s1.id}', f'q_4_{self.s1.id}'):
            self.assertIn(f'name="{name}"', html)
        # Every quarter cell is disabled; the attestation input is not.
        import re
        for m in re.finditer(
                r'<input[^>]*name="q_\d_[^"]+"[^>]*>', html):
            self.assertIn('disabled', m.group(0))
        for m in re.finditer(
                r'<input[^>]*name="att_[^"]+"[^>]*>', html):
            self.assertNotIn('disabled', m.group(0))


# ---------------------------------------------------------------------------
# Derived periods: semi-annual / annual / final stay read-time computed
# ---------------------------------------------------------------------------


class DerivedPeriodTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.entry_url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])

    def test_derived_periods_recomputed_in_context(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q2)
        resp = self.client.get(self.entry_url)
        calc = resp.context['calculated'][self.s1.id]
        # {Q1: 8, Q2: 10} -> Нимсола 1 = 9, projected annual/final = 9.
        self.assertEqual(calc['semi_annual_1'], 9)
        self.assertEqual(calc['annual'], 9)
        self.assertEqual(calc['final'], 9)

    def test_attestation_feeds_final(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        self._quarterly_att(9)
        resp = self.client.get(self.entry_url)
        calc = resp.context['calculated'][self.s1.id]
        # annual = 9 (projected), att = 9 -> final = 9
        self.assertEqual(calc['final'], 9)

    def test_attestation_changes_final(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        self._quarterly_att(6)
        resp = self.client.get(self.entry_url)
        calc = resp.context['calculated'][self.s1.id]
        # annual 9, att 6 -> final = (9 + 6) / 2 = 7.5 -> 8
        self.assertEqual(calc['final'], 8)

    def _quarterly_att(self, score):
        return self.client.post(AJAX_URL, {
            'student_id': self.s1.id, 'subject': MATH,
            'type': 'quarterly', 'quarter': '0', 'score': str(score),
        })


# ---------------------------------------------------------------------------
# Failed recalculation must not leave a misleading stale stored quarter row
# ---------------------------------------------------------------------------


class FailedRecalcTests(GoldenBase):
    """Every journal display prefers a stored QuarterGrade over the live
    projection. If automatic recalculation fails after the source write
    committed, the stale stored row is dropped so the cell falls back to
    the live-projected value — never a silently wrong official-looking
    number. Locked quarters keep their frozen row.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.entry_url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])

    def test_failed_recalc_drops_stale_row_on_save(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        with patch('portal.views.recalculate_quarter_grade',
                   side_effect=RuntimeError('boom')):
            resp = post_grade(self.client, self.s1, score='4', date=D_Q1)
        # The source save itself still succeeded.
        self.assertTrue(resp.json()['success'])
        # The stale stored row is gone — the journal falls back to the
        # live projection (4), flagged as live.
        self.assertIsNone(qg(self.s1, 1))
        resp = self.client.get(self.entry_url)
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 4)
        self.assertIn(1, resp.context['live_quarter_flags'][self.s1.id])

    def test_failed_recalc_drops_stale_row_on_delete(self):
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        with patch('portal.views.recalculate_quarter_grade',
                   side_effect=RuntimeError('boom')):
            resp = post_grade(self.client, self.s1, score='', date=D_Q1)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(qg(self.s1, 1))
        # The surviving source still projects live.
        resp = self.client.get(self.entry_url)
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 10)

    def test_failed_recalc_keeps_locked_quarter(self):
        # The stored row on a locked quarter is the frozen official value —
        # a recalc failure must never delete it.
        make_quarter_grade(self.s1, quarter=1, grade=10)
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        with patch('portal.views.recalculate_quarter_grade',
                   side_effect=RuntimeError('boom')):
            make_grade(self.s1, score=5, date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 10)

    def test_failed_bulk_recalc_drops_stale_rows_on_assessment_toggle(self):
        # The non-deferred assessment path recalcs via the bulk helper; its
        # failure drops the affected stored rows the same way.
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        a = make_control(self.cs_a, is_ajm=False)
        post_grade(self.client, self.s1, score='5', assessment=a)
        self.assertIsNotNone(qg(self.s1, 1))
        with patch('portal.views.recalculate_quarter_grades_bulk',
                   side_effect=RuntimeError('boom')):
            resp = post_assessment(
                self.client, self.cs_a, assessment=a, is_ajm=True)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(qg(self.s1, 1))
        # Live projection still reflects the sources (mean(9, 5) -> 7).
        resp = self.client.get(self.entry_url)
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 7)

    def test_monthly_save_bulk_failure_still_succeeds_and_drops_stale(self):
        # monthly_save commits cells outside a transaction: a failed bulk
        # recalc must neither 500 nor leave the stale stored row behind.
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        with patch('portal.views.recalculate_quarter_grades_bulk',
                   side_effect=RuntimeError('boom')):
            resp = self.client.post(MONTHLY_SAVE_URL, {'payload': json.dumps([{
                'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
                'date': D_Q1_B.isoformat(), 'score': '10',
            }])})
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(qg(self.s1, 1))
        resp = self.client.get(self.entry_url)
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 9)

    def test_daily_save_bulk_failure_rolls_back_loudly(self):
        # daily_save drains inside transaction.atomic() — a recalc failure
        # rolls the whole batch back and reports failure; nothing stale
        # can persist because nothing was committed.
        with patch('portal.views.recalculate_quarter_grades_bulk',
                   side_effect=RuntimeError('boom')):
            resp = self.client.post(DAILY_SAVE_URL, {'payload': json.dumps([{
                'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
                'date': D_Q1.isoformat(), 'score': '8',
            }])})
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())
        self.assertIsNone(qg(self.s1, 1))

    def test_drop_stale_bulk_preserves_locked_and_attestation(self):
        # The bulk stale-row sweep is the safety net behind every deferred
        # drain — it must keep locked quarter rows (frozen official values)
        # and never touch the teacher-entered attestation row (quarter 0),
        # while still clearing stale unlocked quarter rows.
        from portal.views import drop_stale_quarter_grades
        make_quarter_grade(self.s1, quarter=1, grade=8)
        make_quarter_grade(self.s1, quarter=2, grade=7)
        make_quarter_grade(self.s1, quarter=0, att=9)
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        drop_stale_quarter_grades([
            (self.s1.id, MATH, 1),   # locked — must survive
            (self.s1.id, MATH, 2),   # unlocked — dropped
            (self.s1.id, MATH, 0),   # attestation key — filtered out anyway
        ])
        self.assertEqual(qg(self.s1, 1).grade, 8)
        self.assertIsNone(qg(self.s1, 2))
        self.assertEqual(qg(self.s1, 0).att_grade, 9)

    def test_failed_contribution_check_drops_stale_row(self):
        # When the contribution pipeline itself raises before any recalc
        # ran, the outer signal guard drops the stale stored row so the
        # journal falls back to the live projection. The fallback used to
        # compare the period against a misspelled 'Холои' literal and was
        # dead code — a stale row would have survived silently.
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        with patch('portal.views.grade_component_result',
                   side_effect=RuntimeError('boom')):
            resp = post_grade(self.client, self.s1, score='4', date=D_Q1_B)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(qg(self.s1, 1))

    def test_drop_stale_bulk_respects_school_isolation(self):
        # Locks are school-scoped: a school-B lock must not shield school
        # A's stale row, and sweeping only s1's key must leave school B's
        # row untouched. School B's own lock does shield its own row.
        from portal.views import drop_stale_quarter_grades
        make_quarter_grade(self.s1, quarter=1, grade=8)            # school A
        make_quarter_grade(self.s3, class_name='7-Б',
                           quarter=1, grade=6)                     # school B
        lock_quarter(self.school_b, '7-Б', MATH, quarter=1)
        drop_stale_quarter_grades([(self.s1.id, MATH, 1)])
        self.assertIsNone(qg(self.s1, 1))                          # dropped
        self.assertEqual(qg(self.s3, 1).grade, 6)                  # untouched
        drop_stale_quarter_grades([(self.s3.id, MATH, 1)])
        self.assertEqual(qg(self.s3, 1).grade, 6)                  # locked row kept


# ---------------------------------------------------------------------------
# Student moves re-derive stored quarters (QuerySet.update bypasses signals)
# ---------------------------------------------------------------------------


class MoveStudentQuarterTests(GoldenBase):
    """_move_student re-links Grade and QuarterGrade rows via
    QuerySet.update(), which bypasses the recalc signals — the move itself
    re-derives every moved (student, subject, quarter) so a stale stored
    row cannot follow a renamed or transferred student."""

    def _move(self, student, new_class='7-А', new_name='Фирӯз Раҳимов II',
              relabel=True):
        from portal.views import _move_student
        return _move_student(
            student.school, student, new_class, new_name,
            relabel_quarter_grades=relabel,
        )

    def test_move_refreshes_stale_stored_quarter(self):
        # The confirmed production shape: daily rows {3, 8} beside a stored
        # quarter of 3 — the move must re-derive 6.
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        make_quarter_grade(self.s1, quarter=1, grade=3)  # stale stored row
        new = self._move(self.s1)
        self.assertEqual(qg(new, 1).grade, 6)

    def test_move_derives_quarter_when_none_stored(self):
        make_grade(self.s1, score=9, date=D_Q1)
        QuarterGrade.objects.filter(student=self.s1).delete()
        new = self._move(self.s1)
        self.assertEqual(qg(new, 1).grade, 9)

    def test_move_without_source_rows_drops_stale(self):
        make_quarter_grade(self.s1, quarter=1, grade=3)  # no source rows
        new = self._move(self.s1)
        self.assertIsNone(qg(new, 1))

    def test_move_into_locked_quarter_keeps_stored_value(self):
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        make_quarter_grade(self.s1, quarter=1, grade=3)
        lock_quarter(self.school_a, '7-Б', MATH, quarter=1)
        new = self._move(self.s1, new_class='7-Б', new_name='Фирӯз Раҳимов')
        row = QuarterGrade.objects.get(
            student=new, class_name='7-Б', subject=MATH, quarter=1)
        self.assertEqual(row.grade, 3)  # frozen by the target lock

    def test_promotion_keeps_history_row_and_derives_new(self):
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        make_quarter_grade(self.s1, quarter=1, grade=3)  # stale under 7-А
        new = self._move(self.s1, new_class='8-А',
                         new_name='Фирӯз Раҳимов', relabel=False)
        # relabel=False preserves the prior-class row verbatim (history)...
        self.assertEqual(QuarterGrade.objects.get(
            student=new, class_name='7-А', subject=MATH, quarter=1).grade, 3)
        # ...while the current class gets its own derived row.
        self.assertEqual(QuarterGrade.objects.get(
            student=new, class_name='8-А', subject=MATH, quarter=1).grade, 6)


# ---------------------------------------------------------------------------
# Journal rendering — quarter cells must stay display-only with no leaks
# ---------------------------------------------------------------------------


class QuarterJournalRenderTests(GoldenBase):
    """Regression: the template used a multi-line {# ... #} comment, but
    Django treats {# as single-line only — the English implementation note
    was emitted literally into every Ч1-Ч4 cell. The block is now a
    {% comment %}; these tests pin down that nothing internal renders and
    that every quarter input stays disabled."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.url = reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH])
        post_grade(self.client, self.s1, score='8', date=D_Q1)
        post_grade(self.client, self.s1, score='10', date=D_Q1_B)

    def test_no_implementation_text_in_rendered_journal(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for leak in ('derived automatically', 'display-only',
                     'q_* keys', 'never posted', '{#'):
            self.assertNotIn(leak, html)

    def test_quarter_cells_render_disabled_with_derived_value(self):
        html = self.client.get(self.url).content.decode()
        # s1's derived Q1 = mean(8, 10) = 9 — rendered in a disabled input.
        m = re.search(
            r'<input[^>]*name="q_1_%s"[^>]*>' % self.s1.id, html)
        self.assertIsNotNone(m)
        self.assertIn('disabled', m.group(0))
        self.assertIn('value="9"', m.group(0))
        # Every quarter input on the page is disabled — none editable.
        tags = re.findall(r'<input[^>]*name="q_\d+_[^"]+"[^>]*>', html)
        self.assertTrue(tags)
        self.assertTrue(all('disabled' in tag for tag in tags))


# ---------------------------------------------------------------------------
# Recalc coverage gaps — source-row identity changes must refresh BOTH cells
# ---------------------------------------------------------------------------


class RecalcGapTests(GoldenBase):
    """Moving a contribution between quarter cells or between students
    re-derives the cell it LEFT as well as the cell it joined — otherwise
    the abandoned cell keeps a stored value that counts a contribution
    which no longer exists."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_assessment_quarter_move_recalcs_old_quarter(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        a = make_control(self.cs_a, is_ajm=True)
        post_grade(self.client, self.s1, score='5', assessment=a)
        self.assertEqual(qg(self.s1, 1).grade, 7)  # (9 + 5) / 2
        # ORM/admin edit: the assessment moves to quarter 2. Its grade
        # re-buckets, so BOTH quarters must be re-derived.
        a.quarter = 2
        a.save()
        self.assertEqual(qg(self.s1, 1).grade, 9)  # АҶМ left the pool
        self.assertEqual(qg(self.s1, 2).grade, 5)  # and joined Q2

    def test_assessment_quarter_move_recalcs_every_linked_student(self):
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        post_grade(self.client, self.s2, score='7', date=D_Q1)
        a = make_control(self.cs_a, is_ajm=True)
        post_grade(self.client, self.s1, score='5', assessment=a)
        post_grade(self.client, self.s2, score='3', assessment=a)
        self.assertEqual(qg(self.s1, 1).grade, 7)
        self.assertEqual(qg(self.s2, 1).grade, 5)
        a.quarter = 2
        a.save()
        self.assertEqual(qg(self.s1, 1).grade, 9)
        self.assertEqual(qg(self.s1, 2).grade, 5)
        self.assertEqual(qg(self.s2, 1).grade, 7)
        self.assertEqual(qg(self.s2, 2).grade, 3)

    def test_grade_reassigned_with_same_score_recalcs(self):
        # The trap shape: identical contribution on a different owner —
        # without a student check in `moved` the handler early-returns and
        # NEITHER student's stored quarter refreshes.
        g = make_grade(self.s1, score=9)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        g.student = self.s2
        g.save()
        self.assertIsNone(qg(self.s1, 1))          # source left s1
        self.assertEqual(qg(self.s2, 1).grade, 9)  # source joined s2

    def test_grade_reassigned_changes_both_pools(self):
        g = make_grade(self.s1, score=8)
        make_grade(self.s2, score=6)
        self.assertEqual(qg(self.s1, 1).grade, 8)
        self.assertEqual(qg(self.s2, 1).grade, 6)
        g.student = self.s2
        g.save()
        self.assertIsNone(qg(self.s1, 1))
        self.assertEqual(qg(self.s2, 1).grade, 7)  # mean(8, 6)


# ---------------------------------------------------------------------------
# audit_quarter_grades — read-only stored-vs-derived discrepancy report
# ---------------------------------------------------------------------------


class AuditQuarterGradesCommandTests(GoldenBase):
    """The audit re-derives every cell through the runtime pipeline and
    reports disagreements without writing anything."""

    def _run(self, *args):
        # Fixtures live in the current academic year (D_Q1 etc. resolve
        # dynamically in helpers); pin --year so the window matches them
        # regardless of the calendar date the suite runs on.
        out = io.StringIO()
        call_command('audit_quarter_grades', '--year', str(_AY), *args,
                     stdout=out)
        return out.getvalue()

    def test_clean_data_reports_no_findings(self):
        make_grade(self.s1, score=9)
        make_grade(self.s1, score=7, date=D_Q1_B)
        out = self._run()
        self.assertIn('STALE: 0', out)
        self.assertIn('ORPHAN: 0', out)
        self.assertIn('MISSING: 0', out)
        self.assertIn('FROZEN-CONFLICT: 0', out)

    def test_stale_stored_row_reported_not_repaired(self):
        make_grade(self.s1, score=9)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        # Simulate out-of-band staleness (raw update bypasses signals).
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1).update(grade=4)
        out = self._run()
        self.assertIn('STALE', out)
        self.assertIn('захирашуда=4', out)
        self.assertIn('ҳисобшуда=9', out)
        # Read-only contract: the stored row is left untouched.
        self.assertEqual(qg(self.s1, 1).grade, 4)

    def test_orphan_row_reported(self):
        make_quarter_grade(self.s1, quarter=2, grade=7)  # no source rows
        out = self._run()
        self.assertIn('ORPHAN', out)

    def test_missing_stored_row_reported(self):
        make_grade(self.s1, score=9)
        QuarterGrade.objects.filter(student=self.s1, quarter=1).delete()
        out = self._run()
        self.assertIn('MISSING', out)

    def test_locked_conflict_reported_separately(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        make_grade(self.s1, score=9)          # locked: no recalc ran
        make_quarter_grade(self.s1, quarter=1, grade=4)  # frozen value
        out = self._run()
        self.assertIn('FROZEN-CONFLICT', out)
        self.assertNotIn('STALE ', out)
        # The frozen value is never rewritten by the audit.
        self.assertEqual(qg(self.s1, 1).grade, 4)

    def test_stale_label_row_reported(self):
        # History row under a prior class label — invisible to every
        # display and recalc key.
        make_quarter_grade(self.s1, class_name='6-А', quarter=1, grade=8)
        out = self._run()
        self.assertIn('STALE-LABEL', out)

    def test_duplicate_normalized_key_reported(self):
        make_grade(self.s1, score=9)
        make_quarter_grade(self.s1, quarter=1, grade=9)
        # bulk_create bypasses save() normalization — '7-a' normalizes to
        # '7-А', so the pair collides on the recalc key while the raw
        # unique_together lets both rows exist.
        QuarterGrade.objects.bulk_create([QuarterGrade(
            student=self.s1, class_name='7-a', subject=MATH,
            quarter=1, grade=4, academic_year=_AY)])
        out = self._run()
        self.assertIn('DUPLICATE', out)

    def test_audit_is_read_only(self):
        make_grade(self.s1, score=9)
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1).update(grade=4)
        before = list(QuarterGrade.objects.order_by('pk').values_list(
            'pk', 'grade', 'att_grade'))
        self._run()
        after = list(QuarterGrade.objects.order_by('pk').values_list(
            'pk', 'grade', 'att_grade'))
        self.assertEqual(before, after)

    def test_legacy_attestation_row_reported(self):
        # A NULL-year attestation mark is teacher-entered — no source can
        # establish its year — so the audit lists it for manual
        # attribution with row id, student id, class, subject and value.
        att = make_quarter_grade(self.s1, quarter=0, att=9,
                                 academic_year=None)
        out = self._run()
        self.assertIn('AMBIGUOUS-ATTESTATION', out)
        self.assertIn(f'хол={att.pk}', out)
        self.assertIn('атт=9', out)
        self.assertIn('AMBIGUOUS-ATTESTATION: 1', out)
        att.refresh_from_db()
        self.assertEqual(att.att_grade, 9)          # value preserved
        self.assertIsNone(att.academic_year)        # never auto-stamped

    def test_year_stamped_attestation_not_flagged(self):
        make_quarter_grade(self.s1, quarter=0, att=9, academic_year=_AY)
        make_quarter_grade(self.s2, quarter=0, att=8,
                           academic_year=_AY - 1)
        out = self._run()
        self.assertIn('AMBIGUOUS-ATTESTATION: 0', out)


# The fixtures' academic year (D_Q1 etc. resolve dynamically in helpers)
# and the NEXT academic year — the --year target for the out-of-year
# scoping tests below.
_AY = academic_year_start()
_NEXT_AY = _AY + 1
D_NEXT_Q1 = datetime.date(_NEXT_AY, 10, 15)     # inside its Q1
D_NEXT_Q2 = datetime.date(_NEXT_AY + 1, 1, 20)  # inside its Q2


class AuditAcademicYearScopeTests(GoldenBase):
    """--year scopes the comparison to one academic year so historical
    rows cannot masquerade as current-year errors."""

    def _run(self, *args):
        out = io.StringIO()
        call_command('audit_quarter_grades', *args, stdout=out)
        return out.getvalue()

    def test_legacy_null_row_is_ambiguous_not_stale(self):
        # A stored row without an established year is flagged for manual
        # attribution under EVERY audited year — never compared as a
        # confirmed error of that year.
        make_grade(self.s1, score=9, date=D_Q1)   # current academic year
        make_quarter_grade(self.s1, quarter=1, grade=4,
                           academic_year=None)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('AMBIGUOUS: 1', out)
        self.assertIn('STALE: 0', out)
        out = self._run('--year', str(_AY))
        self.assertIn('AMBIGUOUS: 1', out)

    def test_row_of_other_year_not_compared_under_selected_year(self):
        # The signal-stamped current-year row belongs to its own year:
        # auditing the next year does not compare it at all.
        make_grade(self.s1, score=9, date=D_Q1)
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1,
            academic_year=_AY).update(grade=4)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('STALE: 0', out)
        self.assertIn('AMBIGUOUS: 0', out)

    def test_mixed_year_sources_use_only_selected_year(self):
        # Out-of-year 3 + selected-year 9: the selected-year derivation
        # must see 9, not the mixed pool (6).
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=9, date=D_NEXT_Q1)
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1,
            academic_year=_NEXT_AY).update(grade=9)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('STALE: 0', out)
        self.assertIn('OUT-OF-YEAR-ROWS', out)
        # And when the stored value does disagree with the selected-year
        # pool, it is a genuine in-window STALE.
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1,
            academic_year=_NEXT_AY).update(grade=6)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('ҳисобшуда=9', out)
        self.assertIn('STALE: 1', out)

    def test_orphan_year_stamped_row_with_no_same_year_support(self):
        # Stamped next year but nothing in that year backs the cell.
        make_quarter_grade(self.s1, quarter=1, grade=7,
                           academic_year=_NEXT_AY)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('ORPHAN: 1', out)

    def test_stored_row_without_any_quarter_support_is_orphan(self):
        # Rows exist for the cell but bucket only to other quarters —
        # nothing supports the stored quarter in any year.
        make_grade(self.s1, score=9, date=D_Q2)      # quarter 2 only
        make_quarter_grade(self.s1, quarter=1, grade=7)
        out = self._run('--year', str(_AY))
        self.assertIn('ORPHAN: 1', out)

    def test_missing_uses_current_year_pool(self):
        make_grade(self.s1, score=9, date=D_NEXT_Q1)
        QuarterGrade.objects.filter(student=self.s1).delete()
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('MISSING: 1', out)
        out = self._run('--year', str(_AY))
        self.assertIn('MISSING: 0', out)

    def test_conflict_assessment_date_outside_quarter_window(self):
        # Declared quarter 1 but dated inside the Q2 window — flagged.
        make_control(self.cs_a, date=D_NEXT_Q2, quarter=1)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('CONFLICT-ASSESSMENT: 1', out)

    def test_conflict_assessment_linked_row_outside_year(self):
        # Assessment dated inside its own quarter, but a linked grade
        # row sits outside the academic year — flagged via the row.
        a = make_control(self.cs_a, date=D_NEXT_Q1, quarter=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, score=8,
            period='Холҳои ҷорӣ (Онлайн)', date=D_Q1, assessment=a)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('CONFLICT-ASSESSMENT: 1', out)

    def test_assessment_quarter_still_authoritative_within_year(self):
        # Linked row dated in the Q1 window but assigned to Q2 by its
        # assessment — it must contribute to Q2, not Q1.
        a = make_control(self.cs_a, date=D_NEXT_Q2, quarter=2)
        Grade.objects.create(
            student=self.s1, subject=MATH, score=8,
            period='Холҳои ҷорӣ (Онлайн)', date=D_NEXT_Q1, assessment=a)
        # The signal already stored the derived Q2 row — remove it so the
        # audit reports the cell as MISSING under its real quarter.
        QuarterGrade.objects.filter(student=self.s1).delete()
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('чорак=2', out)
        self.assertNotIn('чорак=1', out)

    def test_locked_conflict_against_selected_year(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_NEXT_AY)
        make_grade(self.s1, score=9, date=D_NEXT_Q1)
        make_quarter_grade(self.s1, quarter=1, grade=4,
                           academic_year=_NEXT_AY)
        out = self._run('--year', str(_NEXT_AY))
        self.assertIn('FROZEN-CONFLICT: 1', out)


# ---------------------------------------------------------------------------
# Runtime year scoping — recalc and live projection see only current-year rows
# ---------------------------------------------------------------------------


class RuntimeAcademicYearScopeTests(GoldenBase):
    """QuarterGrade rows carry an explicit September-start academic_year:
    prior-year sources write their own year-stamped row and never feed
    this year's quarter, and a prior-year stored row can neither
    masquerade as the current-year value nor be overwritten by it."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_daily_3_and_8_derive_to_6(self):
        # The confirmed School No. 20 shape: daily 3 + 8, no АҶМ —
        # AT = 5.5, half-up -> 6.
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 6)
        self.assertEqual(qg(self.s1, 1).academic_year, _AY)

    def test_prior_year_q1_grade_excluded_from_current_q1(self):
        # Same month, previous academic year — it writes its own
        # year-stamped row and stays out of the current-year pool.
        make_grade(self.s1, score=10, date=D_PREV_Q1)
        make_grade(self.s1, score=4, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 6)  # mean(4, 8), not 7.33
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 10)

    def test_prior_year_only_sources_write_only_prior_year_row(self):
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        self.assertIsNone(qg(self.s1, 1))              # current year
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 9)

    def test_same_label_two_years_produce_separate_rows(self):
        # 6-Б in both years: Q1 results for each year are separate
        # records — no slot collision, no overwrite.
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        prior = qg(self.s1, 1, year=_AY - 1)
        self.assertEqual(prior.grade, 9)
        self.assertEqual(prior.class_name, '7-А')
        make_grade(self.s1, score=4, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 6)
        prior.refresh_from_db()          # prior-year value untouched
        self.assertEqual(prior.grade, 9)

    def test_current_year_grade_does_not_overwrite_prior_year_row(self):
        # A stored prior-year value under the SAME class label survives
        # the arrival of current-year sources unchanged.
        make_quarter_grade(self.s1, quarter=1, grade=9,
                           academic_year=_AY - 1)
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 6)
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 9)

    def test_prior_year_support_leaves_current_year_row_untouched(self):
        # Editing only prior-year sources must not disturb a stored
        # current-year value for the same class/quarter.
        make_quarter_grade(self.s1, quarter=1, grade=9)
        g = make_grade(self.s1, score=5, date=D_PREV_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 9)
        g.score = 7                       # re-trigger the prior-year cell
        g.save()
        self.assertEqual(qg(self.s1, 1).grade, 9)
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 7)

    def test_orphan_row_removed_when_year_support_is_gone(self):
        make_quarter_grade(self.s1, quarter=1, grade=9)
        g = make_grade(self.s1, score=8, date=D_Q1)
        g.delete()  # re-trigger the cell: no same-year support left
        self.assertIsNone(qg(self.s1, 1))
        # ...while a stored row of a different year is never touched by
        # the same-year cell cleanup.
        make_quarter_grade(self.s1, quarter=1, grade=9,
                           academic_year=_AY - 1)
        h = make_grade(self.s1, score=8, date=D_Q1)
        h.delete()
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 9)

    def test_linked_row_lands_in_assessment_year(self):
        # Assessment.quarter stays authoritative for the bucket AND the
        # assessment's own date sets the academic year — a prior-year
        # assessment's linked row lands in the prior year even when the
        # row itself carries a current-year date (the audit flags the
        # mismatch as a conflict).
        a = make_control(self.cs_a, date=D_PREV_Q1, quarter=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, score=10,
            period=PERIOD, date=D_Q1, assessment=a)
        self.assertIsNone(qg(self.s1, 1))              # current year
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 10)

    def test_grade_date_move_refreshes_both_year_cells(self):
        # Moving a source row across the Sep boundary re-derives BOTH
        # the year it left and the year it entered.
        g = make_grade(self.s1, score=8, date=D_Q1)
        make_grade(self.s1, score=6, date=D_Q1_B)
        self.assertEqual(qg(self.s1, 1).grade, 7)      # mean(8, 6)
        g.date = D_PREV_Q1
        g.save()
        self.assertEqual(qg(self.s1, 1).grade, 6)      # only 6 remains
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 8)

    def test_attestation_row_stamped_with_current_year(self):
        resp = self.client.post(AJAX_URL, {
            'student_id': self.s1.id, 'subject': MATH,
            'type': 'quarterly', 'quarter': '0', 'score': '9',
        })
        self.assertTrue(resp.json()['success'])
        row = QuarterGrade.objects.get(student=self.s1, quarter=0)
        self.assertEqual(row.academic_year, _AY)
        self.assertEqual(row.att_grade, 9)

    def test_live_projection_matches_stored_recalc(self):
        # Same sources -> same value: drop the derived row out-of-band and
        # the live bridge projects exactly what the signals wrote (6).
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        QuarterGrade.objects.filter(student=self.s1, quarter=1).delete()
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertEqual(qmap[1], 6)
        self.assertIn(1, live_flags)

    def test_live_projection_ignores_prior_year_rows(self):
        make_grade(self.s1, score=10, date=D_PREV_Q1)
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertNotIn(1, qmap)

    def test_prior_year_stored_row_not_shown_as_current_year(self):
        # The regression the schema change fixes: a stored prior-year
        # value under the same class label no longer wins the official
        # slot — the live projection derives from current-year sources.
        make_quarter_grade(self.s1, quarter=1, grade=7,
                           academic_year=_AY - 1)
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertNotIn(1, qmap)                     # no sources yet
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertEqual(qmap[1], 6)                  # derived 6, not 7

    def test_legacy_attestation_cannot_influence_current_year(self):
        # A NULL-year attestation mark is invisible to every current-year
        # reader: journal att cell, Ҷамъбастӣ, and student GPA.
        make_quarter_grade(self.s1, quarter=0, att=9,
                           academic_year=None)
        qmap, _ = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertIsNone(qmap['att'])
        make_grade(self.s1, score=8, date=D_Q1)
        # Ҷамъбастӣ = annual when no confirmed-year attestation exists —
        # the legacy 9 must not enter (8 + 9) / 2 = 8.5 -> 9.
        qmap, _ = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertIsNone(qmap['att'])
        self.assertEqual(qmap[1], 8)
        from portal.views import calc_quarterly
        result = calc_quarterly(qmap)
        self.assertEqual(result['final'], 8)  # annual alone, not 9

    def test_year_stamped_attestation_coexists_with_legacy(self):
        # A confirmed current-year attestation and a legacy NULL row for
        # the same cell are different records — the stamped one wins the
        # display, the legacy value is preserved untouched.
        legacy = make_quarter_grade(self.s1, quarter=0, att=9,
                                    academic_year=None)
        resp = self.client.post(AJAX_URL, {
            'student_id': self.s1.id, 'subject': MATH,
            'type': 'quarterly', 'quarter': '0', 'score': '7',
        })
        self.assertTrue(resp.json()['success'])
        qmap, _ = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertEqual(qmap['att'], 7)
        legacy.refresh_from_db()
        self.assertEqual(legacy.att_grade, 9)
        self.assertIsNone(legacy.academic_year)

    def test_legacy_null_row_never_masquerades_as_current(self):
        # A row without an established year stays legacy: displays ignore
        # it, the audit flags it AMBIGUOUS, and a same-year recalc neither
        # overwrites nor deletes it.
        make_quarter_grade(self.s1, quarter=1, grade=7,
                           academic_year=None)
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertNotIn(1, qmap)
        make_grade(self.s1, score=4, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        qmap, live_flags = _live_quarter_payload(
            self.s1, MATH, self.s1.class_name)
        self.assertEqual(qmap[1], 6)                  # derived 6, not 7
        legacy = QuarterGrade.objects.get(
            student=self.s1, quarter=1, academic_year__isnull=True)
        self.assertEqual(legacy.grade, 7)             # preserved

    def test_same_label_legacy_row_audited_ambiguous(self):
        """6-Б in both years, legacy row: a stored row whose year cannot
        be established (academic_year=NULL) is preserved untouched by
        recalcs and reported AMBIGUOUS — never STALE — so a repair
        decision cannot mistake it for a confirmed error."""
        make_quarter_grade(self.s1, quarter=1, grade=7,
                           academic_year=None)
        g = make_grade(self.s1, score=10, date=D_PREV_Q1)
        g.score = 6
        g.save()
        legacy = QuarterGrade.objects.get(
            student=self.s1, quarter=1, academic_year__isnull=True)
        self.assertEqual(legacy.grade, 7)
        out = io.StringIO()
        call_command(
            'audit_quarter_grades', '--year', str(academic_year_start()),
            '--no-lines', stdout=out)
        out = out.getvalue()
        self.assertIn('AMBIGUOUS: 1', out)
        self.assertIn('STALE: 0', out)

    def test_journal_live_bridge_ignores_prior_year_rows(self):
        # The grade_entry bridge shows the same year-scoped projection:
        # only the current-year rows produce a Q1 cell.
        make_grade(self.s1, score=10, date=D_PREV_Q1)
        make_grade(self.s1, score=8, date=D_Q1)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]))
        self.assertEqual(
            resp.context['quarter_grades'][self.s1.id][1], 8)

    def test_journal_official_uses_year_matched_stored_row(self):
        # Prior-year stored row + current-year derived row under the same
        # label: the journal displays the current year's official value.
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        make_grade(self.s1, score=8, date=D_Q1)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]))
        self.assertEqual(
            resp.context['quarter_grades'][self.s1.id][1], 8)
        self.assertNotIn(
            1, resp.context['live_quarter_flags'][self.s1.id])

    def test_student_gpa_ignores_prior_year_grade(self):
        from portal.views import _all_student_gpas
        make_grade(self.s2, score=10, date=D_PREV_Q1)
        make_grade(self.s2, score=8, date=D_Q1)
        # mean(current-year 8, derived quarter 8) = 8.00 — the
        # prior-year 10 must not lift the current-year average.
        self.assertEqual(_all_student_gpas()[self.s2.id], 8.00)

    def test_student_gpa_ignores_prior_year_stored_row(self):
        # A stored prior-year value under the same class label does not
        # feed the current-year GPA (the pre-schema leak).
        from portal.views import _all_student_gpas
        make_quarter_grade(self.s2, quarter=1, grade=10,
                           academic_year=_AY - 1)
        make_grade(self.s2, score=8, date=D_Q1)
        # vals: current-year stored q1=8 + current-year source mean 8
        self.assertEqual(_all_student_gpas()[self.s2.id], 8.00)


# ---------------------------------------------------------------------------
# Year-scoped quarter locks
# ---------------------------------------------------------------------------


class QuarterLockYearScopeTests(GoldenBase):
    """Locks identify (school, class, subject, academic_year, quarter):
    the same quarter is independently lockable per year, a previous-year
    lock never freezes the current quarter, and legacy NULL-year locks
    apply only to the year of their own locked_at date."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_same_quarter_independently_lockable_per_year(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_AY - 1)
        from portal.views import is_quarter_locked
        self.assertTrue(is_quarter_locked(
            self.school_a, '7-А', MATH, 1, year=_AY - 1))
        self.assertFalse(is_quarter_locked(
            self.school_a, '7-А', MATH, 1, year=_AY))
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_AY)
        self.assertTrue(is_quarter_locked(
            self.school_a, '7-А', MATH, 1, year=_AY))

    def test_prior_year_lock_does_not_lock_current_quarter(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_AY - 1)
        resp = post_grade(self.client, self.s1, score='8', date=D_Q1)
        self.assertTrue(resp.json()['success'])
        self.assertEqual(qg(self.s1, 1).grade, 8)

    def test_prior_year_lock_does_not_freeze_current_recalc(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_AY - 1)
        make_grade(self.s1, score=8, date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 8)

    def test_current_year_lock_keeps_frozen_value(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        make_quarter_grade(self.s1, quarter=1, grade=10)
        make_grade(self.s1, score=5, date=D_Q1)
        self.assertEqual(qg(self.s1, 1).grade, 10)
        # ...but a prior-year cell of the same class/subject/quarter is
        # not frozen by the current-year lock.
        g = make_grade(self.s1, score=7, date=D_PREV_Q1)
        self.assertEqual(qg(self.s1, 1, year=_AY - 1).grade, 7)

    def test_legacy_lock_attributed_by_locked_at_year(self):
        # NULL-year lock created last academic year: it locks its own
        # year only — not the current one, not every future year.
        from django.utils import timezone
        lock = lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                            academic_year=None)
        QuarterLock.objects.filter(pk=lock.pk).update(
            locked_at=timezone.make_aware(
                datetime.datetime(_AY - 1, 11, 15)))
        from portal.views import is_quarter_locked
        self.assertTrue(is_quarter_locked(
            self.school_a, '7-А', MATH, 1, year=_AY - 1))
        self.assertFalse(is_quarter_locked(
            self.school_a, '7-А', MATH, 1, year=_AY))


# ---------------------------------------------------------------------------
# 0026 backfill migration — deterministic year attribution only
# ---------------------------------------------------------------------------


class AcademicYearBackfillMigrationTests(GoldenBase):
    """portal.migrations.0026_backfill_quarter_academic_year stamps a year
    only where existing evidence identifies exactly one; every other row
    stays NULL (explicitly ambiguous) and every stored value is preserved.
    """

    def _run(self):
        import importlib
        from django.apps import apps
        mod = importlib.import_module(
            'portal.migrations.0026_backfill_quarter_academic_year')
        mod.backfill_academic_year(apps, None)

    def test_single_year_support_stamps_that_year(self):
        # A legacy NULL row whose source grades all live in ONE academic
        # year gets stamped with it — the same year the runtime derives.
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        make_grade(self.s2, score=8, date=D_Q1)
        # The signals wrote year-stamped derived rows for those sources;
        # at real migration time EVERY stored row is NULL (0025 just added
        # the column), so drop the derived rows to mirror that state —
        # otherwise stamping the NULL row would collide on the new
        # unique_together key.
        QuarterGrade.objects.filter(
            academic_year__isnull=False).delete()
        row = make_quarter_grade(self.s1, quarter=1, grade=9,
                                 academic_year=None)
        cur = make_quarter_grade(self.s2, quarter=1, grade=8,
                                 academic_year=None)
        self._run()
        row.refresh_from_db()
        cur.refresh_from_db()
        self.assertEqual(row.academic_year, _AY - 1)
        self.assertEqual(cur.academic_year, _AY)
        self.assertEqual(row.grade, 9)          # values preserved
        self.assertEqual(cur.grade, 8)
        self.assertEqual(row.class_name, '7-А')  # labels preserved

    def test_mixed_year_support_stays_null(self):
        # Sources in two different years -> no defensible attribution.
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        make_grade(self.s1, score=8, date=D_Q1)
        QuarterGrade.objects.filter(
            academic_year__isnull=False).delete()
        row = make_quarter_grade(self.s1, quarter=1, grade=7,
                                 academic_year=None)
        self._run()
        row.refresh_from_db()
        self.assertIsNone(row.academic_year)
        self.assertEqual(row.grade, 7)

    def test_zero_support_stays_null(self):
        row = make_quarter_grade(self.s1, quarter=1, grade=7,
                                 academic_year=None)
        self._run()
        row.refresh_from_db()
        self.assertIsNone(row.academic_year)

    def test_attestation_row_stays_null(self):
        # quarter=0 has no source evidence — even with scored sources for
        # the same cell, the attestation year cannot be established.
        make_grade(self.s1, score=9, date=D_Q1)
        QuarterGrade.objects.filter(
            academic_year__isnull=False).delete()
        att = make_quarter_grade(self.s1, quarter=0, att=9,
                                 academic_year=None)
        self._run()
        att.refresh_from_db()
        self.assertIsNone(att.academic_year)
        self.assertEqual(att.att_grade, 9)

    def test_lock_stamps_locked_at_year(self):
        from django.utils import timezone
        lock = lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                            academic_year=None)
        QuarterLock.objects.filter(pk=lock.pk).update(
            locked_at=timezone.make_aware(
                datetime.datetime(_AY - 1, 11, 15)))
        other = lock_quarter(self.school_a, '7-Б', MATH, quarter=2,
                             academic_year=None)
        # `other` keeps its creation-time locked_at (today -> _AY).
        self._run()
        lock.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(lock.academic_year, _AY - 1)
        self.assertEqual(other.academic_year, _AY)
        self.assertTrue(lock.locked)            # lock state preserved

    def test_already_stamped_rows_untouched(self):
        # No source rows: the preset year is never re-derived.
        preset = make_quarter_grade(self.s1, quarter=1, grade=5,
                                  academic_year=_AY - 1)
        preset_lock = lock_quarter(self.school_a, '7-А', MATH, quarter=3,
                                   academic_year=_AY)
        self._run()
        preset.refresh_from_db()
        preset_lock.refresh_from_db()
        self.assertEqual(preset.academic_year, _AY - 1)
        self.assertEqual(preset.grade, 5)
        self.assertEqual(preset_lock.academic_year, _AY)

    def test_same_key_three_years_coexist(self):
        # (student, class, subject, quarter) may exist once per year PLUS
        # a NULL legacy row — none replace another.
        current = make_quarter_grade(self.s1, quarter=1, grade=6,
                                     academic_year=_AY)
        prior = make_quarter_grade(self.s1, quarter=1, grade=9,
                                   academic_year=_AY - 1)
        legacy = make_quarter_grade(self.s1, quarter=1, grade=7,
                                  academic_year=None)
        self.assertEqual(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).count(), 3)
        # A year-keyed write touches only its own year's row.
        make_quarter_grade(self.s1, quarter=1, grade=4,
                           academic_year=_AY)
        current.refresh_from_db()
        prior.refresh_from_db()
        legacy.refresh_from_db()
        self.assertEqual(current.grade, 4)
        self.assertEqual(prior.grade, 9)
        self.assertEqual(legacy.grade, 7)


# ---------------------------------------------------------------------------
# reconcile_quarter_grades — controlled year reconciliation
# ---------------------------------------------------------------------------


class ReconcileQuarterGradesCommandTests(GoldenBase):
    """reconcile_quarter_grades: dry-run by default; --apply touches only
    unlocked, unambiguous STALE/MISSING cells of the selected year.
    Everything else is review-only and preserved verbatim."""

    def _run(self, *args):
        out = io.StringIO()
        call_command(
            'reconcile_quarter_grades', '--year', str(_AY), *args,
            stdout=out)
        return out.getvalue()

    def _stale_cell(self):
        # Sources derive 6, the stored row says 3 (as if it predates the
        # auto-recalc system). .update() bypasses signals on purpose —
        # this is exactly the pre-reconciliation state.
        make_grade(self.s1, score=3, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1, academic_year=_AY).update(grade=3)

    def test_dry_run_writes_nothing(self):
        self._stale_cell()
        QuarterGrade.objects.filter(
            student=self.s1, quarter=1, academic_year=_AY).update(grade=3)
        before = list(QuarterGrade.objects.values_list(
            'pk', 'grade', 'att_grade', 'academic_year'))
        out = self._run('--no-lines')
        after = list(QuarterGrade.objects.values_list(
            'pk', 'grade', 'att_grade', 'academic_year'))
        self.assertEqual(before, after)
        self.assertIn('DRY-RUN', out)
        self.assertIn('PLAN-STALE (аз нав ҳисоб): 1', out)

    def test_apply_updates_stale_and_creates_missing(self):
        self._stale_cell()
        # MISSING: s2 has derivable sources but no stored row.
        make_grade(self.s2, score=9, date=D_Q1)
        QuarterGrade.objects.filter(
            student=self.s2, quarter=1, academic_year=_AY).delete()
        self._run('--apply', '--no-lines')
        self.assertEqual(qg(self.s1, 1).grade, 6)
        self.assertEqual(qg(self.s2, 1).grade, 9)

    def test_apply_is_idempotent(self):
        self._stale_cell()
        self._run('--apply', '--no-lines')
        self.assertEqual(qg(self.s1, 1).grade, 6)
        out = self._run('--apply', '--no-lines')
        self.assertIn('PLAN-STALE (аз нав ҳисоб): 0', out)
        self.assertIn('ТАТБИҚШУДА: 0', out)

    def test_orphan_and_ambiguous_never_repaired(self):
        # ORPHAN: year-stamped row with no same-year source support.
        orphan = make_quarter_grade(self.s1, subject=TAJIK, quarter=2,
                                    grade=7, academic_year=_AY)
        # AMBIGUOUS: NULL-year legacy row.
        legacy = make_quarter_grade(self.s1, subject=TAJIK, quarter=1,
                                    grade=8, academic_year=None)
        # AMBIGUOUS-ATTESTATION: NULL-year attestation mark.
        att = make_quarter_grade(self.s1, subject=TAJIK, quarter=0,
                                 att=9, academic_year=None)
        self._run('--apply', '--no-lines')
        orphan.refresh_from_db()
        legacy.refresh_from_db()
        att.refresh_from_db()
        self.assertEqual(orphan.grade, 7)
        self.assertEqual(legacy.grade, 8)
        self.assertIsNone(legacy.academic_year)
        self.assertEqual(att.att_grade, 9)
        self.assertIsNone(att.academic_year)

    def test_frozen_conflict_never_altered(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1,
                     academic_year=_AY)
        make_quarter_grade(self.s1, quarter=1, grade=10,
                           academic_year=_AY)
        make_grade(self.s1, score=5, date=D_Q1)
        out = self._run('--apply', '--no-lines')
        self.assertEqual(qg(self.s1, 1).grade, 10)
        self.assertIn('REVIEW-FROZEN-CONFLICT: 1', out)
        self.assertIn('PLAN-STALE (аз нав ҳисоб): 0', out)

    def test_prior_year_rows_out_of_scope(self):
        # A prior-year stored row and prior-year-only sources are never
        # proposed for a current-year reconciliation.
        make_quarter_grade(self.s1, quarter=1, grade=4,
                           academic_year=_AY - 1)
        make_grade(self.s1, score=9, date=D_PREV_Q1)
        out = self._run('--no-lines')
        self.assertIn('PLAN-STALE (аз нав ҳисоб): 0', out)
        self.assertIn('PLAN-MISSING (эҷод): 0', out)

    def test_nematov_example_derives_6(self):
        # The confirmed production case: daily 3 (Sep 2) + 8 (Oct 2),
        # no AJM -> mean 5.5 -> 6. Stored 3 is STALE in dry-run and
        # becomes 6 under --apply.
        school = self.school_a
        st = self.s1
        Grade.objects.create(
            student=st, subject=MATH, score=3, period=PERIOD,
            date=datetime.date(_AY, 9, 2))
        Grade.objects.create(
            student=st, subject=MATH, score=8, period=PERIOD,
            date=datetime.date(_AY, 10, 2))
        QuarterGrade.objects.filter(
            student=st, quarter=1, academic_year=_AY).update(grade=3)
        out = io.StringIO()
        call_command('reconcile_quarter_grades', '--year', str(_AY),
                     stdout=out)
        self.assertIn('ҳисобшуда=6', out.getvalue())
        self.assertEqual(qg(st, 1).grade, 3)        # dry-run wrote nothing
        self._run('--apply', '--no-lines')
        self.assertEqual(qg(st, 1).grade, 6)

