# -*- coding: utf-8 -*-
"""One-off: district Q1 report as Word (.docx), in Tajik.

Data transcribed from the Ҳисоботҳо page screenshot (меъёр — Ч1).
School order = alphabetical (School.Meta.ordering = ['name']), so
МТУ №2 sits between №19 and №20 and the tail is №7, №8, №9 —
schools №3..№6 are not in the academic list.
"""
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, Cm

# (name, students, teachers, no_grades, met, not_met, fulfillment%,
#  gpa, excellent, ungraded)
ROWS = [
    ('Гимназияи ноҳияи Зафаробод', 450, 33, 2, 3, 28, 70.9, 7.84, 14, 321),
    ('Литсейи №1-и ноҳияи Зафаробод (Марказ)', 280, 29, 0, 1, 28, 76.7, 7.48, 0, 195),
    ('Литсейи №2-и ноҳияи Зафаробод (Меҳнатобод)', 301, 23, 0, 23, 0, 100.0, 7.59, 9, 0),
    ('Муассисаи таҳсилоти умумии №1', 1764, 92, 0, 6, 86, 81.6, 7.39, 63, 988),
    ('Муассисаи таҳсилоти умумии №10', 340, 30, 0, 19, 11, 94.2, 6.37, 0, 157),
    ('Муассисаи таҳсилоти умумии №11', 754, 48, 0, 6, 42, 88.1, 6.36, 0, 353),
    ('Муассисаи таҳсилоти умумии №12', 978, 53, 0, 0, 53, 79.3, 6.9, 7, 423),
    ('Муассисаи таҳсилоти умумии №13', 1400, 79, 2, 4, 73, 71.3, 6.74, 13, 1077),
    ('Муассисаи таҳсилоти умумии №14', 933, 55, 2, 3, 50, 84.6, 6.92, 21, 519),
    ('Муассисаи таҳсилоти умумии №15', 982, 62, 1, 9, 52, 79.4, 7.7, 23, 712),
    ('Муассисаи таҳсилоти умумии №16', 819, 58, 0, 4, 54, 88.2, 6.92, 4, 343),
    ('Муассисаи таҳсилоти умумии №17', 179, 23, 1, 4, 18, 72.8, 7.14, 0, 124),
    ('Муассисаи таҳсилоти умумии №18', 148, 15, 0, 4, 11, 91.3, 7.19, 0, 54),
    ('Муассисаи таҳсилоти умумии №19', 999, 58, 5, 0, 53, 67.7, 7.08, 15, 745),
    ('Муассисаи таҳсилоти умумии №2', 462, 39, 0, 39, 0, 100.0, 7.69, 26, 0),
    ('Муассисаи таҳсилоти умумии №20', 168, 20, 0, 20, 0, 100.0, 7.31, 4, 0),
    ('Муассисаи таҳсилоти умумии №21', 477, 38, 0, 9, 29, 90.5, 7.28, 27, 95),
    ('Муассисаи таҳсилоти умумии №22', 418, 27, 0, 2, 25, 84.0, 6.72, 0, 235),
    ('Муассисаи таҳсилоти умумии №23', 448, 35, 0, 33, 2, 99.9, 6.67, 0, 1),
    ('Муассисаи таҳсилоти умумии №24', 575, 37, 1, 20, 16, 93.6, 6.92, 7, 121),
    ('Муассисаи таҳсилоти умумии №25', 1513, 86, 0, 15, 71, 91.5, 7.16, 48, 416),
    ('Муассисаи таҳсилоти умумии №26', 1263, 71, 2, 0, 69, 65.2, 6.77, 1, 1087),
    ('Муассисаи таҳсилоти умумии №7', 536, 39, 0, 27, 12, 97.3, 6.64, 5, 21),
    ('Муассисаи таҳсилоти умумии №8', 236, 29, 0, 3, 26, 91.9, 7.48, 7, 54),
    ('Муассисаи таҳсилоти умумии №9', 265, 24, 0, 24, 0, 100.0, 7.33, 17, 0),
]

tot_students = sum(r[1] for r in ROWS)
tot_teachers = sum(r[2] for r in ROWS)
tot_no_grades = sum(r[3] for r in ROWS)
tot_met = sum(r[4] for r in ROWS)
tot_not_met = sum(r[5] for r in ROWS)
tot_excellent = sum(r[8] for r in ROWS)
tot_ungraded = sum(r[9] for r in ROWS)
avg_fulfill = sum(r[6] * r[2] for r in ROWS) / tot_teachers
avg_gpa = sum(r[7] * r[1] for r in ROWS) / tot_students
pct_ungraded = 100.0 * tot_ungraded / tot_students
pct_excellent = 100.0 * tot_excellent / tot_students


def fmt(x):
    return f'{x:.1f}'.replace('.', ',')


doc = Document()
style = doc.styles['Normal']
style.font.name = 'Times New Roman'
style.font.size = Pt(12)
for sec in doc.sections:
    sec.left_margin = sec.right_margin = Cm(1.5)


