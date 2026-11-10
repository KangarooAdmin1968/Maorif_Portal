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
from portal.models import StudentExemption
from portal.tests.helpers import (
    D_Q1, D_Q2, D_Q3, GoldenBase, make_class_subject, make_grade,
    make_student, make_user)

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
