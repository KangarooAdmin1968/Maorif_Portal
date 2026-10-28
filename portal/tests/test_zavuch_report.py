"""Ҳуҷҷатнигорӣ → Ҳисоботи завуч — А.Ҷ.Ч. school summary export.

The workbook fills the owner's template: roster counts per grade level,
per-student quarter-average bands, blank Омад/Рафт input cells and the
template's own formulas for everything derived.
"""
import io

from django.urls import reverse
from openpyxl import load_workbook

from portal.models import StudentMovement
from portal.zavuch_report import GRADES_FIRST_DATA_ROW

from .helpers import (
    D_Q1, D_Q1_B, GoldenBase, MATH, TAJIK, make_quarter_grade,
    make_student,
)


class ZavuchReportExportTests(GoldenBase):
    """Level-aggregated rows: 7-А of school A lands on the СИНФИ 7 row."""

    ROW7 = 10  # FIRST_DATA_ROW(5) + (level 7 - 2)

    def _get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('zavuch_report_export'), params)

    def _ws(self, resp):
        return load_workbook(io.BytesIO(resp.content)).active

    def test_export_layout_counts_and_formulas(self):
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        resp = self._get(self.zavuch_a)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])
        ws = self._ws(resp)
        r = self.ROW7
        # Title carries the school's own name and the quarter ordinal.
        self.assertIn(self.school_a.name, ws['A1'].value)
        self.assertIn('1-ум', ws['A1'].value)
        # Roster: s1 + s2 in 7-А.
        self.assertEqual(ws[f'B{r}'].value, 'СИНФИ 7')
        self.assertEqual(ws[f'C{r}'].value, 2)
        self.assertEqual(ws[f'D{r}'].value, 1)   # Мадина — духтар
        # Омад/Рафт come from the mobility journal — no rows yet -> 0.
        for col in 'EFGH':
            self.assertEqual(ws[f'{col}{r}'].value, 0)
        self.assertEqual(ws[f'I{r}'].value, f'=C{r}+E{r}-G{r}')
        # Graded count + bands: s1 avg 9 -> 9-10; s2 has no grade -> Бе хол.
        self.assertEqual(ws[f'K{r}'].value, 1)
        self.assertEqual(ws[f'N{r}'].value, 1)
        self.assertEqual(ws[f'P{r}'].value, 0)
        self.assertEqual(ws[f'T{r}'].value, 0)
        # Derived band cells are computed values (visible in Protected
        # View); only the Омад/Рафт-dependent chain stays formula-driven.
        self.assertEqual(ws[f'R{r}'].value, 0)
        self.assertEqual(ws[f'O{r}'].value, 100.0)
        self.assertEqual(ws[f'V{r}'].value, f'=I{r}-K{r}')

    def test_per_student_average_decides_band(self):
        # s1: 9 and 7 -> avg 8 -> 7-8 band, not 9-10.
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, 1, grade=7)
        ws = self._ws(self._get(self.zavuch_a))
        r = self.ROW7
        self.assertEqual(ws[f'K{r}'].value, 1)
        self.assertEqual(ws[f'N{r}'].value, 0)
        self.assertEqual(ws[f'P{r}'].value, 1)

    def test_parallel_classes_aggregate_into_one_level_row(self):
        make_student(self.school_a, '7-Б', 'Ҳаким Валиев')
        ws = self._ws(self._get(self.zavuch_a))
        r = self.ROW7
        self.assertEqual(ws[f'C{r}'].value, 3)

    def test_other_school_students_not_counted(self):
        # s3 lives in school B's 7-Б — never leaks into school A's row.
        ws = self._ws(self._get(self.zavuch_a))
        self.assertEqual(ws[f'C{self.ROW7}'].value, 2)

    def test_regular_teacher_denied(self):
        resp = self._get(self.teacher_a)
        self.assertEqual(resp.status_code, 302)

    def test_admin_picks_school_via_param(self):
        resp = self._get(self.admin, school_id=self.school_b.id)
        self.assertEqual(resp.status_code, 200)
        ws = self._ws(resp)
        self.assertIn(self.school_b.name, ws['A1'].value)
        self.assertEqual(ws[f'C{self.ROW7}'].value, 1)

    def test_admin_without_school_redirects(self):
        resp = self._get(self.admin)
        self.assertEqual(resp.status_code, 302)

