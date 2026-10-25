"""Омад-рафт — per-school student mobility journal.

collect_movements() powers both the web journal page and the А.Ҷ.Ч.
export columns (E..H in zavuch_report). Baseline rule: «Аввали чоряк»
is the roster at quarter start = current roster − arrivals + departures
recorded inside the quarter window, so no pre-tracking history has to
be reconstructed; «Охири чоряк» lands back on the live roster.
"""
from collections import defaultdict

from django.db.models import Count

from .models import Student, StudentMovement
from .utils import class_numeric_part


def collect_movements(school, quarter, today=None):
    """Per-class аввал/омад/рафт/охир counts + quarter's record list."""
    from .views import academic_year_start, academic_year_quarter_windows

    ay = academic_year_start(today)
    start, end = academic_year_quarter_windows(ay)[quarter]

    current = defaultdict(lambda: [0, 0])
    for row in (Student.objects.filter(school=school)
                .exclude(class_name='')
                .values('class_name', 'gender').annotate(n=Count('id'))):
        current[row['class_name']][0] += row['n']
        if row['gender'] == 'F':
            current[row['class_name']][1] += row['n']

    movements = list(
        StudentMovement.objects
        .filter(school=school, date__gte=start, date__lte=end)
        .order_by('-date', '-id')
        .values('id', 'date', 'student_name', 'class_name', 'gender',
                'movement_type', 'is_manual', 'note'))

    moved = defaultdict(lambda: {'arrival': [0, 0], 'departure': [0, 0]})
    for m in movements:
        side = moved[m['class_name']][m['movement_type']]
        side[0] += 1
        if m['gender'] == 'F':
            side[1] += 1

    class_names = set(current) | set(moved)
    total_keys = ('start_total', 'start_f', 'arr_total', 'arr_f',
                  'dep_total', 'dep_f', 'end_total', 'end_f')
    rows, totals = [], dict.fromkeys(total_keys, 0)
    for name in sorted(class_names,
                       key=lambda n: (class_numeric_part(n) or 0, n)):
        cur_t, cur_f = current[name]
        a_t, a_f = moved[name]['arrival']
        d_t, d_f = moved[name]['departure']
        row = {'class_name': name,
               'start_total': cur_t - a_t + d_t,
               'start_f': cur_f - a_f + d_f,
               'arr_total': a_t, 'arr_f': a_f,
               'dep_total': d_t, 'dep_f': d_f,
               'end_total': cur_t, 'end_f': cur_f}
        rows.append(row)
        for k in total_keys:
            totals[k] += row[k]
    return {'quarter': quarter, 'start': start, 'end': end,
            'rows': rows, 'totals': totals, 'movements': movements}
