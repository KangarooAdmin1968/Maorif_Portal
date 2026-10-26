"""Солҳои таҳсил — quarter closeout (якуни чоряк) and archive export.

The завуч closes a quarter: every active graded class-subject of the
school gets a QuarterLock for that academic year, and a frozen Excel
archive is written to MEDIA_ROOT/archives/<year>-<year+1>/. Reopening
is a маориф/админ action — the lock rows stay (locked=False) so the
admin history keeps showing who closed and when.
"""
import datetime
import io
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.db.models import Count
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .curriculum_hours import UNGRADED_SUBJECTS
from .maorif_report import LEVELS, QUARTER_WORD_ORDINALS
from .models import (ClassSubject, Grade, QuarterGrade, QuarterLock,
                     Student, normalize_subject)
from .utils import class_numeric_part, get_school_number

def _archive_dir():
    # Evaluated per call so tests can override MEDIA_ROOT.
    return Path(settings.MEDIA_ROOT) / 'archives'


def _graded_class_subjects(school):
    """Active class-subjects a quarter close must lock — graded classes
    only (grade-1 sticker classes and UNGRADED_SUBJECTS are skipped,
    matching the Назорат teacher-control scope)."""
    return [cs for cs in ClassSubject.objects
            .filter(school=school, is_active=True)
            .order_by('class_name', 'subject')
            if class_numeric_part(cs.class_name) in LEVELS
            and normalize_subject(cs.subject) not in UNGRADED_SUBJECTS]


def _locked_keys(school, quarter, year):
    return {(l.class_name, l.subject) for l in
            QuarterLock.objects.filter(
                school=school, quarter=quarter, academic_year=year,
                locked=True)}


def collect_quarter_close(school, quarter, year, today=None):
    """Status + deficiencies for one quarter of one academic year.

    `rows` lists every lockable class-subject with whether it is locked
    and how many of its students still lack a чорак grade — the preview
    the завуч reads on the confirmation page.
    """
    from .views import academic_year_quarter_windows  # local import avoids cycles

    today = today or datetime.date.today()
    windows = academic_year_quarter_windows(year)
    start, end = windows[quarter]
    # Break-period marks keep writing to this quarter until the next
    # one opens (01-08.11 -> Q1 etc.), so closing stays possible but the
    # confirmation page warns while that window is still open.
    grading_until = (windows[quarter + 1][0] - datetime.timedelta(days=1)
                     if quarter < 4 else datetime.date(year + 1, 8, 31))
    locked = _locked_keys(school, quarter, year)

    roster = {r['class_name']: r['n'] for r in
              Student.objects.filter(school=school)
              .exclude(class_name='')
              .values('class_name').annotate(n=Count('id'))}
    graded_per_cs = defaultdict(int)
    for row in QuarterGrade.objects.filter(
            student__school=school, quarter=quarter, academic_year=year,
            grade__isnull=False).values(
            'class_name', 'subject').annotate(n=Count('student', distinct=True)):
        graded_per_cs[(row['class_name'], normalize_subject(row['subject']))] = \
            row['n']

    rows = []
    locked_count = missing_total = 0
    for cs in _graded_class_subjects(school):
        norm = normalize_subject(cs.subject)
        is_locked = (cs.class_name, norm) in locked
        missing = roster.get(cs.class_name, 0) - graded_per_cs.get(
            (cs.class_name, norm), 0)
        missing = max(missing, 0)
        locked_count += is_locked
        missing_total += missing
        rows.append({
            'class_name': cs.class_name, 'subject': cs.subject,
            'locked': is_locked,
            'missing': missing, 'total': roster.get(cs.class_name, 0),
        })
    rows.sort(key=lambda r: (class_numeric_part(r['class_name']) or 0,
                             r['class_name'], r['subject']))
    return {
        'quarter': quarter,
        'ordinal': QUARTER_WORD_ORDINALS[quarter],
        'year': year, 'start': start, 'end': end,
        'ended': today > end,
        'grading_until': grading_until,
        'grading_still_open': today <= grading_until,
        'total': len(rows), 'locked_count': locked_count,
        'closed': locked_count == len(rows) and len(rows) > 0,
        'missing_total': missing_total,
        'rows': rows,
    }


def collect_academic_years(school, today=None):
    """Academic years to display: the current year plus every year the
    school has QuarterLock rows for — i.e. years that were actually
    closed/archived — newest first.

    QuarterGrade data alone does not create a card: stray rows stamped
    with an earlier year (e.g. summer entries attributed to the tail of
    the previous Sep..Aug year) must not surface a phantom year card.
    """
    from .views import academic_year_start  # local import avoids cycles

    current = academic_year_start(today)
    years = {current}
    years.update(QuarterLock.objects.filter(
        school=school, academic_year__isnull=False
    ).values_list('academic_year', flat=True))
    return sorted(years, reverse=True)


