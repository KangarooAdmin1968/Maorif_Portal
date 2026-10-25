"""Ҳуҷҷатнигорӣ → Ҳисоботи маориф — district А.Ҷ.Ч. summary export.

One row per academic school: roster totals for levels 2-11, участие
(students with quarter grades), per-student quarter-average bands,
percent formulas, the Хамагӣ total row and the level-1 Эзоҳ note.
"""
import io

from django.urls import reverse
from openpyxl import load_workbook

from .helpers import (
    GoldenBase, MATH, make_quarter_grade, make_student, make_user,
)


class MaorifReportExportTests(GoldenBase):
    """GoldenBase: school A has s1+s2 in 7-А, school B has s3 in 7-Б."""

    FIRST = 6   # first data row in the generated sheet

    def _get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('maorif_report_export'), params)

    def _ws(self, resp):
        return load_workbook(io.BytesIO(resp.content)).active

    def test_district_rows_bands_and_formulas(self):
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        make_quarter_grade(self.s3, '7-Б', MATH, 1, grade=3)
        resp = self._get(self.director)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])
        ws = self._ws(resp)
        # Two school rows: МТМУ №1 then МТМУ №2.
        self.assertEqual(ws['B6'].value, 'МТМУ №1')
        self.assertEqual(ws['B7'].value, 'МТМУ №2')
        # School A: 2 rostered, 1 graded (s1 avg 9 -> 10-9 band).
        self.assertEqual(ws['C6'].value, 2)
        self.assertEqual(ws['D6'].value, 1)
        self.assertEqual(ws['E6'].value, 1)
        self.assertEqual(ws['G6'].value, 0)
        self.assertEqual(ws['K6'].value, 0)
        # School B: s3 avg 3 -> 3-1 band.
        self.assertEqual(ws['C7'].value, 1)
        self.assertEqual(ws['D7'].value, 1)
        self.assertEqual(ws['K7'].value, 1)
        # Percent / quality / mastery cells carry computed values — they
        # must display even in Excel's Protected View (no recalc there).
        self.assertEqual(ws['F6'].value, 100.0)   # 1/1 graded -> 10-9
        self.assertEqual(ws['M6'].value, 100.0)   # сифатнокӣ
        self.assertEqual(ws['N6'].value, 100.0)   # no 1-3 grades
        self.assertEqual(ws['N7'].value, 0.0)     # s3 fully in the 3-1 band
        # Хамагӣ total row holds summed counts.
        self.assertEqual(ws['B8'].value, 'Хамагӣ')
        self.assertEqual(ws['C8'].value, 3)
        self.assertEqual(ws['F8'].value, 50.0)    # 1 of 2 graded -> 10-9
        self.assertEqual(ws['M8'].value, 50.0)
        self.assertEqual(ws['N8'].value, 50.0)

    def test_ungraded_students_listed_in_note(self):
        # s1 gets a grade; s2 and s3 stay ungraded -> Эзоҳ lists both.
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=8)
        ws = self._ws(self._get(self.admin))
        note = ws['A10'].value or ''
        self.assertIn('синфи 1-ум', note)
        self.assertIn('синфи 7-А-и МТМУ №1', note)
        self.assertIn('синфи 7-Б-и МТМУ №2', note)

    def test_level1_students_only_in_note(self):
        make_student(self.school_a, '1-А', 'Сарвиноз Одилова')
        ws = self._ws(self._get(self.admin))
        self.assertEqual(ws['C6'].value, 2)   # 1-А excluded from the row
        self.assertIn('синфи 1-ум 1 нафар', ws['A10'].value)

    def test_zavuch_denied_district_data(self):
        resp = self._get(self.zavuch_a)
        self.assertEqual(resp.status_code, 302)

    def test_teacher_denied(self):
        resp = self._get(self.teacher_a)
        self.assertEqual(resp.status_code, 302)

    def test_staff_and_director_allowed(self):
        staff = make_user('maorif_staff', staff=True)
        for user in (self.director, staff):
            resp = self._get(user)
            self.assertEqual(resp.status_code, 200, user.username)

    def test_director_reaches_documents_page(self):
        # The director now sees Ҳуҷҷатнигорӣ with the district button.
        self.client.force_login(self.director)
        resp = self.client.get(reverse('school_documents'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('maorif_report_export'))
