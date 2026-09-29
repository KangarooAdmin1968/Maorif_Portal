"""Time-aware quarterly grading-norm fulfillment ('Иҷрои меъёр %').

The metric answers: "has the teacher met the elapsed share of the
quarterly minimum?" — numerator = Grade rows dated inside the current
quarter only; denominator = quarter norm x students x elapsed fraction;
display capped at 100%. The cap is monitoring-only and never restricts
grade entry.
"""
import datetime

from django.test import TestCase
from django.urls import reverse

from portal.models import ClassSubject, Grade
from portal.tests.helpers import (
    MATH, GoldenBase, make_class_subject, make_grade, make_student,
    make_teacher_profile,
)
from portal.curriculum_hours import quarter_min_norm
from portal.views import (
    _current_quarter_bounds, _norm_fulfillment, _quarter_progress,
    get_date_quarter,
)

TODAY = datetime.date.today()
Q_START, Q_END = _current_quarter_bounds(TODAY)
Q_PROGRESS = _quarter_progress(TODAY)
PREV_Q = Q_START - datetime.timedelta(days=1)


class QuarterBoundsTests(TestCase):
    """_current_quarter_bounds mirrors get_date_quarter's month buckets."""

    def test_q1_sep_nov(self):
        self.assertEqual(
            _current_quarter_bounds(datetime.date(2026, 10, 15)),
            (datetime.date(2026, 9, 1), datetime.date(2026, 11, 30)))

    def test_q2_dec_feb_crosses_year(self):
        self.assertEqual(
            _current_quarter_bounds(datetime.date(2027, 1, 15)),
            (datetime.date(2026, 12, 1), datetime.date(2027, 2, 28)))

    def test_q2_leap_year(self):
        self.assertEqual(
            _current_quarter_bounds(datetime.date(2028, 2, 10)),
            (datetime.date(2027, 12, 1), datetime.date(2028, 2, 29)))

    def test_q3_mar_may(self):
        self.assertEqual(
            _current_quarter_bounds(datetime.date(2026, 4, 10)),
            (datetime.date(2026, 3, 1), datetime.date(2026, 5, 31)))

    def test_q4_jun_aug(self):
        self.assertEqual(
            _current_quarter_bounds(datetime.date(2026, 7, 1)),
            (datetime.date(2026, 6, 1), datetime.date(2026, 8, 31)))

    def test_bounds_consistent_with_get_date_quarter(self):
        # Every boundary day must map back to the intended quarter number.
        self.assertEqual(get_date_quarter(datetime.date(2026, 9, 1)), 1)
        self.assertEqual(get_date_quarter(datetime.date(2026, 11, 30)), 1)
        self.assertEqual(get_date_quarter(datetime.date(2027, 2, 28)), 2)
        self.assertEqual(get_date_quarter(datetime.date(2026, 5, 31)), 3)
        self.assertEqual(get_date_quarter(datetime.date(2026, 8, 31)), 4)


class QuarterProgressTests(TestCase):
    """_quarter_progress: elapsed fraction of the current quarter."""

    def test_first_day_never_zero(self):
        # Sep 1 is day 1 of 91 (Q1) — small but non-zero.
        self.assertAlmostEqual(
            _quarter_progress(datetime.date(2026, 9, 1)), 1 / 91)

    def test_last_day_is_one(self):
        self.assertEqual(
            _quarter_progress(datetime.date(2026, 11, 30)), 1.0)

    def test_midpoint(self):
        # Oct 16 = day 46 of 91.
        self.assertAlmostEqual(
            _quarter_progress(datetime.date(2026, 10, 16)), 46 / 91)


class NormFulfillmentTests(TestCase):
    """Pure formula: done vs elapsed share of the quarter target."""

    def test_half_quarter_below_target(self):
        # 3h/week -> norm 7; 50% elapsed -> expected 3.5; 3 < 3.5.
        self.assertEqual(_norm_fulfillment(3, 7, 0.5), 85.7)

    def test_half_quarter_meets_target(self):
        # 4 >= 3.5 -> 100% (capped, not 114.3).
        self.assertEqual(_norm_fulfillment(4, 7, 0.5), 100.0)
        self.assertEqual(_norm_fulfillment(5, 7, 0.5), 100.0)

    def test_full_norm_mid_quarter_is_100(self):
        self.assertEqual(_norm_fulfillment(7, 7, 0.5), 100.0)

    def test_over_norm_capped_at_100(self):
        self.assertEqual(_norm_fulfillment(8, 7, 0.5), 100.0)
        self.assertEqual(_norm_fulfillment(8, 7, 1.0), 100.0)

    def test_quarter_end_full_target(self):
        self.assertEqual(_norm_fulfillment(7, 7, 1.0), 100.0)
        self.assertEqual(_norm_fulfillment(3, 7, 1.0), 42.9)

    def test_quarter_start_safety(self):
        # Nearly-zero expected share: no division by zero, sane output.
        self.assertEqual(_norm_fulfillment(0, 7, 1 / 91), 0.0)
        self.assertEqual(_norm_fulfillment(1, 7, 1 / 91), 100.0)

    def test_no_norm_is_zero(self):
        self.assertEqual(_norm_fulfillment(10, 0, 0.5), 0.0)


