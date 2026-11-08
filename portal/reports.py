"""Ҳисоботлар — live role-scoped reports (director district view / zavuch
school view) plus a multi-sheet Excel export.

Every number comes from the same sources the portal pages already use —
nothing is recomputed differently here:

* school/class/subject/student rankings — the cached ranking engines
  (read-only, filled by the refresh_rankings cron job);
* teacher norm fulfillment, journal Пурра/Қисман/Холӣ — the shared
  readiness payload (_compute_school_readiness_payload);
* Аълохон / Бебаҳо — collect_excellent_students / collect_ungraded_students;
* Ҳаракати хонандагон — StudentMovement rows inside the chosen period;
* Таҳсил дар хона — active home TeachingGroup memberships.

The period selector (month / quarter / semester / year) reuses the
Назорат period machinery; quarter-scoped sections use the quarter whose
window contains the period's end date.
"""
import datetime
import io
from collections import defaultdict

from django.db.models import Count

from .excellent import collect_excellent_students
from .models import (
    ClassSubject, School, Student, StudentMovement,
    SubjectGroupMembership,
)
from .nazorat import _period_bounds, attendance_period_options
from .ungraded import collect_ungraded_students
from .utils import is_academic_school


def report_period_options(today=None):
    """[(value, label)] — the same month/quarter/semester/year options the
    Назорат page offers, so both pages speak one period language."""
    from .views import academic_year_start
    return attendance_period_options(academic_year_start(today))


def resolve_period(period, today=None):
    """period value -> (start, end, label, value, quarter).

    `quarter` is the grading quarter whose official window contains the
    period's end (never a future quarter): it drives the quarter-scoped
    sections — Аълохон, Бебаҳо and the journal-status aggregate.
    """
    from .views import academic_year_start, academic_year_quarter_windows
    today = today or datetime.date.today()
    ay = academic_year_start(today)
    start, end, label, value = _period_bounds(period, ay, today)
    ref = min(end, today)
    quarter = None
    for q, (qs, qe) in academic_year_quarter_windows(ay).items():
        if qs <= ref <= qe:
            quarter = q
            break
    if quarter is None:
        # Period entirely inside a break or before the year start — take
        # the quarter whose window is closest to `ref`.
        quarter = min(
            academic_year_quarter_windows(ay),
            key=lambda q: min(abs((academic_year_quarter_windows(ay)[q][0] - ref).days),
                              abs((academic_year_quarter_windows(ay)[q][1] - ref).days)))
    return start, end, label, value, quarter


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

def _rankings_block():
    """Cached dashboard rankings — read-only; empty on a cold cache."""
    from .views import (
        RankingRefreshBusy, rankings_read_only,
        calculate_school_rankings, calculate_subject_rankings,
        calculate_class_rankings, calculate_top_students,
    )

    def get(fn):
        try:
            return fn()
        except RankingRefreshBusy:
            return []

    with rankings_read_only():
        return {
            'schools': get(calculate_school_rankings),
            'subjects': get(calculate_subject_rankings),
            'classes': get(calculate_class_rankings),
            'students': get(calculate_top_students),
        }


def _readiness_payload(schools, gpa_map):
    """(stats, activity) — the shared school-readiness computation.

    District calls go through the readiness cache (same key the
    monitoring page uses); a single school is cheap enough to compute
    directly so a zavuch never waits on district data.
    """
    from .views import (
        READINESS_CACHE_KEY, RankingRefreshBusy, _ranking_cached,
        _compute_school_readiness_payload,
    )
    if len(schools) == 1:
        return _compute_school_readiness_payload(schools, gpa_map)
    try:
        return _ranking_cached(
            READINESS_CACHE_KEY,
            lambda: _compute_school_readiness_payload(schools, gpa_map))
    except RankingRefreshBusy:
        return [], []


def _movements_per_school(school_ids, start, end):
    """{school_id: {'arrival': n, 'departure': n}} inside the period."""
    rows = (StudentMovement.objects
            .filter(school_id__in=school_ids, date__range=(start, end))
            .values('school_id', 'movement_type').annotate(n=Count('id')))
    out = defaultdict(lambda: {'arrival': 0, 'departure': 0})
    for r in rows:
        out[r['school_id']][r['movement_type']] = r['n']
    return out


