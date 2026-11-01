"""Vacation-date write guard — no grading on official non-school days.

portal.academic_calendar.is_school_day is the single source of truth:
days inside an official vacation gap (quarter_for_date -> None) and
Sundays hold no lessons, so journal writes onto them are refused.
Late write-ups on past SCHOOL days of the same quarter stay allowed —
GoldenBase pins _journal_write_today to 2100 so 'past' here means any
fixture date, and dedicated future-date tests already cover the cutoff.
"""
import datetime
import json

from django.urls import reverse
from django.utils import timezone

from portal.academic_calendar import (
    is_school_day, quarter_for_date, quarter_windows)
from portal.models import Assessment, Grade, Lesson

from .helpers import D_Q1, GoldenBase, MATH, make_grade

_AY = (datetime.date.today().year if datetime.date.today().month >= 9
       else datetime.date.today().year - 1)
_W = quarter_windows(_AY)


def _gap_day():
    """A Mon–Sat date inside an official vacation gap of the AY.

    None when the configured calendar has no gap (fallback month
    buckets are contiguous) — such tests are skipped, not failed.
    """
    ay_end = _W[4][1]
    d = _W[1][1] + datetime.timedelta(days=1)
    while d <= ay_end and (
            d.weekday() == 6 or quarter_for_date(d) is not None):
        d += datetime.timedelta(days=1)
    return d if d <= ay_end else None


def _sunday_in_quarter(q=1):
    d = _W[q][0]
    while d.weekday() != 6:
        d += datetime.timedelta(days=1)
    return d


GAP_DAY = _gap_day()
Q1_SUNDAY = _sunday_in_quarter()


class IsSchoolDayTests(GoldenBase):
    """The calendar predicate itself — no HTTP involved."""

    def test_weekday_inside_quarter_is_school_day(self):
        self.assertTrue(is_school_day(D_Q1))
        self.assertEqual(quarter_for_date(D_Q1), 1)

    def test_vacation_gap_is_not_school_day(self):
        if GAP_DAY is None:
            self.skipTest('configured calendar has no vacation gap')
        self.assertIsNone(quarter_for_date(GAP_DAY))
        self.assertFalse(is_school_day(GAP_DAY))
        # A weekday — the refusal comes from the official gap, not the
        # Sunday rule.
        self.assertNotEqual(GAP_DAY.weekday(), 6)

    def test_sunday_is_not_school_day(self):
        self.assertIsNotNone(quarter_for_date(Q1_SUNDAY))
        self.assertFalse(is_school_day(Q1_SUNDAY))

    def test_fallback_year_has_no_gaps(self):
        # An academic year absent from QUARTER_CALENDAR gets contiguous
        # month-bucket windows — every non-Sunday is a school day.
        fallback = quarter_windows(_AY - 10)
        d = fallback[1][0] + datetime.timedelta(days=1)
        while d.weekday() == 6:
            d += datetime.timedelta(days=1)
        self.assertTrue(is_school_day(d))


