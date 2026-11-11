"""One-off: generate a Word report for Q1 district results (Tajik)."""
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

doc = Document()

style = doc.styles['Normal']
style.font.name = 'Times New Roman'
style.font.size = Pt(12)

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
para('оид ба ҳолати муассисаҳои таълимии ноҳия дар чоряки якуми '
     'соли таҳсили 2026–2027',
     bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, size=12)
doc.add_paragraph()

para('Дар давраи чоряки якуми соли таҳсили 2026–2027 '
     '(аз 1 сентябр то 8 ноябр) фаъолияти таълимӣ дар муассисаҳои '
     'таълимии ноҳия чунин ҷараён дошт:')

# --- table -------------------------------------------------------------
headers = ('№', 'Мактаб', 'Хонандагон', 'Омӯзгорон',
           'Баҳо нагузоштаанд', 'Меъёрро иҷро кардаанд',
           'Қафо мондаанд', 'Иҷрои меъёр %', 'Холи миёна',
           'Аълохон', 'Бебаҳо')
row_data = ('1', 'Гимназияи ноҳияи Зафаробод', '450', '33', '2', '3',
            '28', '70,9%', '7,84', '14', '321')

table = doc.add_table(rows=2, cols=len(headers))
table.style = 'Table Grid'
table.alignment = WD_TABLE_ALIGNMENT.CENTER
for i, h in enumerate(headers):
    c = table.cell(0, i)
    c.text = ''
    r = c.paragraphs[0].add_run(h)
    r.bold = True
    r.font.size = Pt(9)
    c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
for i, v in enumerate(row_data):
    c = table.cell(1, i)
    c.text = ''
    r = c.paragraphs[0].add_run(v)
    r.font.size = Pt(10)
    c.paragraphs[0].alignment = (
        WD_ALIGN_PARAGRAPH.LEFT if i == 1 else WD_ALIGN_PARAGRAPH.CENTER)
doc.add_paragraph()

para('1. Шумораи хонандагон ва омӯзгорон', bold=True)
para('Дар Гимназияи ноҳияи Зафаробод 450 нафар хонанда таҳсил '
     'менамояд, ки дар чоряки якум ба онҳо 33 нафар омӯзгор дарс '
     'гузарондааст.')

para('2. Вазъи ворид кардани баҳоҳо', bold=True)
para('Аз 33 нафар омӯзгорон:')
doc.add_paragraph(
    '3 нафар омӯзгор меъёри чорякро пурра иҷро кардаанд — ҳар як '
    'хонандаи онҳо меъёри пурраи чорякро гирифтааст;',
    style='List Bullet')
doc.add_paragraph(
    '28 нафар омӯзгор баҳо ворид намудаанд, вале то меъёри пурра '
    'нарасидаанд (қафо мондаанд);',
    style='List Bullet')
doc.add_paragraph(
    '2 нафар омӯзгор дар давоми чоряки якум умуман баҳо ворид '
    'накардаанд.',
    style='List Bullet')
para('Иҷрои меъёр ба ҳисоби миёна 70,9 фоизро ташкил дод.')

para('3. Сифати таҳсил', bold=True)
para('Холи миёнаи хонандагон дар чоряки якум 7,84-ро ташкил дод '
     '(аз 10 хол).')

para('4. Хонандагони аълохон', bold=True)
para('14 нафар хонанда дар ҳамаи фанҳо баҳои баланд гирифта, дар сафи '
     'аълохонҳо қарор доранд.')

para('5. Хонандагони бебаҳо', bold=True)
para('321 нафар хонанда (71,3 фоиз аз ҳамаи хонандагон) дар чоряки '
     'якум ҳадди ақал аз як фан баҳои чоряк надоранд. Ин нишондиҳанда '
     'баланд буда, асосан аз он сабаб аст, ки 28 нафар омӯзгор меъёри '
     'пурраи чорякро иҷро накардаанд ва на ба ҳамаи хонандагон баҳои '
     'якунии чоряк гузошта шудааст.')

para('Хулоса ва пешниҳодҳо', bold=True)
doc.add_paragraph(
    'Нисбат ба 2 нафар омӯзгоре, ки дар давоми чоряк умуман баҳо '
    'ворид накардаанд, чораи назоратӣ андешидал ва эслатма дода шавад;',
    style='List Number')
doc.add_paragraph(
    'Ба 28 нафар омӯзгорони қафомонда ҳадд вазифа гузошта шавад, ки '
    'дар чоряки дуюм меъёри пурраро иҷро кунанд ва ба ҳамаи '
    'хонандагон баҳои чоряк гузоранд;',
    style='List Number')
doc.add_paragraph(
    'Роҳбарияти муассиса вазъи ворид кардани баҳоҳоро ҳар ҳафта тавассути '
    'саҳифаи «Назорат»-и портал назорат намояд;',
    style='List Number')
doc.add_paragraph(
    'Ҳолати хонандагони бебаҳо то охири чоряки дуюм назорати махсус '
    'гирифта шавад.',
    style='List Number')

doc.add_paragraph()
doc.add_paragraph()
para('Мудири шуъбаи маорифи ноҳия ____________________')
para('«___» ____________ 2026 с.')

out = 'Ҳисобот_чоряки_якум_2026-2027.docx'
doc.save(out)
print('saved:', out)
