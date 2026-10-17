"""Ҳуҷҷатнигорӣ → Ҳисоботи синфҳо — quarterly class report export.

Covers: the Documents-area tab and class picker, school isolation and the
табел audience gate, student/quarter-grade mapping onto the owner's
template cells, formula/layout preservation, Таблица1 resizing, and the
one-page A4 landscape print setup.
"""
import datetime
import io
import re

from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from portal.class_report import (
    TEMPLATE_PATH, _school_short_name, build_class_report_workbook,
    report_subjects,
)
from portal.curriculum_hours import UNGRADED_SUBJECTS
from portal.models import normalize_class_name
from portal.tests.helpers import (
    MATH, TAJIK, GoldenBase, make_class_subject, make_quarter_grade,
    make_school, make_student, make_teacher_profile, make_user,
)

TARBIYAVI = 'СОАТИ ТАРБИЯВӢ'


def _subject_col(ws, subject):
    """Find the report column whose row-3 header is `subject` (E..X)."""
    for col in range(5, 25):
        if ws.cell(row=3, column=col).value == subject:
            return col
    return None


def _row_minima(ws, first, last):
    """Numeric grades actually present on each student row, E..X."""
    out = []
    for r in range(first, last + 1):
        nums = [
            v for v in (ws.cell(row=r, column=c).value for c in range(5, 25))
            if isinstance(v, (int, float))
        ]
        out.append(nums)
    return out


def _band_counts(ws, first, last):
    """Independent band counts from the worksheet's own data cells —
    replicating the emitted MIN-chain semantics, not its text."""
    bands = {'9-10': 0, '7-8': 0, '4-6': 0, '1-3': 0, 'blank': 0}
    for nums in _row_minima(ws, first, last):
        if not nums:
            bands['blank'] += 1
            continue
        m = min(nums)
        bands['9-10' if m >= 9 else '7-8' if m >= 7
              else '4-6' if m >= 4 else '1-3'] += 1
    return bands


def _owner_avg_count(ws, first, last):
    """The superseded owner construction: AVERAGE of per-subject >=4
    counts (Z45) — reproduced in Python to demonstrate the defect."""
    counts = []
    for c in range(5, 24):  # E..W, like the owner's E45:W45 range
        vals = [
            v for v in (ws.cell(row=r, column=c).value for r in range(first, last + 1))
            if isinstance(v, (int, float))
        ]
        if any(v > 0 for v in vals):
            counts.append(sum(1 for v in vals if v >= 4))
    return sum(counts) / len(counts) if counts else 0


