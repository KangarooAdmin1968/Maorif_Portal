# -*- coding: utf-8 -*-
"""One-time script: curriculum cleanup for Grades 1, 6 and 7.

- Grade 7: deactivate 'АСОСҲОИ БЕХАТАРИИ ҲАЁТ'; ensure 'ТЕХНОЛОГИЯ' exists & active.
- Grade 6: deactivate 'ЗАБОНИ АРАБӢ' and 'ЗАБОНИ ФРАНСАВӢ' (фаронсавӣ).
- Grade 1: deactivate 'АЛИФБОИ МУСИҚӢ' if present.
Deactivate only — no deletes."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django

django.setup()

from portal.models import School, Student, ClassSubject
from portal.utils import is_academic_school, class_numeric_part, normalize_subject

RETIRED_G7 = {normalize_subject('Асосҳои бехатарии ҳаёт')}
RETIRED_G6 = {normalize_subject('Забони арабӣ'), normalize_subject('Забони фаронсавӣ'),
              normalize_subject('Забони франсавӣ')}
RETIRED_G1 = {normalize_subject('Алифбои мусиқӣ')}
TECHNOLOGY = normalize_subject('Технология')


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
            if level == 7:
                # Ensure Технология exists and is active
                obj, was_created = ClassSubject.objects.get_or_create(
                    school=school, class_name=cls, subject=TECHNOLOGY,
                    defaults={'is_default': True, 'is_active': True, 'teacher': None},
                )
                if was_created:
                    created += 1
                    print(f'  [CREATED] {school.name} — {cls} — {TECHNOLOGY}')
                elif not obj.is_active:
                    obj.is_active = True
                    obj.is_default = True
                    obj.save()
                    reactivated += 1
                    print(f'  [REACTIVATED] {school.name} — {cls} — {TECHNOLOGY}')
                retired = RETIRED_G7
            elif level == 6:
                retired = RETIRED_G6
            elif level == 1:
                retired = RETIRED_G1
            else:
                continue

            for cs in ClassSubject.objects.filter(school=school, class_name=cls, is_active=True):
                if normalize_subject(cs.subject) in retired:
                    cs.is_active = False
                    cs.save()
                    deactivated += 1
                    print(f'  [DEACTIVATED] {school.name} — {cls} — {cs.subject}')

    print(f'\nDone. Технология created: {created}, reactivated: {reactivated}, '
          f'retired subjects deactivated: {deactivated}, non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
