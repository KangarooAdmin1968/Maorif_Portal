"""Ҳуҷҷатнигорӣ → Ҳисоботи завуч — А.Ҷ.Ч. quarterly summary Excel export.

Reproduces the owner's workbook (portal/excel_templates/zavuch_report.xlsx):
one row per grade level 2-11 with parallel classes aggregated, the
header block, band formulas and signature lines preserved.

Data sources (existing portal data only):
  - Student (class level, gender)               -> C, D (+Эзоҳ row for level 1)
  - QuarterGrade.grade for the chosen quarter    -> K, L and band cells N/P/T
  - School.name / School.director                -> title + signature
  - Омад/Рафт columns E..H stay blank — the portal has no student
    mobility records yet; the I/J formulas fold the zavuch's manual
    entries into the end-of-quarter counts.
"""
import io
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from openpyxl import load_workbook

from .class_report import QUARTER_ORDINALS, _director_name
from .models import QuarterGrade, Student
from .utils import class_numeric_part

TEMPLATE_PATH = (
    Path(settings.BASE_DIR) / 'portal' / 'excel_templates' / 'zavuch_report.xlsx'
)

FIRST_DATA_ROW = 5
LEVELS = range(2, 12)   # report rows map to grade levels 2..11
NOTE_ROW = 17
DIRECTOR_ROW = 20
DEPUTY_ROW = 21

# Band lower bounds for a student's quarter average: >=9 -> 9-10 (N),
# >=7 -> 7-8 (P), 4-6 is the remainder formula (R), <4 -> 1-3 (T).
BAND_9_10, BAND_7_8, BAND_4_6, BAND_1_3 = 'hi', 'md', 'rm', 'lo'


class ZavuchReportError(Exception):
    """Raised when the school summary cannot be rendered."""


def _band(avg):
    if avg >= 9:
        return BAND_9_10
    if avg >= 7:
        return BAND_7_8
    if avg >= 4:
        return BAND_4_6
    return BAND_1_3


def _pct(part, whole):
    """Rounded part/whole*100 as a plain value — displays even in
    Excel's Protected View where formulas are never recalculated."""
    return round(part / whole * 100, 2) if whole else 0


def build_zavuch_report_workbook(school, quarter):
    """Fill the owner's А.Ҷ.Ч. template for one school + quarter.

    Returns a BytesIO xlsx. Per-student band = average of the student's
    QuarterGrade.grade rows in the chosen quarter/current academic year;
    students without any quarter grade land in Бе хол (V = I-K).
    """
    from .views import academic_year_start  # local import avoids cycles

    if quarter not in (1, 2, 3, 4):
        raise ZavuchReportError('Чораки нодуруст.')

    wb = load_workbook(TEMPLATE_PATH)
    ws = wb.active
    ay = academic_year_start()

    # Roster by grade level. "Аввали чоряк" uses the current roster — the
    # Омад/Рафт columns adjust the end-of-quarter count once the zavuch
    # enters mobility numbers by hand.
    levels = {lvl: [0, 0] for lvl in range(1, 12)}   # [total, girls]
    for s in Student.objects.filter(school=school).values(
            'class_name', 'gender'):
        lvl = class_numeric_part(s['class_name'])
        if lvl in levels:
            levels[lvl][0] += 1
            if s['gender'] == 'F':
                levels[lvl][1] += 1

    # Quarter grades -> per-student average -> band, grouped by level.
    sums = {}
    for row in QuarterGrade.objects.filter(
            student__school=school, quarter=quarter,
            academic_year=ay, grade__isnull=False).values(
            'student_id', 'student__class_name', 'student__gender',
            'grade'):
        lvl = class_numeric_part(row['student__class_name'])
        if lvl not in LEVELS:
            continue
        entry = sums.setdefault(
            row['student_id'],
            [0.0, 0, lvl, row['student__gender']])
        entry[0] += float(row['grade'])
        entry[1] += 1

    graded = {lvl: {'total': 0, 'f': 0, 'bands': defaultdict(int)}
              for lvl in LEVELS}
    for total, count, lvl, gender in sums.values():
        g = graded[lvl]
        g['total'] += 1
        if gender == 'F':
            g['f'] += 1
        g['bands'][_band(total / count)] += 1

    for i, lvl in enumerate(LEVELS):
        r = FIRST_DATA_ROW + i
        ws[f'A{r}'] = i + 1
        ws[f'B{r}'] = f'СИНФИ {lvl}'
        ws[f'C{r}'] = levels[lvl][0]
        ws[f'D{r}'] = levels[lvl][1]
        # Омад/Рафт: blank — filled by hand until mobility tracking exists.
        for col in 'EFGH':
            ws[f'{col}{r}'] = None
        ws[f'I{r}'] = f'=C{r}+E{r}-G{r}'
        ws[f'J{r}'] = f'=D{r}+F{r}-H{r}'
        g = graded[lvl]
        k = g['total']
        n, p, t = (g['bands'][BAND_9_10], g['bands'][BAND_7_8],
                   g['bands'][BAND_1_3])
        r46 = k - n - p - t   # 4-6 remainder — owner's K-N-P-T logic
        ws[f'K{r}'] = k
        ws[f'L{r}'] = g['f']
        ws[f'M{r}'] = 100
        # Band cells are computed values (nothing manual affects them);
        # only the Омад/Рафт-dependent chain stays formula-driven so
        # hand-entered E..H numbers fold into I/J/V/W on recalc.
        ws[f'N{r}'] = n
        ws[f'O{r}'] = _pct(n, k)
        ws[f'P{r}'] = p
        ws[f'Q{r}'] = _pct(p, k)
        ws[f'R{r}'] = r46
        ws[f'S{r}'] = _pct(r46, k)
        ws[f'T{r}'] = t
        ws[f'U{r}'] = _pct(t, k)
        ws[f'V{r}'] = f'=I{r}-K{r}'
        ws[f'W{r}'] = f'=IF($K{r}=0,0,V{r}*100/$K{r})'
        ws[f'X{r}'] = _pct(n + p, k)
        ws[f'Y{r}'] = 100

    ordinal = QUARTER_ORDINALS.get(quarter, f'{quarter}-ум')
    ws['A1'] = (f'Натиҷаи ҷамъбастии чоряк (А.Ҷ.Ч.) {ordinal} '
                f'дар {school.name}\nдар соли таҳсили {ay}-{ay + 1}')
    n1, f1 = levels[1]
    ws[f'A{NOTE_ROW}'] = (
        'Эзоҳ: Шумораи хонандагони синфҳои 1-ум '
        f'{n1} нафар аз ҷумла духтарон {f1} нафарро ташкил медиҳад.')
    director = _director_name(school)
    ws[f'A{DIRECTOR_ROW}'] = (
        f'Директори муассиса:    {director}'.rstrip())
    # Deputy name has no reliable portal source — left blank by design.
    ws[f'A{DEPUTY_ROW}'] = 'Муовини директор оид ба таълим:'

    wb.calculation.fullCalcOnLoad = True
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
