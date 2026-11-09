"""Norm-pace indicators — the four-colour journal monitor.

The math (green/yellow/red/blue dots, the four collapsible lists and the
tooltips) lives in portal/norm_pace.py and is shared verbatim by
grade_entry, monthly_journal and Назорат. These tests pin the approved
colour rules — met/behind/critical/exempt — plus the panel markup, the
exemption exclusions and the zero-extra-query guarantee.

Determinism: every pace call takes an explicit ``today``, so the tests
pick dates inside the official quarter windows instead of depending on
the wall clock.
"""
import datetime

from django.db.models import Q
from django.urls import reverse

from portal.academic_calendar import quarter_windows
from portal.exemptions import active_exemption_map
from portal.models import Grade, Lesson, Student, StudentExemption
from portal.norm_pace import (ATTENDANCE_MARKS, absence_counts,
                              norm_pace_context, school_days_left)

from .helpers import D_Q1, GoldenBase, MATH, PERIOD, make_grade

_AY = (datetime.date.today().year if datetime.date.today().month >= 9
       else datetime.date.today().year - 1)
Q1_START, Q1_END = quarter_windows(_AY)[1]


def _pace(cls, today, subject=MATH):
    """norm_pace_context for class 7-А School A — with per_student and
    absences from the same pool _journal_norm_status builds."""
    from portal.views import _current_quarter_bounds
    q_start, q_end = _current_quarter_bounds(today)
    students = list(Student.objects.filter(
        school=cls.school_a, class_name='7-А').order_by('full_name'))
    rows = list(Grade.objects.filter(
        student__in=students, subject=subject,
        date__range=(q_start, q_end)).filter(
            Q(score__isnull=False) | Q(attendance__in=ATTENDANCE_MARKS)))
    per_student = {}
    for g in rows:
        if g.score is not None:
            per_student[g.student_id] = per_student.get(g.student_id, 0) + 1
    ex = active_exemption_map(s.id for s in students)
    return norm_pace_context(
        '7-А', subject, cls.cs_a, students, today, ex, per_student,
        absence_counts(rows))


class SchoolDaysLeftTests(GoldenBase):
    """The working-day counter behind the opportunities estimate."""

    def test_counts_mon_sat_only(self):
        # 2026-10-16 Fri -> q_end 2026-10-19 Mon: Sat 17 + Mon 19 = 2,
        # Sun 18 skipped.
        self.assertEqual(
            school_days_left(datetime.date(2026, 10, 16),
                             datetime.date(2026, 10, 19)), 2)

    def test_zero_at_quarter_end(self):
        self.assertEqual(school_days_left(Q1_END, Q1_END), 0)
        self.assertEqual(
            school_days_left(Q1_END + datetime.timedelta(days=3), Q1_END), 0)

    def test_cap_at_quarter_end(self):
        # Days beyond q_end never count — inter-quarter vacations are
        # excluded by the bound itself.
        got = school_days_left(Q1_END - datetime.timedelta(days=4), Q1_END)
        raw = sum(1 for i in range(1, 5)
                  if (Q1_END - datetime.timedelta(days=4)
                      + datetime.timedelta(days=i)).weekday() != 6)
        self.assertEqual(got, raw)


