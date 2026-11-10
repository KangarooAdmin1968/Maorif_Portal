"""⏰ Ғоибият — «Давомот: соатҳои гузарондашуда».

The journal's back register: every absence is a Grade row carrying
attendance '+' (бо сабаб / excused) or '−' (бе сабаб / unexcused) and no
score. Each lesson-linked row is one missed soat; a legacy lesson-less
mark counts once per date, exactly like norm_pace.absence_counts.

collect_absence_counts() powers the Ғоибият page and its Excel export —
one class at a time, for the period the selector resolves (month,
quarter, semester or year). Read-only: nothing is created or changed.

The counting mirrors the Назорат Давомат card — every canonical
attendance row equals one missed lesson hour — so a class total here
always agrees with the control page the завуч already reads.

led_class_names() answers "which classes does this teacher lead" — the
роҳбари синф is the teacher allocated to the class's СОАТИ ТАРБИЯВӢ
ClassSubject, the same rule class_report.class_teacher_name prints.
"""
import io

from django.db.models import Q
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .exemptions import home_student_ids
from .models import ClassSubject, Grade, Student, TeacherProfile
from .norm_pace import ATTENDANCE_MARKS
from .tajik_sort import sort_students
from .utils import canonical_grade_values, normalize_class_name

TARBIYAVI = 'СОАТИ ТАРБИЯВӢ'


def led_class_names(user, school):
    """Normalized class names where `user` leads СОАТИ ТАРБИЯВӢ.

    The роҳбари синф attribution mirrors class_report.class_teacher_name:
    allocated_teacher (TeacherProfile) wins, the plain teacher FK (User)
    is the fallback. Returns a set — a teacher may lead several classes.
    """
    if school is None:
        return set()
    tp = TeacherProfile.objects.filter(user=user).first()
    cond = Q(teacher=user)
    if tp is not None:
        cond |= Q(allocated_teacher=tp)
    return {
        normalize_class_name(cs.class_name)
        for cs in ClassSubject.objects.filter(
            cond, school=school, subject=TARBIYAVI, is_active=True)
    }


def collect_absence_counts(students, start, end):
    """{student_id: {'excused': n, 'unexcused': n}} — one missed lesson
    hour per canonical Grade row carrying an attendance mark inside
    [start, end], split by '+': бо сабаб / '−': бе сабаб."""
    out = {}
    for r in canonical_grade_values(
            Grade.objects.filter(
                student__in=students,
                date__range=(start, end),
                attendance__in=ATTENDANCE_MARKS).order_by().values(
                'pk', 'student_id', 'subject', 'period', 'date',
                'assessment_id', 'lesson_id', 'attendance')):
        c = out.setdefault(r['student_id'],
                           {'excused': 0, 'unexcused': 0})
        c['unexcused' if r['attendance'] == '-' else 'excused'] += 1
    return out


def collect_class_absences(school, class_name, start, end):
    """One class register for the Ғоибият page: every rostered student
    (home-educated excluded, like every class-level view) with their
    бо сабаб / бе сабаб / ҷамъ counts for [start, end], Tajik-sorted."""
    class_name = normalize_class_name(class_name)
    students = sort_students(
        Student.objects.filter(school=school, class_name=class_name))
    # «Дар хона таҳсил мегирад» students learn in the separate home
    # journal — they never appear in the class register.
    home = home_student_ids(s.id for s in students)
    students = [s for s in students if s.id not in home]
    counts = collect_absence_counts(students, start, end)
    rows = []
    for st in students:
        c = counts.get(st.id) or {}
        exc = c.get('excused', 0)
        unc = c.get('unexcused', 0)
        rows.append({
            'student': st.full_name,
            'excused': exc,
            'unexcused': unc,
            'total': exc + unc,
        })
    return rows


def build_absence_workbook(school, class_name, period_label, rows):
    """The same register as xlsx: title row, the period line,
    №/Ному насаб/Бо сабаб/Бе сабаб/Ҷамъи соатҳо, signature block."""
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Ғоибият'
    for col, width in zip('ABCDE', (7, 34, 12, 12, 14)):
        ws.column_dimensions[col].width = width

    ws.merge_cells('A1:E1')
    cell = ws.cell(
        row=1, column=1,
        value=f'Ғоибияти хонандагони синфи {class_name} дар {school.name}')
    cell.font = Font(size=12, bold=True)
    cell.alignment = Alignment(horizontal='center', vertical='center',
                               wrap_text=True)
    ws.row_dimensions[1].height = 42

    ws.merge_cells('A2:E2')
    cell = ws.cell(row=2, column=1, value=period_label)
    cell.font = Font(size=11, italic=True)
    cell.alignment = Alignment(horizontal='center')

    for c, label in enumerate(
            ('№', 'Ному насаб', 'Бо сабаб', 'Бе сабаб',
             'Ҷамъи соатҳо'), start=1):
        cell = ws.cell(row=4, column=c, value=label)
        cell.font = Font(size=11, bold=True)
        cell.border = border
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.fill = PatternFill('solid', start_color='E8EEF7')

    r = 5
    for i, row in enumerate(rows, start=1):
        for c, value in enumerate(
                (i, row['student'], row['excused'], row['unexcused'],
                 row['total']), start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(size=11)
            cell.border = border
            cell.alignment = Alignment(
                horizontal='left' if c == 2 else 'center',
                vertical='center')
        r += 1
    if not rows:
        ws.merge_cells(start_row=5, start_column=1, end_row=5, end_column=5)
        ws.cell(row=5, column=1,
                value='Дар ин синф хонанда нест.').font = Font(size=11)
        r += 1

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    ws.cell(row=r, column=1,
            value='Роҳбари синф: ______________').font = Font(size=11)
    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    ws.cell(row=r, column=1,
            value='Директори муассиса: ______________').font = Font(size=11)

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
