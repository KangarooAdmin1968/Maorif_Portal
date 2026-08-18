#!/usr/bin/env python
"""Generate a Google Contacts CSV from active school Zavuch phone numbers."""
import csv
import os
import re

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django
django.setup()

from portal.models import School, Teacher


def school_number(name):
    m = re.search(r'№\s*(\d+)', name)
    if m:
        return f'№{m.group(1)}'
    m = re.search(r'\b(\d+)\b', name)
    if m:
        return m.group(1)
    return ''


def main():
    output = 'google_contacts.csv'
    rows = []
    for school in School.objects.all().order_by('name'):
        num = school_number(school.name)
        if num and num in ('№24', '№25', '№26', '24', '25', '26'):
            continue

        teacher = (
            Teacher.objects
            .filter(school=school)
            .exclude(name='')
            .exclude(name__isnull=True)
            .exclude(phone='')
            .exclude(phone__isnull=True)
            .first()
        )
        if not teacher:
            continue

        first_name = f'Завуч МТУ {num} {teacher.name}'.strip()
        rows.append([first_name, '', teacher.phone])

    with open(output, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['First Name', 'Last Name', 'Phone'])
        writer.writerows(rows)

    print(f'Generated {output} with {len(rows)} contacts.')


if __name__ == '__main__':
    main()