def _home_students_per_school(school_ids):
    """{school_id: distinct home-education student count}."""
    rows = (SubjectGroupMembership.objects
            .filter(class_subject__school_id__in=school_ids,
                    group__is_home=True, group__is_active=True,
                    student__is_archived=False)
            .values('class_subject__school_id')
            .annotate(n=Count('student_id', distinct=True)))
    return {r['class_subject__school_id']: r['n'] for r in rows}


def _home_students_list(school):
    """Home-education rows for one school: student, class, subject, teacher."""
    rows = (SubjectGroupMembership.objects
            .filter(class_subject__school=school,
                    group__is_home=True, group__is_active=True,
                    student__is_archived=False)
            .select_related('student', 'class_subject', 'group__teacher')
            .order_by('class_subject__subject', 'student__full_name'))
    out = []
    for m in rows:
        teacher = m.group.teacher
        out.append({
            'student': m.student.full_name,
            'class_name': m.student.class_name,
            'subject': m.class_subject.subject,
            'teacher': teacher.full_name if teacher else '—',
            'hours': m.group.hours_per_week,
        })
    return out


def _journal_totals(teachers):
    """Aggregate Пурра/Қисман/Холӣ over every teacher's journal cells."""
    totals = {'complete': 0, 'partial': 0, 'empty': 0}
    for t in teachers:
        for j in t.get('journal', ()):  # per-ClassSubject status
            totals[j['status']] = totals.get(j['status'], 0) + 1
    total = sum(totals.values())
    totals['total'] = total
    totals['incomplete'] = totals['partial'] + totals['empty']
    totals['pct'] = round(totals['complete'] / total * 100, 1) if total else 0.0
    return totals


def _teacher_summary(teachers):
    """(total, no_grades, met, not_met) — the three director buckets."""
    no_grades = met = not_met = 0
    for t in teachers:
        if t['grades_done'] == 0:
            no_grades += 1
        elif t['fulfillment'] >= 100:
            met += 1
        else:
            not_met += 1
    return len(teachers), no_grades, met, not_met


# ---------------------------------------------------------------------------
# District Аълохон/Бебаҳо counts — one merged scan, cron-warmed cache
# ---------------------------------------------------------------------------

REPORT_COUNTS_CACHE_PREFIX = 'rank:v1:report_counts'


def report_counts_cache_key(quarter, academic_year):
    """Cache key for the district Аълохон/Бебаҳо counts — one entry per
    quarter of an academic year. refresh_rankings warms the current
    quarter; a colder quarter's cells render '—' like other cold
    ranking blocks."""
    return f'{REPORT_COUNTS_CACHE_PREFIX}:q{quarter}:{academic_year}'


def excellent_ungraded_counts(schools, quarter, today=None):
    """{school_id: {'excellent': n, 'ungraded': n}} — the same rules as
    collect_excellent_students / collect_ungraded_students, computed in
    ONE district-wide merged-mark scan.

    The district report used to call both collectors per school — about
    2 × number-of-schools full mark scans inside one request, which
    timed the page out (504). This computes the identical counts once
    for the whole district."""
    from .class_report import merged_quarter_marks
    from .curriculum_hours import UNGRADED_SUBJECTS
    from .excellent import EXCELLENT_MARKS
    from .exemptions import active_exemption_map
    from .maorif_report import LEVELS
    from .utils import (class_numeric_part, is_non_graded,
                        normalize_class_name, normalize_subject)
    from .views import academic_year_start

    today = today or datetime.date.today()
    ay = academic_year_start(today)
    school_ids = [s.id for s in schools]

    # expected[sid][norm_class] = set(norm_subject) — every active
    # graded class-subject (Аълохон scope). graded_cs keeps the raw
    # class_name rows limited to LEVELS (Бебаҳо scope, same as
    # quarter_close._graded_class_subjects).
    expected = {}
    graded_cs = []
    for cs in ClassSubject.objects.filter(
            school_id__in=school_ids, is_active=True):
        norm_subject = normalize_subject(cs.subject)
        if norm_subject in UNGRADED_SUBJECTS:
            continue
        (expected.setdefault(cs.school_id, defaultdict(set))
                 [normalize_class_name(cs.class_name)].add(norm_subject))
        if class_numeric_part(cs.class_name) in LEVELS:
            graded_cs.append((cs.school_id, cs.class_name, norm_subject))

    students = list(Student.objects.filter(school_id__in=school_ids)
                    .exclude(class_name=''))
    all_subjects = {subj for exp in expected.values()
                    for subs in exp.values() for subj in subs}
    marks = (merged_quarter_marks(students, all_subjects, quarter, ay,
                                  class_scoped=True)
             if all_subjects else {})
    exempt = set(active_exemption_map((s.id for s in students), today))

    by_school_class = defaultdict(lambda: defaultdict(list))
    for st in students:
        by_school_class[st.school_id][st.class_name].append(st)

    counts = {sid: {'excellent': 0, 'ungraded': 0} for sid in school_ids}
    for st in students:
        cn = normalize_class_name(st.class_name)
        if is_non_graded(cn):
            continue
        subjects = (expected.get(st.school_id) or {}).get(cn)
        grades = marks.get(st.id, {})
        if not subjects or not grades:
            continue
        if all(subj in grades and grades[subj] in EXCELLENT_MARKS
               for subj in subjects):
            counts[st.school_id]['excellent'] += 1

    ungraded = defaultdict(set)
    for sid, class_name, norm_subject in graded_cs:
        for st in by_school_class[sid].get(class_name, ()):
            if st.id in exempt:
                continue
            if marks.get(st.id, {}).get(norm_subject) is None:
                ungraded[sid].add(st.id)
    for sid, ids in ungraded.items():
        counts[sid]['ungraded'] = len(ids)
    return counts


