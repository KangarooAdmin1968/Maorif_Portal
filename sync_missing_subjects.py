# -*- coding: utf-8 -*-
"""Create and activate missing curriculum subjects for active grade 8/9/10 classes."""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')

import django
django.setup()

from portal.models import School, Student, ClassSubject, normalize_subject, normalize_class_name
from portal.utils import is_academic_school, class_numeric_part

SUBJECTS_TO_ADD = {
    '10': ['Нуҷум (Астрономия)'],
    '9': ['Тарбияи ҷисмонӣ', 'Экология'],
    '8': ['Тарбияи ҷисмонӣ'],
}


def main():
    active_classes = (
        Student.objects
        .select_related('school')
        .values('school__id', 'class_name')
        .distinct()
    )

    school_ids = {row['school__id'] for row in active_classes}
    school_map = {s.id: s for s in School.objects.filter(id__in=school_ids)}

    touched = 0
    created = 0
    updated = 0

    for row in active_classes:
        school = school_map.get(row['school__id'])
        if not school or not is_academic_school(school):
            continue

        class_name = normalize_class_name(row['class_name'])
        grade = str(class_numeric_part(class_name))
        if grade not in SUBJECTS_TO_ADD:
            continue

        for subj in SUBJECTS_TO_ADD[grade]:
            norm_subj = normalize_subject(subj)
            obj, was_created = ClassSubject.objects.get_or_create(
                school=school,
                class_name=class_name,
                subject=norm_subj,
                defaults={
                    'is_default': True,
                    'is_active': True,
                    'teacher': None,
                },
            )
            if was_created:
                created += 1
            elif not obj.is_active or not obj.is_default or obj.teacher is not None:
                obj.is_active = True
                obj.is_default = True
                obj.teacher = None
                obj.save()
                updated += 1
            touched += 1

    print(f'Checked {touched} class/subject pairs, created {created}, reactivated/updated {updated}.', flush=True)


if __name__ == '__main__':
    main()
