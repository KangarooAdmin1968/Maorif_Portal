"""Future-date write guard + delete_future_grades cleanup command.

Teachers must never write scores/attendance on dates after today — the
lesson has not happened, so such rows are phantom data that silently
distorts quarter pools and reports. Every write endpoint rejects them;
the cleanup command deletes rows that already slipped in and re-derives
the affected quarter cells through the standard pipeline.

GoldenBase pins the write cutoff to year 2100 (quarter fixtures are
"future" relative to the real clock for much of the year), so each test
re-patches `_journal_write_today` with the actual date to exercise the
guard itself.
"""
import datetime
import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse

from portal.models import Assessment, Grade, QuarterGrade
from portal.tests.helpers import (
    D_Q1, MATH, PERIOD, GoldenBase, make_grade,
)
from portal.views import academic_year_start, get_date_quarter

TODAY = datetime.date.today
FUTURE = lambda: TODAY() + datetime.timedelta(days=1)

AJAX_URL = reverse('save_grade_ajax')
ASSESS_URL = reverse('assessment_save')
TOGGLE_URL = reverse('assessment_toggle')
MONTHLY_URL = reverse('monthly_save')
DAILY_URL = reverse('daily_save')


def real_today():
    """Re-patch the write cutoff with the actual clock for this call."""
    return patch('portal.views._journal_write_today',
                 return_value=TODAY())


class FutureWriteGuardTests(GoldenBase):
    """Every write path rejects dates after the real today."""

    def test_monthly_save_rejects_future(self):
        self.client.force_login(self.teacher_a)
        item = {
            'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
            'date': FUTURE().isoformat(), 'lesson_id': '', 'score': '8',
        }
        with real_today():
            resp = self.client.post(
                MONTHLY_URL, {'payload': json.dumps([item])})
        data = resp.json()
        self.assertFalse(data['results'][0]['ok'])
        self.assertIn('оянда', data['results'][0]['message'])
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=FUTURE()).exists())

    def test_daily_save_rejects_future(self):
        self.client.force_login(self.teacher_a)
        item = {
            'key': 'k1', 'student_id': self.s1.id, 'subject': MATH,
            'date': FUTURE().isoformat(), 'lesson_id': '', 'score': '8',
        }
        with real_today():
            resp = self.client.post(
                DAILY_URL, {'payload': json.dumps([item])})
        data = resp.json()
        self.assertFalse(data['results'][0]['ok'])
        self.assertIn('оянда', data['results'][0]['message'])
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=FUTURE()).exists())

    def test_save_grade_ajax_rejects_future(self):
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(AJAX_URL, {
                'student_id': self.s1.id, 'subject': MATH,
                'type': 'daily', 'date': FUTURE().isoformat(),
                'score': '9',
            })
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=FUTURE()).exists())

    def test_save_grade_ajax_rejects_future_assessment(self):
        """A control assessment dated in the future takes no grades."""
        a = Assessment.objects.create(
            class_subject=self.cs_a, date=FUTURE(),
            quarter=get_date_quarter(FUTURE()) or 1,
            category='control', number=1,
        )
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(AJAX_URL, {
                'student_id': self.s1.id, 'subject': MATH,
                'type': 'daily', 'assessment_id': str(a.id),
                'score': '9',
            })
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(assessment=a).exists())

    def test_assessment_save_rejects_future_create(self):
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(ASSESS_URL, {
                'class_subject_id': self.cs_a.id,
                'date': FUTURE().isoformat(), 'title': '',
            })
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a, date=FUTURE()).exists())

    def test_assessment_toggle_rejects_future_mark(self):
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(TOGGLE_URL, {
                'class_subject_id': self.cs_a.id,
                'date': FUTURE().isoformat(),
            })
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a, date=FUTURE()).exists())

    def test_assessment_toggle_allows_future_unmark(self):
        """Removing a phantom future Н stays allowed — it is cleanup."""
        Assessment.objects.create(
            class_subject=self.cs_a, date=FUTURE(),
            quarter=get_date_quarter(FUTURE()) or 1,
            category='control', number=1,
        )
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(TOGGLE_URL, {
                'class_subject_id': self.cs_a.id,
                'date': FUTURE().isoformat(),
            })
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['control'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a, date=FUTURE()).exists())

    def test_grade_entry_post_rejects_future(self):
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(
                reverse('grade_entry', kwargs={
                    'school_id': self.school_a.id,
                    'class_name': '7-А', 'subject': MATH,
                }), {'date': FUTURE().isoformat(),
                     f'score_{self.s1.id}': '8'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=FUTURE()).exists())

    def test_today_and_past_still_writable(self):
        """The guard is strictly `date > today` — today passes."""
        self.client.force_login(self.teacher_a)
        with real_today():
            resp = self.client.post(AJAX_URL, {
                'student_id': self.s1.id, 'subject': MATH,
                'type': 'daily', 'date': TODAY().isoformat(),
                'score': '8',
            })
        self.assertTrue(resp.json()['success'])
        self.assertTrue(Grade.objects.filter(
            student=self.s1, date=TODAY()).exists())

    def test_monthly_journal_future_column_not_editable(self):
        """Future columns render read-only in the month grid."""
        self.client.force_login(self.teacher_a)
        month = FUTURE().strftime('%Y-%m')
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH,
        }) + f'?month={month}')
        self.assertEqual(resp.status_code, 200)
        future_cols = [
            c for c in resp.context['columns'] if c['date'] > TODAY()]
        self.assertTrue(future_cols)
        self.assertTrue(all(not c['editable'] for c in future_cols))
        today_cols = [
            c for c in resp.context['columns'] if c['date'] <= TODAY()]
        self.assertTrue(all(c['editable'] for c in today_cols))


