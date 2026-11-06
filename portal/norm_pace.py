"""Per-student quarterly norm pace — shared monitoring math.

Color semantics and the remaining-opportunity estimate live here exactly
once: ``grade_entry``, ``monthly_journal`` and Назорат all consume the
same status map, so the four indicators can never disagree between pages.

Statuses
    'exempt'   🔵 — an active exemption (дар давлати дигар таҳсил мекунад);
                    grading is currently not required.
    'met'      🟢 — effective >= planned (the to-date norm pace is reached).
    'behind'   🟡 — behind plan but still recoverable under the estimate.
    'critical' 🔴 — cannot reach the quarter norm under the estimate.
    'ungraded' ⚪ — not a single real grade yet ("Баҳогузорӣ нашудааст").
    'unrated'  —  no indicator (un-graded subject, norm == 0).

Effective count: each recorded "Иштирок накардан дар дарс" (attendance
'+'/'-') counts as one second-degree unit for the TEACHER's norm check
only — it never becomes a real grade. ``effective = actual + missed``
replaces ``actual`` in the met/behind/critical comparisons. A student
with zero real grades is always 'ungraded' and can never reach 'met'
via missed-lesson units.

``opportunities`` is an ESTIMATE, never an exact lesson count:
    weekly_hours × remaining Mon–Sat days in the quarter / 6.
Sundays carry no lessons anywhere in the journals (the month grid skips
them); inter-quarter vacations are excluded by capping at the quarter's
own end date. Intra-quarter holidays are NOT stored in the database, so
the estimate is a generous upper bound — it feeds the yellow/red
recoverability check only and is never displayed to users. Red therefore
means "limited under the estimate", not a mathematically certain verdict.
"""
import datetime
import math

from .curriculum_hours import (
    quarter_min_norm, weekly_hours, group_weekly_hours, group_min_norm)

PACE_EXEMPT = 'exempt'
PACE_MET = 'met'
PACE_BEHIND = 'behind'
PACE_CRITICAL = 'critical'
PACE_UNGRADED = 'ungraded'
PACE_UNRATED = 'unrated'

ATTENDANCE_MARKS = ('+', '-')  # both mean "Иштирок накардан дар дарс"

STATUS_TITLES = {
    PACE_MET: 'Меъёр иҷро шудааст',
    PACE_BEHIND: 'Огоҳӣ',
    PACE_CRITICAL: 'Меъёр иҷро нашудааст',
    PACE_EXEMPT: 'Баҳогузорӣ талаб карда намешавад',
    PACE_UNGRADED: 'Баҳогузорӣ нашудааст',
}
_STATUS_NOTES = {
    PACE_MET: 'Нақшаи муқарраршуда то имрӯз иҷро шудааст.',
    PACE_BEHIND: 'Барои иҷрои меъёр диққати иловагӣ зарур аст.',
    PACE_CRITICAL: 'Ба меъёри муқарраршуда то имрӯз нарасидааст.',
    PACE_UNGRADED: 'Хонанда ҳануз ягон баҳои воқеӣ нагирифтааст.',
}


def absence_counts(rows):
    """{student_id: missed-lesson count} from Grade rows — pure Python.

    A "Иштирок накардан дар дарс" record is a Grade row with attendance
    '+' or '-' and no score. Deduplication is per lesson, never per day:
      * lesson-linked rows count once per distinct (date, lesson_id);
      * legacy lesson-less rows (lesson_id NULL) count once per date,
        but only on dates carrying neither a lesson-linked mark nor a
        score — a NULL mark cannot be attributed to a specific lesson,
        so it must never merge two real lessons into one absence nor
        inflate a date that already has explicit marks;
      * a slot that carries a score is presence, never an absence.

    Conservative by design: when a NULL mark sits on a multi-lesson
    day, the stored data cannot tell how many of those lessons were
    actually missed, so it is counted exactly once — never estimated.
    """
    scored = {}     # sid -> {(date, lesson_id)}
    explicit = {}   # sid -> {(date, lesson_id)}
    null_dates = {} # sid -> {date}
    for g in rows:
        sid = g.student_id
        if g.score is not None:
            scored.setdefault(sid, set()).add((g.date, g.lesson_id))
        elif g.attendance in ATTENDANCE_MARKS:
            if g.lesson_id:
                explicit.setdefault(sid, set()).add((g.date, g.lesson_id))
            else:
                null_dates.setdefault(sid, set()).add(g.date)
    counts = {}
    for sid in set(explicit) | set(null_dates):
        slots = explicit.get(sid, set()) - scored.get(sid, set())
        covered = {d for d, _ in slots} | {d for d, _ in scored.get(sid, set())}
        counts[sid] = len(slots) + len(null_dates.get(sid, set()) - covered)
    return counts


def school_days_left(today, q_end):
    """Remaining Mon–Sat days strictly after ``today`` up to ``q_end``.

    Working-day count for the opportunities estimate. A quarter spans at
    most ~90 days, so the plain loop is cheaper than formulaic day math.
    """
    if today >= q_end:
        return 0
    days = 0
    d = today + datetime.timedelta(days=1)
    while d <= q_end:
        if d.weekday() != 6:
            days += 1
        d += datetime.timedelta(days=1)
    return days


