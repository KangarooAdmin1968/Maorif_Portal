"""🔎 Назорат — per-school control tab.

Zavuch is locked to their own school; superuser/staff/director may pick
any school. Sections covered here: ungraded students and 1-3-band
students for the selected quarter.
"""
from django.urls import reverse

from .helpers import (
    D_Q1, D_Q1_B, D_Q2, GoldenBase, MATH, make_grade,
    make_quarter_grade, make_user,
)


class NazoratTests(GoldenBase):
    """GoldenBase: school A s1+s2 in 7-А; school B s3 in 7-Б."""

    def test_zavuch_sees_own_school_only(self):
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 200)
        # s1 graded; s2 ungraded -> listed by name, s3 never leaks in.
        self.assertContains(resp, 'Мадина Каримова')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Ҷаҳонгир Назаров')
        # No school picker for a scoped zavuch.
        self.assertNotContains(resp, 'name="school_id"')
        # Even asking for another school_id keeps their own school.
        resp2 = self.client.get(reverse('nazorat'),
                                {'school_id': self.school_b.id})
        self.assertNotContains(resp2, 'Ҷаҳонгир Назаров')

    def test_director_picks_school_and_sees_low_marks(self):
        make_quarter_grade(self.s3, '7-Б', MATH, 1, grade=3)
        self.client.force_login(self.director)
        resp = self.client.get(
            reverse('nazorat'), {'school_id': self.school_b.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ҷаҳонгир Назаров')
        # s3 avg 3 -> counted in the 1-3 card and listed with its mean.
        stats = resp.context['stats']
        self.assertEqual(stats['totals']['low'], 1)
        self.assertEqual(
            stats['classes'][0]['low'][0]['name'], 'Ҷаҳонгир Назаров')
        self.assertEqual(stats['classes'][0]['low'][0]['avg'], 3.0)

    def test_all_graded_shows_ok_note(self):
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=8)
        make_quarter_grade(self.s2, '7-А', MATH, 1, grade=7)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'))
        self.assertContains(resp, 'Ҳамаи хонандагон баҳо гирифтаанд')

    def test_teacher_and_anonymous_denied(self):
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 302)          # -> login
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 302)          # -> dashboard

    def test_nav_link_visible_to_zavuch(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, reverse('nazorat'))

    def test_staff_sees_school_picker(self):
        staff = make_user('maorif_staff_nz', staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="school_id"')

    def test_attendance_month_and_quarter(self):
        # October: s1 unexcused, s2 excused. November: s2 unexcused.
        make_grade(self.s1, attendance='-', date=D_Q1)      # Oct
        make_grade(self.s2, attendance='+', date=D_Q1)      # Oct
        make_grade(self.s2, attendance='-', date=D_Q1_B)    # Nov
        make_grade(self.s1, attendance='-', date=D_Q2)      # Jan (q2)
        self.client.force_login(self.zavuch_a)

        resp = self.client.get(reverse('nazorat'), {'att': 'm10'})
        att = resp.context['attendance']
        cls = att['classes'][0]
        self.assertEqual(cls['class_name'], '7-А')
        self.assertEqual((cls['total'], cls['excused'], cls['unexcused']),
                         (2, 1, 1))
        self.assertEqual(att['totals']['total'], 2)
        self.assertIn('Октябр', att['period_label'])

        resp = self.client.get(reverse('nazorat'), {'att': 'q1'})
        att = resp.context['attendance']
        # Q1 spans Sep-Nov: the two Oct marks + one Nov mark; Jan is out.
        self.assertEqual(att['totals']['total'], 3)
        self.assertEqual(att['totals']['unexcused'], 2)

        # Month dropdown offers every academic month + quarters.
        options = [v for v, _ in resp.context['att_options']]
        self.assertIn('m9', options)
        self.assertIn('m8', options)
        self.assertIn('q4', options)

    def test_attendance_other_school_not_leaked(self):
        make_grade(self.s3, attendance='-', date=D_Q1)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'), {'att': 'm10'})
        att = resp.context['attendance']
        self.assertEqual(att['totals']['total'], 0)   # school A has none

    def test_teacher_gaps_only_listed(self):
        # 7-А МАТЕМАТИКА has 2 students; only s1 has a journal mark this
        # quarter -> teacher_1's row shows 1/2; the auto-seeded default
        # subjects (no teacher assigned) appear as таъйин нашудааст.
        make_grade(self.s1, subject=MATH, score=9, date=D_Q1)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'))
        teachers = resp.context['teachers']
        math_rows = [r for r in teachers['rows'] if r['subject'] == MATH]
        self.assertEqual(len(math_rows), 1)
        row = math_rows[0]
        self.assertEqual(
            (row['class_name'], row['graded'], row['total']),
            ('7-А', 1, 2))
        self.assertEqual(row['teacher'], 'teacher_1')
        self.assertTrue(any(r['teacher'] == 'таъйин нашудааст'
                            for r in teachers['rows']))
        # School B's class never leaks into school A's page.
        self.assertFalse(any(r['class_name'] == '7-Б'
                             for r in teachers['rows']))

        # Once s2 has a mark too, the МАТЕМАТИКА gap disappears while
        # still-ungraded subjects remain listed.
        make_grade(self.s2, subject=MATH, score=8, date=D_Q1)
        resp = self.client.get(reverse('nazorat'))
        math_rows = [r for r in resp.context['teachers']['rows']
                     if r['subject'] == MATH]
        self.assertEqual(math_rows, [])

    def test_teacher_control_school_isolated(self):
        # School A fully graded -> gaps only come from the auto-seeded
        # unassigned subjects; school B's 7-Б never shows up.
        make_grade(self.s1, subject=MATH, score=9, date=D_Q1)
        make_grade(self.s2, subject=MATH, score=9, date=D_Q1)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'))
        rows = resp.context['teachers']['rows']
        self.assertFalse(any(r['subject'] == MATH for r in rows))
        self.assertFalse(any(r['class_name'] == '7-Б' for r in rows))
        # Un-graded subjects (СОАТИ ТАРБИЯВӢ) never appear as a gap.
        self.assertFalse(any('ТАРБИЯВ' in r['subject'] for r in rows))