class PaceStatusTests(GoldenBase):
    """Colour rules at a controlled mid-quarter date."""

    TODAY = Q1_START + datetime.timedelta(days=44)  # same slot as D_Q1

    def setUp(self):
        super().setUp()
        # 1 h/week -> norm 3; mid-quarter (day 45/61) planned≈2.2 and the
        # remaining Mon–Sat days (~14) give opportunities ≈ 2.3, so all
        # three colours are reachable inside one quarter.
        self.cs_a.hours_per_week = 1
        self.cs_a.save(update_fields=['hours_per_week'])

    def test_met_status(self):
        for i in range(3):
            make_grade(self.s1, score=8,
                       date=Q1_START + datetime.timedelta(days=i + 1))
        pace = _pace(self, self.TODAY)
        self.assertEqual(pace['by_id'][self.s1.id]['status'], 'met')
        self.assertIn(self.s1.id,
                      {r['student'].id for r in pace['met']})

    def test_behind_status(self):
        # actual=1 < planned(≈2.2), needed=2 <= opportunities(≈2.3) -> yellow.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        pace = _pace(self, self.TODAY)
        rec = pace['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'behind')
        self.assertLess(rec['actual'], rec['planned'])
        self.assertIn(self.s1.id,
                      {r['student'].id for r in pace['behind']})
        self.assertNotIn(self.s1.id,
                         {r['student'].id for r in pace['met']})

    def test_critical_status(self):
        # Late in the quarter too few working days remain to reach the
        # norm: actual=1, needed=2 > opportunities -> red.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        pace = _pace(self, Q1_END - datetime.timedelta(days=2))
        rec = pace['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'critical')
        self.assertIn(self.s1.id,
                      {r['student'].id for r in pace['behind']})

    def test_zero_grades_is_ungraded_status(self):
        # A student with no real grade gets the dedicated gray state —
        # separate from behind/critical and never counted as met.
        pace = _pace(self, self.TODAY)
        rec = pace['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'ungraded')
        self.assertTrue(rec['dot'])
        self.assertEqual(rec['status_title'], 'Баҳогузорӣ нашудааст')
        self.assertIn(self.s1.id,
                      {r['student'].id for r in pace['ungraded']})
        self.assertNotIn(self.s1.id,
                         {r['student'].id for r in pace['behind']})
        self.assertNotIn(self.s1.id,
                         {r['student'].id for r in pace['met']})

    def test_quarter_end_makes_shortfall_critical(self):
        # On the last quarter day there are no working days left: any
        # student below the full norm is red, never yellow.
        make_grade(self.s1, score=8, date=Q1_START)
        pace = _pace(self, Q1_END)
        self.assertEqual(pace['by_id'][self.s1.id]['status'], 'critical')
        for i in range(3):
            make_grade(self.s2, score=8,
                       date=Q1_START + datetime.timedelta(days=i + 1))
        pace = _pace(self, Q1_END)
        self.assertEqual(pace['by_id'][self.s2.id]['status'], 'met')

    def test_exempt_is_blue_not_ungraded(self):
        StudentExemption.objects.create(
            student=self.s1, reason='abroad', start_date=Q1_START)
        pace = _pace(self, self.TODAY)
        rec = pace['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'exempt')
        self.assertIn(self.s1.id,
                      {r['student'].id for r in pace['exempt']})
        self.assertNotIn(self.s1.id,
                         {r['student'].id for r in pace['ungraded']})
        self.assertNotIn(self.s1.id,
                         {r['student'].id for r in pace['behind']})
        self.assertEqual(rec['reason'], 'Дар давлати дигар таҳсил мекунад')

    def test_expired_exemption_returns_to_normal_status(self):
        StudentExemption.objects.create(
            student=self.s1, reason='abroad',
            start_date=Q1_START,
            end_date=Q1_START + datetime.timedelta(days=10))
        pace = _pace(self, self.TODAY)
        self.assertNotEqual(pace['by_id'][self.s1.id]['status'], 'exempt')

    def test_ungraded_subject_has_no_indicator(self):
        # СОАТИ ТАРБИЯВӢ: norm == 0 -> unrated, no dot anywhere.
        pace = _pace(self, self.TODAY, subject='СОАТИ ТАРБИЯВӢ')
        for sid, rec in pace['by_id'].items():
            self.assertEqual(rec['status'], 'unrated')
            self.assertFalse(rec['dot'])
        self.assertEqual(pace['ungraded'], [])
        self.assertEqual(pace['behind'], [])
        self.assertEqual(pace['met'], [])

    def test_no_extra_queries(self):
        # The pace pass is pure arithmetic on the aggregated counts.
        students = list(Student.objects.filter(
            school=self.school_a, class_name='7-А'))
        ex = active_exemption_map(s.id for s in students)
        with self.assertNumQueries(0):
            norm_pace_context(
                '7-А', MATH, self.cs_a, students, self.TODAY, ex,
                {self.s1.id: 2})

    def test_tooltip_texts(self):
        StudentExemption.objects.create(
            student=self.s2, reason='abroad', start_date=Q1_START)
        pace = _pace(self, self.TODAY)
        ungraded = pace['by_id'][self.s1.id]
        exempt = pace['by_id'][self.s2.id]
        self.assertIn('Баҳогузорӣ нашудааст', ungraded['tip'])
        self.assertIn('Меъёри семоҳа:', ungraded['tip'])
        self.assertIn('ягон баҳои воқеӣ нагирифтааст', ungraded['tip'])
        self.assertIn('Сабаб: Дар давлати дигар таҳсил мекунад',
                      exempt['tip'])
        self.assertIn('ба шумораи камбудиҳо дохил карда намешавад',
                      exempt['tip'])


class PaceAbsenceTests(GoldenBase):
    """"Иштирок накардан дар дарс" — second-degree norm credit.

    Attendance '+'/'-' rows count toward the teacher's norm check only:
    effective = actual + missed. They never become real grades, never
    help a student with zero real grades, and are deduplicated per
    lesson — never per day.
    """

    TODAY = Q1_START + datetime.timedelta(days=44)

    def setUp(self):
        super().setUp()
        # 1 h/week -> quarter norm 3, mid-quarter planned ~2.2.
        self.cs_a.hours_per_week = 1
        self.cs_a.save(update_fields=['hours_per_week'])

    def _abs(self, student, date, lesson=None, mark='-'):
        return Grade.objects.create(
            student=student, subject=MATH, period=PERIOD,
            date=date, attendance=mark, lesson=lesson)

    def _lesson(self, date, number):
        return Lesson.objects.create(
            class_subject=self.cs_a, date=date, lesson_number=number)

    def test_absences_help_reach_norm(self):
        # 1 real grade + 2 missed-lesson marks -> effective 3 >= norm.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        self._abs(self.s1, Q1_START + datetime.timedelta(days=2))
        self._abs(self.s1, Q1_START + datetime.timedelta(days=3))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'met')
        self.assertEqual(rec['actual'], 1)
        self.assertEqual(rec['missed'], 2)
        self.assertEqual(rec['effective'], 3)
        self.assertIn('Иштирок накардан дар дарс: 2', rec['tip'])

    def test_zero_grades_stays_ungraded_with_many_absences(self):
        # Hard rule: 0 real + 7 missed -> ungraded, never met.
        for i in range(7):
            self._abs(self.s1, Q1_START + datetime.timedelta(days=i + 1))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'ungraded')
        self.assertEqual(rec['missed'], 7)
        self.assertIn('Иштирок накардан дар дарс: 7', rec['tip'])

    def test_over_norm_real_grades_need_no_credit(self):
        # 4 real grades on a norm-3 subject -> met via real grades only.
        for i in range(4):
            make_grade(self.s1, score=8,
                       date=Q1_START + datetime.timedelta(days=i + 1))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'met')
        self.assertEqual(rec['actual'], 4)

    def test_below_norm_with_absences_stays_behind(self):
        # 1 grade + 1 missed = 2 < norm 3 -> still recoverable -> yellow.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        self._abs(self.s1, Q1_START + datetime.timedelta(days=2))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['status'], 'behind')
        self.assertEqual(rec['needed'], 1)

    def test_both_attendance_marks_count(self):
        self._abs(self.s1, Q1_START + datetime.timedelta(days=1), mark='+')
        self._abs(self.s1, Q1_START + datetime.timedelta(days=2), mark='-')
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 2)

    def test_same_lesson_marked_twice_counts_once(self):
        lesson = self._lesson(Q1_START + datetime.timedelta(days=1), 1)
        self._abs(self.s1, lesson.date, lesson=lesson)
        self._abs(self.s1, lesson.date, lesson=lesson, mark='+')
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 1)

    def test_two_lessons_same_day_count_twice(self):
        # Distinct lessons on one date must never merge into one absence.
        d = Q1_START + datetime.timedelta(days=1)
        self._abs(self.s1, d, lesson=self._lesson(d, 1))
        self._abs(self.s1, d, lesson=self._lesson(d, 2))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 2)

    def test_lesson_less_mark_counts_once_per_date(self):
        # A legacy NULL-lesson mark cannot say which lesson was missed on
        # a multi-lesson day — counted exactly once, never estimated.
        d = Q1_START + datetime.timedelta(days=1)
        self._lesson(d, 1)
        self._lesson(d, 2)
        self._abs(self.s1, d)  # lesson=None
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 1)

    def test_null_mark_not_added_over_explicit_marks(self):
        # NULL + explicit mark on the same date: the NULL cannot be
        # attributed to another lesson — only the explicit slot counts.
        d = Q1_START + datetime.timedelta(days=1)
        self._abs(self.s1, d, lesson=self._lesson(d, 1))
        self._abs(self.s1, d)  # legacy lesson-less mark, same day
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 1)

    def test_score_on_same_slot_means_presence(self):
        # A row that also carries a score is attendance in the journal
        # sense — never an absence unit.
        lesson = self._lesson(Q1_START + datetime.timedelta(days=1), 1)
        Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            date=lesson.date, score=8, attendance='-', lesson=lesson)
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 0)
        self.assertEqual(rec['actual'], 1)

    def test_subject_isolation(self):
        # Missed lessons in a different subject must not help MATH.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        Grade.objects.create(
            student=self.s1, subject='ЗАБОНИ ТОҶИКӢ', period=PERIOD,
            date=Q1_START + datetime.timedelta(days=2), attendance='-')
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 0)

    def test_quarter_isolation(self):
        # Missed lessons outside the quarter window must not count.
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        self._abs(self.s1, Q1_END + datetime.timedelta(days=40))
        rec = _pace(self, self.TODAY)['by_id'][self.s1.id]
        self.assertEqual(rec['missed'], 0)


