"""Journal completion status (Пурра/Қисман/Холӣ) — quarter-level, per
School + Class + Subject, NOT time-aware.

Two distinct metrics must never be conflated:

- 'Иҷрои меъёр то имрӯз (%)' — time-aware pace monitoring, capped at 100%.
- Journal status — STUDENT-level completion of the FULL quarterly norm:
    done = 0                      -> Холӣ
    every visible student >= norm -> Пурра
    otherwise                     -> Қисман

Raw row totals never suffice for Пурра: one over-entered student cannot
cover classmates still below the norm.
"""
import datetime

from django.test import TestCase
from django.urls import reverse

from portal.models import Assessment, Grade
from portal.tests.helpers import (
    MATH, GoldenBase, make_class_subject, make_grade, make_student,
    make_teacher_profile,
)
from portal.views import (
    _current_quarter_bounds, _journal_norm_status, _journal_status,
    _norm_fulfillment, _quarter_progress, get_date_quarter,
)

TODAY = datetime.date.today()
Q_START, Q_END = _current_quarter_bounds(TODAY)
Q_PROGRESS = _quarter_progress(TODAY)
PREV_Q = Q_START - datetime.timedelta(days=1)


def make_control(cs, date=TODAY):
    return Assessment.objects.create(
        class_subject=cs, date=date, quarter=get_date_quarter(date),
        category='control',
    )


class JournalStatusFormulaTests(TestCase):
    """Pure formula: every visible student must reach the full norm.

    Signature: _journal_status(done, met, total) where ``met`` counts the
    students at/above the quarterly norm out of ``total`` visible students.
    """

    def test_empty_when_nothing_entered(self):
        self.assertEqual(_journal_status(0, 0, 25), 'empty')

    def test_one_overfilled_student_is_not_complete(self):
        # 175 rows on one student, 24 students at 0 — raw total equals the
        # requirement but only one student met the norm: Қисман.
        self.assertEqual(_journal_status(175, 1, 25), 'partial')

    def test_one_student_short_is_partial(self):
        # 24 students at 7, one at 6: 174 rows, still Қисман.
        self.assertEqual(_journal_status(174, 24, 25), 'partial')

    def test_complete_only_when_all_students_met(self):
        self.assertEqual(_journal_status(175, 25, 25), 'complete')

    def test_grades_exist_but_nobody_at_norm(self):
        self.assertEqual(_journal_status(10, 0, 25), 'partial')

    def test_single_early_grade_is_partial_not_complete(self):
        # Day-1 safety: the tiny elapsed target must not flip the status.
        self.assertEqual(_journal_status(1, 0, 25), 'partial')

    def test_zero_students(self):
        self.assertEqual(_journal_status(0, 0, 0), 'empty')
        # Attributed rows with no relevant students: nothing can be unmet.
        self.assertEqual(_journal_status(5, 0, 0), 'complete')

    def test_status_never_mirrors_fulfillment(self):
        # Time-aware metric saturated, journal still partial.
        done, required = 8, 14
        self.assertEqual(_norm_fulfillment(done, required, 0.5), 100.0)
        # ...but only one of two students reached the norm.
        self.assertEqual(_journal_status(done, 1, 2), 'partial')


