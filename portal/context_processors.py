import re

from django.conf import settings

from .models import School, Student, ClassSubject
from .monitoring import can_access_monitoring
from .utils import class_numeric_part, is_academic_school


def user_role(request):
    """Expose the current user's role and Zavuch status to all templates."""
    role = ''
    is_zavuch = False
    can_monitor = False
    if request.user.is_authenticated:
        try:
            role = request.user.userprofile.role
        except Exception:
            role = ''
        is_zavuch = (
            (role and role.lower() == 'zavuch') or
            request.user.username.lower().startswith('zavuch_')
        )
        # userprofile was already resolved above, so this check reuses the
        # cached relation and costs no extra query.
        can_monitor = can_access_monitoring(request.user)
    return {
        'user_role': role,
        'ROLE_ZAVUCH': getattr(settings, 'ROLE_ZAVUCH', 'zavuch'),
        'is_zavuch': is_zavuch,
        'can_monitor': can_monitor,
    }


def parent_portal(request):
    """Preload the school -> classes map for the 'Барои волидон' navbar modal.

    Classes are derived the same way as the class_list view: the union of
    Student.class_name and ClassSubject.class_name per school.
    """
    # Only real general-education schools are offered to parents/students —
    # preschools, kindergartens and education departments stay hidden.
    schools = [s for s in School.objects.all() if is_academic_school(s)]
    eligible_ids = {s.id for s in schools}

    def school_sort_key(school):
        nums = re.findall(r'\d+', school.name or '')
        return (int(nums[0]) if nums else 999999, school.name)

    schools.sort(key=school_sort_key)

    classes_map = {}
    for qs in (
        Student.objects.values('school_id', 'class_name').distinct(),
        ClassSubject.objects.values('school_id', 'class_name').distinct(),
    ):
        for row in qs:
            if row['school_id'] not in eligible_ids:
                continue
            classes_map.setdefault(row['school_id'], set()).add(row['class_name'])

    data = {
        'schools': [{'id': s.id, 'name': s.name} for s in schools],
        'classes': {
            str(sid): sorted(names, key=lambda c: (class_numeric_part(c), c))
            for sid, names in classes_map.items()
        },
    }
    return {'parent_portal_data': data}