class ZavuchGradesExportTests(GoldenBase):
    """АЧЧ form: per-level rows + group subtotals + per-mark dist."""

    ROW5 = GRADES_FIRST_DATA_ROW  # 5

    # Column map: 3 classes, 4 start, 5 start_f, 6-9 омад/рафт,
    # 10 end, 11 end_f, 12 graded, 13 graded_f, 14 %graded;
    # marks 10..1 at cols 15..34 (count, %), 35 quality, 36 mastery.

    def _get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('zavuch_grades_export'), params)

    def _ws(self, resp):
        return load_workbook(io.BytesIO(resp.content)).active

    def _names(self, ws):
        return [ws.cell(row=r, column=2).value
                for r in range(self.ROW5, self.ROW5 + 12)]

    def test_export_layout_counts(self):
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        resp = self._get(self.zavuch_a)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])
        ws = self._ws(resp)
        self.assertIn(self.school_a.name, ws['A1'].value)
        self.assertIn('АЧЧ1', ws['A1'].value)
        # One level present -> Синфи 7, Ҳамагӣ (5-9), Ҳамагӣ (синфи 7).
        self.assertEqual(
            self._names(ws)[:3],
            ['Синфи 7', 'Ҳамагӣ (синфҳои 5-9)', 'Ҳамагӣ (синфи 7)'])
        r = self.ROW5
        self.assertEqual(ws.cell(row=r, column=1).value, 1)   # №
        self.assertEqual(ws.cell(row=r, column=3).value, 1)   # 1 класс
        self.assertEqual(ws.cell(row=r, column=4).value, 2)   # аввал
        self.assertEqual(ws.cell(row=r, column=5).value, 1)   # духтарон
        for col in (6, 7, 8, 9):                              # омад/рафт
            self.assertEqual(ws.cell(row=r, column=col).value, 0)
        self.assertEqual(ws.cell(row=r, column=10).value, 2)  # охир
        self.assertEqual(ws.cell(row=r, column=12).value, 1)  # баҳогир.
        self.assertEqual(ws.cell(row=r, column=14).value, 50) # %
        # avg 9 -> mark 9: count at col 17, % at col 18.
        self.assertEqual(ws.cell(row=r, column=17).value, 1)
        self.assertEqual(ws.cell(row=r, column=18).value, 100)
        self.assertEqual(ws.cell(row=r, column=35).value, 100)  # 7-10
        self.assertEqual(ws.cell(row=r, column=36).value, 100)  # 4-10

    def test_absent_levels_and_groups_skipped(self):
        ws = self._ws(self._get(self.zavuch_a))
        names = self._names(ws)
        self.assertNotIn('Синфи 6', names)
        self.assertNotIn('Ҳамагӣ (синфҳои 1-4)', names)
        self.assertNotIn('Ҳамагӣ (синфҳои 10-11)', names)

    def test_groups_and_grand_total(self):
        make_student(self.school_a, '1-А', 'Кишвар Одинаев')
        make_student(self.school_a, '10-А', 'Сабоҳ Неъматова')
        ws = self._ws(self._get(self.zavuch_a))
        self.assertEqual(
            self._names(ws)[:7],
            ['Синфи 1', 'Ҳамагӣ (синфҳои 1-4)', 'Синфи 7',
             'Ҳамагӣ (синфҳои 5-9)', 'Синфи 10', 'Ҳамагӣ (синфҳои 10-11)',
             'Ҳамагӣ (синфҳои 1-10)'])
        # Grand total: 4 students across levels 1, 7, 10.
        r = self.ROW5 + 6
        self.assertEqual(ws.cell(row=r, column=10).value, 4)
        self.assertEqual(ws.cell(row=r, column=3).value, 3)  # 3 синфа

    def test_per_student_average_rounds_to_whole_mark(self):
        # s1: 9 and 7 -> avg 8 -> the «8» column, not «9».
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, 1, grade=7)
        ws = self._ws(self._get(self.zavuch_a))
        r = self.ROW5
        self.assertEqual(ws.cell(row=r, column=17).value, 0)  # 9
        self.assertEqual(ws.cell(row=r, column=19).value, 1)  # 8
        self.assertEqual(ws.cell(row=r, column=12).value, 1)

    def test_movements_fill_omad_raft_and_start(self):
        StudentMovement.objects.create(
            school=self.school_a, student_name='Мадина Каримова',
            class_name='7-А', gender='F', movement_type='departure',
            date=D_Q1, academic_year=D_Q1.year)
        StudentMovement.objects.create(
            school=self.school_a, student_name='Олим Саидов',
            class_name='7-А', gender='M', movement_type='arrival',
            date=D_Q1_B, academic_year=D_Q1.year)
        ws = self._ws(self._get(self.zavuch_a))
        r = self.ROW5
        # аввал = охир(2) - омад(1) + рафт(1) = 2; духтарон 1-0+1=2.
        self.assertEqual(ws.cell(row=r, column=4).value, 2)
        self.assertEqual(ws.cell(row=r, column=5).value, 2)
        self.assertEqual(ws.cell(row=r, column=6).value, 1)   # омад
        self.assertEqual(ws.cell(row=r, column=7).value, 0)
        self.assertEqual(ws.cell(row=r, column=8).value, 1)   # рафт
        self.assertEqual(ws.cell(row=r, column=9).value, 1)
        self.assertEqual(ws.cell(row=r, column=10).value, 2)  # охир

    def test_regular_teacher_denied(self):
        resp = self._get(self.teacher_a)
        self.assertEqual(resp.status_code, 302)

    def test_admin_picks_school_via_param(self):
        resp = self._get(self.admin, school_id=self.school_b.id)
        self.assertEqual(resp.status_code, 200)
        ws = self._ws(resp)
        self.assertIn(self.school_b.name, ws['A1'].value)
        self.assertEqual(self._names(ws)[0], 'Синфи 7')
        self.assertEqual(ws.cell(row=self.ROW5, column=10).value, 1)

    def test_principal_exports_own_school(self):
        resp = self._get(self.principal_a)
        self.assertEqual(resp.status_code, 200)
        ws = self._ws(resp)
        self.assertIn(self.school_a.name, ws['A1'].value)

    def test_principal_cannot_pick_other_school(self):
        resp = self._get(self.principal_a, school_id=self.school_b.id)
        ws = self._ws(resp)
        self.assertIn(self.school_a.name, ws['A1'].value)
