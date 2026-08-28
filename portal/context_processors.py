from django.conf import settings


def user_role(request):
    """Expose the current user's role and Zavuch status to all templates."""
    role = ''
    is_zavuch = False
    if request.user.is_authenticated:
        try:
            role = request.user.userprofile.role
        except Exception:
            role = ''
        is_zavuch = (
            (role and role.lower() == 'zavuch') or
            request.user.username.lower().startswith('zavuch_')
        )
    return {
        'user_role': role,
        'ROLE_ZAVUCH': getattr(settings, 'ROLE_ZAVUCH', 'zavuch'),
        'is_zavuch': is_zavuch,
    }