class JournalNormStatusHelperTests(GoldenBase):
    """_journal_norm_status: per-student completion against the norm."""

    def setUp(self):
        super().setUp()
        self.cs_a.hours_per_week = 3  # norm 7/student, 2 students -> 14

    def _status(self):
        return _journal_norm_status(
            '7-А', MATH, self.cs_a, [self.s1, self.s2], TODAY)

    def test_required_is_norm_times_students(self):
        j = self._status()
        self.assertEqual(j['norm'], 7)
        self.assertEqual(j['required'], 14)
        self.assertEqual(j['students'], 2)

    def test_raw_total_at_required_but_one_student_zero(self):
        # The core regression: 14 rows all on s1 -> met=1/2 -> Қисман,
        # even though done == required.
        for _ in range(14):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 14)
        self.assertEqual(j['students_met'], 1)
        self.assertEqual(j['status'], 'partial')

    def test_all_students_at_norm_is_complete(self):
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=TODAY)
        j = self._status()
        self.assertEqual(j['students_met'], 2)
        self.assertEqual(j['status'], 'complete')

    def test_counts_only_current_quarter(self):
        for _ in range(7):
            make_grade(self.s1, subject=MATH, score=8, date=PREV_Q)
        for _ in range(2):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 2)
        self.assertEqual(j['status'], 'partial')

    # -- scored-only counting (the Grade row is multi-purpose) -------------

    def test_attendance_plus_only_rows_do_not_count(self):
        # 7 'ғоиб бо сабаб' marks and zero scores -> met=False, Холӣ.
        for _ in range(7):
            make_grade(self.s1, subject=MATH, attendance='+', date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 0)
        self.assertEqual(j['students_met'], 0)
        self.assertEqual(j['status'], 'empty')

    def test_attendance_minus_only_rows_do_not_count(self):
        for _ in range(7):
            make_grade(self.s1, subject=MATH, attendance='-', date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 0)
        self.assertEqual(j['status'], 'empty')

    def test_behavior_only_rows_do_not_count(self):
        for _ in range(7):
            make_grade(self.s1, subject=MATH, behavior=4, date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 0)
        self.assertEqual(j['status'], 'empty')

    def test_sticker_only_rows_do_not_count(self):
        for _ in range(7):
            make_grade(self.s1, subject=MATH, sticker='⭐', date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 0)
        self.assertEqual(j['status'], 'empty')

    def test_scores_plus_absences_count_scores_only(self):
        # 3 real scores + 10 absence marks -> done=3, not 13.
        for _ in range(3):
            make_grade(self.s1, subject=MATH, score=8, date=TODAY)
        for _ in range(10):
            make_grade(self.s1, subject=MATH, attendance='-', date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 3)
        self.assertEqual(j['students_met'], 0)
        self.assertEqual(j['status'], 'partial')

    def test_full_norm_scores_with_absences_still_complete(self):
        # 7 scored rows each, plus many absence marks -> still Пурра.
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=TODAY)
            for _ in range(10):
                make_grade(s, subject=MATH, attendance='+', date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 14)
        self.assertEqual(j['students_met'], 2)
        self.assertEqual(j['status'], 'complete')

    def test_row_with_score_and_attendance_counts_once(self):
        make_grade(self.s1, subject=MATH, score=9, attendance='+',
                   date=TODAY)
        j = self._status()
        self.assertEqual(j['done'], 1)
        self.assertEqual(j['status'], 'partial')

    def test_assessment_linked_score_row_counts(self):
        a = make_control(self.cs_a)
        Grade.objects.create(student=self.s1, subject=MATH, score=9,
                             date=TODAY, assessment=a)
        j = self._status()
        self.assertEqual(j['done'], 1)
        self.assertEqual(j['status'], 'partial')


class MonthlyJournalStatusTests(GoldenBase):
    """View-level: the monthly journal exposes the quarter status."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.cs_a.hours_per_week = 3
        self.cs_a.save()
        self.url = reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А', 'subject': MATH,
        })

    def _journal(self, resp):
        assert resp.status_code == 200
        return resp.context['journal']

    def test_empty(self):
        # No grades -> Холӣ.
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['status'], 'empty')
        self.assertEqual(j['done'], 0)
        self.assertEqual(j['required'], 14)

    def test_partial(self):
        # 4 grades for one of two students -> some progress, norm unmet.
        for _ in range(4):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['status'], 'partial')
        self.assertEqual(j['done'], 4)
        self.assertEqual(j['students_met'], 0)

    def test_full_total_on_one_student_is_partial(self):
        # s1 alone reaches/exceeds the whole class requirement in rows;
        # s2 has none -> Қисман, never Пурра.
        for _ in range(14):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['done'], 14)
        self.assertEqual(j['students_met'], 1)
        self.assertEqual(j['status'], 'partial')

    def test_complete(self):
        # Every student at the full norm (7 each) -> Пурра.
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=TODAY)
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['status'], 'complete')
        self.assertEqual(j['done'], 14)
        self.assertEqual(j['students_met'], 2)

    def test_previous_quarter_grades_ignored(self):
        # A full norm banked last quarter must not complete the journal.
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=PREV_Q)
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['status'], 'empty')
        self.assertEqual(j['done'], 0)

    def test_prev_plus_current_counts_current_only(self):
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=PREV_Q)
        make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        j = self._journal(self.client.get(self.url))
        self.assertEqual(j['done'], 1)
        self.assertEqual(j['status'], 'partial')

    def test_classes_not_combined(self):
        # Grades in 7-А must not leak into another class context.
        make_student(self.school_a, '8-А', 'Хонандаи Нав')
        cs8 = make_class_subject(self.school_a, '8-А', 'АЛГЕБРА',
                                 teacher=self.teacher_a)
        cs8.hours_per_week = 3
        cs8.save()
        for _ in range(7):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        url8 = reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '8-А',
            'subject': 'АЛГЕБРА',
        })
        j8 = self._journal(self.client.get(url8))
        self.assertEqual(j8['status'], 'empty')
        self.assertEqual(j8['done'], 0)
        j7 = self._journal(self.client.get(self.url))
        self.assertEqual(j7['done'], 7)

    def test_zero_student_class_is_empty(self):
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '9-Г',
            'subject': 'АЛГЕБРА',
        }))
        j = self._journal(resp)
        self.assertEqual(j['status'], 'empty')

    def test_page_shows_status_and_to_imruz_label(self):
        make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        resp = self.client.get(self.url)
        html = resp.content.decode()
        self.assertIn('Иҷрои меъёр то имрӯз', html)
        self.assertIn('Ҳолати ҷурнал', html)
        self.assertIn('Қисман', html)
        self.assertNotIn('Нимра', html)

    def test_complete_status_does_not_block_grade_entry(self):
        # Journal Пурра early — a normal save must still pass.
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=TODAY)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id, 'subject': MATH,
            'date': TODAY.isoformat(), 'type': 'daily', 'score': '10',
        })
        self.assertTrue(resp.json()['success'])
        self.assertTrue(Grade.objects.filter(
            student=self.s1, subject=MATH, date=TODAY).exists())


class GradeEntryStatusTests(GoldenBase):
    """The daily journal shows the same status; entry stays available."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.cs_a.hours_per_week = 3
        self.cs_a.save()
        self.url = reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А', 'subject': MATH,
        })

    def test_status_present(self):
        make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context['journal']['status'], 'partial')
        html = resp.content.decode()
        self.assertIn('Иҷрои меъёр то имрӯз', html)
        self.assertIn('Қисман', html)