class ClassReportBuilderTests(GoldenBase):
    """portal.class_report.build_class_report_workbook."""

    def _build(self, school=None, class_name='7-А', quarter=1):
        return load_workbook(build_class_report_workbook(
            school or self.school_a, class_name, quarter,
            today=datetime.date(2025, 10, 15)))

    def setUp(self):
        super().setUp()
        make_quarter_grade(self.s1, '7-А', MATH, quarter=1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, quarter=1, grade=7)
        make_quarter_grade(self.s1, '7-А', MATH, quarter=2, grade=4)
        make_quarter_grade(self.s2, '7-А', MATH, quarter=1, grade=5)
        make_quarter_grade(self.s2, '7-А', TAJIK, quarter=1, grade=10)
        # School B data must never leak into a school-A report.
        make_quarter_grade(self.s3, '7-Б', MATH, quarter=1, grade=10)

    # -- selection / data -------------------------------------------------

    def test_selected_class_and_students_only(self):
        ws = self._build().worksheets[0]
        names = [ws.cell(row=r, column=3).value for r in range(6, 8)]
        self.assertEqual(sorted(names), ['Мадина Каримова', 'Фирӯз Раҳимов'])
        self.assertNotIn('Ҷаҳонгир Назаров', names)
        self.assertEqual(ws['B6'].value, '7-А')
        self.assertEqual(ws.title, '7-А')

    def test_quarter_selects_matching_grades(self):
        ws1 = self._build(quarter=1).worksheets[0]
        ws2 = self._build(quarter=2).worksheets[0]
        math = _subject_col(ws1, MATH)
        self.assertIsNotNone(math)
        letter = get_column_letter(math)
        row_s1 = 6 if ws1['C6'].value == 'Фирӯз Раҳимов' else 7
        row_s2 = 6 if ws1['C6'].value == 'Мадина Каримова' else 7
        self.assertEqual(ws1[f'{letter}{row_s1}'].value, 9)
        self.assertEqual(ws1[f'{letter}{row_s2}'].value, 5)
        self.assertEqual(ws2[f'{letter}{row_s1}'].value, 4)
        self.assertIsNone(ws2[f'{letter}{row_s2}'].value)

    def test_gender_and_row_numbers(self):
        ws = self._build().worksheets[0]
        gender = {ws.cell(row=r, column=3).value: ws.cell(row=r, column=4).value
                  for r in (6, 7)}
        self.assertEqual(gender['Мадина Каримова'], 'д')
        self.assertEqual(gender['Фирӯз Раҳимов'], 'п')
        self.assertEqual([ws.cell(row=r, column=1).value for r in (6, 7)],
                         [1, 2])

    def test_subject_headers_are_class_subjects(self):
        ws = self._build().worksheets[0]
        headers = {ws.cell(row=3, column=c).value for c in range(5, 25)}
        headers.discard(None)
        self.assertEqual(headers, set(report_subjects(self.school_a, '7-А')))
        self.assertIn(MATH, headers)
        # СОАТИ ТАРБИЯВӢ is an ungraded subject — never a report column.
        self.assertFalse(set(UNGRADED_SUBJECTS) & headers)

    def test_title_and_school_line(self):
        ws = self._build().worksheets[0]
        self.assertIn('чоряки 1-ум дар синфи', ws['A2'].value)
        self.assertIn('мактаб №1', ws['A2'].value.lower())
        self.assertIn('2025-2026', ws['A2'].value)
        # Template A3's INDIRECT evaluates to #REF! — replaced by the row
        # header label instead of copying a broken formula.
        self.assertEqual(ws['A3'].value, '№')

    def test_class_teacher_from_tarbiyavi_allocation(self):
        profile = make_teacher_profile(self.teacher_a, self.school_a,
                                       full_name='Раҳимова О.')
        make_class_subject(self.school_a, '7-А', TARBIYAVI,
                           allocated=profile)
        ws = self._build().worksheets[0]
        self.assertEqual(ws['AG1'].value, 'Раҳимова О.')

    def test_signature_uses_school_director(self):
        self.school_a.director = 'Одинаев С.З.'
        self.school_a.save()
        ws = self._build().worksheets[0]
        sig = ws['A18'].value  # 49 shifted by delta = (6+2)-39 = -31+... = 18
        self.assertIn('Одинаев С.З.', sig)
        self.assertIn('Директори', sig)

    # -- formulas ---------------------------------------------------------

    def test_student_band_formulas_preserved(self):
        ws = self._build().worksheets[0]
        self.assertEqual(
            ws['Y6'].value,
            '=IF(COUNT($E6:$X6)>0,COUNTIF($E6:$X6,">=9"),"")')
        self.assertEqual(
            ws['Z6'].value,
            '=IF(COUNT($E6:$X6)>0,Y6/COUNT($E6:$X6)*100,"")')
        self.assertEqual(
            ws['AA6'].value,
            '=IF(COUNT($E6:$X6)>0,COUNTIF($E6:$X6,">=7")-Y6,"")')
        self.assertEqual(
            ws['AG6'].value, '=IF(COUNT(E6:W6)>0,AVERAGE(E6:W6),"")')
        self.assertEqual(ws['AH6'].value, '=IF(COUNT(E6:W6)>0,100,"")')
        self.assertEqual(ws['AI6'].value, '=IF(COUNT(E6:W6)>0,Z6+AB6,"")')

    def test_footer_formulas_scaled_to_class_size(self):
        # 2 students -> data rows 6..7, band block starts at row 8.
        ws = self._build().worksheets[0]
        math = get_column_letter(_subject_col(ws, MATH))
        self.assertEqual(ws[f'{math}8'].value,
                         f'=COUNTIF({math}$6:{math}7,">=9")')
        self.assertEqual(ws[f'{math}9'].value,
                         f'=COUNTIF({math}$6:{math}7,">=7")-{math}8')
        # Owner order: the 4-6 row subtracts s+1 first, then s.
        self.assertEqual(ws[f'{math}10'].value,
                         f'=COUNTIF({math}$6:{math}7,">=4")-{math}9-{math}8')
        self.assertEqual(ws[f'{math}11'].value,
                         f'=COUNTIF({math}$6:{math}7,"<4")')
        self.assertEqual(
            ws['Z8'].value,
            '=IF(MIN(E6:X6)>=9,1,0)+IF(MIN(E7:X7)>=9,1,0)')
        # 1-3 band: direct per-row chain (has grades AND min <4), guarded
        # by the same COUNTIF the owner used — never a negative difference.
        self.assertEqual(
            ws['Z11'].value,
            '=IF(COUNTIF(E12:X12,">0"),'
            'IF(AND(COUNT(E6:X6)>0,MIN(E6:X6)<4),1,0)+'
            'IF(AND(COUNT(E7:X7)>0,MIN(E7:X7)<4),1,0),"")')
        self.assertEqual(ws['A8'].value, 'Хол')
        self.assertEqual(ws['C8'].value, '9-10')
        self.assertEqual(ws['A12'].value, 'Холи миёна')
        self.assertEqual(ws['A13'].value, 'Фоиз')
        self.assertEqual(ws['A14'].value, 'Шумораи хонандагон')
        self.assertIn('Муовини директор', ws['A16'].value)
        self.assertIn('Директори', ws['A18'].value)

    def test_band_counts_match_source_data(self):
        """Independent check against source data: quarter grades land in
        the cell at (student row, subject column); subjects without grades
        stay blank so the owner's COUNTIF/IF guards render "" not 0."""
        ws = self._build().worksheets[0]
        math, tajik = _subject_col(ws, MATH), _subject_col(ws, TAJIK)
        cols = {math: [9, 5], tajik: [7, 10]}
        for col, grades in cols.items():
            cells = [ws.cell(row=r, column=col).value for r in (6, 7)]
            self.assertEqual(sorted(cells), sorted(grades))
        graded = {math, tajik}
        for col in range(5, 25):
            if ws.cell(row=3, column=col).value and col not in graded:
                self.assertIsNone(ws.cell(row=6, column=col).value)

    # -- layout preservation ----------------------------------------------

    def test_layout_preserved(self):
        ws = self._build().worksheets[0]
        template = load_workbook(TEMPLATE_PATH).worksheets[0]
        for col in ('A', 'C', 'E', 'Y', 'AJ'):
            self.assertEqual(ws.column_dimensions[col].width,
                             template.column_dimensions[col].width)
        self.assertEqual(ws.row_dimensions[4].height, 36)
        self.assertTrue(ws.row_dimensions[5].hidden)
        self.assertEqual(ws['E3'].alignment.textRotation, 90)
        # Footer merges shift with class size: 2 students -> band merge
        # A8:B11 (was A39:B42), signature merges A16:AJ16 + A18:AJ18.
        merged = {str(m) for m in ws.merged_cells.ranges}
        self.assertIn('A8:B11', merged)
        self.assertIn('A16:AJ16', merged)
        self.assertIn('A18:AJ18', merged)
        self.assertEqual(ws.tables['Таблица1'].ref, 'A5:AJ7')

    def test_bigger_class_shifts_footer_down(self):
        make_student(self.school_a, '7-А', 'Ғаффоров Аҳмад')
        ws = self._build().worksheets[0]
        # 3 students -> last data row 8, band block 9..12.
        self.assertEqual(ws['A9'].value, 'Хол')
        merged = {str(m) for m in ws.merged_cells.ranges}
        self.assertIn('A9:B12', merged)
        self.assertIn('A17:AJ17', merged)
        self.assertEqual(ws.tables['Таблица1'].ref, 'A5:AJ8')

    # -- print setup -------------------------------------------------------

    def test_single_page_a4_landscape_print(self):
        ws = self._build().worksheets[0]
        self.assertEqual(ws.page_setup.orientation, 'landscape')
        self.assertEqual(int(ws.page_setup.paperSize), 9)  # A4
        self.assertEqual(ws.page_setup.fitToWidth, 1)
        self.assertEqual(ws.page_setup.fitToHeight, 1)
        self.assertTrue(ws.sheet_properties.pageSetUpPr.fitToPage)
        self.assertTrue(ws.print_options.horizontalCentered)
        # Print area covers the whole report through the signature row.
        self.assertTrue(str(ws.print_area).endswith('$A$1:$AJ$18'))


