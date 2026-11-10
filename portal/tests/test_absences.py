"""⏰ Ғоибият — the journal's back register: per-student missed-lesson
counts (бо сабаб / бе сабаб) for a class and period.

Covered:
  * counting — '+' → сабабли, '−' → сабабсиз, canonical cells deduped,
    scored rows never count;
  * роҳбари синф scope — the teacher allocated to the class's СОАТИ
    ТАРБИЯВӢ sees only their own class(es); a teacher who leads no
    class is turned away;
  * zavuch sees every graded class of the school, the district viewer
    picks a school;
  * home-educated students stay out of the register;
  * the period selector really bounds the counted window;
  * Excel export uses the same scoping.
"""
import datetime

from django.test import Client
from django.urls import reverse

from portal.absences import (collect_class_absences,
                             led_class_names)
from portal.models import Grade, StudentExemption
from portal.tests.helpers import (
    D_Q1, D_Q2, D_Q3, GoldenBase, MATH, lock_quarter,
    make_class_subject, make_grade, make_student, make_user)

TARBIYAVI = 'СОАТИ ТАРБИЯВӢ'


class AbsenceFixture(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # teacher_a leads 7-А — its Тарбиявӣ hour is allocated to them.
        make_class_subject(cls.school_a, '7-А', TARBIYAVI,
                           teacher=cls.teacher_a)
        # A second teacher at school A who leads nothing.
        cls.teacher_c = make_user('teacher_c', role='teacher',
                                  school=cls.school_a)


class CountTests(AbsenceFixture):
    def test_excused_and_unexcused_split(self):
        make_grade(self.s1, attendance='+', date=D_Q1)
        make_grade(self.s1, attendance='-', date=D_Q2, subject='ТОҶИКӢ')
        make_grade(self.s1, attendance='-', date=D_Q2, subject='ФИЗИКА')
        counts = collect_class_absences(
            self.school_a, '7-А',
            D_Q1 - datetime.timedelta(days=30),
            D_Q2 + datetime.timedelta(days=30))
        row = next(r for r in counts if r['student'] == 'Фирӯз Раҳимов')
        self.assertEqual(row['excused'], 1)
        self.assertEqual(row['unexcused'], 2)
        self.assertEqual(row['total'], 3)

    def test_canonical_duplicates_count_once(self):
        # Two identical marks in one logical cell → one missed hour.
        make_grade(self.s1, attendance='-', date=D_Q1)
        make_grade(self.s1, attendance='-', date=D_Q1)
        counts = collect_class_absences(
            self.school_a, '7-А',
            D_Q1 - datetime.timedelta(days=5),
            D_Q1 + datetime.timedelta(days=5))
        row = next(r for r in counts if r['student'] == 'Фирӯз Раҳимов')
        self.assertEqual(row['unexcused'], 1)

    def test_scores_never_count(self):
        make_grade(self.s1, score=9, date=D_Q1)
        make_grade(self.s1, score=None, attendance=None, date=D_Q2)
        counts = collect_class_absences(
            self.school_a, '7-А',
            D_Q1 - datetime.timedelta(days=5),
            D_Q2 + datetime.timedelta(days=5))
        row = next(r for r in counts if r['student'] == 'Фирӯз Раҳимов')
        self.assertEqual(row['total'], 0)

    def test_every_rostered_student_appears(self):
        make_grade(self.s1, attendance='-', date=D_Q1)
        counts = collect_class_absences(
            self.school_a, '7-А',
            D_Q1 - datetime.timedelta(days=5), D_Q1)
        # Both 7-А students are listed; the absent one has 0s.
        self.assertEqual(len(counts), 2)
        zero = next(r for r in counts if r['student'] == 'Мадина Каримова')
        self.assertEqual(zero['total'], 0)

    def test_home_student_excluded(self):
        StudentExemption.objects.create(
            student=self.s2, reason='home', start_date=D_Q1)
        counts = collect_class_absences(
            self.school_a, '7-А',
            D_Q1 - datetime.timedelta(days=5), D_Q1)
        self.assertEqual([r['student'] for r in counts],
                         ['Фирӯз Раҳимов'])


class LedClassesTests(AbsenceFixture):
    def test_led_class_names(self):
        self.assertEqual(led_class_names(self.teacher_a, self.school_a),
                         {'7-А'})
        self.assertEqual(led_class_names(self.teacher_c, self.school_a),
                         set())
        self.assertEqual(led_class_names(self.teacher_b, self.school_b),
                         set())

    def test_allocated_teacher_also_leads(self):
        from portal.tests.helpers import make_teacher_profile
        tp = make_teacher_profile(self.teacher_c, self.school_a)
        make_class_subject(self.school_a, '8-А', TARBIYAVI,
                           allocated=tp)
        self.assertIn('8-А', led_class_names(self.teacher_c, self.school_a))


class ViewAccessTests(AbsenceFixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        make_grade(self.s1, attendance='-', date=D_Q1)

    def test_class_leader_sees_own_class(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('absences'), {'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertContains(resp, '7-А')

    def test_class_leader_cannot_open_other_class(self):
        self.client.force_login(self.teacher_a)
        # 8-А exists in school A but teacher_a does not lead it.
        make_student(self.school_a, '8-А', 'Алӣ Юлдошев')
        resp = self.client.get(
            reverse('absences'), {'class': '8-А', 'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Алӣ Юлдошев')

    def test_teacher_without_lead_class_redirected(self):
        self.client.force_login(self.teacher_c)
        resp = self.client.get(reverse('absences'))
        self.assertEqual(resp.status_code, 302)

    def test_zavuch_sees_school_classes(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('absences'), {'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')

    def test_director_picks_school(self):
        self.client.force_login(self.director)
        resp = self.client.get(
            reverse('absences'),
            {'school_id': self.school_a.id, 'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')

    def test_director_without_school_gets_picker(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse('absences'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_period_bounds_the_count(self):
        self.client.force_login(self.teacher_a)
        make_grade(self.s1, attendance='-', date=D_Q3)  # far later
        resp = self.client.get(reverse('absences'), {'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        # Only the Q1 mark is inside the window → total column shows 1.
        row = next(r for r in resp.context['rows']
                   if r['student'] == 'Фирӯз Раҳимов')
        self.assertEqual(row['unexcused'], 1)
        resp = self.client.get(reverse('absences'), {'period': 'all'})
        row = next(r for r in resp.context['rows']
                   if r['student'] == 'Фирӯз Раҳимов')
        self.assertEqual(row['unexcused'], 2)

    def test_export_same_scope(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('absences_export'), {'period': 'q1'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])

    def test_export_rejected_for_teacher_without_class(self):
        self.client.force_login(self.teacher_c)
        resp = self.client.get(reverse('absences_export'))
        self.assertEqual(resp.status_code, 302)


class RetagTests(AbsenceFixture):
    """POST on the Ғоибият page — re-tag a student's marks inside a
    date window (маълумотнома arrived, or a mark set by mistake)."""

    def setUp(self):
        super().setUp()
        self.client = Client()

    def _post(self, user, **over):
        data = {
            'student_id': str(self.s1.id),
            'date_from': str(D_Q1 - datetime.timedelta(days=1)),
            'date_to': str(D_Q1 + datetime.timedelta(days=1)),
            'action': 'excuse',
        }
        data.update(over)
        self.client.force_login(user)
        return self.client.post(
            reverse('absences') + '?class=7-А&period=q1', data)

    def test_excuse_flips_marks(self):
        g1 = make_grade(self.s1, attendance='-', date=D_Q1)
        g2 = make_grade(self.s1, attendance='-', date=D_Q1,
                        subject='ТОҶИКӢ')
        keep = make_grade(self.s1, attendance='+', date=D_Q1,
                          subject='ФИЗИКА')
        resp = self._post(self.teacher_a)
        self.assertEqual(resp.status_code, 302)
        for g in (g1, g2, keep):
            g.refresh_from_db()
        self.assertEqual(g1.attendance, '+')
        self.assertEqual(g2.attendance, '+')
        self.assertEqual(keep.attendance, '+')

    def test_unexcuse_flips_back(self):
        g = make_grade(self.s1, attendance='+', date=D_Q1)
        self._post(self.teacher_a, action='unexcuse')
        g.refresh_from_db()
        self.assertEqual(g.attendance, '-')

    def test_locked_quarter_stays_frozen(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        locked_g = make_grade(self.s1, attendance='-', date=D_Q1,
                              subject=MATH)
        free_g = make_grade(self.s1, attendance='-', date=D_Q1,
                            subject='ТОҶИКӢ')
        self._post(self.teacher_a)
        locked_g.refresh_from_db()
        free_g.refresh_from_db()
        self.assertEqual(locked_g.attendance, '-')
        self.assertEqual(free_g.attendance, '+')

    def test_scored_row_untouched(self):
        g = make_grade(self.s1, score=8, attendance='-', date=D_Q1)
        self._post(self.teacher_a)
        g.refresh_from_db()
        self.assertEqual(g.attendance, '-')
        self.assertEqual(g.score, 8)

    def test_outside_window_untouched(self):
        g = make_grade(self.s1, attendance='-', date=D_Q2)
        self._post(self.teacher_a)
        g.refresh_from_db()
        self.assertEqual(g.attendance, '-')

    def test_swapped_dates_still_work(self):
        g = make_grade(self.s1, attendance='-', date=D_Q1)
        self._post(
            self.teacher_a,
            date_from=str(D_Q1 + datetime.timedelta(days=1)),
            date_to=str(D_Q1 - datetime.timedelta(days=1)))
        g.refresh_from_db()
        self.assertEqual(g.attendance, '+')

    def test_leader_cannot_retag_other_class(self):
        other = make_student(self.school_a, '8-А', 'Алӣ Юлдошев')
        g = make_grade(other, attendance='-', date=D_Q1)
        self._post(self.teacher_a, student_id=other.id)
        g.refresh_from_db()
        self.assertEqual(g.attendance, '-')

    def test_teacher_without_class_cannot_post(self):
        g = make_grade(self.s1, attendance='-', date=D_Q1)
        resp = self._post(self.teacher_c)
        g.refresh_from_db()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('dashboard'))
        self.assertEqual(g.attendance, '-')

    def test_zavuch_can_retag_own_school(self):
        g = make_grade(self.s1, attendance='-', date=D_Q1)
        self._post(self.zavuch_a)
        g.refresh_from_db()
        self.assertEqual(g.attendance, '+')

    def test_home_student_rejected(self):
        StudentExemption.objects.create(
            student=self.s2, reason='home', start_date=D_Q1)
        g = make_grade(self.s2, attendance='-', date=D_Q1)
        self._post(self.teacher_a, student_id=self.s2.id)
        g.refresh_from_db()
        self.assertEqual(g.attendance, '-')

    def test_other_fields_never_change(self):
        g = make_grade(self.s1, attendance='-', date=D_Q1)
        self._post(self.teacher_a)
        fresh = Grade.objects.get(pk=g.pk)
        self.assertEqual(fresh.student_id, self.s1.id)
        self.assertEqual(fresh.subject, g.subject)
        self.assertEqual(fresh.date, D_Q1)
        self.assertIsNone(fresh.score)