class JournalFulfillmentTests(GoldenBase):
    """The journal-level fulfillment percentage with absence credit."""

    TODAY = Q1_START + datetime.timedelta(days=44)

    def setUp(self):
        super().setUp()
        self.cs_a.hours_per_week = 1   # norm 3
        self.cs_a.save(update_fields=['hours_per_week'])

    def _journal(self):
        from portal.views import _journal_norm_status
        students = list(Student.objects.filter(
            school=self.school_a, class_name='7-А'))
        return _journal_norm_status(
            '7-А', MATH, self.cs_a, students, self.TODAY)

    def test_fulfillment_capped_and_absence_credited(self):
        # s1: 1 grade + 2 missed -> credited up to norm (3), not beyond.
        # s2: 0 grades + 5 missed -> zero credit (grade-less rule).
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        for i in range(2):
            Grade.objects.create(
                student=self.s1, subject=MATH, period=PERIOD,
                date=Q1_START + datetime.timedelta(days=i + 2),
                attendance='-')
        for i in range(5):
            Grade.objects.create(
                student=self.s2, subject=MATH, period=PERIOD,
                date=Q1_START + datetime.timedelta(days=i + 3),
                attendance='-')
        journal = self._journal()
        self.assertEqual(journal['per_student'][self.s1.id], 1)
        self.assertNotIn(self.s2.id, journal['per_student'])
        self.assertLessEqual(journal['fulfillment'], 100.0)

    def test_met_count_uses_effective_but_needs_a_real_grade(self):
        make_grade(self.s1, score=8,
                   date=Q1_START + datetime.timedelta(days=1))
        for i in range(2):
            Grade.objects.create(
                student=self.s1, subject=MATH, period=PERIOD,
                date=Q1_START + datetime.timedelta(days=i + 2),
                attendance='-')
        journal = self._journal()
        self.assertEqual(journal['students_met'], 1)


