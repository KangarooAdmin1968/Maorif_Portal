#!/usr/bin/env python
"""Import school contacts, update School director/phone, and create Director/Zavuch users."""
import os
import re
import sys

# Ensure Cyrillic output prints correctly on Windows consoles
sys.stdout.reconfigure(encoding='utf-8')

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
django.setup()

from django.contrib.auth.models import User
from django.db import transaction
from django.conf import settings
from portal.models import School, UserProfile

RAW_CONTACTS = """1	МТМУ 1	Бобозода Гавҳар	600-22-14	Ҳоҷимуродова Гулҷаҳон	802-34-54
2	МТМУ 2	Усмонова Ойсара	110-08-85-87	Темирова Гулмира	765-10-55
3	МТМУ 3	Қурбонов Фаромӯз	762-64-66	Ҳамроқулова Маҳбуба	642-91-97
4	МТМУ 4	Исмоилов Давронбек	727-50-19	Тӯхтаева Ноила	722-08-21
5	МТМУ 5	Соқиев Исомиддин	809-89-73	Муллоев М	832-33-98
6	МТМУ 6	Холиқов Абдуманнон	922-53-35	Боймуродов Чамшед	898-82-42
7	МТМУ 7	Сангинов Ҳамзахон	717-71-68	Сафаров Сафар	704-43-14
8	МТМУ 8	Мирзоева Ҷумагул	897-72-33	Холмуротова	020-15-23
9	МТМУ 9	Саидова Гулбаҳор	824-48-28	Шералиев Ином	747-05-24
10	МТМУ 10	Раҳматов Ёқуб	110-03-72-74	Ҳақназарова Нигина	766-61-58
11	МТМУ 11	Қудратов Бозорбой	753-90-64		844-24-94
12	МТМУ 12	Амонов Отабек	976-17-71	Ҷӯраева Хоҷарой	722-54-67
13	МТМУ 13	Сангинов Мунаввар	827-21-40	Муҳаммадиева Таҳмина	922-71-39
14	МТМУ 14	Наврӯзова Наврӯзмоҳ	859-05-31	Наврӯзов Наврӯзмаҳмад	612-69-44
15	МТМУ 15	Абдуқаюмов Абдуаҳад	112-601-066	Ҳоҷимуродов Акобир	732-05-54
16	МТМУ 16	Азизов Шодиқул	799-96-43	Ҷӯраева Ҷасур	890-10-25
17	МТМУ 17	Қурбонов Ҳусрав	300-80-97	Ҷумаев Тоҷиддин	93-556-55-58
18	Гимназия №1	Одилзода Мирзоодил	99-010-11-25	Ортиқов Р	639-47-74
19	МТМУ 19	Элмирзоев Қ	943-54-07	Валиев Умедҷон	907-33-32
20	МТМУ 20	Одинаев Санҷар	666-65-44	Калонхӯҷаева Моҳира	792-97-05
21	МТМУ 21	Норова Шаҳноза	891-03-02	Абдураҳмонова Ирода	031-60-95
22	МТМУ 22	Айдарова Нуринисо	852-62-18	Орипова Дилбар	704-77-16
23	МТМУ 23	Сангинова Фирӯза	444-85-23	Ғафурзода Акмал	775-33-64
24	Литсей №1	Эсанов Усмон	733-35-93	Носирова Гулнора	88-500-46-13
25	Литсей №2	Умаров Суҳроб	750-93-09	Тошов Абубакр	777-13-47
26	МДТТ №1	Абдуллоева Дилафрӯз	757-62-69	Ҳайдарова Лайло	950-70-65
27	МДТТ №2	Асадова Маҳбуба	771-06-82		
28	МДТТ №3	Ҳасанова Рано	748-71-26	Баротова Мадина	800-53-07
29	МДТТ №4	Олимова Гулноза	822-41-41		
30	МДТТ №5	Ҷумабоева Шаҳло	605-66-49		
31	Афсона хусусӣ	Олимова Мадина	944-03-86		
32	Табассум	Боймуродов Фирдавс	947-69-90		"""


def parse_contacts(raw):
    rows = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split('\t')]
        # Ensure at least 4 columns (index, school, director, phone)
        while len(parts) < 4:
            parts.append('')
        while len(parts) < 6:
            parts.append('')
        try:
            idx = int(parts[0])
        except ValueError:
            idx = 0
        rows.append({
            'idx': idx,
            'school_raw': parts[1],
            'director_name': parts[2],
            'director_phone': parts[3],
            'zavuch_name': parts[4],
            'zavuch_phone': parts[5],
        })
    return rows


