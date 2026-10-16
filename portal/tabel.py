"""Teacher workload tabel (Ҳуҷҷатнигорӣ → Табел) — calculation and export.

Workload attribution mirrors the existing Ҳуҷҷатнигорӣ documents page:
``ClassSubject.allocated_teacher`` wins, ``ClassSubject.teacher`` (User →
TeacherProfile) is the fallback, and an active TeachingGroup credits its
own teacher with the subject's full weekly load ("ФАН — гурӯҳ"). Weekly
hours always resolve through ``curriculum_hours.weekly_hours`` — the
national annex tables plus explicit ``hours_per_week`` overrides — never
the flat ``settings.TJC_SUBJECT_HOURS`` map.

СОАТИ ТАРБИЯВӢ is counted like any other allocated subject (the class
head teacher conducts it); the separate class-head payroll supplement is
a manual tabel entry, not a computed value.
"""
import io
import re
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.page import PageMargins

from .curriculum_hours import weekly_hours
from .models import ClassSubject, TeacherProfile
from .utils import class_numeric_part

# Grade bands used by the official tabel (columns E/F/G, L/M/N, P/Q/R, T/U/V).
BAND_1_4 = '1-4'
BAND_5_9 = '5-9'
BAND_10_11 = '10-11'
BAND_KEYS = (BAND_1_4, BAND_5_9, BAND_10_11)

MONTH_NAMES_TJ = {
    1: 'Январ', 2: 'Феврал', 3: 'Март', 4: 'Апрел', 5: 'Май', 6: 'Июн',
    7: 'Июл', 8: 'Август', 9: 'Сентябр', 10: 'Октябр', 11: 'Ноябр', 12: 'Декабр',
}

# Month-specific TeacherTabelMonth fields grouped by tabel column purpose.
MONTHLY_BAND_FIELDS = {
    'missed': ('missed_1_4', 'missed_5_9', 'missed_10_11'),
    'substitution': ('substitution_1_4', 'substitution_5_9', 'substitution_10_11'),
    'notebook': ('notebook_1_4', 'notebook_5_9', 'notebook_10_11'),
}
MONTHLY_ALLOWANCE_FIELDS = (
    'allowance_class_head', 'allowance_admin',
    'allowance_methodical', 'allowance_language',
)

MAX_MONTHLY_HOURS = Decimal('300')
MAX_MONEY_VALUE = Decimal('99999999')


def band_key_for_class(class_name):
    """Map a class name to its tabel grade band ('7-А' -> '5-9')."""
    level = class_numeric_part(class_name)
    if 1 <= level <= 4:
        return BAND_1_4
    if 5 <= level <= 9:
        return BAND_5_9
    if 10 <= level <= 11:
        return BAND_10_11
    return None