class ClassReportPageTests(GoldenBase):
    """GET /schools/documents/class-reports/ + export endpoint."""

    def setUp(self):
        super().setUp()
        self.url = reverse('class_reports')
        self.export_url = reverse('class_report_export')

    def test_zavuch_lists_own_school_classes(self):
        make_student(self.school_a, '1-А', 'Хуршед Одилов')
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        listed = [c['class_name'] for c in r.context['classes']]
        self.assertEqual(listed, ['7-А'])  # non-graded 1-А excluded

    def test_regular_teacher_denied(self):
        self.client.force_login(self.teacher_a)
        for url in (self.url,
                    f'{self.export_url}?class_name=7-А'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn(reverse('dashboard'), r.url)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r.url)

    def test_superuser_picks_school(self):
        self.client.force_login(self.admin)
        r = self.client.get(self.url, {'school_id': self.school_b.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['school'], self.school_b)
        self.assertEqual([c['class_name'] for c in r.context['classes']],
                         ['7-Б'])

    def test_documents_tab_links_to_class_reports(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('school_documents'))
        self.assertContains(r, 'Ҳисоботи синфҳо')
        self.assertContains(r, reverse('class_reports'))

    def test_export_returns_workbook_for_own_school(self):
        make_quarter_grade(self.s1, '7-А', MATH, quarter=1, grade=9)
        self.client.force_login(self.zavuch_a)
        r = self.client.get(
            self.export_url, {'class_name': '7-А', 'quarter': 1})
        self.assertEqual(r.status_code, 200)
        ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
        self.assertEqual(ws.title, '7-А')
        math = get_column_letter(_subject_col(ws, MATH))
        row = 6 if ws['C6'].value == 'Фирӯз Раҳимов' else 7
        self.assertEqual(ws[f'{math}{row}'].value, 9)

    def test_export_school_isolation(self):
        """zavuch_1 (school A) cannot pull school B's 7-Б even by
        forging school_id — the school resolves to their own."""
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.export_url, {
            'school_id': self.school_b.id, 'class_name': '7-Б',
            'quarter': 1})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('class_reports'), r.url)
        # And school B's zavuch gets only school-B data.
        zavuch_b = make_user('zavuch_2', role='teacher',
                             school=self.school_b)
        self.client.force_login(zavuch_b)
        r = self.client.get(self.export_url, {'class_name': '7-Б',
                                              'quarter': 1})
        self.assertEqual(r.status_code, 200)
        ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
        self.assertEqual(ws['C6'].value, 'Ҷаҳонгир Назаров')

    def test_export_rejects_unknown_and_nongraded_class(self):
        make_student(self.school_a, '1-А', 'Хуршед Одилов')
        self.client.force_login(self.zavuch_a)
        for cls in ('9-З', '1-А'):
            r = self.client.get(self.export_url,
                                {'class_name': cls, 'quarter': 1})
            self.assertEqual(r.status_code, 302)
            self.assertIn(reverse('class_reports'), r.url)

    def test_export_content_disposition(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.export_url,
                            {'class_name': '7-А', 'quarter': 1})
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn('.xlsx', r['Content-Disposition'])


