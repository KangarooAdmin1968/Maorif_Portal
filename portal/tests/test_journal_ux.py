"""Journal UX markup-contract tests — sticky headers/columns, active
row/column highlighting, desktop-vs-mobile grade panel, Давомот/Тартиб
tabs, and navigation. These assert rendered markup/CSS/JS hooks only;
grading semantics are covered by the existing suites.
"""
import datetime
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from portal.models import Grade, Lesson, SubjectGroupMembership, TeachingGroup
from portal.tests.helpers import (
    D_Q1, MATH, PERIOD, GoldenBase, make_teacher_profile,
)


class MonthlyJournalUxTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_teacher_profile(cls.teacher_a, cls.school_a)

    def _get(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_sticky_student_column(self):
        html = self._get()
        self.assertIn('position: sticky; left: 0', html)
        self.assertIn('stu-head', html)
        self.assertIn('stu-cell', html)

    def test_sticky_date_header(self):
        html = self._get()
        self.assertIn('th.date-head { height: 1.7rem; position: sticky; top: 0', html)
        self.assertIn('thead .sub-head { position: sticky', html)
        self.assertIn('th.stu-head { top: 0; z-index: 5', html)

    def test_wrap_is_bounded_scroller(self):
        html = self._get()
        self.assertIn('.month-wrap { overflow: auto; max-height: 74vh; }', html)

    def test_active_row_and_column_hooks(self):
        html = self._get()
        for marker in ('mrow-active', 'mcol-active',
                       'applyHighlights', 'clearHighlights'):
            self.assertIn(marker, html)
        self.assertIn('applyHighlights(activeCell)', html)

    def test_active_cell_preserved(self):
        html = self._get()
        for marker in ('mcell-active', 'tabindex="-1"', 'setActiveCell',
                       'commitTypeBuf', 'typeBuf'):
            self.assertIn(marker, html)

    def test_keyboard_workflow_preserved(self):
        html = self._get()
        for marker in ("'Enter'", "'ArrowDown'", "'ArrowUp'",
                       "'Backspace'", "'Escape'", 'moveFocus'):
            self.assertIn(marker, html)

    def test_desktop_picker_hidden_mobile_preserved(self):
        html = self._get()
        # Desktop >=769px: floating panel hidden.
        self.assertIn('@media (min-width: 769px)', html)
        self.assertIn('.grade-picker { display: none !important; }', html)
        # Mobile <=768px: bottom-sheet picker preserved.
        self.assertIn('@media (max-width: 768px)', html)
        self.assertIn('.grade-picker { left: 0 !important', html)
        self.assertIn('id="grade-picker"', html)

    def test_desktop_attendance_behavior_tabs(self):
        html = self._get()
        self.assertIn('id="ab-tabs"', html)
        self.assertIn('Давомот', html)
        self.assertIn('Рафтор', html)
        self.assertIn('data-ab-att=""', html)
        self.assertIn('data-ab-att="+"', html)
        self.assertIn('data-ab-att="-"', html)
        for n in '12345':
            self.assertIn(f'data-ab-beh="{n}"', html)
        # Tabs write through the same stageEdit path — no second backend.
        self.assertIn("stageEdit('attendance', btn.dataset.abAtt)", html)
        self.assertIn("stageEdit('behavior_score', btn.dataset.abBeh)", html)
        # The desktop reveal rule must come AFTER the display:none base rule.
        base = html.index('.ab-tabs { display: none')
        reveal = html.index('@media (min-width: 769px)')
        self.assertGreater(reveal, base)

    def test_arrow_keys_only_no_visible_up_down_buttons(self):
        html = self._get()
        # Visible ▲/▼ controls removed — keyboard is the only source.
        self.assertNotIn('onclick="moveFocus(-1)"', html)
        self.assertNotIn('onclick="moveFocus(1)"', html)
        self.assertNotIn('>▲<', html)
        self.assertNotIn('>▼<', html)

    def test_horizontal_arrow_navigation(self):
        import re
        html = self._get()
        for marker in ("'ArrowLeft'", "'ArrowRight'", 'moveFocusCol'):
            self.assertIn(marker, html)
        # Horizontal arrows move the cell — they never navigate away.
        m = re.search(r'function moveFocusCol.*?\n\}', html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn('location', m.group(0))
        self.assertNotIn('month', m.group(0))

    def test_save_keeps_context(self):
        html = self._get()
        body = html.split('function saveMonthly')[1].split('function discardEdits')[0]
        self.assertNotIn('location.reload', body)
        self.assertNotIn('location.href', body)
        self.assertIn('monthlySaveUrl', html)
        self.assertIn('save-bar', html)

    def test_mobile_day_cards_preserved(self):
        html = self._get()
        self.assertIn('day-list', html)
        self.assertIn('day-card', html)
        self.assertIn('.month-wrap { display: none; }', html)

    def test_scroll_margins_present(self):
        html = self._get()
        self.assertIn('scroll-margin-top', html)
        self.assertIn('scroll-margin-left', html)

    def test_unsaved_protection_preserved(self):
        html = self._get()
        self.assertIn('beforeunload', html)
        self.assertIn('leave-modal', html)


class DailyJournalUxTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_teacher_profile(cls.teacher_a, cls.school_a)

    def _get(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_wrap_and_sticky_header(self):
        html = self._get()
        self.assertIn('class="daily-wrap"', html)
        self.assertIn('.daily-wrap { overflow-x: auto; }', html)
        self.assertIn('#daily .data-table thead th', html)
        self.assertIn('position: sticky; top: 0', html)

    def test_sticky_first_two_columns(self):
        html = self._get()
        self.assertIn('position: sticky; left: 0', html)
        self.assertIn('left: 3rem', html)

    def test_active_row_highlight_hooks(self):
        html = self._get()
        self.assertIn('drow-active', html)
        self.assertIn("tr.classList.add('drow-active')", html)
        self.assertIn("tr.classList.remove('drow-active')", html)

    def test_desktop_quick_pick_hidden_mobile_preserved(self):
        html = self._get()
        self.assertIn('@media (min-width: 769px)', html)
        self.assertIn('.quick-pick { display: none !important; }', html)
        self.assertIn('.quick-pick { left: 0 !important', html)
        self.assertIn('showQuickPick', html)

    def test_date_navigation_preserved(self):
        html = self._get()
        self.assertIn('onclick="navDate(-1)"', html)
        self.assertIn('onclick="navDate(1)"', html)
        self.assertIn('navToDate', html)

    def test_lesson_chips_and_add_button_preserved(self):
        html = self._get()
        self.assertIn('lesson-chip', html)
        self.assertIn('+ Дарси нав', html)
        self.assertIn('selectLesson', html)

    def test_quarter_table_sticky_too(self):
        html = self._get()
        self.assertIn('.quarter-wrapper { max-height: 75vh; overflow: auto; }',
                      html)

    def _get_daily(self, **params):
        self.client.force_login(self.teacher_a)
        return self.client.get(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), params)

    def test_lesson_slot_grade_flagged_on_umumi_view(self):
        # A Ҷорӣ row saved under a lesson slot (the monthly journal writes
        # Grade.lesson whenever a Lesson exists for the day) is correctly
        # absent from the Умумӣ grid — but its chip must flag the data.
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            score=8, date=D_Q1, lesson=lesson)

        resp = self._get_daily(date=D_Q1.isoformat())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['date'], D_Q1)
        # Умумӣ keeps its lesson-less semantics — the row must not leak in.
        self.assertNotIn(self.s1.id, resp.context['daily_grades'])
        self.assertNotIn(self.s1.id, resp.context['daily_attendance'])
        # ...while the Дарси 1 chip advertises the waiting row.
        self.assertContains(resp, 'Сабтшуда: 1')

        # Explicit lesson navigation still resolves the row unchanged.
        resp = self._get_daily(date=D_Q1.isoformat(), lesson=lesson.id)
        self.assertEqual(resp.context['daily_grades'].get(self.s1.id), 8)

    def test_lesson_slot_attendance_flagged_without_leaking(self):
        # Attendance-only lesson rows count toward the badge too, and stay
        # lesson-scoped in Умумӣ.
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            attendance='-', date=D_Q1, lesson=lesson)

        resp = self._get_daily(date=D_Q1.isoformat())
        self.assertNotIn(self.s1.id, resp.context['daily_attendance'])
        self.assertContains(resp, 'class="chip-count"')
        self.assertContains(resp, 'Сабтшуда: 1')

    def test_no_badge_when_lesson_slot_empty(self):
        Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1)
        resp = self._get_daily(date=D_Q1.isoformat())
        self.assertNotIn('class="chip-count"', resp.content.decode())

    def test_no_badge_on_selected_lesson_view(self):
        # The aggregate is gated on the Умумӣ view — a lesson view must not
        # pay for it, and chips render plain there.
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            score=8, date=D_Q1, lesson=lesson)
        resp = self._get_daily(date=D_Q1.isoformat(), lesson=lesson.id)
        self.assertNotIn('class="chip-count"', resp.content.decode())

    def test_umumi_badge_costs_exactly_one_query(self):
        # Regression guard: the discoverability hint is ONE grouped
        # aggregate, not per-lesson/per-student work.
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1)
        Grade.objects.create(
            student=self.s1, subject=MATH, period=PERIOD,
            score=8, date=D_Q1, lesson=lesson)
        self.client.force_login(self.teacher_a)
        url = reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH})
        # Warm up once — the first request performs one-off middleware
        # writes (DailyActivity) that would skew the comparison.
        self.client.get(url, {'date': D_Q1.isoformat()})
        with CaptureQueriesContext(connection) as umumi:
            self.client.get(url, {'date': D_Q1.isoformat()})
        with CaptureQueriesContext(connection) as lesson_view:
            self.client.get(url, {'date': D_Q1.isoformat(),
                                  'lesson': lesson.id})
        self.assertEqual(len(umumi) - len(lesson_view), 1)

    def test_default_date_uses_localdate_not_server_date(self):
        # TIME_ZONE=Asia/Dushanbe: the default day must follow the project
        # timezone, not the OS clock — otherwise late-evening opens land
        # on the wrong date.
        self.client.force_login(self.teacher_a)
        fake = datetime.date(2030, 5, 4)
        with mock.patch.object(timezone, 'localdate', return_value=fake):
            resp = self._get_daily()
        self.assertEqual(resp.context['date'], fake)

    def test_group_label_on_lesson_chip(self):
        profile = self.teacher_a.teacherprofile
        g = TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1', teacher=profile)
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_a, group=g, student=self.s1)
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_a, group=g, student=self.s2)
        Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1, group=g)
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH}), {'date': D_Q1.isoformat()})
        self.assertContains(resp, 'Гурӯҳи 1')
