"""Academic quarter calendar — single source of truth.

Exact official windows per September-start academic year, from the
published ministry order. Academic years absent from QUARTER_CALENDAR
fall back to month buckets (Q1 Sep-Nov ...) so legacy data keeps a
stable attribution; update the table when a new order is published —
or let a future settings tab store overrides here.

Gap days between windows are official vacations and belong to no
quarter: quarter_for_date() returns None for them, while
get_date_quarter() attributes them to the quarter that just ended —
a grade entered during a break is a late write-up of that quarter,
which also matches the old month-bucket answer for every gap here.
"""
import calendar as _cal
import datetime

# key: September-start year of the academic year (2026 -> 2026-09..2027-08)
QUARTER_CALENDAR = {
    # Вазорат тартиби — 2026–2027 ўқув йили:
    # 1-ч 01.09–31.10 | 2-ч 09.11–31.12 (8.11 якшанба)
    # 3-ч 08.01–20.03 | 4-ч 30.03–31.05
    2026: {
        1: (datetime.date(2026, 9, 1), datetime.date(2026, 10, 31)),
        2: (datetime.date(2026, 11, 9), datetime.date(2026, 12, 31)),
        3: (datetime.date(2027, 1, 8), datetime.date(2027, 3, 20)),
        4: (datetime.date(2027, 3, 30), datetime.date(2027, 5, 31)),
    },
}


def _fallback_windows(year):
    """Month-bucket windows for academic years absent from the table."""
    feb_last = _cal.monthrange(year + 1, 2)[1]
    return {
        1: (datetime.date(year, 9, 1), datetime.date(year, 11, 30)),
        2: (datetime.date(year, 12, 1), datetime.date(year + 1, 2, feb_last)),
        3: (datetime.date(year + 1, 3, 1), datetime.date(year + 1, 5, 31)),
        4: (datetime.date(year + 1, 6, 1), datetime.date(year + 1, 8, 31)),
    }


def academic_year_for_date(date):
    """September-start year of the academic year containing `date`."""
    return date.year if date.month >= 9 else date.year - 1


def quarter_windows(year):
    """Inclusive (start, end) dates per quarter of one academic year."""
    return QUARTER_CALENDAR.get(year, _fallback_windows(year))


def quarter_for_date(date):
    """Quarter 1-4 strictly containing `date`, or None in official gaps."""
    windows = quarter_windows(academic_year_for_date(date))
    for q, (start, end) in windows.items():
        if start <= date <= end:
            return q
    return None


def get_date_quarter(date):
    """Quarter attribution for grade/work records, 1-4 (or None).

    Dates inside a window take that quarter; dates inside a vacation
    gap take the quarter that just ended (late write-ups belong to it).
    """
    prev = None
    windows = quarter_windows(academic_year_for_date(date))
    for num, (start, end) in sorted(windows.items()):
        if start <= date <= end:
            return num
        if date <= end:
            break
        prev = num
    return prev