BAND13_PATTERN = re.compile(
    r'IF\(AND\(COUNT\(E(\d+):X\d+\)>0,MIN\(E\d+:X\d+\)<4\),1,0\)')


def _band_row(ws, label):
    """Locate the footer row whose C-label is '9-10'/'7-8'/'4-6'/'1-3'."""
    for r in range(6, ws.max_row + 1):
        if ws.cell(row=r, column=3).value == label:
            return r
    return None


def _band13_result(formula, ws):
    """Evaluate the emitted 1-3 chain's own terms on real worksheet data."""
    count = 0
    for m in BAND13_PATTERN.finditer(formula):
        row = int(m.group(1))
        nums = [
            v for v in (ws.cell(row=row, column=c).value
                        for c in range(5, 25))
            if isinstance(v, (int, float))
        ]
        if nums and min(nums) < 4:
            count += 1
    return count


class Band13FormulaTests(GoldenBase):
    """The «Танҳо бо 1–3» cell must count students whose minimum numeric
    grade is 1-3 — consistently with the other per-row MIN-chain bands,
    never as a difference against the Z45 per-subject average (which can
    go negative when the quarter's grade matrix is sparse)."""

    def _build(self, class_name='7-А', quarter=1):
        return load_workbook(build_class_report_workbook(
            self.school_a, class_name, quarter,
            today=datetime.date(2025, 10, 15))).worksheets[0]

    def test_band13_counts_failing_students_only(self):
        # s1: 9,7 -> 7-8 | s2: 5,10 -> 4-6 | s3: 2 (one subject) -> 1-3
        # s4: 9,3 -> 1-3 | s5: no grades -> belongs to no band
        s3 = make_student(self.school_a, '7-А', 'Наврӯз Шарифов')
        s4 = make_student(self.school_a, '7-А', 'Парвиз Олимов')
        s5 = make_student(self.school_a, '7-А', 'Тоҳир Бобоев')
        make_quarter_grade(self.s1, '7-А', MATH, quarter=1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, quarter=1, grade=7)
        make_quarter_grade(self.s2, '7-А', MATH, quarter=1, grade=5)
        make_quarter_grade(self.s2, '7-А', TAJIK, quarter=1, grade=10)
        make_quarter_grade(s3, '7-А', MATH, quarter=1, grade=2)
        make_quarter_grade(s4, '7-А', MATH, quarter=1, grade=9)
        make_quarter_grade(s4, '7-А', TAJIK, quarter=1, grade=3)
        ws = self._build()
        row13 = _band_row(ws, '1-3')
        # Evaluate the emitted formula's own row terms against real cells.
        got = _band13_result(ws.cell(row=row13, column=26).value, ws)
        self.assertEqual(got, 2)
        # Independent recount over the same worksheet cells.
        self.assertEqual(
            _band_counts(ws, 6, 10),
            {'9-10': 0, '7-8': 1, '4-6': 1, '1-3': 2, 'blank': 1})

    def test_band13_not_negative_on_sparse_data(self):
        """One subject graded per student made the superseded construction
        negative: avg per-subject >=4 count = 1, min>=9 chain = 2 -> -1."""
        sa = make_student(self.school_a, '8-А', 'Аҳмад Неъматов')
        sb = make_student(self.school_a, '8-А', 'Баҳром Юлдошев')
        # МАТЕМАТИКА is not a grade-8+ curriculum subject — use whatever
        # subjects this class actually offers.
        subj_a, subj_b = report_subjects(self.school_a, '8-А')[:2]
        make_quarter_grade(sa, '8-А', subj_a, quarter=1, grade=9)
        make_quarter_grade(sb, '8-А', subj_b, quarter=1, grade=10)
        ws = self._build('8-А')
        # The old Z45-difference would have produced -1 on this very data.
        self.assertLess(_owner_avg_count(ws, 6, 7) - 2, 0)
        row13 = _band_row(ws, '1-3')
        self.assertEqual(
            _band13_result(ws.cell(row=row13, column=26).value, ws), 0)
        self.assertEqual(_band_counts(ws, 6, 7)['9-10'], 2)

    def test_all_blank_rows_count_in_no_band(self):
        make_student(self.school_a, '8-А', 'Валӣ Қосимов')
        make_student(self.school_a, '8-А', 'Гулнора Азимова')
        ws = self._build('8-А')
        row13 = _band_row(ws, '1-3')
        self.assertEqual(
            _band13_result(ws.cell(row=row13, column=26).value, ws), 0)
        self.assertEqual(_band_counts(ws, 6, 7)['blank'], 2)
        # Blank-result convention preserved: the COUNTIF guard stays.
        self.assertIn('COUNTIF(E', ws.cell(row=row13, column=26).value)

    def test_bands_partition_and_scale_with_class(self):
        """All four bands present; band total equals the number of
        students holding at least one grade, for a different class size."""
        st = [make_student(self.school_a, '9-В', n) for n in (
            'Азиз Камолов', 'Барно Расулова', 'Ворис Ҷалилов',
            'Гулчеҳра Тоҳирова', 'Диловар Ҷумъаев')]
        subj_a, subj_b = report_subjects(self.school_a, '9-В')[:2]
        make_quarter_grade(st[0], '9-В', subj_a, quarter=1, grade=10)
        make_quarter_grade(st[1], '9-В', subj_a, quarter=1, grade=8)
        make_quarter_grade(st[1], '9-В', subj_b, quarter=1, grade=9)
        make_quarter_grade(st[2], '9-В', subj_a, quarter=1, grade=4)
        make_quarter_grade(st[2], '9-В', subj_b, quarter=1, grade=10)
        make_quarter_grade(st[3], '9-В', subj_a, quarter=1, grade=3)
        # st[4] intentionally ungraded.
        ws = self._build('9-В')
        bands = _band_counts(ws, 6, 10)
        self.assertEqual(
            bands, {'9-10': 1, '7-8': 1, '4-6': 1, '1-3': 1, 'blank': 1})
        graded = (bands['9-10'] + bands['7-8']
                  + bands['4-6'] + bands['1-3'])
        self.assertEqual(graded, 4)
        row13 = _band_row(ws, '1-3')
        self.assertEqual(
            _band13_result(ws.cell(row=row13, column=26).value, ws), 1)
        self.assertGreaterEqual(graded, 0)


