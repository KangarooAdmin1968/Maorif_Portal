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


# Session flag written at login (portal.signals.on_user_logged_in) and
# cleared by PortalPasswordChangeView — lets the middleware decide without
# a per-request profile query.
PW_CHANGE_SESSION_KEY = 'portal_must_change_password'


class ForceTeacherPasswordMiddleware:
    """Route teachers still on a system-generated default password to the
    password-change form until they set their own.

    NOT currently registered in settings.MIDDLEWARE — the owner decided
    legacy default-password accounts must keep working without a forced
    reset. Add 'portal.middleware.ForceTeacherPasswordMiddleware' after
    AuthenticationMiddleware to enforce it later; the session flag, login
    seeding and flag sync below already support it.

    custom_password_set is False only for accounts created on the legacy
    Teacher_<school>_<n>@2026 formula (or bulk-import equivalents). Those
    passwords are derivable from the public username, so every request —
    not just the page after login — is redirected to password_change until
    the flag flips. Zavuch, director and superuser accounts don't carry a
    TeacherProfile on that pattern and are unaffected. Login itself also
    redirects to password_change, so this middleware is the enforcement
    backstop for navigation within an existing session.

    Cost: zero queries once the login signal has seeded the session flag;
    sessions that predate the flag pay one slim lookup on the first
    request, which caches the result back into the session.
    """

    _EXEMPT_PREFIXES = (
        '/password_change',
        '/login/', '/logout/', '/admin/',
        '/static/', '/media/', '/favicon.ico', '/sw.js',
        '/google3c6e6431fb434e83.html',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (user is None or not user.is_authenticated
                or request.path.startswith(self._EXEMPT_PREFIXES)):
            return self.get_response(request)

        session = getattr(request, 'session', None)
        needs_change = None
        if session is not None:
            needs_change = session.get(PW_CHANGE_SESSION_KEY)
        if needs_change is None:
            needs_change = self._profile_needs_change(user)
            if session is not None:
                session[PW_CHANGE_SESSION_KEY] = needs_change
        if needs_change:
            from django.shortcuts import redirect
            return redirect('password_change')
        return self.get_response(request)

    @staticmethod
    def _profile_needs_change(user):
        """Slim existence check for a profile still flagged with a
        system-generated default password — one column, no row built."""
        from .models import TeacherProfile
        return (TeacherProfile.objects
                .filter(user_id=user.pk, custom_password_set=False)
                .exists())
