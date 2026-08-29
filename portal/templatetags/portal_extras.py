from django import template
from django.conf import settings

from ..views import get_user_role, get_user_school

register = template.Library()


@register.filter
def dict_key(d, k):
    """Return the value for key k in dict d, or empty string."""
    if d is None:
        return ''
    return d.get(k, '')


@register.simple_tag(takes_context=True)
def can_manage_school(context, school):
    """Return True if the current user can edit/import data for this school."""
    user = context['user']
    if not user or user.is_anonymous:
        return False
    if user.is_superuser:
        return True
    role = get_user_role(user)
    is_zavuch = (role and role.lower() == 'zavuch') or user.username.lower().startswith('zavuch_')
    if not is_zavuch:
        return False
    return get_user_school(user) == school
