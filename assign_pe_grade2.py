# -*- coding: utf-8 -*-
"""One-time script: bulk-assign ТАРБИЯИ ҶИСМОНӢ to all Grade 2 classes in all academic schools."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django

django.setup()

from portal.models import School, Student, ClassSubject
from portal.utils import is_academic_school, class_numeric_part, normalize_subject

PE_SUBJECT = normalize_subject('Тарбияи ҷисмонӣ')


def grade2_classes_for_school(school):
    names = set(
        Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    names.update(
        ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    return sorted(c for c in names if class_numeric_part(c) == 2)


def main():
    created = 0
    existing = 0
    skipped_schools = 0
    for school in School.objects.all():
        if not is_academic_school(school):
            skipped_schools += 1
            continue
        classes = grade2_classes_for_school(school)
        for cls in classes:
            obj, was_created = ClassSubject.objects.get_or_create(
                school=school,
                class_name=cls,
                subject=PE_SUBJECT,
                defaults={'is_default': True, 'is_active': True, 'teacher': None},
            )
            if was_created:
                created += 1
                print(f'  [CREATED] {school.name} — {cls} — {PE_SUBJECT}')
            else:
                changed = False
                if not obj.is_active:
                    obj.is_active = True
                    changed = True
                if not obj.is_default:
                    obj.is_default = True
                    changed = True
                if changed:
                    obj.save()
                    print(f'  [REACTIVATED] {school.name} — {cls} — {PE_SUBJECT}')
                else:
                    print(f'  [EXISTS] {school.name} — {cls} — {PE_SUBJECT}')
                existing += 1
        if classes:
            print(f'{school.name}: {len(classes)} grade-2 class(es) -> {classes}')
    print(f'\nDone. Created: {created}, already existed/reactivated: {existing}, non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
