import logging

from django.utils import timezone

from .signals import record_daily_activity

logger = logging.getLogger(__name__)

# Date-valued session flag: 'YYYY-MM-DD' of the local day already recorded.
SESSION_FLAG_KEY = 'portal_daily_activity_recorded'


class DailyActivityMiddleware:
    """Record each authenticated user's presence at most once per local day.

    A date-valued session flag gates all database work: once today's flag
    is set the middleware performs zero ORM queries. Because the stored
    value is the date itself (not a boolean), a flag left over from
    yesterday no longer matches and activity is recorded again for the new
    local day. Recording is delegated to record_daily_activity() — the
    single source of truth — and never increments login_count (the
    user_logged_in signal owns that).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            self._record(request)
        except Exception:
            logger.exception('Daily activity recording failed; continuing request.')
        return self.get_response(request)

    def _record(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return
        session = getattr(request, 'session', None)
        if session is None:
            return
        today = timezone.localdate().isoformat()
        if session.get(SESSION_FLAG_KEY) == today:
            return
        record_daily_activity(user, count_login=False)
        # Set the flag only after a successful write so a failure retries
        # on the next request instead of being suppressed for the day.
        session[SESSION_FLAG_KEY] = today
