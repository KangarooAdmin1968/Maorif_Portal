"""⭐ Аълохон — «Рӯйхати хонандагони аълохон (бо холи 9 ва 10)».

collect_excellent_students() powers the Ҳуҷҷатнигорӣ preview page and the
Excel export. A student qualifies when EVERY graded subject of their
class shows a quarter mark of 9 or 10 for the chosen quarter and the
current academic year. Marks are read exactly like the official Ҳисобот:
the stored QuarterGrade wins, and a subject with no stored row is filled
with the journal's live provisional value (the Ҷ marker) derived from
daily marks — so the list can never disagree with the report a zavuch
prints. A subject with no mark at all still disqualifies the student.
Only the class's own graded subjects decide; Grade-1 sticker classes
and UNGRADED_SUBJECTS never take part.
"""
import io
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.properties import PageSetupProperties

from .curriculum_hours import UNGRADED_SUBJECTS
from .models import ClassSubject, Student
from .utils import (class_numeric_part, is_non_graded,
                    normalize_class_name, normalize_subject)

EXCELLENT_MARKS = (9, 10)


def excellent_list_title(school, ay=None):
    """Official header shared by the page and the workbook."""
    from .views import academic_year_start
    if ay is None:
        ay = academic_year_start()
    return ('Рӯйхати хонандагони аълохон (бо холи 9 ва 10) дар соли '
            f'таҳсили {ay}–{ay + 1} дар {school.name}')


def collect_excellent_students(school, quarter, today=None):
    """Students of `school` whose every quarter mark is 9 or 10.

    Expected subjects = active graded ClassSubject rows of the student's
    class (UNGRADED_SUBJECTS skipped). The student must have a 9/10
    QuarterGrade in every expected subject and no recorded mark below 9.
    """
    from .views import academic_year_start
    ay = academic_year_start(today)

    expected = defaultdict(set)
    for cs in ClassSubject.objects.filter(school=school, is_active=True):
        if normalize_subject(cs.subject) in UNGRADED_SUBJECTS:
            continue
        expected[normalize_class_name(cs.class_name)].add(
            normalize_subject(cs.subject))

    # Match the Ҳисобот's merged picture: stored QuarterGrade rows win
    # (read only under the student's own class label — a row left under
    # a different class_name can never shadow what the report shows)
    # and cells without one are filled with the journal's live
    # provisional mark (Ҷ) — the shared report source.
    students = list(
        Student.objects.filter(school=school).exclude(class_name=''))
    all_subjects = {s for subs in expected.values() for s in subs}
    from .class_report import merged_quarter_marks
    marks = merged_quarter_marks(
        students, all_subjects, quarter, ay, class_scoped=True) \
        if all_subjects else defaultdict(dict)

    result = []
    blocked = []   # (student, missing_subjects, low_subjects)
    for st in students:
        cn = normalize_class_name(st.class_name)
        if is_non_graded(cn):
            continue
        subjects = expected.get(cn)
        grades = marks.get(st.id, {})
        if not subjects or not grades:
            continue
        # Only her own class's subjects decide — a mark in a subject
        # the class does not study never shows in her Ҳисобот row,
        # so it cannot keep her off this list either.
        missing = sorted(s for s in subjects if s not in grades)
        low = sorted(s for s in subjects
                     if s in grades and grades[s] not in EXCELLENT_MARKS)
        if not missing and not low:
            result.append(st)
        elif not low:
            # Every expected mark she HAS is 9-10 — the only blocker is
            # a subject with no mark at all. Invisible in the Ҳисобот
            # (the row looks perfect), so list it for the zavuch.
            blocked.append((st, missing))
    result.sort(key=lambda s: (class_numeric_part(s.class_name) or 0,
                               normalize_class_name(s.class_name),
                               s.full_name))
    blocked.sort(key=lambda b: (
        len(b[1]), class_numeric_part(b[0].class_name) or 0,
        normalize_class_name(b[0].class_name), b[0].full_name))
    return {'quarter': quarter, 'academic_year': ay, 'students': result,
            'blocked': blocked,
            'title': excellent_list_title(school, ay)}


def build_excellent_workbook(school, quarter, today=None):
    """The official list as xlsx: title, №/Ному насаб/Синф rows, signatures."""
    data = collect_excellent_students(school, quarter, today)
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Аълохон'
    for col, width in zip('ABC', (6, 42, 10)):
        ws.column_dimensions[col].width = width

    ws.merge_cells('A1:C1')
    cell = ws.cell(row=1, column=1, value=data['title'])
    cell.font = Font(size=12, bold=True)
    cell.alignment = Alignment(horizontal='center', vertical='center',
                               wrap_text=True)
    ws.row_dimensions[1].height = 48

    ws.merge_cells('A2:C2')
    cell = ws.cell(row=2, column=1, value=f'Чоряки {quarter}')
    cell.font = Font(size=11, italic=True)
    cell.alignment = Alignment(horizontal='center')

    for c, label in enumerate(('№', 'Ному насаб', 'Синф'), start=1):
        cell = ws.cell(row=4, column=c, value=label)
        cell.font = Font(size=11, bold=True)
        cell.border = border
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.fill = PatternFill('solid', start_color='E8EEF7')

    r = 5
    for i, st in enumerate(data['students'], start=1):
        for c, value in enumerate(
                (i, st.full_name, st.class_name), start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(size=11)
            cell.border = border
            cell.alignment = Alignment(
                horizontal='left' if c == 2 else 'center',
                vertical='center')
        r += 1
    if not data['students']:
        ws.merge_cells(start_row=5, start_column=1, end_row=5, end_column=3)
        cell = ws.cell(row=5, column=1,
                       value='Дар ин чоряк хонандаи аълохон ёфт нашуд.')
        cell.font = Font(size=11)
        r += 1

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    ws.cell(row=r, column=1,
            value='Директори муассиса: ______________').font = Font(size=11)
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    ws.cell(row=r, column=1,
            value='Муовини директор оид ба таълим: ______________'
            ).font = Font(size=11)

    # Print setup: portrait A4 scaled to the sheet width — printing the
    # workbook from Excel fills the page instead of a narrow column.
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