def parse_month_param(raw):
    """Parse a 'YYYY-MM' month selector value. Returns (year, month) or None."""
    if not raw:
        return None
    m = re.match(r'^(\d{4})-(\d{1,2})$', raw.strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return year, month


def parse_decimal(raw, *, max_value):
    """Parse a manual tabel input. '' -> None; invalid/negative -> InvalidOperation."""
    raw = (raw or '').strip().replace(',', '.')
    if raw == '':
        return None
    value = Decimal(raw)
    if not value.is_finite() or value < 0 or value > max_value:
        raise InvalidOperation
    return value


def _class_sort_key(class_name):
    return (class_numeric_part(class_name) or 0, class_name)


def _unassigned_entry(cs, subject_label, note=None):
    entry = {
        'class_name': cs.class_name,
        'subject': subject_label,
        'hours': weekly_hours(cs.class_name, cs.subject, cs),
    }
    if note:
        entry['note'] = note
    return entry


def _user_to_profile_map(profiles):
    """Index profiles by their linked user id.

    A profile without a user account (user_id NULL) can never be the target
    of a ClassSubject.teacher User-FK — excluding it prevents a NULL
    teacher_id from being silently attributed to a user-less profile.
    """
    return {tp.user_id: tp for tp in profiles if tp.user_id is not None}


def teacher_workload(school):
    """Per-teacher weekly workload for one school.

    Returns (rows, unassigned, unbanded):
      rows — [{'teacher', 'items': [{'subject', 'classes'}],
               'hours': {band: total}, 'total_hours', 'subjects'}]
             sorted by teacher name; СОАТИ ТАРБИЯВӢ hours are included in
             the band totals like any other allocated subject.
      unassigned — [{'class_name', 'subject', 'hours', 'note'?}] for active
             loads with no usable teacher attribution: no teacher at all,
             or an allocation pointing at another school's teacher (the
             note flags it without exposing the other school's data).
      unbanded — [{'teacher', 'class_name', 'subject', 'hours'}] for
             allocated loads whose class name maps to no 1–4/5–9/10–11
             band; the hours are reported, not silently dropped.
    """
    class_subjects = ClassSubject.objects.filter(
        school=school, is_active=True
    ).select_related('allocated_teacher', 'teacher').prefetch_related(
        'groups__teacher'
    ).order_by('class_name', 'subject')
    user_to_profile = _user_to_profile_map(
        TeacherProfile.objects.filter(school=school, is_archived=False))

    # teacher -> {display_subject: [(base_subject, class_name, cs)]}
    grouped = {}
    unassigned = []
    unbanded = []
    for cs in class_subjects:
        cs_groups = [g for g in cs.groups.all() if g.is_active]
        if cs_groups:
            # Grouped CS: each group teacher is credited with the subject's
            # full weekly load for their group.
            for g in cs_groups:
                label = f"{cs.subject} — {g.label}"
                if not g.teacher_id:
                    unassigned.append(_unassigned_entry(cs, label))
                elif g.teacher.school_id != school.id:
                    unassigned.append(_unassigned_entry(
                        cs, label, note='таъиншуда ба омӯзгори мактаби дигар'))
                else:
                    subj_map = grouped.setdefault(g.teacher, {})
                    subj_map.setdefault(label, []).append(
                        (cs.subject, cs.class_name, cs))
        else:
            profile = cs.allocated_teacher
            if profile and profile.school_id != school.id:
                unassigned.append(_unassigned_entry(
                    cs, cs.subject,
                    note='таъиншуда ба омӯзгори мактаби дигар'))
            else:
                profile = profile or user_to_profile.get(cs.teacher_id)
                if profile:
                    subj_map = grouped.setdefault(profile, {})
                    subj_map.setdefault(cs.subject, []).append(
                        (cs.subject, cs.class_name, cs))
                else:
                    unassigned.append(_unassigned_entry(cs, cs.subject))

    rows = []
    for profile, subj_map in grouped.items():
        items = []
        hours = {band: 0 for band in BAND_KEYS}
        subjects = []
        for subject, entries in sorted(subj_map.items(), key=lambda kv: kv[0]):
            classes_sorted = sorted(
                {cn for _, cn, _ in entries}, key=_class_sort_key)
            items.append({'subject': subject, 'classes': classes_sorted})
            subjects.append(subject)
            for base_subject, class_name, cs in entries:
                week_hours = weekly_hours(class_name, base_subject, cs)
                band = band_key_for_class(class_name)
                if band is None:
                    unbanded.append({
                        'teacher': profile.full_name,
                        'class_name': class_name,
                        'subject': subject,
                        'hours': week_hours,
                    })
                    continue
                hours[band] += week_hours
        rows.append({
            'teacher': profile,
            'items': items,
            'hours': hours,
            'total_hours': sum(hours.values()),
            'subjects': ', '.join(subjects),
        })
    rows.sort(key=lambda r: r['teacher'].full_name)
    return rows, unassigned, unbanded


def workload_snapshot(workload_row):
    """JSON-serializable copy of one teacher_workload() row."""
    return {
        'items': [
            {'subject': item['subject'], 'classes': item['classes']}
            for item in workload_row['items']
        ],
        'hours': dict(workload_row['hours']),
        'total_hours': workload_row['total_hours'],
        'subjects': workload_row['subjects'],
    }


def display_name(monthly, teacher):
    """Recorded name wins; live profile name only for rows saved before
    teacher_name existed (and for unsaved months)."""
    if monthly and monthly.teacher_name:
        return monthly.teacher_name
    return teacher.full_name


def display_rate(monthly, teacher):
    """Line-1 rate for the month.

    A row with a rates_snapshot always uses it — a null recorded rate
    renders blank, never the later live rate. Rows without any snapshot
    (created before the field existed) fall back to the live monthly_rate
    so legacy months keep their previous behavior without inventing data.
    """
    if monthly and monthly.rates_snapshot is not None:
        for line in monthly.rates_snapshot:
            if line.get('line_no') == 1:
                return line.get('rate')
        return None
    return teacher.monthly_rate


def row_display(workload_row, monthly, rate_hint=None):
    """Display row for page/export: frozen snapshot when the month was
    already recorded, live workload otherwise.

    ``rate_hint`` is an optional verified-tariff suggestion
    ({'value', 'kind', 'period'} from tariff_rates.suggested_rate_map).
    It never changes ``display_rate``: a 'verified' hint pre-fills the
    editable input when no rate is stored, while a 'derived' hint only
    shows as text next to the input — derived values must be confirmed
    by the deputy, never auto-entered. Frozen months keep their recorded
    rate untouched and get no hint.
    """
    snapshot = monthly.workload_snapshot if monthly else None
    if snapshot:
        row = {
            'teacher': workload_row['teacher'] if workload_row else monthly.teacher,
            'items': snapshot.get('items', []),
            'hours': {band: snapshot.get('hours', {}).get(band, 0) for band in BAND_KEYS},
            'total_hours': snapshot.get('total_hours', 0),
            'subjects': snapshot.get('subjects', ''),
            'frozen': True,
            'monthly': monthly,
        }
    else:
        base = workload_row or {
            'teacher': monthly.teacher,
            'items': [], 'hours': {band: 0 for band in BAND_KEYS},
            'total_hours': 0, 'subjects': '',
        }
        row = {
            'teacher': base['teacher'],
            'items': base['items'],
            'hours': base['hours'],
            'total_hours': base['total_hours'],
            'subjects': base['subjects'],
            'frozen': False,
            'monthly': monthly,
        }
    teacher = row['teacher']
    row['display_name'] = display_name(monthly, teacher)
    row['display_rate'] = display_rate(monthly, teacher)
    row['rate_input'] = row['display_rate']
    row['rate_hint'] = None
    # A frozen row whose snapshot rate was never recorded (NULL) is the
    # one case where a hint still helps: it can be completed once.
    # A frozen row WITH a recorded rate gets no hint — recorded history
    # is never questioned on the page.
    if rate_hint and not (row['frozen'] and row['display_rate'] is not None):
        if row['display_rate'] is None:
            row['rate_hint'] = rate_hint
            if rate_hint['kind'] == 'verified':
                row['rate_input'] = rate_hint['value']
        else:
            # A stored value differing from the period-correct
            # suggestion is flagged for review — e.g. an old-scale rate
            # left on a month that belongs to a later pay scale.
            try:
                stored = Decimal(str(row['display_rate']))
            except (InvalidOperation, ValueError):
                stored = None
            if stored != rate_hint['value']:
                row['rate_hint'] = rate_hint
    row['pay'] = payroll_amounts(row)
    return row


def _d(value):
    """Coerce to Decimal; a blank input counts as zero, exactly like
    Excel arithmetic on empty cells."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def payroll_amounts(row):
    """Computed payroll for one display row — the Python twin of
    ``_PAYROLL_FORMULAS`` so the website shows the same results the
    exported workbook calculates on open.

    Mirrors the IF(D…="","") guard: a missing rate blanks every
    computed column (None), it never yields a misleading zero salary.
    Returns {'K', 'O', 'S', 'W', 'AB'} — base pay, missed-hours
    deduction, substitution pay, notebook-check pay, month total.
    """
    rate = row.get('display_rate')
    if rate is None:
        return {'K': None, 'O': None, 'S': None, 'W': None, 'AB': None}
    rate = _d(rate)
    hours = row.get('hours') or {}
    monthly = row.get('monthly')

    def m(field):
        return _d(getattr(monthly, field, None) if monthly else None)

    k = (rate / 20 * _d(hours.get(BAND_1_4))
         + rate / 18 * _d(hours.get(BAND_5_9))
         + rate / 18 * _d(hours.get(BAND_10_11)))
    o = (rate / 80 * m('missed_1_4')
         + rate / 72 * m('missed_5_9')
         + rate / 72 * m('missed_10_11'))
    s = (rate / 80 * m('substitution_1_4')
         + rate / 72 * m('substitution_5_9')
         + rate / 72 * m('substitution_10_11'))
    pct = Decimal('0.2')
    w = (rate / 18 * pct * m('notebook_5_9')
         + rate / 18 * pct * m('notebook_10_11')
         + rate / 20 * pct * m('notebook_1_4'))
    ab = (k - o + s + w
          + sum((m(f) for f in MONTHLY_ALLOWANCE_FIELDS), Decimal(0)))
    return {'K': k, 'O': o, 'S': s, 'W': w, 'AB': ab}


def merge_workload_and_monthly(workload_rows, monthly_map, rate_hints=None):
    """Combine live workload rows with saved TeacherTabelMonth rows.

    Teachers with a saved month but no current allocation still appear.
    Returns display rows sorted by teacher name. ``rate_hints`` is the
    optional {teacher_id: hint} map from suggested_rate_map.
    """
    rate_hints = rate_hints or {}
    rows_by_teacher = {r['teacher'].id: r for r in workload_rows}
    display = [
        row_display(
            rows_by_teacher.get(tid), monthly_map.get(tid),
            rate_hint=rate_hints.get(tid))
        for tid in set(rows_by_teacher) | set(monthly_map)
    ]
    display.sort(key=lambda r: r['teacher'].full_name)
    return display


# ---------------------------------------------------------------------------
# Excel export — reproduces the official табел workbook (columns A–AB).
# ---------------------------------------------------------------------------

_HEADER_FONT = Font(name='TAJIKAN', size=10)
_THIN = Side(style='thin', color='000000')
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
_CENTER_ROT = Alignment(horizontal='center', vertical='center',
                        wrap_text=True, text_rotation=90)
_LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)

# (range, text, rotated) — identical to the reference template.
_HEADER_MERGES = [
    ('A2:A4', None, False),
    ('B2:B4', 'Ному насаб      (рӯз, моҳ, соли таваллуд)', False),
    ('C2:C4', 'Фаннӣ таълимие, ки аз он дарс медиҳад', True),
    ('D2:D4', 'Музди меҳнати моҳона (1 ставка)', True),
    ('E2:G3', 'Моҳона аз рӯи даража', False),
    ('H2:J3', 'Миқдори соатҳои ҳафта аз рӯи фан', False),
    ('K2:K4', 'ҳамагӣ', True),
    ('L2:N3', 'Миқдори соатҳои нагузашта', False),
    ('O2:O4', 'ҳамагӣ', True),
    ('P2:R3', 'Ивазкунии дарсҳо', False),
    ('S2:S4', 'ҳамагӣ', True),
    ('T2:V3', 'Тафтиши дафтар', False),
    ('W2:W4', 'ҳамагӣ', True),
    ('X2:Y3', 'Иловапулиҳо', True),
    ('Z2:Z4', 'Иловапули барои роҳбарони иттиҳоди методи 15%', True),
    ('AA2:AA4', 'Иловапули барои омӯзгорони забони русӣ ва англиси 10%', True),
    ('AB2:AB4', 'ҳамагӣ музди меҳнат дар як моҳ', True),
]

_SUB_HEADERS = {
    'E4': '1-4.', 'F4': '5-9.', 'G4': '10-11.',
    'H4': '1-4.', 'I4': '5-9.', 'J4': '10-11.',
    'L4': '1-4.', 'M4': '5-9.', 'N4': '10-11.',
    'P4': '1-4.', 'Q4': '5-9.', 'R4': '10-11.',
    'T4': '1-4.', 'U4': '5-9.', 'V4': '10-11.',
    'X4': 'Роҳбари синф (меъёри 1 ставка *15%) ',
    'Y4': 'Сарбории маъмурият',
}

_COLUMN_WIDTHS = {
    'A': 2.9, 'B': 17.1, 'C': 8.8, 'D': 9.0, 'E': 4.0, 'F': 4.0, 'G': 6.0,
    'H': 7.5, 'I': 7.5, 'J': 7.5, 'K': 10.3, 'L': 4.5, 'M': 4.5, 'N': 6.0,
    'O': 5.9, 'P': 4.7, 'Q': 4.7, 'R': 6.5, 'S': 5.7, 'T': 4.8, 'U': 5.5,
    'V': 6.0, 'W': 7.5, 'X': 6.9, 'Y': 8.6, 'Z': 7.0, 'AA': 6.9, 'AB': 7.5,
}

ROWS_PER_TEACHER = 3  # each teacher block in the template spans 3 rows
FIRST_DATA_ROW = 6

# Payroll formulas verified against the official workbook. Every cell is
# guarded by IF(D…="","") — a missing rate renders blank, not a
# misleading zero salary. Blank inputs evaluate as zero inside Excel
# arithmetic, so unset monthly fields never error.
_PAYROLL_FORMULAS = {
    'H': '=IF(D{r}="","",D{r}/20*E{r})',
    'I': '=IF(D{r}="","",D{r}/18*F{r})',
    'J': '=IF(D{r}="","",D{r}/18*G{r})',
    'K': '=IF(D{r}="","",SUM(H{r}:J{r}))',
    'O': '=IF(D{r}="","",(D{r}/80*L{r})+(D{r}/72*M{r})+(D{r}/72*N{r}))',
    'S': '=IF(D{r}="","",(D{r}/80*P{r})+(D{r}/72*Q{r})+(D{r}/72*R{r}))',
    'W': '=IF(D{r}="","",(D{r}/18*20%*U{r})+(D{r}/18*20%*V{r})+(D{r}/20*20%*T{r}))',
    'AB': '=IF(D{r}="","",K{r}-O{r}+S{r}+W{r}+X{r}+Y{r}+Z{r}+AA{r})',
}


def _num_or_blank(value):
    """Hours/amounts render as numbers; unset values stay blank (never zero)."""
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else value


def build_tabel_workbook(school, year, month, display_rows):
    """Build the official-style табел workbook for one school+month.

    Populates verified data — № (A), name (B), subjects (C), the
    deputy-entered monthly rate (D), weekly hours per band (E–G), and the
    manually captured monthly hours/amounts (L–N, P–R, T–V, X–AA) — and
    writes the payroll formulas verified against the official workbook on
    all three rows of each teacher block: band pay H–J, totals K,
    missed-hours deduction O, substitution S, notebook-check W, and the
    month total AB = K−O+S+W+X+Y+Z+AA. Column AC is never touched.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = 'Табел'

    month_name = MONTH_NAMES_TJ.get(month, str(month))
    ws['A1'] = f"{school.name} табели мохи {month_name}   соли {year}"
    ws['A1'].font = _HEADER_FONT
    ws['A1'].alignment = Alignment(horizontal='center')
    ws.merge_cells('A1:AB1')

    for cell_range, text, rotated in _HEADER_MERGES:
        ws.merge_cells(cell_range)
        top_left = ws[cell_range.split(':')[0]]
        top_left.value = text
        top_left.font = _HEADER_FONT
        top_left.alignment = _CENTER_ROT if rotated else _CENTER

    for coord, text in _SUB_HEADERS.items():
        cell = ws[coord]
        cell.value = text
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER

    # Row 5: template's column index row (0-25 across A-AB).
    for col_idx in range(1, 29):
        cell = ws.cell(row=5, column=col_idx, value=col_idx - 1)
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER

    # Borders for header block rows 2-5.
    for row in ws.iter_rows(min_row=2, max_row=5, min_col=1, max_col=28):
        for cell in row:
            cell.border = _BORDER

    for col_letter, width in _COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width
    ws.row_dimensions[4].height = 64

    # Print setup identical to the reference workbook (A4 portrait, ~10mm).
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_margins = PageMargins(
        left=0.3937, right=0.3937, top=0.3937, bottom=0.3937,
        header=0.31496, footer=0.31496)

    for index, row in enumerate(display_rows, start=1):
        r = FIRST_DATA_ROW + (index - 1) * ROWS_PER_TEACHER
        r_end = r + ROWS_PER_TEACHER - 1
        for col_letter in ('A', 'B', 'C'):
            ws.merge_cells(f'{col_letter}{r}:{col_letter}{r_end}')

        cell = ws.cell(row=r, column=1, value=index)
        cell.alignment = _CENTER
        cell = ws.cell(row=r, column=2, value=row['display_name'])
        cell.alignment = _LEFT
        cell = ws.cell(row=r, column=3, value=row['subjects'])
        cell.alignment = _LEFT

        rate = row.get('display_rate')
        if rate is not None:
            ws.cell(row=r, column=4, value=float(rate))

        hours = row['hours']
        for col_idx, band in ((5, BAND_1_4), (6, BAND_5_9), (7, BAND_10_11)):
            value = _num_or_blank(hours.get(band))
            if value:
                ws.cell(row=r, column=col_idx, value=value)

        monthly = row.get('monthly')
        if monthly:
            band_values = (
                (12, 13, 14, 'missed'),        # L,M,N
                (16, 17, 18, 'substitution'),  # P,Q,R
                (20, 21, 22, 'notebook'),      # T,U,V
            )
            for c1, c2, c3, prefix in band_values:
                fields = MONTHLY_BAND_FIELDS[prefix]
                for col_idx, field in zip((c1, c2, c3), fields):
                    value = _num_or_blank(getattr(monthly, field))
                    if value is not None:
                        ws.cell(row=r, column=col_idx, value=value)
            for col_idx, field in zip((24, 25, 26, 27), MONTHLY_ALLOWANCE_FIELDS):
                value = _num_or_blank(getattr(monthly, field))
                if value is not None:
                    ws.cell(row=r, column=col_idx, value=value)

        for block_row in range(r, r_end + 1):
            for col_letter, template in _PAYROLL_FORMULAS.items():
                ws[f'{col_letter}{block_row}'] = template.format(r=block_row)

        for block_row in ws.iter_rows(min_row=r, max_row=r_end, min_col=1, max_col=28):
            for cell in block_row:
                cell.border = _BORDER
                if cell.font.name != 'TAJIKAN':
                    cell.font = _HEADER_FONT
                # The template formats every data cell H..AB as '0.00'
                # (A–G stay General); blanks keep the format too.
                if cell.column >= 8:
                    cell.number_format = '0.00'

    footer_row = FIRST_DATA_ROW + len(display_rows) * ROWS_PER_TEACHER
    ws.cell(row=footer_row, column=2,
            value='Муовини директор оид ба таълим').font = _HEADER_FONT
    director = (school.director or '').strip()
    director_text = 'Директори муассиса'
    if director and director != 'Номи директор':
        director_text = f"{director_text}   {director}"
    ws.cell(row=footer_row, column=11, value=director_text).font = _HEADER_FONT

    # openpyxl stores formulas without calculated results — force
    # Excel/LibreOffice to recalculate when the file is opened.
    wb.calculation.fullCalcOnLoad = True

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
