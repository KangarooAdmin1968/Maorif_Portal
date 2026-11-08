"""ҚОИДАҲОИ ҲАРАКАТ ДАР РОҲ — ungraded-subject semantics.

The traffic-rules subject joins СОАТИ ТАРБИЯВӢ in UNGRADED_SUBJECTS:

* no quarter grading norm (norm == 0, teachers cannot fall behind),
* stored scores never reach school/class/subject/student rankings or
  the GPA map — even legacy rows already in the database,
* the journals refuse score/attestation writes (UI hides the controls;
  forged POSTs are dropped server-side) while attendance/conduct stay
  writable,
* workload hours still count toward the teacher's load.

All checks are read-side: historical rows are never deleted or moved.
"""
import datetime

from django.test import Client
from django.urls import reverse

from portal.curriculum_hours import (
    UNGRADED_SUBJECTS, is_graded_subject, quarter_min_norm, weekly_hours,
)
from portal.models import Grade, QuarterGrade, normalize_subject
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_grade,
    make_quarter_grade,
)
from portal.views import (
    _calculate_class_rankings_uncached,
    _calculate_school_rankings_uncached,
    _calculate_subject_rankings_uncached,
    _calculate_top_students_uncached,
    _all_student_gpas,
)

QOH = 'ҚОИДАҲОИ ҲАРАКАТ ДАР РОҲ'          # canonical curriculum spelling
QOH_VARIANT = 'Қоидаи ҳаракати роҳ'      # common journal spelling


class UngradedSubjectConstantTests(GoldenBase):
    def test_qoh_is_ungraded(self):
        for spelling in (QOH, 'ҚОИДАИ ҲАРАКАТ ДАР РОҲ',
                         'ҚОИДАИ ҲАРАКАТИ РОҲ', QOH_VARIANT):
            self.assertIn(normalize_subject(spelling), UNGRADED_SUBJECTS)
            self.assertFalse(is_graded_subject(spelling))
        self.assertTrue(is_graded_subject(MATH))

    def test_qoh_zero_norm_keeps_workload(self):
        cs = make_class_subject(self.school_a, '7-А', QOH,
                                teacher=self.teacher_a)
        self.assertEqual(quarter_min_norm('7-А', QOH, cs), 0)
        # Not listed in the grade-7 annex -> default hours still count.
        self.assertGreaterEqual(weekly_hours('7-А', QOH, cs), 1)


class UngradedRankingExclusionTests(GoldenBase):
    """A stored ҚоҲ score must not move any ranking — nobody is harmed
    by a mark entered before the subject became ungraded."""

    def _seed(self):
        make_class_subject(self.school_a, '7-А', QOH,
                           teacher=self.teacher_a)
        # 7-А: one math score + one ҚоҲ score of 10 (would inflate GPA).
        make_grade(self.s1, subject=MATH, score=5, date=D_Q1)
        self.qoh_grade = make_grade(
            self.s1, subject=QOH, score=10, date=D_Q1)
        make_quarter_grade(self.s1, '7-А', QOH, quarter=1, grade=10)
        make_quarter_grade(self.s1, '7-А', QOH, quarter=0, att=10)
        # 7-Б: one math score of 8 — school B GPA 8.0.
        make_grade(self.s3, subject=MATH, score=8, date=D_Q1)

    def test_historical_rows_survive(self):
        self._seed()
        self.assertTrue(Grade.objects.filter(pk=self.qoh_grade.pk).exists())
        self.assertTrue(QuarterGrade.objects.filter(
            student=self.s1, subject=QOH).exists())

    def test_school_ranking_ignores_qoh(self):
        self._seed()
        data = {d['school'].id: d['gpa']
                for d in _calculate_school_rankings_uncached()}
        # A: only the math 5 counts (10s under ҚоҲ dropped).
        self.assertEqual(data[self.school_a.id], 5.0)
        self.assertEqual(data[self.school_b.id], 8.0)

    def test_class_ranking_ignores_qoh(self):
        self._seed()
        data = {d['class_name']: d['gpa']
                for d in _calculate_class_rankings_uncached()}
        self.assertEqual(data['7-А'], 5.0)
        self.assertEqual(data['7-Б'], 8.0)

    def test_subject_ranking_omits_qoh(self):
        self._seed()
        subjects = [d['subject']
                    for d in _calculate_subject_rankings_uncached()]
        self.assertNotIn(QOH, subjects)
        self.assertIn(MATH, subjects)

    def test_top_students_ignores_qoh(self):
        self._seed()
        gpas = {d['student_id']: d['gpa']
                for d in _calculate_top_students_uncached()}
        self.assertEqual(gpas[self.s1.id], 5.0)
        self.assertEqual(gpas[self.s3.id], 8.0)

    def test_gpa_map_ignores_qoh(self):
        self._seed()
        self.assertEqual(_all_student_gpas()[self.s1.id], 5.0)


class UngradedJournalWriteTests(GoldenBase):
    """Journal write paths must refuse score/attestation for ҚоҲ."""

    def setUp(self):
        super().setUp()
        self.cs = make_class_subject(self.school_a, '7-А', QOH,
                                     teacher=self.teacher_a)
        self.client = Client()
        self.client.force_login(self.teacher_a)
        self.date = D_Q1.isoformat()

    def test_grade_entry_get_hides_score_controls(self):
        resp = self.client.get(reverse(
            'grade_entry',
            args=[self.school_a.id, '7-А', QOH]) + f'?date={self.date}')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('name="score_', html)
        self.assertNotIn('name="att_', html)
        # Attendance/conduct remain writable.
        self.assertIn('name="attendance_', html)
        self.assertIn('name="behavior_', html)

    def test_grade_entry_post_drops_score_and_attestation(self):
        resp = self.client.post(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', QOH]), {
            'date': self.date,
            f'score_{self.s1.id}': '9',
            f'att_{self.s1.id}': '9',
            f'attendance_{self.s1.id}': '+',
        })
        self.assertEqual(resp.status_code, 302)
        grade = Grade.objects.get(student=self.s1, subject=QOH,
                                  date=D_Q1)
        self.assertIsNone(grade.score)
        self.assertEqual(grade.attendance, '+')
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, subject=QOH).exists())

    def test_monthly_save_drops_score_keeps_attendance(self):
        import json as _json
        payload = [{
            'key': f'{self.s1.id}|{self.date}|',
            'student_id': self.s1.id,
            'subject': QOH,
            'date': self.date,
            'lesson_id': '',
            'score': '7',
            'attendance': '+',
        }]
        resp = self.client.post(reverse('monthly_save'), {
            'payload': _json.dumps(payload)})
        self.assertEqual(resp.status_code, 200)
        grade = Grade.objects.get(student=self.s1, subject=QOH,
                                  date=D_Q1)
        self.assertIsNone(grade.score)
        self.assertEqual(grade.attendance, '+')

    def test_save_grade_ajax_rejects_quarterly(self):
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id,
            'subject': QOH,
            'type': 'quarterly',
            'quarter': '0',
            'score': '8',
        })
        self.assertNotEqual(Grade.objects.filter(
            student=self.s1, subject=QOH, score__isnull=False).count(), 1)
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, subject=QOH).exists())