class ReadinessJournalStatusTests(GoldenBase):
    """Teacher drilldown: per-class-subject statuses, never combined."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_teacher_profile(
            cls.teacher_a, cls.school_a, full_name='Омӯзгор А')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.cs_a.hours_per_week = 3
        self.cs_a.save()

    def _teacher(self):
        resp = self.client.get(reverse('school_readiness_rating'))
        assert resp.status_code == 200
        activity = {a['school'].id: a for a in resp.context['activity']}
        teachers = {
            t['name']: t for t in activity[self.school_a.id]['teachers']}
        return resp, teachers['Омӯзгор А']

    def test_per_class_subject_entries(self):
        # Grades in 7-А leave 8-А at Холӣ — no teacher-wide pool.
        make_student(self.school_a, '8-А', 'Хонандаи Нав')
        cs8 = make_class_subject(self.school_a, '8-А', 'АЛГЕБРА',
                                 teacher=self.teacher_a)
        cs8.hours_per_week = 3
        cs8.save()
        for _ in range(7):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        _, t = self._teacher()
        statuses = {j['class_name']: j['status'] for j in t['journal']}
        self.assertEqual(statuses['7-А'], 'partial')
        self.assertEqual(statuses['8-А'], 'empty')

    def test_full_norm_gives_complete_entry(self):
        for s in (self.s1, self.s2):
            for _ in range(7):
                make_grade(s, subject=MATH, score=9, date=TODAY)
        _, t = self._teacher()
        entry = next(j for j in t['journal'] if j['class_name'] == '7-А')
        self.assertEqual(entry['status'], 'complete')
        self.assertEqual(entry['students_met'], 2)
        self.assertEqual(t['fulfillment'], 100.0)

    def test_raw_total_on_one_student_is_partial(self):
        # 14 rows all on s1: required total met numerically, but s2 is at
        # zero — the per-student rule keeps the entry Қисман.
        for _ in range(14):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        _, t = self._teacher()
        entry = next(j for j in t['journal'] if j['class_name'] == '7-А')
        self.assertEqual(entry['status'], 'partial')
        self.assertEqual(entry['students_met'], 1)
        self.assertEqual(entry['students_total'], 2)

    def test_absence_only_rows_do_not_satisfy_norm(self):
        # s1: 7 'н' rows, 0 scores; s2: 3 real scores. The norm numerator
        # counts academic rows only — 3, not 10.
        for _ in range(7):
            make_grade(self.s1, subject=MATH, attendance='-', date=TODAY)
        for _ in range(3):
            make_grade(self.s2, subject=MATH, score=8, date=TODAY)
        _, t = self._teacher()
        entry = next(j for j in t['journal'] if j['class_name'] == '7-А')
        self.assertEqual(entry['status'], 'partial')
        self.assertEqual(entry['students_met'], 0)
        self.assertEqual(t['fulfillment'],
                         _norm_fulfillment(3, 14, Q_PROGRESS))

    def test_ungraded_subject_has_no_status(self):
        make_class_subject(self.school_a, '7-А', 'СОАТИ ТАРБИЯВӢ',
                           teacher=self.teacher_a)
        _, t = self._teacher()
        self.assertNotIn(
            'СОАТИ ТАРБИЯВӢ', [j['subject'] for j in t['journal']])

    def test_page_uses_to_imruz_wording(self):
        resp, _ = self._teacher()
        self.assertContains(resp, 'Иҷрои меъёр то имрӯз')
