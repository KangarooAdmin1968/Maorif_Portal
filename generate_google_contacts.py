#!/usr/bin/env python
"""Generate a Google Contacts CSV from the matched contacts in import_contacts.py."""
import csv
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
from import_contacts import RAW_CONTACTS, parse_contacts


def normalize_phone(raw):
    if not raw:
        return ''
    digits = re.sub(r'\D', '', str(raw))
    if not digits:
        return ''

    if digits.startswith('992'):
        tail = digits[3:]
        if len(tail) >= 9:
            tail = tail[-9:]
        if not tail or tail[0] != '9':
            tail = '9' + (tail[1:] if len(tail) > 1 else '')
            tail = tail[:9].ljust(9, '0')
        return f'+992{tail}'

    if len(digits) == 7:
        return f'+99292{digits}'

    if len(digits) == 9:
        if digits[0] == '9':
            return f'+992{digits}'
        return f'+9929{digits[1:]}'

    if len(digits) == 10:
        tail = digits[1:]
        if not tail or tail[0] != '9':
            tail = '9' + tail[1:]
        return f'+992{tail[:9]}'

    if len(digits) > 10:
        tail = digits[-9:]
        if tail[0] != '9':
            tail = '9' + tail[1:]
        return f'+992{tail}'

    return f'+992{digits.rjust(9, "0")}'


def school_number(raw):
    m = re.search(r'№\s*(\d+)', raw)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d+)\b', raw)
    if m:
        return m.group(1)
    return None


def main():
    output = 'google_contacts.csv'
    rows = parse_contacts(RAW_CONTACTS)

    with open(output, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['First Name', 'Last Name', 'Phone'])

        skipped = 0
        written = 0
        for row in rows:
            idx = row['idx']
            if idx in (24, 25, 26):
                skipped += 1
                continue

            zavuch_name = row['zavuch_name'].strip()
            zavuch_phone = row['zavuch_phone'].strip()
            if not zavuch_name or not zavuch_phone:
                skipped += 1
                continue

            num = school_number(row['school_raw'])
            if not num:
                skipped += 1
                continue

            phone = normalize_phone(zavuch_phone)
            first_name = f'Завуч МТУ №{num} {zavuch_name}'
            writer.writerow([first_name, '', phone])
            written += 1

    print(f'Generated {output} with {written} contacts ({skipped} skipped).')


if __name__ == '__main__':
    main()