def _tip_text(status, norm, planned, actual, needed, reason, missed=0):
    """Tooltip body — the exact mandated Tajik strings per status."""
    if status == PACE_EXEMPT:
        return '\n'.join((
            'Баҳогузорӣ талаб карда намешавад',
            f'Сабаб: {reason}',
            'Меъёр: Ҳисоб карда намешавад.',
            'Баҳогузорӣ: Ҳисоб карда намешавад.',
            'Ин хонанда ба шумораи камбудиҳо дохил карда намешавад.'))
    if status == PACE_UNGRADED:
        lines = [
            STATUS_TITLES[status],
            f'Меъёри семоҳа: {norm} хол',
            'Холҳои ҷамъшуда: 0 хол',
        ]
        if missed:
            lines.append(f'Иштирок накардан дар дарс: {missed}')
        lines.append(_STATUS_NOTES[status])
        return '\n'.join(lines)
    shown_needed = 0 if status == PACE_MET else needed
    lines = [
        STATUS_TITLES[status],
        f'Меъёри семоҳа: {norm} хол',
        f'Меъёри то имрӯз: {planned} хол',
        f'Холҳои ҷамъшуда: {actual} хол',
    ]
    if missed:
        lines.append(f'Иштирок накардан дар дарс: {missed}')
        lines.append(f'Ҳамагӣ барои меъёр: {actual + missed} хол')
    lines.append(f'Холҳои боқимонда: {shown_needed} хол')
    lines.append(_STATUS_NOTES[status])
    return '\n'.join(lines)


def norm_pace_context(class_name, subject, class_subject, students, today,
                      exemption_map, per_student, absences=None, group=None):
    """Pace payload for one journal: per-student records + the lists.

    ``per_student`` is the scored-row count map and ``absences`` the
    missed-lesson count map (see absence_counts) already built by
    _journal_norm_status — this function adds pure arithmetic only,
    no SQL.

    Color rules (approved):
        exempt   — sid in exemption_map
        ungraded — actual == 0 (never met, however many missed units)
        met      — actual + missed >= planned
        behind   — needed (norm - effective) <= opportunities
        critical — everything else
    """
    from .views import _current_quarter_bounds  # local import avoids cycles

    # ``group`` is the regular teacher's TeachingGroup on this CS — a
    # таҳсил дар хона journal paces against that group's own weekly
    # hours instead of the class subject's.
    if group is not None and class_subject is not None:
        norm = group_min_norm(group, class_subject)
        hours = group_weekly_hours(group, class_subject)
    else:
        norm = quarter_min_norm(class_name, subject, class_subject)
        hours = weekly_hours(class_name, subject, class_subject)
    q_start, q_end = _current_quarter_bounds(today)
    total = (q_end - q_start).days + 1
    elapsed = min(max((today - q_start).days + 1, 1), total)
    progress = elapsed / total
    planned = norm * progress
    # Integer students can never sit between planned and ceil(planned),
    # so the to-date requirement is displayed and compared as a whole
    # number (epsilon guards float dust like 4.0000000001).
    planned_int = math.ceil(planned - 1e-9)
    # ESTIMATE — see module docstring; used only for the behind/critical
    # recoverability check, never rendered as an exact lesson count.
    opportunities = float(hours) * school_days_left(today, q_end) / 6.0

    by_id = {}
    lists = {'ungraded': [], 'behind': [], 'met': [], 'exempt': []}
    for s in students:
        ex = exemption_map.get(s.id)
        actual = per_student.get(s.id, 0)
        missed = absences.get(s.id, 0) if absences else 0
        effective = actual + missed
        if norm <= 0 and ex is None:
            # Un-graded subject — no indicator at all.
            by_id[s.id] = {
                'student': s, 'status': PACE_UNRATED, 'dot': False,
                'actual': actual, 'norm': 0, 'planned': 0, 'needed': 0,
                'weekly_hours': hours, 'tip': '', 'status_title': '',
                'note': '', 'reason': '', 'missed': missed,
                'effective': effective,
            }
            continue
        if ex is not None:
            status = PACE_EXEMPT
        elif actual == 0:
            # Hard rule: no real grade -> never met, however many
            # missed-lesson units exist.
            status = PACE_UNGRADED
        elif effective >= planned:
            status = PACE_MET
        elif max(0, norm - effective) <= opportunities:
            status = PACE_BEHIND
        else:
            status = PACE_CRITICAL
        needed = max(0, norm - effective)
        reason = ex.get_reason_display() if ex is not None else ''
        rec = {
            'student': s, 'status': status, 'dot': True,
            'actual': actual, 'norm': norm, 'planned': planned_int,
            'needed': needed, 'weekly_hours': hours,
            'tip': _tip_text(status, norm, planned_int, actual, needed,
                             reason, missed),
            'status_title': STATUS_TITLES[status],
            'note': _STATUS_NOTES.get(status, ''),
            'reason': reason,
            'missed': missed, 'effective': effective,
        }
        by_id[s.id] = rec
        if status == PACE_EXEMPT:
            lists['exempt'].append(rec)
        elif status == PACE_MET:
            lists['met'].append(rec)
        elif status == PACE_UNGRADED:
            lists['ungraded'].append(rec)
        else:
            lists['behind'].append(rec)
    return {
        'norm': norm, 'planned': planned_int, 'weekly_hours': hours,
        'by_id': by_id,
        'ungraded': lists['ungraded'],
        'behind': lists['behind'],
        'met': lists['met'],
        'exempt': lists['exempt'],
    }
