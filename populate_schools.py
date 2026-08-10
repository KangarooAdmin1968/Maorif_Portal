#!/usr/bin/env python
"""Populate the School table with Zafarobod District educational institutions."""
import sys
import os

# Ensure Cyrillic output prints correctly on Windows consoles
sys.stdout.reconfigure(encoding='utf-8')

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
django.setup()

from portal.models import School


SCHOOLS = [
    {"name": "Идораи маорифи ноҳияи Зафаробод", "type": "Идораи маориф", "language": "Тоҷикӣ"},
    {"name": "Литсейи №1-и ноҳияи Зафаробод (Марказ)", "type": "Литсей", "language": "Тоҷикӣ"},
    {"name": "Литсейи №2-и ноҳияи Зафаробод (Меҳнатобод)", "type": "Литсей", "language": "Тоҷикӣ"},
    {"name": "Гимназияи ноҳияи Зафаробод", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №1", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №2", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №3", "type": "Мактаб", "language": "Ӯзбекӣ"},
    {"name": "Муассисаи таҳсилоти умумии №4", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №5", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №6", "type": "Мактаб", "language": "Русӣ"},
    {"name": "Муассисаи таҳсилоти умумии №7", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №8", "type": "Мактаб", "language": "Ӯзбекӣ"},
    {"name": "Муассисаи таҳсилоти умумии №9", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №10", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №11", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №12", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №13", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №14", "type": "Мактаб", "language": "Ӯзбекӣ"},
    {"name": "Муассисаи таҳсилоти умумии №15", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №16", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №17", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №18", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №19", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №20", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №21", "type": "Мактаб", "language": "Ӯзбекӣ"},
    {"name": "Муассисаи таҳсилоти умумии №22", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №23", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №24", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №25", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Муассисаи таҳсилоти умумии №26", "type": "Мактаб", "language": "Тоҷикӣ"},
    {"name": "Кӯдакистони №1-и ноҳияи Зафаробод", "type": "Кӯдакистон", "language": "Тоҷикӣ"},
    {"name": "Кӯдакистони №2-и ноҳияи Зафаробод", "type": "Кӯдакистон", "language": "Тоҷикӣ"},
    {"name": "Кӯдакистони №3-и ноҳияи Зафаробод", "type": "Кӯдакистон", "language": "Тоҷикӣ"},
    {"name": "Кӯдакистони деҳаи Лоҷин", "type": "Кӯдакистон", "language": "Тоҷикӣ"},
]


def main():
    created_count = 0
    existing_count = 0
    for item in SCHOOLS:
        school, created = School.objects.get_or_create(
            name=item["name"],
            defaults={
                "director": "Номи директор",
                "phone": "+992000000000",
                "type": item["type"],
                "language": item["language"],
            },
        )
        if created:
            created_count += 1
            print(f"Created: {school.name}")
        else:
            existing_count += 1
            print(f"Already exists: {school.name}")
    print(f"\nDone. Created: {created_count}, Existing: {existing_count}, Total: {len(SCHOOLS)}")


if __name__ == '__main__':
    main()
