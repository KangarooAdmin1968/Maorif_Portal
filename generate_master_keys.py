#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate a Word-compatible HTML master keys table for all Zafarobod schools."""
import argparse
import os
import sys

# Ensure Cyrillic output prints correctly on Windows consoles
sys.stdout.reconfigure(encoding='utf-8')

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
django.setup()

from pathlib import Path
from portal.models import School
from portal.utils import get_school_number, get_school_password_base


def sort_key(school):
    n = get_school_number(school)
    if n.isdigit():
        return (0, int(n))
    if n.startswith('l'):
        return (1, int(n[1:]) if n[1:].isdigit() else 0)
    if n.startswith('g'):
        return (2, int(n[1:]) if n[1:].isdigit() else 0)
    if n.startswith('mdtt'):
        return (3, int(n[4:]) if n[4:].isdigit() else 0)
    return (4, n)


def build_html(schools):
    rows = []
    for idx, school in enumerate(schools, 1):
        num = get_school_number(school)
        base = get_school_password_base(school)
        director_user = f'director_{num}'
        director_pass = f'Director_{base}_2026@'
        zavuch_user = f'zavuch_{num}'
        zavuch_pass = f'Zavuch_{base}_2026@'
        rows.append(f"""
        <tr>
            <td>{idx}</td>
            <td>{school.name}</td>
            <td>{director_user}</td>
            <td>{director_pass}</td>
            <td>{zavuch_user}</td>
            <td>{zavuch_pass}</td>
        </tr>""")

    rows_html = '\n'.join(rows)

    return f"""<!DOCTYPE html>
<html lang="tg">
<head>
    <meta charset="utf-8">
    <title>Зафаробод - Калибҳои Мастер</title>
    <style>
        body {{
            font-family: 'Times New Roman', Times, serif;
            margin: 20px;
            color: #000;
        }}
        h1 {{
            text-align: center;
            font-size: 22pt;
            margin-bottom: 20px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 11pt;
        }}
        th, td {{
            border: 1px solid #000;
            padding: 6px 8px;
            text-align: left;
            vertical-align: middle;
        }}
        th {{
            background-color: #003366;
            color: #fff;
            font-weight: bold;
            text-align: center;
        }}
        td:nth-child(1) {{
            text-align: center;
            width: 40px;
        }}
        td:nth-child(3), td:nth-child(4), td:nth-child(5), td:nth-child(6) {{
            font-family: 'Courier New', Courier, monospace;
        }}
        tr:nth-child(even) {{
            background-color: #f2f2f2;
        }}
    </style>
</head>
<body>
    <h1>Зафаробод - Калибҳои мастер барои портали Маориф</h1>
    <table>
        <thead>
            <tr>
                <th>№</th>
                <th>Муассиса</th>
                <th>Логини Директор</th>
                <th>Пароли Директор</th>
                <th>Логини Завуч</th>
                <th>Пароли Завуч</th>
            </tr>
        </thead>
        <tbody>
{rows_html}
        </tbody>
    </table>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description='Generate master keys table.')
    parser.add_argument('--output', '-o', default=r'D:\Loihalar\Zafarobod_Maorif_Master_Keys.html',
                        help='Output HTML file path.')
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    schools = [s for s in School.objects.all() if get_school_number(s) != '0']
    schools = sorted(schools, key=sort_key)

    html = build_html(schools)
    output.write_text(html, encoding='utf-8')

    print(f'Generated master keys for {len(schools)} institutions: {output}')


if __name__ == '__main__':
    main()
