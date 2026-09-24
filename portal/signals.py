import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.db import IntegrityError
from django.db.models import F
from django.dispatch import receiver
from django.utils import timezone

from .models import DailyActivity

logger = logging.getLogger(__name__)


def _activity_category(user):
    """Map a user to a DailyActivity category.

    Order matters: production zavuch accounts carry role='teacher' with a
    'zavuch_' username, so the zavuch check must run before the role check.
    get_user_role() defaults to ROLE_TEACHER for anonymous/profile-less
    users, so is_authenticated must be checked first.
    """
    from .views import _is_zavuch, get_user_role  # local import avoids cycles

    if not user or not user.is_authenticated:
        return 'parent_student'
    if user.is_superuser or user.is_staff:
        return 'staff'
    role = get_user_role(user)
    if _is_zavuch(user, role):
        return 'zavuch'
    if role == settings.ROLE_DIRECTOR:
        return 'director'
    if role == settings.ROLE_PRINCIPAL:
        return 'principal'
    return 'teacher'


def record_daily_activity(user, count_login=False):
    """Single source of truth for writing DailyActivity rows.

    Authenticated users get one row per (user, local date); anonymous
    visitors share the single (user=None, date) aggregate row enforced by
    the dailyactivity_anon_date partial unique constraint. count_login=True
    marks a real login event; False marks a once-per-day presence update
    (future middleware). Never overwrites first_seen.
    """
    from .views import get_user_school  # local import avoids cycles

    now = timezone.now()
    today = timezone.localdate()

    if user is not None and user.is_authenticated:
        username = user.username or ''
        category = _activity_category(user)
        school = get_user_school(user)
    else:
        user = None
        username = ''
        category = 'parent_student'
        school = None

    defaults = {
        'username': username,
        'category': category,
        'school': school,
        'login_count': 1 if count_login else 0,
        'first_seen': now,
        'last_seen': now,
    }
    try:
        row, created = DailyActivity.objects.get_or_create(
            user=user, date=today, defaults=defaults
        )
    except IntegrityError:
        # SQLite race: another worker inserted the first row concurrently.
        row = DailyActivity.objects.filter(user=user, date=today).first()
        created = False
        if row is None:
            return
    if created:
        return

    updates = {'last_seen': now}
    if username and row.username != username:
        updates['username'] = username
    if count_login:
        updates['login_count'] = F('login_count') + 1
    DailyActivity.objects.filter(pk=row.pk).update(**updates)


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    """Record the login on DailyActivity. Never breaks the login itself."""
    try:
        record_daily_activity(user, count_login=True)
    except Exception:
        logger.exception('Failed to record daily activity on login')