def close_quarter(school, quarter, year):
    """Lock every graded class-subject; returns the number locked."""
    n = 0
    for cs in _graded_class_subjects(school):
        _, created = QuarterLock.objects.update_or_create(
            school=school, class_name=cs.class_name,
            subject=normalize_subject(cs.subject), quarter=quarter,
            academic_year=year, defaults={'locked': True})
        n += 1
    return n


def reopen_quarter(school, quarter, year):
    """Маориф/админ undo: keeps the lock rows (locked=False) so the
    admin history shows who closed the quarter and when."""
    return QuarterLock.objects.filter(
        school=school, quarter=quarter, academic_year=year,
        locked=True).update(locked=False)


# ---------------------------------------------------------------- archive

def archive_path(school, quarter, year):
    return (_archive_dir() / f'{year}-{year + 1}'
            / f'maktab{get_school_number(school)}_chorak{quarter}.xlsx')


def build_quarter_archive(school, quarter, year):
    """Frozen quarter snapshot as xlsx: one sheet per class (students ×
    subjects чорак grades) + attendance + conduct summary sheets."""
    from .views import academic_year_quarter_windows  # local import avoids cycles

    start, end = academic_year_quarter_windows(year)[quarter]
    wb = Workbook()
    wb.remove(wb.active)
    head = Font(bold=True)
    fill = PatternFill('solid', fgColor='E8EEF7')
    center = Alignment(horizontal='center')

    students = (Student.objects.filter(school=school)
                .exclude(class_name='')
                .order_by('class_name', 'full_name'))
    classes = defaultdict(list)
    for st in students:
        classes[st.class_name].append(st)

    qg = {}
    for row in QuarterGrade.objects.filter(
            student__school=school, quarter=quarter,
            academic_year=year).values(
            'student_id', 'subject', 'grade', 'att_grade'):
        qg[(row['student_id'], normalize_subject(row['subject']))] = row

    subjects_by_class = {}
    for cs in ClassSubject.objects.filter(
            school=school, is_active=True).values('class_name', 'subject'):
        subjects_by_class.setdefault(
            cs['class_name'], set()).add(normalize_subject(cs['subject']))

    for cls in sorted(classes, key=lambda c: (class_numeric_part(c) or 0, c)):
        if class_numeric_part(cls) not in LEVELS:
            continue
        subjects = sorted(subjects_by_class.get(cls, ()) -
                          UNGRADED_SUBJECTS)
        if not subjects:
            continue
        ws = wb.create_sheet(cls.replace('/', '-')[:31])
        ws.append([f'{school.name} — {cls} — чоряки '
                   f'{QUARTER_WORD_ORDINALS[quarter]} '
                   f'({start:%d.%m.%Y}–{end:%d.%m.%Y})'])
        ws['A1'].font = head
        ws.append(['№', 'Хонанда'] + subjects)
        for c in ws[2]:
            c.font, c.fill, c.alignment = head, fill, center
        for i, st in enumerate(classes[cls], 1):
            row = [i, st.full_name]
            for s in subjects:
                row.append(qg.get((st.id, s), {}).get('grade'))
            ws.append(row)
        ws.column_dimensions['B'].width = 30
        for col in ws.iter_cols(min_col=3):
            for c in col:
                c.alignment = center

    att = defaultdict(lambda: [0, 0])
    for row in Grade.objects.filter(
            student__school=school, date__gte=start, date__lte=end,
            attendance__in=('+', '-')).values(
            'student__class_name', 'student__full_name',
            'attendance').annotate(n=Count('id')):
        key = (row['student__class_name'], row['student__full_name'])
        att[key][row['attendance'] == '-'] += row['n']
    ws = wb.create_sheet('Давомат')
    ws.append(['Давомат дар чоряк — босабаб / бесабаб соатҳо'])
    ws['A1'].font = head
    ws.append(['Синф', 'Хонанда', 'Босабаб', 'Бесабаб', 'Ҳамагӣ'])
    for c in ws[2]:
        c.font, c.fill = head, fill
    for (cls, name), (exc, unx) in sorted(
            att.items(), key=lambda kv: (class_numeric_part(kv[0][0]) or 0,
                                         kv[0][0], kv[0][1])):
        ws.append([cls, name, exc, unx, exc + unx])
    ws.column_dimensions['B'].width = 30

    ws = wb.create_sheet('Ҳолат')
    ws.append(['Камбудиҳо вақти якуни чоряк'])
    ws['A1'].font = head
    ws.append(['Синф', 'Фан', 'Баҳо нагирифтаанд', 'Ҳамагӣ'])
    for c in ws[2]:
        c.font, c.fill = head, fill
    for r in collect_quarter_close(school, quarter, year)['rows']:
        if r['missing']:
            ws.append([r['class_name'], r['subject'],
                       r['missing'], r['total']])
    ws.column_dimensions['B'].width = 25

    out = io.BytesIO()
    wb.save(out)
    return out


def save_quarter_archive(school, quarter, year):
    """Write the archive file at close time; returns the Path."""
    path = archive_path(school, quarter, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_quarter_archive(school, quarter, year).getvalue())
    return path
