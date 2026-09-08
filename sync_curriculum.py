# -*- coding: utf-8 -*-
"""One-time script: sync ClassSubject records to the updated curriculum.

- Adds 'СОАТИ ТАРБИЯВӢ' to every class (grades 1-11) in all academic schools.
- Deactivates retired subjects: 'ТАРЗИ ҲАЁТИ СОЛИМ', 'АСОСҲОИ ИНТИХОБИ КАСБ'.
- Grade 10: deactivates 'АСОСҲОИ ИҚТИСОДИЁТ'.
- Grade 11: deactivates 'ЗАБОНИ АРАБӢ', 'ЗАБОНИ НЕМИСӢ', 'ЗАБОНИ ФРАНСАВӢ'
  (keeps 'АСОСҲОИ ИҚТИСОДИЁТ' which is officially taught there).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django

django.setup()

from portal.models import School, Student, ClassSubject
from portal.utils import is_academic_school, class_numeric_part, normalize_subject

HOMEROOM = normalize_subject('Соати тарбиявӣ')

RETIRED_ALL = {normalize_subject(s) for s in ['Тарзи ҳаёти солим', 'Асосҳои интихоби касб']}
RETIRED_G10 = RETIRED_ALL | {normalize_subject('Асосҳои иқтисодиёт')}
RETIRED_G11 = RETIRED_ALL | {normalize_subject(s) for s in ['Забони арабӣ', 'Забони немисӣ', 'Забони франсавӣ']}


def retired_for(level):
    if level == 10:
        return RETIRED_G10
    if level == 11:
        return RETIRED_G11
    return RETIRED_ALL


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
            if level is None or not (1 <= level <= 11):
                continue

            # 1) Ensure Соати тарбиявӣ exists and is active
            obj, was_created = ClassSubject.objects.get_or_create(
                school=school, class_name=cls, subject=HOMEROOM,
                defaults={'is_default': True, 'is_active': True, 'teacher': None},
            )
            if was_created:
                created += 1
                print(f'  [CREATED] {school.name} — {cls} — {HOMEROOM}')
            elif not obj.is_active:
                obj.is_active = True
                obj.is_default = True
                obj.save()
                reactivated += 1
                print(f'  [REACTIVATED] {school.name} — {cls} — {HOMEROOM}')

            # 2) Deactivate retired subjects for this level
            retired = retired_for(level)
            for cs in ClassSubject.objects.filter(school=school, class_name=cls, is_active=True):
                if normalize_subject(cs.subject) in retired:
                    cs.is_active = False
                    cs.save()
                    deactivated += 1
                    print(f'  [DEACTIVATED] {school.name} — {cls} — {cs.subject}')

    print(f'\nDone. Соати тарбиявӣ created: {created}, reactivated: {reactivated}, '
          f'retired subjects deactivated: {deactivated}, non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
