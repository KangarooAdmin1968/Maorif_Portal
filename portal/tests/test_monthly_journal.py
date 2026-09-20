"""Stage 4 tests: monthly journal view.

Covers: month grid shape, per-lesson columns (no same-day collapse),
legacy lesson-less slot visibility, completion statuses, control markers,
month navigation, permissions, and the mobile day-list markup.
"""
import datetime

from django.urls import reverse

from portal.models import Assessment, Grade
from portal.tests.helpers import D_Q1, D_Q1_B, MATH, GoldenBase
from portal.tests.test_phase4_lessons import (
    make_control, make_lesson, post_grade,
)

OCT = '2025-10'
NOV = '2025-11'
D_OCT_A = datetime.date(2025, 10, 15)   # D_Q1
D_OCT_B = datetime.date(2025, 10, 20)
D_OCT_C = datetime.date(2025, 10, 22)


class MonthlyJournalTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.url = reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А', 'subject': MATH,
        })

    def _get(self, **params):
        params.setdefault('month', OCT)
        return self.client.get(self.url, params)

    def _col_index(self, ctx, date, lesson_id):
        for i, col in enumerate(ctx['columns']):
            if col['date'] == date and col['lesson_id'] == lesson_id:
                return i
        return None

    def _cells_for(self, ctx, student, date, lesson_id):
        idx = self._col_index(ctx, date, lesson_id)
        self.assertIsNotNone(idx, f'no column for {date} lesson={lesson_id}')
        row = next(r for r in ctx['table_rows'] if r['student'].id == student.id)
        return row['cells'][idx]

    # -- grid shape --------------------------------------------------------

    def test_get_renders_200(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'portal/monthly_journal.html')

    def test_all_days_present(self):
        resp = self._get()
        self.assertEqual(len(resp.context['header_groups']), 31)
        self.assertEqual(len(resp.context['day_rows']), 31)

    def test_one_column_per_lesson_same_day(self):
        l1 = make_lesson(self.cs_a, D_OCT_A, 1)
        l2 = make_lesson(self.cs_a, D_OCT_A, 2)
        resp = self._get()
        idx1 = self._col_index(resp.context, D_OCT_A, l1.id)
        idx2 = self._col_index(resp.context, D_OCT_A, l2.id)
        self.assertIsNotNone(idx1)
        self.assertIsNotNone(idx2)
        # No extra legacy column when it holds no data.
        self.assertIsNone(self._col_index(resp.context, D_OCT_A, None))

    def test_same_day_lesson_grades_not_collapsed(self):
        l1 = make_lesson(self.cs_a, D_OCT_A, 1)
        l2 = make_lesson(self.cs_a, D_OCT_A, 2)
        post_grade(self.client, self.s1, score='8', lesson=l1)
        post_grade(self.client, self.s1, score='10', lesson=l2)
        resp = self._get()
        c1 = self._cells_for(resp.context, self.s1, D_OCT_A, l1.id)
        c2 = self._cells_for(resp.context, self.s1, D_OCT_A, l2.id)
        self.assertEqual([c['text'] for c in c1], ['8'])
        self.assertEqual([c['text'] for c in c2], ['10'])

    def test_legacy_slot_column_only_when_data(self):
        make_lesson(self.cs_a, D_OCT_A, 1)
        post_grade(self.client, self.s1, score='9')  # lesson-less legacy row
        resp = self._get()
        idx = self._col_index(resp.context, D_OCT_A, None)
        self.assertIsNotNone(idx)
        cells = self._cells_for(resp.context, self.s1, D_OCT_A, None)
        self.assertEqual([c['text'] for c in cells], ['9'])

    def test_day_without_lesson_single_column(self):
        post_grade(self.client, self.s1, score='7', date=D_OCT_B)
        resp = self._get()
        idx = self._col_index(resp.context, D_OCT_B, None)
        self.assertIsNotNone(idx)

    # -- status derivation --------------------------------------------------

    def test_status_empty_partial_complete(self):
        post_grade(self.client, self.s1, score='7', date=D_OCT_B)
        post_grade(self.client, self.s1, score='8', date=D_OCT_C)
        post_grade(self.client, self.s2, score='9', date=D_OCT_C)
        resp = self._get()
        status = {d['date']: d['status'] for d in resp.context['day_rows']}
        self.assertEqual(status[D_OCT_A], 'empty')
        self.assertEqual(status[D_OCT_B], 'partial')
        self.assertEqual(status[D_OCT_C], 'complete')

    def test_control_flag_on_date_and_column(self):
        l1 = make_lesson(self.cs_a, D_OCT_A, 1)
        l2 = make_lesson(self.cs_a, D_OCT_A, 2)
        make_control(self.cs_a, date=D_OCT_A, lesson=l1)
        resp = self._get()
        day = next(d for d in resp.context['day_rows'] if d['date'] == D_OCT_A)
        self.assertTrue(day['has_control'])
        self.assertTrue(day['multi_lesson'])
        idx1 = self._col_index(resp.context, D_OCT_A, l1.id)
        idx2 = self._col_index(resp.context, D_OCT_A, l2.id)
        self.assertTrue(resp.context['columns'][idx1]['control'])
        self.assertFalse(resp.context['columns'][idx2]['control'])

    def test_control_grade_cell_marked(self):
        a = make_control(self.cs_a, date=D_OCT_A)
        post_grade(self.client, self.s1, score='6', assessment=a)
        resp = self._get()
        cells = self._cells_for(resp.context, self.s1, D_OCT_A, None)
        self.assertEqual([c['text'] for c in cells], ['6'])
        self.assertTrue(cells[0]['control'])

    def test_absent_mark_shown(self):
        post_grade(self.client, self.s1, extra={'attendance': '-'})
        resp = self._get()
        cells = self._cells_for(resp.context, self.s1, D_OCT_A, None)
        self.assertEqual([c['text'] for c in cells], ['н'])
        self.assertTrue(cells[0]['absent'])

    def test_paired_work_shows_slash_not_mean(self):
        a = make_control(self.cs_a, date=D_OCT_A)
        a.work_type = 'retelling'
        a.save(update_fields=['work_type'])
        post_grade(self.client, self.s1, score='8/9', assessment=a)
        resp = self._get()
        cells = self._cells_for(resp.context, self.s1, D_OCT_A, None)
        self.assertEqual([c['text'] for c in cells], ['8/9'])

    # -- month navigation ---------------------------------------------------

    def test_month_param_selects_month(self):
        post_grade(self.client, self.s1, score='7', date=D_Q1_B)  # November
        resp = self._get(month=OCT)
        # Nov 20 is not in the October grid at all.
        self.assertEqual(len(resp.context['day_rows']), 31)
        self.assertNotIn(
            D_Q1_B, [d['date'] for d in resp.context['day_rows']]
        )
        resp = self._get(month=NOV)
        status = {d['date']: d['status'] for d in resp.context['day_rows']}
        self.assertEqual(status[D_Q1_B], 'partial')
        self.assertEqual(len(resp.context['day_rows']), 30)

    def test_prev_next_month_links(self):
        resp = self._get(month=OCT)
        self.assertEqual(resp.context['prev_month'], datetime.date(2025, 9, 1))
        self.assertEqual(resp.context['next_month'], datetime.date(2025, 11, 1))

    def test_invalid_month_falls_back_to_current(self):
        resp = self.client.get(self.url, {'month': 'not-a-month'})
        self.assertEqual(resp.status_code, 200)
        today = datetime.date.today()
        self.assertEqual(
            resp.context['month_start'], today.replace(day=1)
        )

    # -- permissions ---------------------------------------------------------

    def test_cross_school_teacher_redirected(self):
        self.client.force_login(self.teacher_b)
        resp = self._get()
        self.assertEqual(resp.status_code, 302)

    def test_wrong_subject_teacher_forbidden_or_redirect(self):
        self.client.force_login(self.teacher_a)
        url = reverse('monthly_journal', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': 'ЗАБОНИ ТОҶИКӢ',
        })
        resp = self.client.get(url, {'month': OCT})
        self.assertIn(resp.status_code, (302, 403))

    def test_anonymous_redirected(self):
        self.client.logout()
        resp = self._get()
        self.assertEqual(resp.status_code, 302)

    # -- mobile-safe rendering ------------------------------------------------

    def test_mobile_day_list_rendered(self):
        l1 = make_lesson(self.cs_a, D_OCT_A, 1)
        resp = self._get()
        html = resp.content.decode()
        self.assertIn('day-list', html)
        self.assertIn('month-wrap', html)
        # Lesson chip links into the daily journal for that lesson.
        self.assertIn(f'lesson={l1.id}', html)