def _report_counts(schools, quarter, today):
    """Cached excellent_ungraded_counts — requests are read-only; the
    refresh_rankings cron keeps the current quarter warm. Returns None
    on a cold/busy cache (cells render '—', the page never stalls)."""
    from .views import (
        RankingRefreshBusy, _ranking_cached, academic_year_start,
        rankings_read_only,
    )
    key = report_counts_cache_key(quarter, academic_year_start(today))
    with rankings_read_only():
        try:
            return _ranking_cached(
                key, lambda: excellent_ungraded_counts(
                    schools, quarter, today))
        except RankingRefreshBusy:
            return None


# ---------------------------------------------------------------------------
# District report (director / staff)
# ---------------------------------------------------------------------------

def collect_district_report(period, today=None):
    """Full district state — every section the director approved."""
    from .views import academic_year_start  # local import, no cycle

    today = today or datetime.date.today()
    start, end, label, value, quarter = resolve_period(period, today)
    schools = [s for s in School.objects.all() if is_academic_school(s)]
    school_ids = [s.id for s in schools]

    rankings = _rankings_block()
    gpa_map = {d['school'].id: d['gpa'] for d in rankings['schools']} or None
    _, activity = _readiness_payload(schools, gpa_map)
    activity_by_school = {a['school'].id: a for a in activity}

    movements = _movements_per_school(school_ids, start, end)
    home_counts = _home_students_per_school(school_ids)

    # Аълохон / Бебаҳо per school for the resolved quarter — one
    # district-wide merged scan served from the cron-warmed cache
    # (None on a cold cache; cells render '—').
    counts = _report_counts(schools, quarter, today)

    school_rows = []
    for s in schools:
        a = activity_by_school.get(s.id, {})
        teachers = a.get('teachers', [])
        t_n, no_grades, met, not_met = _teacher_summary(teachers)
        c = (counts or {}).get(s.id) or {}
        school_rows.append({
            'school': s,
            'students': a.get('students', 0),
            'total_grades': a.get('total_grades', 0),
            'fulfillment': a.get('fulfillment', 0.0),
            'gpa': a.get('gpa'),
            'teachers': t_n,
            'no_grades': no_grades,
            'met': met,
            'not_met': not_met,
            'journal': _journal_totals(teachers),
            'arrivals': movements[s.id]['arrival'],
            'departures': movements[s.id]['departure'],
            'home_students': home_counts.get(s.id, 0),
            'excellent': c.get('excellent'),
            'ungraded': c.get('ungraded'),
        })

    classes = rankings['classes']
    graded_classes = [c for c in classes if c['gpa'] > 0]
    top_classes = graded_classes[:20]
    weak_classes = list(reversed(graded_classes[-20:]))
    top_students = rankings['students'][:100]

    return {
        'kind': 'district',
        'period': value, 'period_label': label, 'quarter': quarter,
        'academic_year': academic_year_start(today),
        'school_rows': school_rows,
        'school_ranking': rankings['schools'],
        'subject_ranking': rankings['subjects'],
        'top_classes': top_classes,
        'weak_classes': weak_classes,
        'top_students': top_students,
        'schools': schools,
    }