class VacationWriteGuardTests(GoldenBase):
    """Every dated write path refuses vacation dates — same message."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        if GAP_DAY is None:
            self.skipTest('configured calendar has no vacation gap')

    def _daily_post(self, date):
        return self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id, 'subject': MATH,
            'type': 'daily', 'score': '8',
            'date': date.isoformat(),
        })

    # ------------------------------------------------- AJAX daily save
    def test_ajax_grade_on_vacation_refused(self):
        resp = self._daily_post(GAP_DAY)
        self.assertFalse(resp.json()['success'])
        self.assertIn('таътил', resp.json()['message'])
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=GAP_DAY).exists())

    def test_ajax_grade_on_sunday_refused(self):
        resp = self._daily_post(Q1_SUNDAY)
        self.assertFalse(resp.json()['success'])
        self.assertIn('таътил', resp.json()['message'])

    def test_ajax_grade_on_school_day_allowed(self):
        # Late write-up on a past school day of the same quarter works.
        resp = self._daily_post(D_Q1)
        self.assertTrue(resp.json()['success'], resp.json())
        self.assertTrue(Grade.objects.filter(
            student=self.s1, date=D_Q1, score=8).exists())

    # ------------------------------------------------- batch pipelines
    def test_monthly_save_on_vacation_refused(self):
        resp = self.client.post(reverse('monthly_save'), {
            'payload': json.dumps([{
                'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
                'date': GAP_DAY.isoformat(), 'score': '7',
            }]),
        })
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertFalse(data['results'][0]['ok'])
        self.assertIn('таътил', data['results'][0]['message'])

    def test_monthly_save_on_school_day_allowed(self):
        resp = self.client.post(reverse('monthly_save'), {
            'payload': json.dumps([{
                'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
                'date': D_Q1.isoformat(), 'score': '7',
            }]),
        })
        self.assertEqual(resp.json()['saved'], 1)

    def test_daily_save_on_vacation_refused(self):
        resp = self.client.post(reverse('daily_save'), {
            'payload': json.dumps([{
                'key': 'score_1', 'student_id': self.s1.id, 'subject': MATH,
                'date': GAP_DAY.isoformat(), 'attendance': '+',
            }]),
        })
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertIn('таътил', data['results'][0]['message'])

    # ------------------------------------------------- grade_entry POST
    def test_batch_post_on_vacation_refused(self):
        resp = self.client.post(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {
            'date': GAP_DAY.isoformat(),
            f'score_{self.s1.id}': '9',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('таътил', resp.content.decode())
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=GAP_DAY).exists())

    def test_batch_post_on_school_day_allowed(self):
        resp = self.client.post(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {
            'date': D_Q1.isoformat(),
            f'score_{self.s1.id}': '9',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Grade.objects.filter(
            student=self.s1, date=D_Q1).exists())

    # ------------------------------------------------- lesson / control
    def test_lesson_on_vacation_refused(self):
        resp = self.client.post(reverse('lesson_save'), {
            'class_subject_id': self.cs_a.id,
            'date': GAP_DAY.isoformat(),
        })
        self.assertFalse(resp.json()['success'])
        self.assertIn('таътил', resp.json()['message'])
        self.assertFalse(Lesson.objects.filter(
            class_subject=self.cs_a, date=GAP_DAY).exists())

    def test_lesson_on_school_day_allowed(self):
        resp = self.client.post(reverse('lesson_save'), {
            'class_subject_id': self.cs_a.id,
            'date': D_Q1.isoformat(),
        })
        self.assertTrue(resp.json()['success'], resp.json())

    def test_control_assessment_on_vacation_refused(self):
        resp = self.client.post(reverse('assessment_save'), {
            'class_subject_id': self.cs_a.id,
            'date': GAP_DAY.isoformat(),
        })
        self.assertFalse(resp.json()['success'])
        self.assertIn('таътил', resp.json()['message'])

    def test_assessment_toggle_on_vacation_refused(self):
        resp = self.client.post(reverse('assessment_toggle'), {
            'class_subject_id': self.cs_a.id,
            'date': GAP_DAY.isoformat(),
        })
        self.assertFalse(resp.json()['success'])
        self.assertIn('таътил', resp.json()['message'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a, date=GAP_DAY).exists())


class VacationUiTests(GoldenBase):
    """The journals show the restriction — no live inputs on holiday."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_grade_entry_holiday_disables_inputs(self):
        if GAP_DAY is None:
            self.skipTest('configured calendar has no vacation gap')
        resp = self.client.get(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'date': GAP_DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('таътил', body)
        self.assertNotIn('+ Дарси нав', body)
        # The score input for s1 is rendered disabled.
        self.assertRegex(
            body,
            rf'name="score_{self.s1.id}"[^>]*disabled|'
            rf'name="score_{self.s1.id}".*?disabled')

    def test_grade_entry_school_day_keeps_inputs(self):
        if D_Q1 > timezone.localdate():
            self.skipTest('fixture date is still in the future')
        resp = self.client.get(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'date': D_Q1.isoformat()})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn('Ин сана таътил аст', body)
        self.assertNotRegex(
            body, rf'name="score_{self.s1.id}"[^>]*disabled')

    def test_monthly_journal_vacation_cells_not_editable(self):
        if GAP_DAY is None:
            self.skipTest('configured calendar has no vacation gap')
        # The month containing the Q1->Q2 gap renders the gap columns but
        # none of them carry the editable cell attributes.
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'month': GAP_DAY.strftime('%Y-%m')})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(f'data-date="{GAP_DAY.isoformat()}"', body)

    def test_monthly_journal_sunday_cell_not_editable(self):
        # A Sunday that holds legacy data still renders — but its cells
        # never get the editable attributes.
        make_grade(self.s2, score=5, date=Q1_SUNDAY)
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'month': Q1_SUNDAY.strftime('%Y-%m')})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn(f'data-date="{Q1_SUNDAY.isoformat()}"', body)

    def test_monthly_journal_school_day_stays_editable(self):
        if D_Q1 > timezone.localdate():
            self.skipTest('fixture date is still in the future')
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'month': D_Q1.strftime('%Y-%m')})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'data-date="{D_Q1.isoformat()}"',
                      resp.content.decode())

    def test_focus_handler_present_in_both_journals(self):
        # Requirement 1: focusing a grade cell closes open panels — the
        # shared focusin handler must render on both journal pages.
        for url in (
            reverse('grade_entry', kwargs={
                'school_id': self.school_a.id, 'class_name': '7-А',
                'subject': MATH}),
            reverse('monthly_journal', kwargs={
                'school_id': self.school_a.id, 'class_name': '7-А',
                'subject': MATH}),
        ):
            resp = self.client.get(url)
            body = resp.content.decode()
            self.assertIn('focusin', body, url)
            self.assertIn('pace-ctl[open]', body, url)
