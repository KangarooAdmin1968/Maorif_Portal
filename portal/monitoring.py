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
