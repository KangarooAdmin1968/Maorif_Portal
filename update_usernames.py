#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Align Director and Zavuch usernames/passwords to physical school numbers."""
import os
import sys
import argparse

# Ensure Cyrillic output prints correctly on Windows consoles
sys.stdout.reconfigure(encoding='utf-8')

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
django.setup()

from django.contrib.auth.models import User
from django.conf import settings
from portal.models import School, UserProfile
from portal.utils import get_school_number, get_school_password_base


def expected_users(school):
    """Return (username, password, role) tuples for the director and zavuch."""
    slug = get_school_number(school)
    base = get_school_password_base(school)
    return [
        (f'director_{slug}', f'Director_{base}_2026@', settings.ROLE_PRINCIPAL),
        (f'zavuch_{slug}', f'Zavuch_{base}@2026', settings.ROLE_TEACHER),
    ]


def update_or_warn(profile, expected_username, expected_password, expected_role, dry_run=False):
    if not profile:
        return False
    user = profile.user
    old_username = user.username
    if dry_run:
        print(f'  [DRY-RUN] {old_username} -> {expected_username}  (role={expected_role})')
        return True

    if old_username != expected_username:
        if User.objects.filter(username=expected_username).exclude(pk=user.pk).exists():
            print(f'  [CONFLICT] {expected_username} already exists; cannot rename {old_username}')
            return False
        user.username = expected_username
    user.set_password(expected_password)
    user.save()

    if profile.role != expected_role:
        profile.role = expected_role
        profile.save()

    print(f'  [UPDATED] {old_username} -> {expected_username}  (role={expected_role})')
    return True


def main():
    parser = argparse.ArgumentParser(description='Align Director/Zavuch usernames to physical school numbers.')
    parser.add_argument('--dry-run', action='store_true', help='Show planned changes without saving.')
    args = parser.parse_args()

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    print(f'Starting username alignment in {mode} mode...\n')

    updated = 0
    skipped = 0
    for school in School.objects.all().order_by('name'):
        print(f'[{get_school_number(school)}] {school.name}')
        expected = expected_users(school)

        director_profile = UserProfile.objects.filter(
            school=school, user__username__startswith='director_'
        ).select_related('user').first()

        zavuch_profile = UserProfile.objects.filter(
            school=school, user__username__startswith='zavuch_'
        ).select_related('user').first()

        if not director_profile and not zavuch_profile:
            print('  [SKIP] no director or zavuch user found')
            skipped += 1
            continue

        if director_profile:
            u, p, r = expected[0]
            if update_or_warn(director_profile, u, p, r, args.dry_run):
                updated += 1
        else:
            print('  [WARN] director user not found')

        if zavuch_profile:
            u, p, r = expected[1]
            if update_or_warn(zavuch_profile, u, p, r, args.dry_run):
                updated += 1
        else:
            print('  [WARN] zavuch user not found')

    print(f'\nDone. Profiles updated/planned: {updated}, schools skipped: {skipped}')


if __name__ == '__main__':
    main()
