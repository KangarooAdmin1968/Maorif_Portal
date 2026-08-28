from django.conf import settings


def user_role(request):
    """Expose the current user's role to all templates."""
    role = ''
    if request.user.is_authenticated:
        try:
            role = request.user.userprofile.role
        except Exception:
            role = ''
    return {
        'user_role': role,
        'ROLE_ZAVUCH': getattr(settings, 'ROLE_ZAVUCH', 'zavuch'),
    }