def para(text, bold=False, align=None, size=None):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    if size:
        r.font.size = Pt(size)
    if align:
        p.alignment = align
    return p


para('ҲИСОБОТ', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size=14)
para('оид ба ҳолати умумии ноҳия аз рӯйи мактабҳо дар чоряки якуми '
     'соли таҳсили 2026–2027',
     bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size=12)
para('(давра: 1 сентябр – 8 ноябр)',
     align=WD_ALIGN_PARAGRAPH.CENTER, size=11)
doc.add_paragraph()

para(f'Дар чоряки якуми соли таҳсили 2026–2027 дар {len(ROWS)} '
     f'муассисаи таълимии ноҳия {tot_students} нафар хонанда таҳсил '
     f'намуда, ба онҳо {tot_teachers} нафар омӯзгор дарс гузарондааст. '
     'Вазъи ҳар як муассиса ба ин монанд аст:')

# --- full table --------------------------------------------------------
headers = ('№', 'Мактаб', 'Хонандагон', 'Омӯзгорон',
           'Баҳо нагузоштаанд', 'Меъёрро иҷро кардаанд',
           'Қафо мондаанд', 'Иҷрои меъёр %', 'Холи миёна',
           'Аълохон', 'Бебаҳо')
table = doc.add_table(rows=len(ROWS) + 2, cols=len(headers))
table.style = 'Table Grid'
table.alignment = WD_TABLE_ALIGNMENT.CENTER
for i, h in enumerate(headers):
    c = table.cell(0, i)
    c.text = ''
    r = c.paragraphs[0].add_run(h)
    r.bold = True
    r.font.size = Pt(8)
    c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

for ri, row in enumerate(ROWS, start=1):
    vals = (str(ri), row[0], str(row[1]), str(row[2]), str(row[3]),
            str(row[4]), str(row[5]), fmt(row[6]) + '%',
            fmt(row[7]), str(row[8]), str(row[9]))
    for i, v in enumerate(vals):
        c = table.cell(ri, i)
        c.text = ''
        r = c.paragraphs[0].add_run(v)
        r.font.size = Pt(8)
        c.paragraphs[0].alignment = (
            WD_ALIGN_PARAGRAPH.LEFT if i == 1
            else WD_ALIGN_PARAGRAPH.CENTER)

last = table.rows[len(ROWS) + 1].cells
tot = ('', 'ҶАМЪ / миёна', str(tot_students), str(tot_teachers),
       str(tot_no_grades), str(tot_met), str(tot_not_met),
       fmt(avg_fulfill) + '%', fmt(avg_gpa), str(tot_excellent),
       str(tot_ungraded))
for i, v in enumerate(tot):
    last[i].text = ''
    r = last[i].paragraphs[0].add_run(v)
    r.bold = True
    r.font.size = Pt(8)
    last[i].paragraphs[0].alignment = (
        WD_ALIGN_PARAGRAPH.LEFT if i == 1 else WD_ALIGN_PARAGRAPH.CENTER)
doc.add_paragraph()

para('1. Вазъи умумии ноҳия', bold=True)
para(f'Аз {tot_teachers} нафар омӯзгорони ноҳия {tot_met} нафар '
     f'({100.0 * tot_met / tot_teachers:.1f}%) меъёри пурраи чорякро '
     f'иҷро кардаанд, {tot_not_met} нафар баҳо ворид намуда, вале то '
     f'меъёр нарасидаанд (қафо мондаанд), {tot_no_grades} нафар дар '
     'давоми чоряк умуман баҳо ворид накардаанд. Иҷрои меъёр дар '
     f'миқёси ноҳия ба ҳисоби миёна {fmt(avg_fulfill)}%-ро ташкил дод. '
     f'Холи миёнаи хонандагон — {fmt(avg_gpa)} аз 10 хол.')

para('2. Муассисаҳои пешрафта', bold=True)
para('Чоряки якумро бо натиҷаҳои комил анҷом доданд:')
doc.add_paragraph(
    'Литсейи №2 (Меҳнатобод) — иҷрои меъёр 100%, ҳар 23 омӯзгор '
    'меъёрро пурра иҷро кард, ягон хонандаи бебаҳо нест;',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №2 — 100%, ҳар 39 омӯзгор иҷро кард, ягон бебаҳо нест, '
    '26 нафар аълохон;',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №20 — 100%, ҳар 20 омӯзгор иҷро кард, ягон бебаҳо нест;',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №9 — 100%, ҳар 24 омӯзгор иҷро кард, ягон бебаҳо нест, '
    '17 нафар аълохон.',
    style='List Bullet')
para('Наздики пурра: МТУ №23 — 99,9% (33 иҷро, 2 қафо, танҳо 1 хонандаи '
     'бебаҳо); МТУ №7 — 97,3% (27 иҷро, 12 қафо); МТУ №10 — 94,2%.')