class DeleteFutureGradesCommandTests(GoldenBase):
    """delete_future_grades: dry-run by default, --apply deletes + recalcs."""

    def _future_grade(self):
        return Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            date=FUTURE(), score=3)

    def test_dry_run_keeps_rows(self):
        g = self._future_grade()
        Assessment.objects.create(
            class_subject=self.cs_a, date=FUTURE(),
            quarter=get_date_quarter(FUTURE()) or 1,
            category='control', number=1,
        )
        out = io.StringIO()
        call_command('delete_future_grades', stdout=out)
        self.assertTrue(Grade.objects.filter(pk=g.pk).exists())
        self.assertTrue(Assessment.objects.filter(
            class_subject=self.cs_a, date=FUTURE()).exists())
        self.assertIn('DRY-RUN', out.getvalue())

    def test_apply_deletes_grades_and_assessments(self):
        g = self._future_grade()
        Assessment.objects.create(
            class_subject=self.cs_a, date=FUTURE(),
            quarter=get_date_quarter(FUTURE()) or 1,
            category='control', number=1,
        )
        call_command('delete_future_grades', apply=True, stdout=io.StringIO())
        self.assertFalse(Grade.objects.filter(pk=g.pk).exists())
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a, date=FUTURE()).exists())

    def test_apply_recalculates_quarter(self):
        """A phantom low mark inflating nothing: after cleanup the
        quarter re-derives from the real scores only."""
        today = TODAY()
        future = FUTURE()
        q_future = get_date_quarter(future)
        year = academic_year_start(future)
        make_grade(self.s1, score=9, date=today)
        self._future_grade()
        qg = QuarterGrade.objects.get(
            student=self.s1, subject=MATH,
            quarter=q_future, academic_year=year)
        # Phantom 3 pulled the stored value down before cleanup.
        if get_date_quarter(today) == q_future:
            self.assertEqual(qg.grade, 6)
        else:
            self.assertEqual(qg.grade, 3)

        call_command('delete_future_grades', apply=True, stdout=io.StringIO())

        qg_qs = QuarterGrade.objects.filter(
            student=self.s1, subject=MATH,
            quarter=q_future, academic_year=year)
        if get_date_quarter(today) == q_future:
            # Same quarter: only the real 9 remains.
            self.assertEqual(qg_qs.get().grade, 9)
        else:
            # Boundary case: no same-quarter sources left → row dropped.
            self.assertFalse(qg_qs.exists())
