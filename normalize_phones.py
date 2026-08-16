#!/usr/bin/env python
"""Normalize School and Teacher phone numbers to Tajik international format."""
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
import django
django.setup()

from django.conf import settings
from portal.models import School, Teacher, UserProfile


def normalize_phone(raw):
    if not raw:
        return ''
    digits = re.sub(r'\D', '', str(raw))
    if not digits:
        return ''

    # Already contains country code
    if digits.startswith('992'):
        tail = digits[3:]
        if len(tail) >= 9:
            tail = tail[-9:]
        if not tail or tail[0] != '9':
            tail = '9' + (tail[1:] if len(tail) > 1 else '')
            tail = tail[:9].ljust(9, '0')
        return f'+992{tail}'

    # 7-digit local numbers -> +99292XXXXXXX
    if len(digits) == 7:
        return f'+99292{digits}'

    # 9-digit numbers -> +992XXXXXXXXX
    if len(digits) == 9:
        if digits[0] == '9':
            return f'+992{digits}'
        # Invalid leading digit, replace it with 9
        return f'+9929{digits[1:]}'

    # 10-digit numbers with a leading 0, 8, 9 or other bad digit -> strip leading and ensure 9
    if len(digits) == 10:
        tail = digits[1:]
        if not tail or tail[0] != '9':
            tail = '9' + tail[1:]
        return f'+992{tail[:9]}'

    # Fallback for any other length: keep the last 9 digits and ensure leading 9
    if len(digits) > 10:
        tail = digits[-9:]
        if tail[0] != '9':
            tail = '9' + tail[1:]
        return f'+992{tail}'

    # Too short: pad with leading 9 to reach 9 digits
    return f'+992{digits.rjust(9, "0")}'


def abbreviate_school(name):
    match = re.search(r'(№\s*\d+|\b\d+\b)', name)
    if match:
        number = re.sub(r'\s+', '', match.group(0))
        prefix = name[:match.start()].strip()
        initials = ''.join(w[0].upper() for w in prefix.split() if w.strip())
        return f'{initials} {number}'.strip()
    initials = ''.join(w[0].upper() for w in name.split() if w.strip())
    return initials


def main():
    # Normalize School phones
    schools = School.objects.all()
    for school in schools:
        norm = normalize_phone(school.phone)
        if norm and norm != school.phone:
            school.phone = norm
            school.save(update_fields=['phone'])

    # Normalize Teacher phones
    teachers = Teacher.objects.all()
    for teacher in teachers:
        norm = normalize_phone(teacher.phone)
        if norm and norm != teacher.phone:
            teacher.phone = norm
            teacher.save(update_fields=['phone'])

    # Build and print the master reference table
    print('| Муассиса | Директор ва телефон | Завуч ва телефон | Логини директор | Логини завуч |')
    print('|---|---|---|---|---|')
    for school in schools.order_by('name'):
        director_name = (school.director or '').strip() or '—'
        director_phone = normalize_phone(school.phone) or '—'

        teacher = Teacher.objects.filter(school=school).first()
        zavuch_name = teacher.name.strip() if teacher else '—'
        zavuch_phone = (teacher.phone or '—') if teacher else '—'

        director_user = UserProfile.objects.filter(
            school=school, role=settings.ROLE_PRINCIPAL
        ).select_related('user').first()
        zavuch_user = UserProfile.objects.filter(
            school=school, role=settings.ROLE_TEACHER
        ).select_related('user').first()

        director_username = director_user.user.username if director_user and director_user.user else '—'
        zavuch_username = zavuch_user.user.username if zavuch_user and zavuch_user.user else '—'

        school_abbr = abbreviate_school(school.name)

        print(f'| {school_abbr} | {director_name} — {director_phone} | {zavuch_name} — {zavuch_phone} | {director_username} | {zavuch_username} |')


if __name__ == '__main__':
    main()
