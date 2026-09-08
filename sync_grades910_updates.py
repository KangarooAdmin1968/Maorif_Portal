# -*- coding: utf-8 -*-
"""One-time script: deactivate 'АСОСҲОИ КАСБУ ҲУНАР' from Grade 9 in academic schools.

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


def classes_for_school(school):
    names = set(
        Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    names.update(
        ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    )
    return names


def main():
    deactivated = skipped_schools = 0
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

    print(f'\nDone. Асосҳои касбу ҳунар deactivated (Grade 9): {deactivated}, '
          f'non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
