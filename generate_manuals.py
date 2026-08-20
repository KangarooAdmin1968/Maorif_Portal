from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

BASE_DIR = Path(__file__).resolve().parent


def set_run(run, size=11, bold=False, color=None, name='Calibri'):
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)


def add_title(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    set_run(run, size=18, bold=True, color=(0, 51, 102))
    p.space_after = Pt(12)


def add_heading2(doc, text):
    p = doc.add_heading(level=2)
    run = p.add_run(text)
    set_run(run, size=14, bold=True, color=(0, 51, 102))
    p.space_before = Pt(12)
    p.space_after = Pt(6)


def add_bullet(doc, text):
    p = doc.add_paragraph(style='List Bullet')
    run = p.add_run(text)
    set_run(run)
    p.space_after = Pt(4)


def add_paragraph(doc, text, bold=False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    set_run(run, bold=bold)
    p.space_after = Pt(6)


def add_note(doc, title, body):
    table = doc.add_table(rows=1, cols=1)
    table.autofit = False
    table.allow_autofit = False
    table.columns[0].width = Inches(6)
    cell = table.cell(0, 0)
    p = cell.paragraphs[0]
    r = p.add_run(title)
    set_run(r, bold=True, color=(133, 100, 4))
    p2 = cell.add_paragraph(body)
    set_run(p2.runs[0] if p2.runs else p2.add_run(body))
    shading = OxmlElement('w:shd')
    shading.set(qn('w:fill'), 'FFF3CD')
    cell._tc.get_or_add_tcPr().append(shading)
    doc.add_paragraph()


def add_table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = 'Table Grid'
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        p = hdr_cells[i].paragraphs[0]
        r = p.add_run(h)
        set_run(r, bold=True, color=(255, 255, 255))
        shading = OxmlElement('w:shd')
        shading.set(qn('w:fill'), '003366')
        hdr_cells[i]._tc.get_or_add_tcPr().append(shading)
    for row in rows:
        cells = table.add_row().cells
        for i, v in enumerate(row):
            p = cells[i].paragraphs[0]
            r = p.add_run(str(v))
            set_run(r)
    doc.add_paragraph()


def build_zavuch_manual():
    doc = Document()
    add_title(doc, 'Дастури истифодабарии Портали ягонаи маорифи ноҳияи Зафаробод')
    add_title(doc, '(Барои ҳамоҳангсозон ва муовинони директор)')
    add_paragraph(doc, 'Ин дастур ба муовинон ва ҳамоҳангсозони муассисаҳо (завучҳо) ёрӣ медиҳад, ки ба таври самаранок аз портали ягонаи маорифи ноҳияи Зафаробод истифода баранд. Дар инҷо қадам-ба-қадам шарҳ дода шудааст, ки чӣ тавр ба тизим ворид шудан, синфҳоро идора кардан, хонандагон ва омӯзгоронро ворид намудан, дарсҳоро тақсим кардан ва холгузорӣ бурд.')

    add_heading2(doc, '1. Вуруд ба тизим')
    add_paragraph(doc, 'Барои ворид шудан ба портал чунин амалҳоро анҷом диҳед:')
    add_bullet(doc, 'Браузери худро кушода, суроғаи порталро нависед.')
    add_bullet(doc, 'Дар саҳифаи вуруд, логин ва рамзи ҳисоби завучи худро ворид кунед.')
    add_bullet(doc, 'Логинҳои маъмул ба шакли zavuch_M{id} мебошанд, масалан zavuch_M1, zavuch_M2 ва ғ.')
    add_bullet(doc, 'Рамз ҳангоми сохтани ҳисоб дода шудааст. Агар рамзро фаромӯш карда бошед, бо администратор Оҳсар Дӯсарович тамос гиред.')
    add_table(doc, ['Логин', 'Рамзи намоишӣ', 'Рол'], [
        ['zavuch_M1', 'Zavuch_M1@2026', 'Ҳамоҳангсоз'],
        ['zavuch_M5', 'Zavuch_M5@2026', 'Ҳамоҳангсоз'],
    ])
    add_note(doc, 'Эзоҳи муҳим:', 'Логин ва рамзро бо дигарон нашарик кунед. Дар сурати шубҳа дар бехатарии ҳисоб, дарҳол онро иваз намоед.')

    add_heading2(doc, '2. Идоракунии Синфҳо')
    add_paragraph(doc, 'Пас аз вуруд, аз менюи асосӣ "Муассисаҳо"-ро интихоб карда, муассисаи худро кушоед. Сипас "Синфҳо"-ро зер кунед.')
    add_bullet(doc, 'Саҳифаи Синфҳо рӯйхати синфҳои муассисаро нишон медиҳад.')
    add_bullet(doc, 'Ҳар синф аз рӯи хонандагоне, ки ворид карда шудаанд, ба таври худкор пайдо мешавад.')
    add_bullet(doc, 'Барои дидани рӯйхати хонандагони синф, дар сатри он синф тугмаи "Холҳо"-ро зер кунед.')
    add_bullet(doc, 'Барои иловаи синфи нав, аввал хонандагони он синфро ё дастӣ ворид кунед, ё шаблони Excel-ро истифода баред.')

    add_heading2(doc, '3. Боргирии шаблони хонандагон (Excel) ва воридоти он')
    add_paragraph(doc, 'Портал шаблони оқилонаи Excel дорад, ки рақами хонандаро ба таври худкор месозад.')
    add_bullet(doc, 'Аз саҳифаи "Синфҳо" ё саҳифаи ҷузъиёти синф, тугмаи "Боргирии шаблон"-ро зер кунед.')
    add_bullet(doc, 'Файли Excel-ро кушода, танҳо сутунҳои "Ному насаб" (C) ва "Синф" (D) -ро пур кунед.')
    add_bullet(doc, 'Сутунҳои "№ мактаб" (A) ва "№ синф" (B) дорои формула ҳастанд. Онҳоро тағйир надиҳед.')
    add_bullet(doc, 'Баъди пур кардан, файлро нигоҳ дошта, ба портал боз гардед.')
    add_bullet(doc, 'Тугмаи "Воридоти Excel"-ро зер карда, файлро интихоб ва бор кунед.')
    add_table(doc, ['Амал', 'Сутун', 'Шарҳ'], [
        ['Боргирӣ', 'A', 'Формулаи рақами мактаб - ғайрифаъол аст'],
        ['Боргирӣ', 'B', 'Формулаи рақами синф - ғайрифаъол аст'],
        ['Пур кардан', 'C', 'Ному насаби хонанда'],
        ['Пур кардан', 'D', 'Синф, масалан 11-А, 10-Б'],
    ])
    add_note(doc, 'Эзоҳ:', 'Агар шаблонро бо телефон ё барномаҳои дигари Excel кушодед, аз тағйир додани сутунҳои A ва B худдорӣ кунед. Ин сутунҳо барои ҳифзи формулаҳо қулф карда шудаанд.')

    add_heading2(doc, '4. Тезкор ворид кардани Омӯзгорон')
    add_paragraph(doc, 'Барои вориди якбораи омӯзгорон, аз саҳифаи "Омӯзгорон" истифода баред:')
    add_bullet(doc, 'Тугмаи "Шаблони омӯзгорон"-ро зер кунед. Файли Shabloni_Omuzgoron.xlsx бор гирифта мешавад.')
    add_bullet(doc, 'Сутуни A дорои формулаи рақами худкор аст, онро тағйир надиҳед.')
    add_bullet(doc, 'Сутунҳои B, C, D ва E-ро бо иттилоои омӯзгор пур кунед: ном, телефон, маълумот ва ихтисос.')
    add_bullet(doc, 'Файлро нигоҳ дошта, тавассути тугмаи "Воридоти омӯзгорон" бор кунед.')
    add_bullet(doc, 'Пас аз ворид, логин ва рамзи ҳар омӯзгор намоиш дода мешавад. Ин рӯйхатро чоп кунед ё нигоҳ доред.')
    add_note(doc, 'Маслиҳат:', 'Номи омӯзгорро бо ҳарфи калон нависед, масалан "Раҷабов А.", то низоми ҷустуҷӯ дуруст кор кунад.')

    add_heading2(doc, '5. Тақсимоти дарсҳо')
    add_paragraph(doc, 'Барои таъин кардани омӯзгор ба ҳар фан ва синф:')
    add_bullet(doc, 'Аз менюи боло "Тақсимоти дарсҳо"-ро интихоб кунед.')
    add_bullet(doc, 'Дар ҷадвал, дар ҳар сатр синф, фан ва рӯйхати омӯзгорон нишон дода мешавад.')
    add_bullet(doc, 'Аз менюи кушодашаванда дар сутуни "Омӯзгори масъул", омӯзгори дилхоҳро интихоб кунед.')
    add_bullet(doc, 'Барои ҳар як сатр, ки тағйир меёбад, интихобро анҷом диҳед.')
    add_bullet(doc, 'Тугмаи "Сабт кардан"-ро зер кунед. Пас аз он паёми муваффақият намоиш дода мешавад.')
    add_note(doc, 'Эзоҳ:', 'Танҳо омӯзгорони муассисаи худатон дар рӯйхати интихоб нишон дода мешаванд.')

    add_heading2(doc, '6. Журнал ва холгузорӣ')
    add_paragraph(doc, 'Омӯзгорон пас аз вуруд метавонанд журнали холгузории худро кушоянд:')
    add_bullet(doc, 'Омӯзгор бо логини худ ворид мешавад.')
    add_bullet(doc, 'Синф ва фани вобастаро аз рӯйхати худ интихоб мекунад.')
    add_bullet(doc, 'Дар саҳифаи журнал, холҳои хонандагон, ҳозирӣ ва хулқ-атвор сабт карда мешаванд.')
    add_bullet(doc, 'Холҳо ба таври онлайн нигоҳ дошта мешаванд ва зуд ба рейтинги синф таъсир мерасонанд.')
    add_bullet(doc, 'Агар зарурат бошад, омӯзгор метавонад холҳои чорякро ҳисоб карда, ба низом ворид намояд.')

    path = BASE_DIR / 'Дастури_истифодабарии_Портали_Маориф_барои_Завуч.docx'
    doc.save(path)
    print('Saved manual')


def build_admin_manual():
    doc = Document()
    add_title(doc, 'Зафаробод тумани маориф бўлими ягона веб-порталидан фойдаланиш бўйича умумий йўриқнома')
    add_title(doc, '(Администратор ва Маориф мудири учун)')
    add_paragraph(doc, 'Ушбу йўриқнома Зафаробод тумани маориф бўлимининг ягона веб-порталини бошқариш, созлаш ва хавфсиз ишлатиш учун тайёрланди. Унда сервер ҳақидаги малумотлар, администраторлик ваколатлари ва GitHub Desktop орқали захиралаш кўрсатмалари келтирилган.')

    add_heading2(doc, '1. Сервер созламалари')
    add_paragraph(doc, 'Лойиҳа қайси каталогда жойлашганлигини ва асосий бошқарув буйруқларини биласиз:')
    add_bullet(doc, 'Лойиҳа каталоги: D:\\Loihalar\\Maorif_Portal')
    add_bullet(doc, 'Маҳаллий ишлаб чиқиш серверини ишга тушириш: .\\venv\\Scripts\\python manage.py runserver')
    add_bullet(doc, 'Миграцияларни яратиш: .\\venv\\Scripts\\python manage.py makemigrations')
    add_bullet(doc, 'Миграцияларни қўллаш: .\\venv\\Scripts\\python manage.py migrate')
    add_bullet(doc, 'Админ панелга кириш: /admin/')
    add_table(doc, ['Буйруқ', 'Маъноси'], [
        ['python manage.py check', 'Тизим соғлигини текшириш'],
        ['python manage.py makemigrations', 'Янги миграцияларни яратиш'],
        ['python manage.py migrate', 'Миграцияларни базага қўллаш'],
        ['python manage.py runserver', 'Маҳаллий серверни ишга тушириш'],
    ])
    add_note(doc, 'Эслатма:', 'Серверни янгилашдан аввал `.venv\\pyvenv.cfg` файлини текширинг. Агар `python.exe` манзили ўзгарган бўлса, уни тўғрилаб, венвни янгидан фаоллаштиринг.')

    add_heading2(doc, '2. Маориф мудири ваколатлари')
    add_paragraph(doc, 'Маориф бўлими мудири порталда умумий рейтинг ва статистикаларни кўриши мумкин:')
    add_bullet(doc, 'Асосий панелда умумий миқдорлар: муассисалар, хонандалар, омӯзгорлар ва нисбат кўрсатилган.')
    add_bullet(doc, 'Рейтинги муассисаҳо: ҳар бир мактабнинг холҳои миёнаи ғайрирасмӣ.')
    add_bullet(doc, 'Рейтинги фанҳо: ҳар бир фан бўйича туман миқёсида хол.')
    add_bullet(doc, 'Рейтинги синфҳо: синфлар бўйича ноҳиявий ва муассисаи ҷой.')
    add_bullet(doc, 'Маориф мудири бутун ноҳия миқёсидаги маълумотларни филтр ва кузатиш имкониятига эга.')
    add_note(doc, 'Эслатма:', 'Рейтинглар кунлик холгузорилар ва чораклик холларга асосланиб автоматик ҳисобланади.')

    add_heading2(doc, '3. Администраторлик ваколатлари ва Админ Панел')
    add_paragraph(doc, 'Портални техник ривожлантирувчи ва администратор Оҳсар Дӯсарович бошқариб туриши лозим. Унинг асосий вазифалари:')
    add_bullet(doc, 'Браузерда /admin/ манзилига кириш.')
    add_bullet(doc, 'Superuser ҳисоби ёки администратор логини билан кириш.')
    add_bullet(doc, 'Муассисалар рўйхатини қўшиш, таҳрирлаш ва ўчириш.')
    add_bullet(doc, 'Корбарлар, UserProfile ва TeacherProfile записларини бошқариш.')
    add_bullet(doc, 'Синфлар, фанлар, хонандалар ва омӯзгорларни қўlda тиклаш ёки тузатиш.')
    add_bullet(doc, 'Дарс тақсимоти, чорак баҳолари ва хулқ-атвор маълумотларини назорат қилиш.')
    add_bullet(doc, 'Агар Maълумотлар базасида хатолик юз берса, SQLite файлини ёки захира нусхасини тиклаш.')
    add_table(doc, ['Объект', 'Бошқариш'], [
        ['School', 'Муассиса маълумотлари'],
        ['User / UserProfile', 'Корбар ҳисоб ва роллари'],
        ['TeacherProfile', 'Омӯзгор профиллари'],
        ['ClassSubject', 'Синф-фан ва омӯзгор алоқалари'],
        ['Student / Grade', 'Хонандар ва холлар'],
    ])
    add_note(doc, 'Хавфсизлик:', 'Админ панелга фақат ваколатли шахслар кириши керак. IP фильтр ёки локал тarmoqlanish орқали мураккаблаштириш тавсия этилади.')

    add_heading2(doc, '4. Хавфсизлик ва Заҳиралаш')
    add_paragraph(doc, 'Лойиҳа файларини GitHub Desktop ёки Git буйруқлари орқали таққослаш ва захиралаш зарур:')
    add_bullet(doc, 'GitHub Desktop ёки Git маҳаллий орнатилганлигини текширинг.')
    add_bullet(doc, 'Loyiҳа каталогида `git status` натижасини кўриб, ўзгартирилган файлларни тасдиқланг.')
    add_bullet(doc, 'Har bir muhim ўзгаришдан кейин commit қилинг. Масалан: "Yangi shablon va manual qo\'shildi".')
    add_bullet(doc, 'Commit xabarларида нима ва нега ўзгартирилганлиги кўрсатилсин.')
    add_bullet(doc, 'Push тугмаси ёки `git push` орқали масофавий захира (remote repository) га юборинг.')
    add_bullet(doc, 'db.sqlite3, schools.db ва media каталогини ҳам вақти-вақти билан нусха олиб туриш тавсия этилади.')
    add_table(doc, ['Амал', 'Буйруқ ёки ҳаракат'], [
        ['Ўзгаришларни кўриш', 'git status'],
        ['Индексация', 'git add .'],
        ['Сақлаш', 'git commit -m \"\u042f\u043d\u0433\u0438 \u045e\u0437\u0433\u0430\u0440\u0442\u0438\u0448\u043b\u0430\u0440\"'],
        ['Юбориш', 'git push origin main'],
    ])
    add_note(doc, 'Эслатма:', 'Сирли маълумотлар (пароллар, калитлар) гитга тарқалмаслиги учун `.gitignore` файлини текширинг.')

    path = BASE_DIR / 'Веб_Портал_Админ_Умумий_Йўриқномаси.docx'
    doc.save(path)
    print('Saved manual')


if __name__ == '__main__':
    build_zavuch_manual()
    build_admin_manual()
