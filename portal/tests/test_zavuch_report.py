"""Ҳуҷҷатнигорӣ → Ҳисоботи завуч — А.Ҷ.Ч. school summary export.

The workbook fills the owner's template: roster counts per grade level,
per-student quarter-average bands, blank Омад/Рафт input cells and the
template's own formulas for everything derived.
"""
import io

from django.urls import reverse
from openpyxl import load_workbook

from .helpers import (
    GoldenBase, MATH, TAJIK, make_quarter_grade, make_student,
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
        # Омад/Рафт stay blank — manual entry until mobility tracking.
        for col in 'EFGH':
            self.assertIsNone(ws[f'{col}{r}'].value)
        self.assertEqual(ws[f'I{r}'].value, f'=C{r}+E{r}-G{r}')
        # Graded count + bands: s1 avg 9 -> 9-10; s2 has no grade -> Бе хол.
        self.assertEqual(ws[f'K{r}'].value, 1)
        self.assertEqual(ws[f'N{r}'].value, 1)
        self.assertEqual(ws[f'P{r}'].value, 0)
        self.assertEqual(ws[f'T{r}'].value, 0)
        self.assertEqual(ws[f'R{r}'].value, f'=K{r}-N{r}-P{r}-T{r}')
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
