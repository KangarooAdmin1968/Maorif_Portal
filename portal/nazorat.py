"""Назорат — per-school control data for the завуч / маориф tab.

Same read-only approach as the maorif reports: collect_* returns plain
JSON-friendly data (so a future quarter closure can freeze it) and the
template only renders what it gets. New sections get their own
collect_* function here and a card in the template.
"""
from collections import defaultdict

from .maorif_report import LEVELS, QUARTER_WORD_ORDINALS
from .models import QuarterGrade, Student
from .utils import class_numeric_part


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