class InstitutionLabelTests(GoldenBase):
    """Signature-line institution label from the school's own data."""

    def test_general_school_mtm(self):
        # 'Мактаб №1', type 'Мактаб' — the GoldenBase fixture.
        self.assertEqual(_school_short_name(self.school_a), 'МТМУ №1')

    def test_mtm_from_long_name(self):
        school = make_school('Муассисаи таҳсилоти умумии №20')
        self.assertEqual(_school_short_name(school), 'МТМУ №20')

    def test_litsey(self):
        school = make_school(
            'Литсейи №3-и ноҳияи Зафаробод (Марказ)', type='Литсей')
        self.assertEqual(_school_short_name(school), 'Литсей №3')

    def test_litsey_russian_variant(self):
        school = make_school('Лицей №7', type='Лицей')
        self.assertEqual(_school_short_name(school), 'Литсей №7')

    def test_gymnasium(self):
        # In the real DB a gymnasium can carry type='Мактаб' with the type
        # only in its name — the name must still win over a blind МТМУ.
        school = make_school('Гимназияи №1', type='Мактаб')
        self.assertEqual(_school_short_name(school), 'Гимназия №1')

    def test_unnumbered_gymnasium_uses_full_name(self):
        school = make_school('Гимназияи ноҳияи Зафаробод', type='Мактаб')
        self.assertEqual(
            _school_short_name(school), 'Гимназияи ноҳияи Зафаробод')

    def test_kindergarten_never_mtm(self):
        school = make_school(
            'Кӯдакистони №1-и ноҳияи Зафаробод', type='Кӯдакистон')
        self.assertEqual(
            _school_short_name(school),
            'Кӯдакистони №1-и ноҳияи Зафаробод')

    def test_litsey_signature_in_workbook(self):
        school = make_school(
            'Литсейи №3-и ноҳияи Зафаробод (Марказ)', type='Литсей')
        make_student(school, '10-А', 'Аҳмад Зайнуллоев')
        ws = load_workbook(build_class_report_workbook(
            school, '10-А', 1,
            today=datetime.date(2025, 10, 15))).worksheets[0]
        sig = ws.cell(row=ws.max_row, column=1).value
        self.assertIn('Директори Литсей №3', sig)
        self.assertNotIn('МТМУ', sig)