class JournalPageTests(GoldenBase):
    """Both journals render the same panel, legend and dots."""

    def _get(self, url_name):
        self.client.force_login(self.teacher_a)
        return self.client.get(reverse(
            url_name, args=[self.school_a.id, '7-А', 'Математика']))

    def test_grade_entry_context_and_markup(self):
        resp = self._get('grade_entry')
        self.assertEqual(resp.status_code, 200)
        pace = resp.context['pace']
        self.assertIn('by_id', pace)
        for s in resp.context['students']:
            self.assertIsNotNone(s.pace)
        for label in ('Хонандагоне, ки баҳо нагирифтаанд',
                      'Хонандагоне, ки аз меъёр қафо мондаанд',
                      'Хонандагоне, ки меъёрро иҷро кардаанд',
                      'Хонандагоне, ки баҳогузорӣ талаб карда намешавад',
                      'Сурх — Меъёр иҷро нашудааст',
                      'Зард — Огоҳӣ',
                      'Сабз — Меъёр иҷро шудааст',
                      'Кабуд — Баҳогузорӣ талаб карда намешавад',
                      'Хокистарӣ — Баҳогузорӣ нашудааст',
                      'pace-dot', 'data-tip', 'Меъёри семоҳа:'):
            self.assertContains(resp, label)

    def test_monthly_journal_same_semantics(self):
        resp = self._get('monthly_journal')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('by_id', resp.context['pace'])
        for label in ('Хонандагоне, ки баҳо нагирифтаанд',
                      'Хонандагоне, ки баҳогузорӣ талаб карда намешавад',
                      'pace-dot', 'data-tip'):
            self.assertContains(resp, label)
        # Same calculation, same statuses for the same students.
        ge = self._get('grade_entry')
        for sid in ge.context['pace']['by_id']:
            self.assertEqual(
                ge.context['pace']['by_id'][sid]['status'],
                resp.context['pace']['by_id'][sid]['status'])

    def test_exempt_student_gets_blue_dot_in_html(self):
        StudentExemption.objects.create(
            student=self.s1, reason='abroad',
            start_date=datetime.date.today() - datetime.timedelta(days=30))
        resp = self._get('grade_entry')
        self.assertContains(resp, 'pace-exempt')
        self.assertNotIn(self.s1.id,
                         {r['student'].id
                          for r in resp.context['pace']['ungraded']})

    def test_shared_js_tooltip_present(self):
        # One client-side tip element shared by every dot — the mobile
        # tap path; desktop additionally gets the native title tooltip.
        resp = self._get('grade_entry')
        self.assertContains(resp, 'pace-tip')
        self.assertContains(resp, 'addEventListener')


