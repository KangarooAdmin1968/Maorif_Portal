"""Назорат — per-school control data for the завуч / маориф tab.

Same read-only approach as the maorif reports: collect_* returns plain
JSON-friendly data (so a future quarter closure can freeze it) and the
template only renders what it gets. New sections get their own
collect_* function here and a card in the template.
"""
import calendar
import datetime
from collections import defaultdict

from django.db.models import Count, Min

from .curriculum_hours import UNGRADED_SUBJECTS
from .maorif_report import LEVELS, QUARTER_WORD_ORDINALS
from .models import ClassSubject, Grade, QuarterGrade, Student, normalize_subject
from .utils import class_numeric_part

# Academic-year order: September..August; quarter bounds come from the
# official calendar in portal.academic_calendar.
ACADEMIC_MONTHS = (9, 10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8)
MONTH_NAMES = {1: 'Январ', 2: 'Феврал', 3: 'Март', 4: 'Апрел',
               5: 'Май', 6: 'Июн', 7: 'Июл', 8: 'Август',
               9: 'Сентябр', 10: 'Октябр', 11: 'Ноябр', 12: 'Декабр'}


def attendance_period_options(ay):
    """[(value, label)] for the Давомат dropdown — months then quarters."""
    options = [(f'm{m}', f'{MONTH_NAMES[m]} {ay if m >= 9 else ay + 1}')
               for m in ACADEMIC_MONTHS]
    options += [(f'q{q}', f'Чоряки {QUARTER_WORD_ORDINALS[q]}')
                for q in (1, 2, 3, 4)]
    return options


def _month_bounds(year, month):
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _period_bounds(period, ay, today):
    """'m9'..'m8' / 'q1'..'q4' -> (start, end, label, value).

    Invalid/missing input falls back to the current calendar month.
    """
    if period and period[0] == 'm' and period[1:].isdigit():
        m = int(period[1:])
        if m in ACADEMIC_MONTHS:
            y = ay if m >= 9 else ay + 1
            start, end = _month_bounds(y, m)
            return start, end, f'{MONTH_NAMES[m]} {y}', period
    if period and period[0] == 'q' and period[1:].isdigit():
        q = int(period[1:])
        if q in (1, 2, 3, 4):
            from .views import academic_year_quarter_windows
            start, end = academic_year_quarter_windows(ay)[q]
            label = f'Чоряки {QUARTER_WORD_ORDINALS[q]}'
            return start, end, label, period
    m = today.month
    y = ay if m >= 9 else ay + 1
    start, end = _month_bounds(y, m)
    return start, end, f'{MONTH_NAMES[m]} {y}', f'm{m}'


def collect_attendance_control(school, period, today=None):
    """Per-class missed-lesson counts (Grade.attendance rows) for the
    chosen month or quarter; every attendance mark equals one lesson
    hour. All classes appear — zero rows stay visible so the завуч can
    confirm a class really had no absences.
    """
    from .views import academic_year_start  # local import avoids cycles

    ay = academic_year_start(today)
    today = today or datetime.date.today()
    start, end, label, value = _period_bounds(period, ay, today)

    counts = (Grade.objects
              .filter(student__school=school,
                      date__gte=start, date__lte=end,
                      attendance__in=('+', '-'))
              .values('student__class_name', 'attendance')
              .annotate(n=Count('id')))
    per_class = defaultdict(lambda: {'excused': 0, 'unexcused': 0})
    for row in counts:
        key = 'excused' if row['attendance'] == '+' else 'unexcused'
        per_class[row['student__class_name']][key] += row['n']

    class_names = set(
        Student.objects.filter(school=school)
        .exclude(class_name='').values_list('class_name', flat=True))
    classes = []
    totals = {'total': 0, 'excused': 0, 'unexcused': 0}
    for name in sorted(class_names,
                       key=lambda n: (class_numeric_part(n) or 0, n)):
        c = per_class[name]
        t = c['excused'] + c['unexcused']
        classes.append({'class_name': name, 'excused': c['excused'],
                        'unexcused': c['unexcused'], 'total': t})
        totals['excused'] += c['excused']
        totals['unexcused'] += c['unexcused']
        totals['total'] += t
    return {'period': value, 'period_label': label,
            'classes': classes, 'totals': totals}


ABSENCE_MIN_OPTIONS = (1, 3, 5, 10)


def collect_absence_students(school, period, min_hours=3, today=None):
    """Students with >= min_hours unexcused absences in the period —
    the 'skip list' the завуч asks parents about. Same period selector
    (month/quarter) as the attendance table."""
    from .views import academic_year_start  # local import avoids cycles

    ay = academic_year_start(today)
    today = today or datetime.date.today()
    start, end, label, value = _period_bounds(period, ay, today)
    rows = (Grade.objects
            .filter(student__school=school,
                    date__gte=start, date__lte=end,
                    attendance='-')
            .values('student__class_name', 'student__full_name')
            .annotate(n=Count('id'))
            .filter(n__gte=min_hours))
    rows = sorted(rows, key=lambda r: (
        class_numeric_part(r['student__class_name']) or 0,
        r['student__class_name'], -r['n'], r['student__full_name']))
    return {'period': value, 'period_label': label, 'min': min_hours,
            'rows': [{'class_name': r['student__class_name'],
                      'name': r['student__full_name'], 'n': r['n']}
                     for r in rows]}