para('3. Муассисаҳои камбудиҳодошта', bold=True)
para('Пасттарин нишондиҳандаҳои иҷрои меъёр:')
doc.add_paragraph(
    'МТУ №26 — 65,2%: ягон омӯзгор меъёрро пурра иҷро накардааст '
    '(0 иҷро, 69 қафо, 2 нафар умуман баҳо нагузоштааст); 1087 нафар '
    'аз 1263 хонанда (86%) бебаҳо — баландтарин дар ноҳия;',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №19 — 67,7%: 5 нафар омӯзгор умуман баҳо нагузоштааст '
    '(бештарин дар ноҳия), ягон омӯзгор меъёрро пурра иҷро накардааст; '
    '745 нафар бебаҳо (75%);',
    style='List Bullet')
doc.add_paragraph(
    'Гимназияи ноҳия — 70,9%: 2 нафар бебаҳогузор, танҳо 3 нафар иҷро '
    'кардааст; 321 нафар бебаҳо (71%);',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №13 — 71,3%: 2 нафар бебаҳогузор, 73 нафар қафо; 1077 нафар '
    'бебаҳо (77%);',
    style='List Bullet')
doc.add_paragraph(
    'МТУ №17 — 72,8%: 1 нафар бебаҳогузор; 124 нафар аз 179 хонанда '
    '(69%) бебаҳо.',
    style='List Bullet')

para('4. Хонандагони аълохон ва бебаҳо', bold=True)
para(f'Дар саросари ноҳия {tot_excellent} нафар хонанда '
     f'({pct_excellent:.1f}%) дар ҳамаи фанҳо баҳои баланд гирифта, '
     'дар сафи аълохон қарор доранд. Бештарин: МТУ №1 — 63 нафар, '
     'МТУ №25 — 48 нафар, МТУ №21 — 27 нафар, МТУ №2 — 26 нафар, '
     'МТУ №15 — 23 нафар.')
para(f'{tot_ungraded} нафар хонанда ({pct_ungraded:.1f}% аз ҳамаи '
     'хонандагони ноҳия) дар чоряки якум ҳадди ақал аз як фан баҳои '
     'чоряк надоранд. Бештарин ҳиссаро МТУ №26 (1087 нафар), МТУ №13 '
     '(1077), МТУ №1 (988), МТУ №19 (745) ва МТУ №15 (712) ташкил '
     'медиҳанд. Ин аслии сабаби паст будани натиҷаҳои ноҳия аст — '
     'омӯзгорон на ба ҳамаи хонандагон баҳои якунии чоряк гузоштаанд.')

para('5. Омӯзгорони бебаҳогузор', bold=True)
para(f'Ҳамагӣ {tot_no_grades} нафар омӯзгор дар давоми чоряк умуман '
     'баҳо ворид накардаанд: МТУ №19 — 5 нафар; Гимназия, МТУ №13, '
     '№14, №26 — 2 нафар; МТУ №15, №17, №24 — 1 нафар.')

para('Хулоса ва пешниҳодҳо', bold=True)
doc.add_paragraph(
    'Нисбат ба муассисаҳои №26, №19, №13, Гимназия ва №17 тафтишоти '
    'сабабҳои иҷрои сусти меъёр гузаронида шавад;',
    style='List Number')
doc.add_paragraph(
    'Ба 16 нафар омӯзгори бебаҳогузор тавассути роҳбарияти муассиса '
    'эслатмаи расмӣ дода шавад;',
    style='List Number')
doc.add_paragraph(
    'МТУ №19 ва №26, ки ягон омӯзгори пурраиҷро надоранд, назорати '
    'махсус гирифта шаванд;',
    style='List Number')
doc.add_paragraph(
    'Вазъи хонандагони бебаҳо то анҷоми чоряки дуюм дар ҳамаи '
    'муассисаҳо барқарор гардида, ба ҳамаи хонандагон баҳои чоряк '
    'гузошта шавад;',
    style='List Number')
doc.add_paragraph(
    'Таҷрибаи муассисаҳои пешрафта (Литсейи №2, МТУ №2, №20, №9) '
    'дар мадҳияи омӯзгорон умумикарда шавад.',
    style='List Number')

doc.add_paragraph()
doc.add_paragraph()
para('Мудири шуъбаи маорифи ноҳия ____________________')
para('«___» ____________ 2026 с.')

out = 'Ҳисобот_ноҳия_чоряки_якум_2026-2027.docx'
doc.save(out)
import os
print('saved:', os.path.abspath(out).encode('unicode_escape').decode())
print(f'schools={len(ROWS)} students={tot_students} '
      f'teachers={tot_teachers} met={tot_met} not_met={tot_not_met} '
      f'no_grades={tot_no_grades} excellent={tot_excellent} '
      f'ungraded={tot_ungraded} fulfill={avg_fulfill:.1f} '
      f'gpa={avg_gpa:.2f}')
