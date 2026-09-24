"""Centralized monitoring-permission layer for the activity dashboard.

Scope model (composed from the existing helpers in portal.views):

    'district'    — superuser, Maorif staff (is_staff), ROLE_DIRECTOR:
                    all schools, full detail.
    'school'      — ROLE_PRINCIPAL: own school only, full detail.
    'school_agg'  — zavuch (production accounts carry role='teacher' with
                    a 'zavuch_' username, detected via _is_zavuch): own
                    school full detail, other schools aggregate comparison
                    only — never individual users or last_login timestamps.
    None          — regular teachers, profile-less users, anonymous:
                    no monitoring access.

These helpers decide access only; they never return user data. Existing
access helpers (has_school_access, _can_manage_school, ...) are unchanged.
"""
from django.conf import settings

SCOPE_DISTRICT = 'district'
SCOPE_SCHOOL = 'school'
SCOPE_SCHOOL_AGG = 'school_agg'


def _monitoring_scope(user):
    """Return the monitoring scope for `user`, or None when denied."""
    from .views import _is_zavuch, get_user_role  # local import avoids cycles

    if not user or not user.is_authenticated:
        return None
    if user.is_superuser or user.is_staff:
        return SCOPE_DISTRICT
    role = get_user_role(user)
    if _is_zavuch(user, role):
        return SCOPE_SCHOOL_AGG
    if role == settings.ROLE_DIRECTOR:
        return SCOPE_DISTRICT
    if role == settings.ROLE_PRINCIPAL:
        return SCOPE_SCHOOL
    return None


def can_access_monitoring(user):
    """True if the user may open any monitoring surface."""
    return _monitoring_scope(user) is not None


def can_view_all_schools_monitoring(user):
    """True if the user may see detailed monitoring for every school."""
    return _monitoring_scope(user) == SCOPE_DISTRICT


def can_view_school_monitoring(user, school):
    """True if the user may see ANY monitoring row for `school`.

    District scope sees all schools; school_agg (zavuch) may see aggregate
    rows for any school (detail is gated by can_view_full_school_detail);
    school scope (principal) is limited to the user's own school.
    """
    scope = _monitoring_scope(user)
    if scope == SCOPE_DISTRICT:
        return True
    if school is None:
        return False
    if scope == SCOPE_SCHOOL_AGG:
        return True
    if scope == SCOPE_SCHOOL:
        from .views import get_user_school
        return get_user_school(user) == school
    return False


def can_view_full_school_detail(user, school):
    """True if the user may see individual users/timestamps for `school`."""
    scope = _monitoring_scope(user)
    if scope == SCOPE_DISTRICT:
        return True
    if school is None:
        return False
    if scope in (SCOPE_SCHOOL, SCOPE_SCHOOL_AGG):
        from .views import get_user_school
        return get_user_school(user) == school
    return False


def can_view_cross_school_aggregate(user):
    """True if the user may see the multi-school aggregate comparison."""
    return _monitoring_scope(user) in (SCOPE_DISTRICT, SCOPE_SCHOOL_AGG)


def scope_monitoring_stats(user, stats):
    """Apply the viewer's monitoring scope to _get_monitoring_stats() rows.

    District scope: rows pass through unchanged. School scope (principal):
    only the user's own school survives. school_agg (zavuch): all rows are
    kept, but schools outside their own are reduced to aggregate columns —
    zavuch_username/zavuch_user/logged_in are masked so no individual user
    data or last_login timestamp ever reaches the response or workbook.
    """
    if can_view_all_schools_monitoring(user):
        return stats
    scoped = []
    for row in stats:
        school = row['school']
        if not can_view_school_monitoring(user, school):
            continue
        if not can_view_full_school_detail(user, school):
            row = {
                **row,
                'zavuch_username': '—',
                'zavuch_user': None,
                'logged_in': False,
                'masked': True,
            }
        scoped.append(row)
    return scoped


def get_activity_summary(user):
    """Aggregate DailyActivity statistics for the monitoring dashboard.

    Read-only: exactly two aggregate queries regardless of school count —
    (1) today + current-month totals via filtered aggregates, (2) one
    grouped query for the last 7 local dates. Anonymous aggregate rows
    (user IS NULL) are excluded, matching the Phase 2 design.

    Scope: district and school_agg (zavuch) see merged district-wide
    aggregates (no individual or per-school data is produced); school
    scope (principal) is limited to their own school. Returns None when
    the user has no monitoring access. Only plain numbers and dates are
    returned — never usernames, users, or seen timestamps.
    """
    import datetime

    from django.db.models import Count, Q, Sum, Value
    from django.db.models.functions import Coalesce
    from django.utils import timezone

    from .models import DailyActivity

    scope = _monitoring_scope(user)
    if scope is None:
        return None

    today = timezone.localdate()
    month_start = today.replace(day=1)
    week_start = today - datetime.timedelta(days=6)

    school = None
    if scope == SCOPE_SCHOOL:
        from .views import get_user_school
        school = get_user_school(user)
        if school is None:
            empty = {'active_users': 0, 'logins': 0}
            return {
                'today': dict(empty),
                'month': dict(empty),
                'daily': [
                    {'date': today - datetime.timedelta(days=i),
                     'active_users': 0, 'logins': 0}
                    for i in range(6, -1, -1)
                ],
            }

    base = DailyActivity.objects.filter(user__isnull=False)
    if school is not None:
        base = base.filter(school=school)

    agg = base.filter(
        date__gte=month_start, date__lte=today
    ).aggregate(
        today_users=Count('user', distinct=True, filter=Q(date=today)),
        today_logins=Coalesce(
            Sum('login_count', filter=Q(date=today)), Value(0)),
        month_users=Count('user', distinct=True),
        month_logins=Coalesce(Sum('login_count'), Value(0)),
    )

    daily_map = {
        row['date']: row
        for row in base.filter(
            date__gte=week_start, date__lte=today
        ).values('date').annotate(
            active_users=Count('user', distinct=True),
            logins=Sum('login_count'),
        )
    }
    daily = []
    for i in range(6, -1, -1):
        day = today - datetime.timedelta(days=i)
        row = daily_map.get(day)
        daily.append({
            'date': day,
            'active_users': row['active_users'] if row else 0,
            'logins': row['logins'] if row else 0,
        })

    return {
        'today': {
            'active_users': agg['today_users'],
            'logins': agg['today_logins'],
        },
        'month': {
            'active_users': agg['month_users'],
            'logins': agg['month_logins'],
        },
        'daily': daily,
    }