# ---------------------------------------------------------------------------
# School report (zavuch / director drill-down)
# ---------------------------------------------------------------------------

def collect_school_report(school, period, today=None):
    """One school's private state — teacher norms, journals, lists."""
    from .views import academic_year_start

    today = today or datetime.date.today()
    start, end, label, value, quarter = resolve_period(period, today)

    rankings = _rankings_block()
    gpa_map = {d['school'].id: d['gpa'] for d in rankings['schools']} or None
    _, activity = _readiness_payload([school], gpa_map)
    act = activity[0] if activity else {
        'students': 0, 'total_grades': 0, 'fulfillment': 0.0,
        'gpa': None, 'teachers': []}
    teachers = act.get('teachers', [])

    # Per-teacher status label for the norm table.
    teacher_rows = []
    for t in teachers:
        if t['grades_done'] == 0:
            status = 'Баҳо нест'
        elif t['fulfillment'] >= 100:
            status = 'Иҷро шуд'
        else:
            status = 'Қафо монд'
        teacher_rows.append({**t, 'status': status})

    excellent = collect_excellent_students(school, quarter, today)
    ungraded = collect_ungraded_students(school, quarter, today)

    movements = list(
        StudentMovement.objects.filter(
            school=school, date__range=(start, end))
        .order_by('-date'))

    class_ranking = [c for c in rankings['classes']
                     if c['school_id'] == school.id]
    school_students = [s for s in rankings['students']
                       if s['school_id'] == school.id][:50]

    return {
        'kind': 'school',
        'school': school,
        'period': value, 'period_label': label, 'quarter': quarter,
        'academic_year': academic_year_start(today),
        'students': act.get('students', 0),
        'total_grades': act.get('total_grades', 0),
        'fulfillment': act.get('fulfillment', 0.0),
        'gpa': act.get('gpa'),
        'teacher_rows': teacher_rows,
        'journal': _journal_totals(teachers),
        'excellent': excellent,
        'ungraded': ungraded,
        'home_students': _home_students_list(school),
        'movements': movements,
        'class_ranking': class_ranking,
        'school_students': school_students,
    }


# ---------------------------------------------------------------------------
# Excel export — one workbook, each section its own landscape sheet
# ---------------------------------------------------------------------------

