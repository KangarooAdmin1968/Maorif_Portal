"""Automatic derived quarter grades — regression tests.

QuarterGrade rows for quarters 1-4 are a derived aggregate of the source
Grade rows (daily + control-assessment results). portal.signals recalculates
the affected student/subject/quarter whenever a source row is added, edited,
or deleted — no manual recalculation step is needed. The same signals protect
the data: quarter grades cannot be created, overwritten, or deleted through
any teacher grade-entry endpoint, while the attestation mark (quarter 0)
stays teacher-entered and feeds the derived Ҷамъбастӣ.
"""
import json
import re
from unittest.mock import patch

from django.urls import reverse

from portal.models import Assessment, Grade, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, D_Q2, MATH, TAJIK, GoldenBase, lock_quarter,
    make_class_subject, make_grade, make_quarter_grade,
)
from portal.tests.test_phase4_lessons import (
    make_control, post_assessment, post_grade,
)

AJAX_URL = reverse('save_grade_ajax')
DAILY_SAVE_URL = reverse('daily_save')
MONTHLY_SAVE_URL = reverse('monthly_save')
DELETE_URL = reverse('assessment_delete')


def qg(student, quarter, subject=MATH):
    return QuarterGrade.objects.filter(
        student=student, subject=subject, quarter=quarter).first()


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

