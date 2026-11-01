"""Per-student quarterly norm pace — shared monitoring math.

Color semantics and the remaining-opportunity estimate live here exactly
once: ``grade_entry``, ``monthly_journal`` and Назорат all consume the
same status map, so the four indicators can never disagree between pages.

Statuses
    'exempt'   🔵 — an active exemption (дар давлати дигар таҳсил мекунад);
                    grading is currently not required.
    'met'      🟢 — actual >= planned (the to-date norm pace is reached).
    'behind'   🟡 — behind plan but still recoverable under the estimate.
    'critical' 🔴 — cannot reach the quarter norm under the estimate.
    'unrated'  —  no indicator (un-graded subject, norm == 0).

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

from .curriculum_hours import quarter_min_norm, weekly_hours

PACE_EXEMPT = 'exempt'
PACE_MET = 'met'
PACE_BEHIND = 'behind'
PACE_CRITICAL = 'critical'
PACE_UNRATED = 'unrated'

STATUS_TITLES = {
    PACE_MET: 'Меъёр иҷро шудааст',
    PACE_BEHIND: 'Огоҳӣ',
    PACE_CRITICAL: 'Меъёр иҷро нашудааст',
    PACE_EXEMPT: 'Баҳогузорӣ талаб карда намешавад',
}
_STATUS_NOTES = {
    PACE_MET: 'Нақшаи муқарраршуда то имрӯз иҷро шудааст.',
    PACE_BEHIND: 'Барои иҷрои меъёр диққати иловагӣ зарур аст.',
    PACE_CRITICAL: 'Ба меъёри муқарраршуда то имрӯз нарасидааст.',
}


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


def _tip_text(status, norm, planned, actual, needed, reason):
    """Tooltip body — the exact mandated Tajik strings per status."""
    if status == PACE_EXEMPT:
        return '\n'.join((
            'Баҳогузорӣ талаб карда намешавад',
            f'Сабаб: {reason}',
            'Меъёр: Ҳисоб карда намешавад.',
            'Баҳогузорӣ: Ҳисоб карда намешавад.',
            'Ин хонанда ба шумораи камбудиҳо дохил карда намешавад.'))
    shown_needed = 0 if status == PACE_MET else needed
    return '\n'.join((
        STATUS_TITLES[status],
        f'Меъёри семоҳа: {norm} хол',
        f'Меъёри то имрӯз: {planned} хол',
        f'Холҳои ҷамъшуда: {actual} хол',
        f'Холҳои боқимонда: {shown_needed} хол',
        _STATUS_NOTES[status]))


def norm_pace_context(class_name, subject, class_subject, students, today,
                      exemption_map, per_student):
    """Pace payload for one journal: per-student records + the four lists.

    ``per_student`` is the scored-row count map already built by
    _journal_norm_status (one grouped query) — this function adds pure
    arithmetic only, no SQL.

    Color rules (approved):
        exempt   — sid in exemption_map
        met      — actual >= planned
        behind   — actual < planned AND needed <= opportunities
        critical — everything else
    """
    from .views import _current_quarter_bounds  # local import avoids cycles

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
        if norm <= 0 and ex is None:
            # Un-graded subject — no indicator at all.
            by_id[s.id] = {
                'student': s, 'status': PACE_UNRATED, 'dot': False,
                'actual': actual, 'norm': 0, 'planned': 0, 'needed': 0,
                'weekly_hours': hours, 'tip': '', 'status_title': '',
                'note': '', 'reason': '',
            }
            continue
        if ex is not None:
            status = PACE_EXEMPT
        elif actual >= planned:
            status = PACE_MET
        elif max(0, norm - actual) <= opportunities:
            status = PACE_BEHIND
        else:
            status = PACE_CRITICAL
        needed = max(0, norm - actual)
        reason = ex.get_reason_display() if ex is not None else ''
        rec = {
            'student': s, 'status': status, 'dot': True,
            'actual': actual, 'norm': norm, 'planned': planned_int,
            'needed': needed, 'weekly_hours': hours,
            'tip': _tip_text(status, norm, planned_int, actual, needed,
                             reason),
            'status_title': STATUS_TITLES[status],
            'note': _STATUS_NOTES.get(status, ''),
            'reason': reason,
        }
        by_id[s.id] = rec
        if status == PACE_EXEMPT:
            lists['exempt'].append(rec)
        elif status == PACE_MET:
            lists['met'].append(rec)
        else:
            lists['behind'].append(rec)
            if actual == 0:
                lists['ungraded'].append(rec)
    return {
        'norm': norm, 'planned': planned_int, 'weekly_hours': hours,
        'by_id': by_id,
        'ungraded': lists['ungraded'],
        'behind': lists['behind'],
        'met': lists['met'],
        'exempt': lists['exempt'],
    }
