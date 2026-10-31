"""⚠ Бебаҳогон — «Рӯйхати хонандагони бебаҳо».

collect_ungraded_students() powers the Ҳуҷҷатнигорӣ preview page and the
Excel export: for every graded class-subject of the school it lists the
students who have NO quarter mark at all — neither a stored QuarterGrade
nor a live journal value (the Ҷ marker the Ҳисобот would show). The
merged source (merged_quarter_marks, class_scoped) is shared with every
summary document, so a student who looks graded in the Ҳисобот never
appears here, and the per-student count matches the «баҳогузорӣ
нашудааст» figure on the Солҳои таҳсил quarter card.

Scope = the same graded class-subjects a quarter close locks
(quarter_close._graded_class_subjects): sticker-journal classes and
UNGRADED_SUBJECTS (СОАТИ ТАРБИЯВӢ …) never take part.
"""
import io
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .models import Student, TeacherProfile
from .utils import (class_numeric_part, normalize_class_name,
                    normalize_subject)


def _cs_teacher_names(class_subjects):
    """{cs_id: teacher display name} — allocated_teacher wins, the plain
    teacher FK falls back through TeacherProfile (same rule the Табел
    and Ҳисобот use)."""
    alloc_ids = [cs.allocated_teacher_id for cs in class_subjects
                 if cs.allocated_teacher_id]
    alloc_names = {
        p.id: p.full_name for p in
        TeacherProfile.objects.filter(id__in=alloc_ids)}
    user_ids = [cs.teacher_id for cs in class_subjects
                if cs.teacher_id and not cs.allocated_teacher_id]
    user_names = {
        p.user_id: p.full_name for p in
        TeacherProfile.objects.filter(user_id__in=user_ids)}
    # Last resort for rows pointing at a User with no TeacherProfile —
    # still show a name rather than a blank cell.
    from django.contrib.auth.models import User
    for u in User.objects.filter(id__in=user_ids):
        user_names.setdefault(u.id, u.get_full_name() or u.username)
    return {
        cs.id: (alloc_names.get(cs.allocated_teacher_id)
                or user_names.get(cs.teacher_id) or '')
        for cs in class_subjects}


def collect_ungraded_students(school, quarter, today=None):
    """Rows of (student, class, subject, teacher) with no quarter mark.

    A student counts as ungraded in a subject only when the merged
    source has nothing — no stored QuarterGrade AND no live journal
    value. Students gone abroad whose documents were never withdrawn
    still sit on the roster and appear here once per missing subject.
    """
    from .views import academic_year_start  # local import avoids cycles
    from .quarter_close import _graded_class_subjects
    from .class_report import merged_quarter_marks

    ay = academic_year_start(today)
    class_subjects = _graded_class_subjects(school)
    class_names = {cs.class_name for cs in class_subjects}
    subjects = {normalize_subject(cs.subject) for cs in class_subjects}

    students = [s for s in Student.objects.filter(school=school)
                .exclude(class_name='')
                if normalize_class_name(s.class_name) in class_names]
    marks = merged_quarter_marks(
        students, subjects, quarter, ay, class_scoped=True) \
        if subjects else {}

    by_class = defaultdict(list)
    for st in students:
        by_class[st.class_name].append(st)
    teacher_names = _cs_teacher_names(class_subjects)

    rows = []
    for cs in class_subjects:
        norm = normalize_subject(cs.subject)
        for st in by_class.get(cs.class_name, ()):
            if marks.get(st.id, {}).get(norm) is None:
                rows.append({
                    'student': st.full_name,
                    'class_name': cs.class_name,
                    'subject': cs.subject,
                    'teacher': teacher_names.get(cs.id, ''),
                })
    rows.sort(key=lambda r: (class_numeric_part(r['class_name']) or 0,
                             r['class_name'], r['student'], r['subject']))
    return {
        'quarter': quarter, 'academic_year': ay, 'rows': rows,
        'students': len({r['student'] for r in rows}),
        'title': (f'Рӯйхати хонандагони бебаҳо дар соли таҳсили '
                  f'{ay}–{ay + 1} дар {school.name}'),
    }


def build_ungraded_workbook(school, quarter, today=None):
    """The same list as xlsx: title, №/Хонанда/Синф/Фан/Омӯзгор, signatures."""
    data = collect_ungraded_students(school, quarter, today)
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Бебаҳо'
    for col, width in zip('ABCDE', (7, 34, 10, 26, 26)):
        ws.column_dimensions[col].width = width

    ws.merge_cells('A1:E1')
    cell = ws.cell(row=1, column=1, value=data['title'])
    cell.font = Font(size=12, bold=True)
    cell.alignment = Alignment(horizontal='center', vertical='center',
                               wrap_text=True)
    ws.row_dimensions[1].height = 48

    ws.merge_cells('A2:E2')
    cell = ws.cell(row=2, column=1, value=f'Чоряки {quarter}')
    cell.font = Font(size=11, italic=True)
    cell.alignment = Alignment(horizontal='center')

    for c, label in enumerate(
            ('№', 'Ному насаб', 'Синф', 'Фан', 'Омӯзгор'), start=1):
        cell = ws.cell(row=4, column=c, value=label)
        cell.font = Font(size=11, bold=True)
        cell.border = border
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.fill = PatternFill('solid', start_color='E8EEF7')

    r = 5
    for i, row in enumerate(data['rows'], start=1):
        for c, value in enumerate(
                (i, row['student'], row['class_name'], row['subject'],
                 row['teacher']), start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(size=11)
            cell.border = border
            cell.alignment = Alignment(
                horizontal='left' if c in (2, 4, 5) else 'center',
                vertical='center')
        r += 1
    if not data['rows']:
        ws.merge_cells(start_row=5, start_column=1, end_row=5, end_column=5)
        cell = ws.cell(
            row=5, column=1,
            value='Ҳамаи хонандагон дар ин чоряк баҳогузорӣ шудаанд.')
        cell.font = Font(size=11)
        r += 1

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    ws.cell(row=r, column=1,
            value='Директори муассиса: ______________').font = Font(size=11)
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    ws.cell(row=r, column=1,
            value='Муовини директор оид ба таълим: ______________'
            ).font = Font(size=11)

    # Portrait A4; the five columns are a bit wider than the sheet, so
    # fitToWidth shrinks the print to one page wide (it only ever
    # shrinks, which is exactly what a wide table needs).
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    from openpyxl.worksheet.properties import PageSetupProperties
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