class NormBandsUnchangedTests(TestCase):
    """The established quarterly norm table is untouched."""

    def test_bands(self):
        cases = [(1, 3), (2, 5), (3, 7), (4, 9), (5, 9),
                 (8, 14), (9, 14), (10, 14)]
        cs = make_class_subject(
            make_school_for(self), '7-А', MATH)
        for hours, expected in cases:
            cs.hours_per_week = hours
            self.assertEqual(
                quarter_min_norm('7-А', MATH, cs), expected,
                f'{hours}h/week should map to {expected} grades')

    def test_ungraded_subject_zero_norm(self):
        cs = make_class_subject(
            make_school_for(self), '7-А', 'СОАТИ ТАРБИЯВӢ')
        self.assertEqual(
            quarter_min_norm('7-А', 'СОАТИ ТАРБИЯВӢ', cs), 0)


def make_school_for(testcase):
    from portal.tests.helpers import make_school
    return make_school(f'Санҷиш {testcase}')


class ReadinessTimeAwareTests(GoldenBase):
    """View-level: current-quarter filtering + elapsed-share denominator."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_teacher_profile(
            cls.teacher_a, cls.school_a, full_name='Омӯзгор А')
        # 3 h/week -> quarterly norm 7 (7-А has s1 + s2 = 2 students).
        cls.cs_a.hours_per_week = 3
        cls.cs_a.save()

    def _readiness(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('school_readiness_rating'))
        assert resp.status_code == 200
        activity = {a['school'].id: a for a in resp.context['activity']}
        row = activity[self.school_a.id]
        teachers = {t['name']: t for t in row['teachers']}
        return row, teachers['Омӯзгор А']

    def test_only_current_quarter_grades_count(self):
        # 7 previous-quarter grades + 2 current-quarter grades.
        for _ in range(7):
            make_grade(self.s1, subject=MATH, score=8, date=PREV_Q)
        for _ in range(2):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        row, t = self._readiness()
        # Previous-quarter rows must not inflate the count.
        self.assertEqual(t['grades_done'], 2)
        self.assertEqual(row['total_grades'], 2)

    def _school_expected_min(self):
        # Full-quarter school target: norm x students summed over every
        # active class-subject (all rows here are 7-А with 2 students).
        return sum(
            quarter_min_norm(cs.class_name, cs.subject, cs)
            for cs in ClassSubject.objects.filter(
                school=self.school_a, is_active=True)
        ) * 2

    def test_fulfillment_uses_elapsed_share(self):
        for _ in range(2):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        row, t = self._readiness()
        # Teacher: 2 students -> full-quarter target 14; elapsed share only.
        self.assertEqual(
            t['fulfillment'],
            _norm_fulfillment(2, 7 * 2, Q_PROGRESS))
        # School: same done-count over the whole curriculum's target.
        self.assertEqual(
            row['fulfillment'],
            _norm_fulfillment(2, self._school_expected_min(), Q_PROGRESS))

    def test_full_norm_reached_early_caps_at_100(self):
        for _ in range(14):  # 7 norm x 2 students, all inside the quarter
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        row, t = self._readiness()
        self.assertEqual(t['fulfillment'], 100.0)
        self.assertEqual(
            row['fulfillment'],
            _norm_fulfillment(14, self._school_expected_min(), Q_PROGRESS))

    def test_early_100_does_not_block_grading(self):
        # Metric at 100% — a normal daily save must still succeed.
        for _ in range(14):
            make_grade(self.s1, subject=MATH, score=9, date=TODAY)
        _, t = self._readiness()
        self.assertEqual(t['fulfillment'], 100.0)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s2.id, 'subject': MATH,
            'date': TODAY.isoformat(), 'type': 'daily', 'score': '10',
        })
        self.assertTrue(resp.json()['success'])
        self.assertTrue(Grade.objects.filter(
            student=self.s2, subject=MATH, date=TODAY).exists())

    def test_per_allocation_norms_not_combined(self):
        # 7-А MATH 3h (norm 7 x 2 students) + 8-А MATH 2h (norm 5 x 1)
        # must NOT collapse into an 8h -> 14-per-student allocation.
        s8 = make_student(self.school_a, '8-А', 'Хонандаи Ҳастум')
        make_class_subject(self.school_a, '8-А', MATH,
                           teacher=self.teacher_a)  # defaults to annex hours
        cs8 = ClassSubject.objects.get(
            school=self.school_a, class_name='8-А', subject=MATH)
        cs8.hours_per_week = 2
        cs8.save()
        _, t = self._readiness()
        self.assertEqual(t['min_grades'], 7 * 2 + 5 * 1)

    def test_ungraded_subject_excluded_from_norm(self):
        make_class_subject(self.school_a, '7-А', 'СОАТИ ТАРБИЯВӢ',
                           teacher=self.teacher_a)
        make_grade(self.s1, subject='СОАТИ ТАРБИЯВӢ',
                   behavior=5, date=TODAY)
        _, t = self._readiness()
        # Norm 0 -> denominator untouched; its rows excluded from norm_done.
        self.assertEqual(t['min_grades'], 7 * 2)
        self.assertEqual(
            t['fulfillment'], _norm_fulfillment(0, 7 * 2, Q_PROGRESS))

    def test_first_day_of_quarter_no_divzero(self):
        # Simulate 'today = quarter start' through the pure helpers: a
        # single entered grade on day 1 is already ahead of target.
        progress = _quarter_progress(Q_START)
        self.assertGreater(progress, 0)
        self.assertEqual(_norm_fulfillment(1, 14, progress), 100.0)
        self.assertEqual(_norm_fulfillment(0, 14, progress), 0.0)
