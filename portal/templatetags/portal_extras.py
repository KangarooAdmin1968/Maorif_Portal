from django import template

register = template.Library()


@register.filter
def dict_key(d, k):
    """Return the value for key k in dict d, or empty string."""
    if d is None:
        return ''
    return d.get(k, '')
