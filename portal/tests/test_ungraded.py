"""⚠ Бебаҳогон — per-student list of missing quarter marks.

The list reads the shared merged source (stored QuarterGrade + live Ҷ
journal value), so a student is listed only when a subject has NO mark
at all — the same definition the Ҳисобот/Аълохон/А.Ҷ.Ч. reports use.
"""
import datetime
import io

from django.test import override_settings
from django.urls import reverse
from openpyxl import load_workbook

from portal.ungraded import (build_ungraded_workbook,
                             collect_ungraded_students)
from portal.quarter_close import collect_quarter_close

from .helpers import (GoldenBase, MATH, TAJIK, make_class_subject,
                      make_grade, make_quarter_grade)


class UngradedCollectorTests(GoldenBase):
    """Collector semantics — merged source, class scope, teacher name."""

    def test_student_without_marks_is_listed(self):
        data = collect_ungraded_students(self.school_a, 1)
        math_rows = [r for r in data['rows'] if r['subject'] == MATH]
        # Both 7-А students have no marks at all -> listed for МАТЕМАТИКА.
        self.assertEqual({r['student'] for r in math_rows},
                         {'Фирӯз Раҳимов', 'Мадина Каримова'})
        # The responsible teacher is shown next to the row.
        for r in math_rows:
            self.assertTrue(r['teacher'])
        self.assertEqual(data['students'], 2)

    def test_stored_quarter_grade_removes_row(self):
        make_quarter_grade(self.s1, quarter=1, grade=9)
        data = collect_ungraded_students(self.school_a, 1)
        math = [(r['student']) for r in data['rows']
                if r['subject'] == MATH]
        self.assertNotIn('Фирӯз Раҳимов', math)
        self.assertIn('Мадина Каримова', math)

    def test_live_journal_mark_removes_row(self):
        """A daily mark alone (no stored QuarterGrade) fills the cell —
        the Ҳисобот shows the Ҷ value, so the student is NOT ungraded."""
        make_grade(self.s1, score=9)
        data = collect_ungraded_students(self.school_a, 1)
        math = [r['student'] for r in data['rows']
                if r['subject'] == MATH]
        self.assertNotIn('Фирӯз Раҳимов', math)
        self.assertIn('Мадина Каримова', math)

    def test_one_row_per_missing_subject(self):
        """A student missing several subjects appears once per subject —
        the abroad-student case (nothing entered anywhere)."""
        make_class_subject(self.school_a, '7-А', TAJIK,
                           teacher=self.teacher_a)
        data = collect_ungraded_students(self.school_a, 1)
        s1_rows = [r for r in data['rows']
                   if r['student'] == 'Фирӯз Раҳимов']
        self.assertGreaterEqual(len(s1_rows), 2)
        self.assertIn(MATH, {r['subject'] for r in s1_rows})
        self.assertIn(TAJIK, {r['subject'] for r in s1_rows})

    def test_quarter_close_missing_uses_same_source(self):
        """The Солҳои таҳсил card count matches this list — a live Ҷ
        mark clears the 'баҳогузорӣ нашудааст' flag too."""
        qs = collect_quarter_close(self.school_a, 1, self._ay())
        math_before = next(r for r in qs['rows']
                           if r['subject'] == MATH)
        make_grade(self.s1, score=9)
        qs = collect_quarter_close(self.school_a, 1, self._ay())
        math_after = next(r for r in qs['rows']
                          if r['subject'] == MATH)
        self.assertEqual(math_after['missing'],
                         math_before['missing'] - 1)

    def _ay(self):
        from portal.views import academic_year_start
        return academic_year_start()


class UngradedViewTests(GoldenBase):
    """Page + export access — same gate as the other school documents."""

    def test_zavuch_sees_own_school(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('ungraded_students'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Бебаҳогон')
        self.assertContains(resp, 'Фирӯз Раҳимов')

    def test_teacher_is_redirected(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('ungraded_students'))
        self.assertEqual(resp.status_code, 302)

    def test_maorif_picks_school(self):
        self.client.force_login(self.director)
        resp = self.client.get(
            reverse('ungraded_students'),
            {'school_id': self.school_b.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ҷаҳонгир Назаров')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_export_xlsx(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(
            reverse('ungraded_students_export'), {'quarter': 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        header = [c.value for c in ws[4]]
        self.assertEqual(header, ['№', 'Ному насаб', 'Синф',
                                  'Фан', 'Омӯзгор'])
        names = [ws.cell(r, 2).value
                 for r in range(5, ws.max_row + 1)]
        self.assertIn('Фирӯз Раҳимов', names)


class UngradedArchiveTests(GoldenBase):
    """The frozen quarter archive keeps live Ҷ values — closing a
    quarter must not lose marks the reports showed."""

    @override_settings(MEDIA_ROOT='/tmp/ung_archive_test')
    def test_archive_contains_live_mark(self):
        import tempfile
        from portal.quarter_close import save_quarter_archive
        make_grade(self.s1, score=9)   # live only — no stored QuarterGrade
        with self.settings(MEDIA_ROOT=tempfile.mkdtemp()):
            path = save_quarter_archive(self.school_a, 1, self._ay())
            wb = load_workbook(io.BytesIO(path.read_bytes()))
        ws = wb['7-А']
        header = [c.value for c in ws[2]]
        math_col = header.index(MATH) + 1
        name_col = header.index('Хонанда') + 1
        for r in range(3, ws.max_row + 1):
            if ws.cell(r, name_col).value == 'Фирӯз Раҳимов':
                self.assertEqual(ws.cell(r, math_col).value, 9)
                return
        self.fail('Фирӯз Раҳимов not found in archive sheet')

    def _ay(self):
        from portal.views import academic_year_start
        return academic_year_start()