def resolve_school(raw_school):
    raw = raw_school.strip()

    mtmu = re.match(r'^МТМУ\s*(\d+)$', raw)
    if mtmu:
        number = int(mtmu.group(1))
        return School.objects.filter(name=f'Муассисаи таҳсилоти умумии №{number}').first()

    gimn = re.match(r'^Гимназия\s*№?(\d+)$', raw)
    if gimn:
        return School.objects.filter(name='Гимназияи ноҳияи Зафаробод').first()

    lit = re.match(r'^Литсей\s*№?(\d+)$', raw)
    if lit:
        number = int(lit.group(1))
        if number == 1:
            return School.objects.filter(name='Литсейи №1-и ноҳияи Зафаробод (Марказ)').first()
        if number == 2:
            return School.objects.filter(name='Литсейи №2-и ноҳияи Зафаробод (Меҳнатобод)').first()
        return None

    mdtt = re.match(r'^МДТТ\s*№?(\d+)$', raw)
    if mdtt:
        number = int(mdtt.group(1))
        if 1 <= number <= 3:
            return School.objects.filter(name=f'Кӯдакистони №{number}-и ноҳияи Зафаробод').first()
        if number == 4:
            return School.objects.filter(name='Кӯдакистони деҳаи Лоҷин').first()
        # MDTT 5 and private kindergartens have no official match
        return None

    return None


def make_ids(raw_school):
    raw = raw_school.strip()

    mtmu = re.match(r'^МТМУ\s*(\d+)$', raw)
    if mtmu:
        number = mtmu.group(1)
        return f'M{number}', number

    gimn = re.match(r'^Гимназия\s*№?(\d+)$', raw)
    if gimn:
        number = gimn.group(1)
        return f'Gimn{number}', f'gimn{number}'

    lit = re.match(r'^Литсей\s*№?(\d+)$', raw)
    if lit:
        number = lit.group(1)
        return f'Lit{number}', f'lit{number}'

    mdtt = re.match(r'^МДТТ\s*№?(\d+)$', raw)
    if mdtt:
        number = mdtt.group(1)
        return f'Mdtt{number}', f'mdtt{number}'

    return None, None


def get_or_create_user(username, password, full_name, role, school):
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=full_name,
            )
    except Exception as exc:
        print(f"  [WARN] Could not create user {username}: {exc}")
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return None

    UserProfile.objects.update_or_create(
        user=user,
        defaults={
            'role': role,
            'school': school,
        },
    )
    return user


def main():
    rows = parse_contacts(RAW_CONTACTS)
    created_directors = 0
    created_zavuchs = 0
    updated_schools = 0
    skipped = 0

    for row in rows:
        raw_school = row['school_raw']
        school = resolve_school(raw_school)
        if not school:
            print(f"{row['idx']}: SKIP '{raw_school}' — no matching official school")
            skipped += 1
            continue

        director_name = row['director_name'].strip()
        director_phone = row['director_phone'].strip()

        # Update School model
        if director_name:
            school.director = director_name
            school.phone = director_phone
            school.save()
            updated_schools += 1
            print(f"{row['idx']}: UPDATED {school.name}")
        else:
            print(f"{row['idx']}: no director name for {school.name}, skipping school update")

        password_base, username_abbr = make_ids(raw_school)
        if not username_abbr:
            print(f"{row['idx']}: could not derive username for '{raw_school}'")
            continue

        # Director user
        if director_name:
            director_username = f'director_{username_abbr}'
            director_password = f'Director_{password_base}_2026@'
            user = get_or_create_user(
                director_username,
                director_password,
                director_name,
                settings.ROLE_PRINCIPAL,
                school,
            )
            if user:
                created_directors += 1
                print(f"  -> director user {director_username}")

        # Zavuch user
        zavuch_name = row['zavuch_name'].strip()
        if zavuch_name:
            zavuch_username = f'zavuch_{username_abbr}'
            zavuch_password = f'Zavuch_{password_base}_2026@'
            user = get_or_create_user(
                zavuch_username,
                zavuch_password,
                zavuch_name,
                settings.ROLE_TEACHER,
                school,
            )
            if user:
                created_zavuchs += 1
                print(f"  -> zavuch user {zavuch_username}")

    print(f"\nDone. Schools updated: {updated_schools}, Directors: {created_directors}, Zavuchs: {created_zavuchs}, Skipped: {skipped}")


if __name__ == '__main__':
    main()