class JournalExcelExportTests(GoldenBase):
    """Stage 6: flat lesson-aware export over the same journal data."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.url = reverse('export_journal_excel', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А', 'subject': MATH,
        })

    def _rows(self, resp):
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        return [tuple(c.value for c in r) for r in ws.iter_rows()]

    def test_export_returns_xlsx(self):
        resp = self.client.get(self.url, {'month': OCT})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])

    def test_lesson_number_preserved_per_row(self):
        l1 = make_lesson(self.cs_a, D_OCT_A, 1)
        l2 = make_lesson(self.cs_a, D_OCT_A, 2)
        post_grade(self.client, self.s1, score='8', lesson=l1)
        post_grade(self.client, self.s1, score='10', lesson=l2)
        resp = self.client.get(self.url, {'month': OCT})
        rows = self._rows(resp)
        self.assertEqual(rows[0][:4], ('№', 'Хонанда', 'Сана', 'Дарс'))
        data = [r for r in rows[1:] if r[1] == self.s1.full_name]
        self.assertEqual(len(data), 2)
        self.assertEqual(
            sorted((r[3], r[6]) for r in data), [(1, '8'), (2, '10')]
        )

    def test_legacy_row_exports_empty_lesson(self):
        post_grade(self.client, self.s1, score='9')
        resp = self.client.get(self.url, {'month': OCT})
        rows = self._rows(resp)
        data = [r for r in rows[1:] if r[1] == self.s1.full_name]
        self.assertEqual(len(data), 1)
        self.assertIn(data[0][3], (None, ''))
        self.assertEqual(data[0][6], '9')
        self.assertEqual(data[0][4], 'Ҷорӣ')

    def test_control_row_marked(self):
        a = make_control(self.cs_a, date=D_OCT_A, title='Санҷиши 1')
        post_grade(self.client, self.s1, score='7', assessment=a)
        resp = self.client.get(self.url, {'month': OCT})
        rows = self._rows(resp)
        data = [r for r in rows[1:] if r[1] == self.s1.full_name]
        self.assertEqual(data[0][4], 'Назорат')
        self.assertIn('Санҷиши 1', data[0][5])

    def test_out_of_month_grades_excluded(self):
        post_grade(self.client, self.s1, score='7', date=D_Q1_B)  # Nov
        post_grade(self.client, self.s1, score='8', date=D_OCT_A)
        resp = self.client.get(self.url, {'month': OCT})
        rows = self._rows(resp)
        self.assertEqual(len(rows), 2)  # header + one grade row

    def test_cross_school_teacher_denied(self):
        self.client.force_login(self.teacher_b)
        resp = self.client.get(self.url, {'month': OCT})
        self.assertIn(resp.status_code, (302, 403))