def _sheet(wb, name, title, subtitle, headers, rows, widths):
    """Write one report sheet: merged title, subtitle, styled header,
    bordered data rows, landscape + one-page-wide print setup."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    ws = wb.create_sheet(name[:31])
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ncols = len(headers)

    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(size=12, bold=True)
    c.alignment = Alignment(horizontal='center', vertical='center',
                            wrap_text=True)
    ws.row_dimensions[1].height = 30

    if subtitle:
        ws.merge_cells(start_row=2, start_column=1, end_row=2,
                       end_column=ncols)
        c = ws.cell(row=2, column=1, value=subtitle)
        c.font = Font(size=10, italic=True)
        c.alignment = Alignment(horizontal='center')

    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = Font(size=10, bold=True)
        c.border = border
        c.alignment = Alignment(horizontal='center', vertical='center',
                                wrap_text=True)
        c.fill = PatternFill('solid', start_color='E8EEF7')

    r = 5
    for row in rows:
        for col, v in enumerate(row, start=1):
            c = ws.cell(row=r, column=col, value=v)
            c.font = Font(size=10)
            c.border = border
            c.alignment = Alignment(horizontal='center', vertical='center',
                                    wrap_text=True)
        r += 1
    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r,
                       end_column=ncols)
        c = ws.cell(row=r, column=1, value='Маълумот нест.')
        c.font = Font(size=10, italic=True)

    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    from openpyxl.worksheet.properties import PageSetupProperties
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.3
    ws.page_margins.top = ws.page_margins.bottom = 0.4
    return ws


def build_report_workbook(data, today=None):
    """Multi-sheet xlsx for a collected report — every sheet prints on
    one landscape page width."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)

    def fnum(v):
        return v if v is not None else '—'

    if data['kind'] == 'district':
        sub = (f"{data['period_label']} · Соли таҳсили "
               f"{data['academic_year']}–{data['academic_year'] + 1}")
        _sheet(wb, 'Ҳолати умумӣ',
               'Ҳолати умумии ноҳия — аз рӯи мактабҳо', sub,
               ('№', 'Мактаб', 'Хонандагон', 'Омӯзгорон',
                'Баҳо нагузоштаанд', 'Меъёрро иҷро кардаанд',
                'Қафо мондаанд', 'Иҷрои меъёр %', 'Холи миёна'),
               [(i, r['school'].name, r['students'], r['teachers'],
                 r['no_grades'], r['met'], r['not_met'],
                 f"{r['fulfillment']}%", fnum(r['gpa']))
                for i, r in enumerate(data['school_rows'], 1)],
               (5, 34, 11, 11, 11, 12, 11, 9, 8))

        _sheet(wb, 'Рейтинги мактабҳо', 'Рейтинги муассисаҳо', sub,
               ('№', 'Мактаб', 'Холи миёна'),
               [(d.get('rank', i), d['school'].name, d['gpa'])
                for i, d in enumerate(data['school_ranking'], 1)],
               (6, 46, 10))

        _sheet(wb, 'Синфҳои пешқадам',
               '20 синфи пешқадам', sub,
               ('№', 'Мактаб', 'Синф', 'Холи миёна'),
               [(i, c['school_name'], c['class_name'], c['gpa'])
                for i, c in enumerate(data['top_classes'], 1)],
               (6, 40, 10, 10))

        _sheet(wb, 'Синфҳои заиф',
               '20 синфи заиф', sub,
               ('№', 'Мактаб', 'Синф', 'Холи миёна'),
               [(i, c['school_name'], c['class_name'], c['gpa'])
                for i, c in enumerate(data['weak_classes'], 1)],
               (6, 40, 10, 10))

        _sheet(wb, 'Рейтинги фанҳо', 'Рейтинги фанҳо', sub,
               ('№', 'Фан', 'Холи миёна'),
               [(d.get('rank', i), d['subject'], d['gpa'])
                for i, d in enumerate(data['subject_ranking'], 1)],
               (6, 46, 10))

        _sheet(wb, 'Хонандагони беҳтарин',
               '100 хонандаи беҳтарин', sub,
               ('№', 'Ному насаб', 'Синф', 'Мактаб', 'Холи миёна',
                'Ҷойи ноҳиявӣ', 'Ҷойи муассисавӣ'),
               [(i, s['full_name'], s['class_name'], s['school_name'],
                 s['gpa'], s['district_rank'], s['school_rank'])
                for i, s in enumerate(data['top_students'], 1)],
               (6, 30, 9, 30, 8, 10, 10))

        _sheet(wb, 'Аълохон ва Бебаҳо',
               'Аълохон ва бебаҳо — аз рӯи мактабҳо',
               f"{sub} · Чоряки {data['quarter']}",
               ('№', 'Мактаб', 'Аълохон', 'Бебаҳо'),
               [(i, r['school'].name, fnum(r['excellent']),
                 fnum(r['ungraded']))
                for i, r in enumerate(data['school_rows'], 1)],
               (5, 44, 12, 12))

        _sheet(wb, 'Ҳолати журналҳо',
               'Ҳолати журналҳо — аз рӯи мактабҳо', sub,
               ('№', 'Мактаб', 'Пурра', 'Қисман', 'Холӣ', 'Пурра %'),
               [(i, r['school'].name, r['journal']['complete'],
                 r['journal']['partial'], r['journal']['empty'],
                 f"{r['journal']['pct']}%")
                for i, r in enumerate(data['school_rows'], 1)],
               (5, 40, 10, 10, 10, 10))

        _sheet(wb, 'Ҳаракати хонандагон',
               'Ҳаракати хонандагон — аз рӯи мактабҳо', sub,
               ('№', 'Мактаб', 'Омад', 'Рафт'),
               [(i, r['school'].name, r['arrivals'], r['departures'])
                for i, r in enumerate(data['school_rows'], 1)],
               (5, 44, 12, 12))

        _sheet(wb, 'Таҳсил дар хона',
               'Таҳсил дар хона — аз рӯи мактабҳо', sub,
               ('№', 'Мактаб', 'Хонандагон'),
               [(i, r['school'].name, r['home_students'])
                for i, r in enumerate(data['school_rows'], 1)],
               (5, 44, 14))
    else:
        school = data['school']
        sub = (f"{school.name} · {data['period_label']} · Соли таҳсили "
               f"{data['academic_year']}–{data['academic_year'] + 1}")
        _sheet(wb, 'Меъёри омӯзгорон',
               'Иҷрои меъёр — аз рӯи омӯзгорон', sub,
               ('№', 'Омӯзгор', 'Фан ва синфҳо', 'Соат', 'Меъёр',
                'Сабтшуда', '%', 'Ҳолат'),
               [(i, t['name'], '; '.join(t['subjects']), t['hours'],
                 t['min_grades'], t['grades_done'],
                 f"{t['fulfillment']}%", t['status'])
                for i, t in enumerate(data['teacher_rows'], 1)],
               (5, 24, 36, 8, 9, 11, 8, 11))

        _sheet(wb, 'Ҳолати журналҳо',
               'Ҳолати журналҳо — аз рӯи синф ва фан', sub,
               ('№', 'Омӯзгор', 'Синф', 'Фан', 'Гурӯҳ',
                'Меъёрро иҷро кардаанд', 'Ҳолат'),
               [(i, t['name'], j['class_name'], j['subject'],
                 j['group'] or '—',
                 f"{j['students_met']}/{j['students_total']}",
                 {'complete': 'Пурра', 'partial': 'Қисман',
                  'empty': 'Холӣ'}.get(j['status'], j['status']))
                for i, (t, j) in enumerate(
                    ((t, j) for t in data['teacher_rows']
                     for j in t.get('journal', ())), 1)],
               (5, 22, 9, 24, 16, 11, 9))

        exc = data['excellent']
        _sheet(wb, 'Аълохон', exc['title'],
               f"Чоряки {exc['quarter']}",
               ('№', 'Ному насаб', 'Синф'),
               [(i, s.full_name, s.class_name)
                for i, s in enumerate(exc['students'], 1)],
               (6, 46, 12))

        ung = data['ungraded']
        _sheet(wb, 'Бебаҳо', ung['title'],
               f"Чоряки {ung['quarter']}",
               ('№', 'Хонанда', 'Синф', 'Фан', 'Омӯзгор'),
               [(i, r['student'], r['class_name'], r['subject'],
                 r['teacher'])
                for i, r in enumerate(ung['rows'], 1)],
               (6, 28, 10, 24, 26))

        _sheet(wb, 'Таҳсил дар хона',
               'Хонандагони таҳсил дар хона', sub,
               ('№', 'Хонанда', 'Синф', 'Фан', 'Омӯзгор',
                'Соат дар ҳафта'),
               [(i, r['student'], r['class_name'], r['subject'],
                 r['teacher'], r['hours'])
                for i, r in enumerate(data['home_students'], 1)],
               (5, 26, 9, 24, 24, 10))

        _sheet(wb, 'Ҳаракати хонандагон',
               'Ҳаракати хонандагон', sub,
               ('№', 'Хонанда', 'Синф', 'Навъ', 'Сана'),
               [(i, m.student_name, m.class_name,
                 m.get_movement_type_display(),
                 m.date.strftime('%d.%m.%Y'))
                for i, m in enumerate(data['movements'], 1)],
               (5, 30, 10, 10, 12))

        _sheet(wb, 'Рейтинги синфҳо',
               'Рейтинги синфҳо', sub,
               ('№', 'Синф', 'Холи миёна'),
               [(i, c['class_name'], c['gpa'])
                for i, c in enumerate(data['class_ranking'], 1)],
               (6, 12, 10))

        _sheet(wb, 'Хонандагони беҳтарин',
               'Хонандагони беҳтарин — мактаб', sub,
               ('№', 'Ному насаб', 'Синф', 'Холи миёна',
                'Ҷойи муассисавӣ'),
               [(i, s['full_name'], s['class_name'], s['gpa'],
                 s['school_rank'])
                for i, s in enumerate(data['school_students'], 1)],
               (5, 34, 10, 9, 12))

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
