# -*- coding: utf-8 -*-
"""One-time script: deactivate 'ЗАБОНИ НЕМИСӢ' and 'Одоби муошират (ва рӯзгордорӣ)'
for all Grade 6 and Grade 7 classes in academic schools. Deactivate only — no deletes."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django

django.setup()

from portal.models import School, ClassSubject
from portal.utils import is_academic_school, class_numeric_part, normalize_subject

RETIRED = {
    normalize_subject('Забони немисӣ'),
    normalize_subject('Одоби муошират'),
    normalize_subject('Одоби муошират ва рӯзгордорӣ'),
}


def main():
    deactivated = skipped_schools = 0
    for school in School.objects.all():
        if not is_academic_school(school):
            skipped_schools += 1
            continue
        for cs in ClassSubject.objects.filter(school=school, is_active=True):
            if class_numeric_part(cs.class_name) not in (6, 7):
                continue
            if normalize_subject(cs.subject) in RETIRED:
                cs.is_active = False
                cs.save()
                deactivated += 1
                print(f'  [DEACTIVATED] {school.name} — {cs.class_name} — {cs.subject}')

    print(f'\nDone. Deactivated: {deactivated}, non-academic schools skipped: {skipped_schools}')


if __name__ == '__main__':
    main()