def _label_row(ws, label, col=1):
    """Locate a footer row by its column-A label ('Холи миёна', 'Фоиз')."""
    for r in range(6, ws.max_row + 1):
        if ws.cell(row=r, column=col).value == label:
            return r
    return None


def _numeric_cells(ws, c1, r1, c2, r2):
    vals = []
    for r in range(r1, r2 + 1):
        for c in range(column_index_from_string(c1),
                       column_index_from_string(c2) + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, (int, float)):
                vals.append(v)
    return vals


FORMULA_RANGE = re.compile(r'(?:AVERAGE|SUM)\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)')


def _eval_avg_formula(ws, formula):
    """Evaluate an emitted overall-average cell by its own shape —
    AVERAGE(range) or SUM(range)/COUNT(range) — on real sheet cells."""
    m = FORMULA_RANGE.search(formula)
    assert m, formula
    vals = _numeric_cells(ws, m.group(1), int(m.group(2)),
                          m.group(3), int(m.group(4)))
    return sum(vals) / len(vals)


def _subject_columns(ws):
    """Report columns that carry a subject header (E..X, spare excluded)."""
    return [c for c in range(5, 25) if ws.cell(row=3, column=c).value]


def _subject_mean_of_means(ws, first, last):
    """Superseded owner path behind Z{avg}: average of per-subject
    averages — reproduced in Python to demonstrate the divergence."""
    means = []
    for c in _subject_columns(ws):
        nums = _numeric_cells(
            ws, get_column_letter(c), first, get_column_letter(c), last)
        if nums:
            means.append(sum(nums) / len(nums))
    return sum(means) / len(means)