class NazoratTreeTests(GoldenBase):
    """Омӯзгор → Синф → Хонанда drill-down + the four indicators."""

    def test_collect_norm_tree_structure(self):
        from portal.nazorat import collect_norm_tree
        tree = collect_norm_tree(self.school_a)
        self.assertEqual(set(tree), {'teachers', 'indicators'})
        self.assertEqual(set(tree['indicators']),
                         {'ungraded', 'behind', 'met', 'exempt'})
        self.assertTrue(tree['teachers'])
        item = tree['teachers'][0]['items'][0]
        self.assertIn('class_name', item)
        self.assertIn('rows', item)

    def test_indicators_count_students_not_rows(self):
        # s1 gets a grade in the CURRENT quarter -> lands in a pace
        # bucket (met early in the quarter when planned is tiny,
        # behind later); s2 (no grades) -> ungraded. The fixture date
        # must track the quarter today is in — a date from the quarter
        # that just ended no longer counts after rollover.
        from portal.nazorat import collect_norm_tree
        from portal.views import _current_quarter_bounds
        q_start, q_end = _current_quarter_bounds(datetime.date.today())
        in_q = min(datetime.date.today(), q_end)
        make_grade(self.s1, score=9, date=max(in_q, q_start))
        tree = collect_norm_tree(self.school_a)
        self.assertEqual(tree['indicators']['exempt'], 0)
        self.assertGreaterEqual(
            tree['indicators']['behind'] + tree['indicators']['met'], 1)
        self.assertGreaterEqual(tree['indicators']['ungraded'], 1)

    def test_exempt_counts_separately(self):
        from portal.nazorat import collect_norm_tree
        StudentExemption.objects.create(
            student=self.s1, reason='abroad',
            start_date=datetime.date.today() - datetime.timedelta(days=5))
        tree = collect_norm_tree(self.school_a)
        self.assertEqual(tree['indicators']['exempt'], 1)
        self.assertNotIn(self.s1.id, {
            r['student'].id
            for t in tree['teachers'] for i in t['items'] for r in i['rows']
            if r['status'] in ('behind', 'critical')})

    def test_zavuch_sees_tree_on_page(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.context['norm_tree'])
        self.assertContains(resp, 'Аз меъёр қафо мондаанд')
        self.assertContains(resp, 'Меъёрро иҷро кардаанд')
        self.assertContains(resp, 'Баҳогузорӣ талаб карда намешавад')
        self.assertContains(resp, 'Омӯзгор → Синф → Хонанда')

    def test_regular_teacher_has_no_access(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('nazorat'))
        self.assertEqual(resp.status_code, 302)

    def test_school_isolation(self):
        # School A's tree must never contain school B students.
        from portal.nazorat import collect_norm_tree
        tree_a = collect_norm_tree(self.school_a)
        ids = {r['student'].id
               for t in tree_a['teachers'] for i in t['items']
               for r in i['rows']}
        self.assertNotIn(self.s3.id, ids)


class ArchivedStudentTests(GoldenBase):
    """Archived (рафт) students stay out of pace entirely."""

    def test_archived_absent_from_pace(self):
        self.s2.is_archived = True
        self.s2.archived_at = datetime.datetime.now(datetime.timezone.utc)
        self.s2.save(update_fields=['is_archived', 'archived_at'])
        pace = _pace(self, Q1_START + datetime.timedelta(days=44))
        self.assertNotIn(self.s2.id, pace['by_id'])
