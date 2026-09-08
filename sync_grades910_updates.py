# -*- coding: utf-8 -*-
"""One-time script: curriculum updates for Grades 9 and 10.

- Grade 9: deactivate 'АСОСҲОИ КАСБУ ҲУНАР' (not in official curriculum).
- Grade 10: ensure 'АДАБИЁТИ ҶАҲОН' exists and is active (is_default, teacher=None).
Deactivate only — no deletes."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django

django.setup()

from portal.models import School, Student, ClassSubject
from portal.utils import is_academic_school, class_numeric_part, normalize_subject

RETIRED_G9 = {normalize_subject('Асосҳои касбу ҳунар')}
NEW_G10 = normalize_subject('Адабиёти ҷаҳон')


def classes_for_school(school):
    names = set(
        Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    names.update(
        ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    return names


def main():
    created = reactivated = deactivated = skipped_schools = 0
    for school in School.objects.all():
        if not is_academic_school(school):
            skipped_schools += 1
            continue
        for cls in classes_for_school(school):
            level = class_numeric_part(cls)
            if level == 9:
                for cs in ClassSubject.objects.filter(school=school, class_name=cls, is_active=True):
                    if normalize_subject(cs.subject) in RETIRED_G9:
                        cs.is_active = False
                        cs.save()
                        deactivated += 1
                        print(f'  [DEACTIVATED] {school.name} — {cls} — {cs.subject}')
            elif level == 10:
                obj, was_created = ClassSubject.objects.get_or_create(
                    school=school, class_name=cls, subject=NEW_G10,
                    defaults={'is_default': True, 'is_active': True, 'teacher': None},
                )
                if was_created:
                    created += 1
                    print(f'  [CREATED] {school.name} — {cls} — {NEW_G10}')
                elif not obj.is_active:
                    obj.is_active = True
                    obj.is_default = True
                    obj.save()
                    reactivated += 1
                    print(f'  [REACTIVATED] {school.name} — {cls} — {NEW_G10}')

    print(f'\nDone. Адабиёти ҷаҳон created: {created}, reactivated: {reactivated}, '
          f'Асосҳои касбу ҳунар deactivated (g9): {deactivated}, non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