def _student_mean_of_means(ws, first, last):
    """Superseded owner path behind AG{avg}: average of per-student
    row averages over the subject columns."""
    cols = _subject_columns(ws)
    means = []
    for r in range(first, last + 1):
        nums = [v for v in (ws.cell(row=r, column=c).value for c in cols)
                if isinstance(v, (int, float))]
        if nums:
            means.append(sum(nums) / len(nums))
    return sum(means) / len(means)


def _audit_verdict(audit_formula, z_avg, ag_avg, z_pct, ag_pct):
    """Apply the emitted audit predicate IF(AND(Z=AG, Z=AG)) to given
    values — evaluating the comparison's logic, not a stored string."""
    assert 'AND(' in audit_formula
    assert 'Ҳисоб дақиқ аст' in audit_formula
    assert 'Хатои ҳисоббарорӣ' in audit_formula
    if z_avg == ag_avg and z_pct == ag_pct:
        return 'Ҳисоб дақиқ аст'
    return 'Хатои ҳисоббарорӣ'


class OverallAverageAuditTests(GoldenBase):
    """Regression for the 11-Б case: «Холи миёна» showed 7.88 in column
    Z vs 7.86 in column AG, so the audit cell flagged «Хатои ҳисоббарорӣ».

    Cause: the owner's Z averaged per-subject means (equal weight per
    subject) while AG averaged per-student means (equal weight per
    student). On a fully populated matrix both equal the pooled mean,
    but a partial quarter makes them diverge. Both cells now pool the
    same grade range via different paths — SUM/COUNT vs AVERAGE — so the
    audit still compares two genuinely computed values.
    """

    def _sparse_sheet(self):
        """3 students, unequal subject coverage:
        s1 M=9,T=8 | s2 M=5,T=6 | s3 T=10 only."""
        s3 = make_student(self.school_a, '7-А', 'Ситора Исмоилова')
        make_quarter_grade(self.s1, '7-А', MATH, quarter=1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, quarter=1, grade=8)
        make_quarter_grade(self.s2, '7-А', MATH, quarter=1, grade=5)
        make_quarter_grade(self.s2, '7-А', TAJIK, quarter=1, grade=6)
        make_quarter_grade(s3, '7-А', TAJIK, quarter=1, grade=10)
        return load_workbook(build_class_report_workbook(
            self.school_a, '7-А', 1,
            today=datetime.date(2025, 10, 15))).worksheets[0]

    def test_both_cells_pool_the_same_range(self):
        ws = self._sparse_sheet()
        avg_row = _label_row(ws, 'Холи миёна')
        z_f = ws.cell(row=avg_row, column=26).value
        ag_f = ws.cell(row=avg_row, column=33).value
        z_rng = FORMULA_RANGE.search(z_f).groups()
        ag_rng = FORMULA_RANGE.search(ag_f).groups()
        # Same underlying data — all grade cells of the 3 student rows.
        self.assertEqual(z_rng, ag_rng)
        last_letter = ('X' if len(report_subjects(self.school_a, '7-А')) >= 20
                       else 'W')
        self.assertEqual(z_rng, ('E', '6', last_letter, '8'))
        self.assertIn(f'SUM(E6:{last_letter}8)/COUNT(E6:{last_letter}8)', z_f)
        self.assertIn(f'AVERAGE(E6:{last_letter}8)', ag_f)

    def test_values_agree_and_equal_pooled_mean(self):
        ws = self._sparse_sheet()
        avg_row = _label_row(ws, 'Холи миёна')
        z = _eval_avg_formula(ws, ws.cell(row=avg_row, column=26).value)
        ag = _eval_avg_formula(ws, ws.cell(row=avg_row, column=33).value)
        # Independent pooled mean over the worksheet's own grade cells:
        # (9+8+5+6+10)/5 = 7.6 — distinct from both superseded paths.
        self.assertEqual(sum(_numeric_cells(ws, 'E', 6, 'X', 8)), 38)
        self.assertAlmostEqual(z, 38 / 5)
        self.assertAlmostEqual(ag, 38 / 5)
        self.assertEqual(z, ag)

    def test_owner_paths_diverge_on_this_data(self):
        """Reproduces the screenshot defect: on this fixture the
        superseded constructions give 7.5 vs 8.0 — the same kind of
        split that produced 7.88 vs 7.86 on 11-Б — and the audit
        correctly flagged it as an error."""
        ws = self._sparse_sheet()
        z_old = _subject_mean_of_means(ws, 6, 8)
        ag_old = _student_mean_of_means(ws, 6, 8)
        self.assertAlmostEqual(z_old, 7.5)
        self.assertAlmostEqual(ag_old, 8.0)
        audit_f = ws.cell(row=_label_row(ws, 'Холи миёна'), column=27).value
        self.assertEqual(
            _audit_verdict(audit_f, z_old, ag_old, z_old * 10, ag_old * 10),
            'Хатои ҳисоббарорӣ')

    def test_audit_reports_accurate_for_new_values(self):
        ws = self._sparse_sheet()
        avg_row = _label_row(ws, 'Холи миёна')
        pct_row = _label_row(ws, 'Фоиз')
        z = _eval_avg_formula(ws, ws.cell(row=avg_row, column=26).value)
        ag = _eval_avg_formula(ws, ws.cell(row=avg_row, column=33).value)
        # Фоиз row keeps the x10 relation on both sides (owner AG44=AG43*10).
        self.assertIn(f'Z{avg_row}*10',
                      ws.cell(row=pct_row, column=26).value)
        self.assertIn(f'AG{avg_row}*10',
                      ws.cell(row=pct_row, column=33).value)
        audit_f = ws.cell(row=avg_row, column=27).value
        self.assertEqual(
            _audit_verdict(audit_f, z, ag, z * 10, ag * 10),
            'Ҳисоб дақиқ аст')

    def test_audit_fires_on_genuine_mismatch(self):
        """The audit is still a real comparison — mismatched inputs
        report «Хатои ҳисоббарорӣ»; nothing is hardcoded to success."""
        ws = self._sparse_sheet()
        audit_f = ws.cell(row=_label_row(ws, 'Холи миёна'), column=27).value
        self.assertEqual(
            _audit_verdict(audit_f, 7.88, 7.86, 78.8, 78.6),
            'Хатои ҳисоббарорӣ')
        # A percentage-only mismatch also fires.
        self.assertEqual(
            _audit_verdict(audit_f, 7.6, 7.6, 76.0, 76.1),
            'Хатои ҳисоббарорӣ')

    def test_complete_matrix_still_agrees(self):
        """The owner's original full-data case: every student graded in
        every subject — both cells show the same overall average and the
        audit reports success."""
        make_quarter_grade(self.s1, '7-А', MATH, quarter=1, grade=9)
        make_quarter_grade(self.s1, '7-А', TAJIK, quarter=1, grade=8)
        make_quarter_grade(self.s2, '7-А', MATH, quarter=1, grade=7)
        make_quarter_grade(self.s2, '7-А', TAJIK, quarter=1, grade=6)
        ws = load_workbook(build_class_report_workbook(
            self.school_a, '7-А', 1,
            today=datetime.date(2025, 10, 15))).worksheets[0]
        avg_row = _label_row(ws, 'Холи миёна')
        z = _eval_avg_formula(ws, ws.cell(row=avg_row, column=26).value)
        ag = _eval_avg_formula(ws, ws.cell(row=avg_row, column=33).value)
        self.assertAlmostEqual(z, 7.5)
        self.assertEqual(z, ag)
        audit_f = ws.cell(row=avg_row, column=27).value
        self.assertEqual(
            _audit_verdict(audit_f, z, ag, z * 10, ag * 10),
            'Ҳисоб дақиқ аст')