def collect_behavior_issues(school, period, today=None):
    """Students with low conduct marks (behavior_score <= 3) in the
    period: who was marked, how many times, and the worst mark."""
    from .views import academic_year_start  # local import avoids cycles

    ay = academic_year_start(today)
    today = today or datetime.date.today()
    start, end, label, value = _period_bounds(period, ay, today)
    rows = (Grade.objects
            .filter(student__school=school,
                    date__gte=start, date__lte=end,
                    behavior_score__isnull=False, behavior_score__lte=3)
            .values('student__class_name', 'student__full_name')
            .annotate(n=Count('id'), worst=Min('behavior_score')))
    rows = sorted(rows, key=lambda r: (
        class_numeric_part(r['student__class_name']) or 0,
        r['student__class_name'], -r['n'], r['student__full_name']))
    return {'period': value, 'period_label': label,
            'rows': [{'class_name': r['student__class_name'],
                      'name': r['student__full_name'],
                      'n': r['n'], 'worst': r['worst']}
                     for r in rows]}


def collect_teacher_control(school, quarter, today=None):
    """Only class-subjects WITH a gap: allocated active subjects where
    fewer students have a journal mark (Grade.score) this quarter than
    are enrolled — i.e. the teacher has not graded everyone yet.

    Daily Grade rows are read, not QuarterGrade: the чорак mark is only
    entered at quarter close, so counting it would falsely show 0 for
    teachers who already work in the journal.
    """
    from .views import academic_year_start  # local import avoids cycles

    ay = academic_year_start(today)
    today = today or datetime.date.today()
    start, end, _, _ = _period_bounds(f'q{quarter}', ay, today)
    subjects = (ClassSubject.objects
                .filter(school=school, is_active=True)
                .select_related('teacher', 'allocated_teacher')
                .order_by('class_name', 'subject'))
    roster = {r['class_name']: r['n'] for r in
              Student.objects.filter(school=school)
              .exclude(class_name='')
              .values('class_name').annotate(n=Count('id'))}
    graded = {(r['student__class_name'], r['subject']): r['n'] for r in
              Grade.objects
              .filter(student__school=school,
                      date__gte=start, date__lte=end,
                      score__isnull=False)
              .values('student__class_name', 'subject')
              .annotate(n=Count('student', distinct=True))}

    rows = []
    for cs in subjects:
        if class_numeric_part(cs.class_name) not in LEVELS:
            continue  # grade-1 classes follow the sticker journal, not marks
        if normalize_subject(cs.subject) in UNGRADED_SUBJECTS:
            continue  # e.g. СОАТИ ТАРБИЯВӢ — no grades are required
        total = roster.get(cs.class_name, 0)
        got = graded.get(
            (cs.class_name, normalize_subject(cs.subject)), 0)
        if got >= total:
            continue  # fully graded — only the gaps are listed
        if cs.allocated_teacher_id:
            teacher_name = cs.allocated_teacher.full_name
        elif cs.teacher_id:
            teacher_name = (cs.teacher.get_full_name()
                            or cs.teacher.username)
        else:
            teacher_name = 'таъйин нашудааст'
        rows.append({
            'class_name': cs.class_name,
            'subject': cs.subject,
            'teacher': teacher_name,
            'graded': got,
            'total': total,
        })
    rows.sort(key=lambda r: (class_numeric_part(r['class_name']) or 0,
                             r['class_name'], r['subject']))
    return {'rows': rows, 'gaps': len(rows)}


def collect_grading_control(school, quarter, today=None):
    """Per-class grading control for one school + quarter.

    Two groups per class:
      * ungraded — students with NO quarter grade at all
      * low      — students whose quarter average sits in the 1-3 band

    Both derive from the same single QuarterGrade pass the reports use,
    so the numbers always agree with the А.Ҷ.Ч. exports.
    """
    from .views import academic_year_start  # local import avoids cycles

    ay = academic_year_start(today)
    students = {}
    per_class = defaultdict(lambda: {'total': 0, 'ungraded': [], 'low': []})
    for row in Student.objects.filter(school=school).values(
            'id', 'full_name', 'class_name'):
        if class_numeric_part(row['class_name']) not in LEVELS:
            continue
        students[row['id']] = row
        per_class[row['class_name']]['total'] += 1

    sums = {}
    for row in QuarterGrade.objects.filter(
            student__school=school, quarter=quarter, academic_year=ay,
            grade__isnull=False).values('student_id', 'grade'):
        entry = sums.setdefault(row['student_id'], [0.0, 0])
        entry[0] += float(row['grade'])
        entry[1] += 1

    totals = {'total': 0, 'graded': 0, 'ungraded': 0, 'low': 0}
    for sid, st in students.items():
        cls = st['class_name']
        totals['total'] += 1
        if sid not in sums:
            per_class[cls]['ungraded'].append(st['full_name'])
            totals['ungraded'] += 1
            continue
        totals['graded'] += 1
        total, count = sums[sid]
        avg = total / count
        if avg < 4:
            per_class[cls]['low'].append(
                {'name': st['full_name'], 'avg': round(avg, 2)})
            totals['low'] += 1

    def sort_key(name):
        return (class_numeric_part(name), name)

    classes = [{'class_name': name, **per_class[name]}
               for name in sorted(per_class, key=sort_key)]
    for cls in classes:
        cls['ungraded'].sort()
        cls['low'].sort(key=lambda r: r['avg'])
    return {
        'quarter': quarter,
        'quarter_ordinal': QUARTER_WORD_ORDINALS.get(
            quarter, f'{quarter}-ум'),
        'academic_year': ay,
        'totals': totals,
        'classes': classes,
    }
