"""Verified teacher tariff rates for the Tabel suggestion feature.

The absolute somoni rate table is not published in open legal texts, so
this module stores per-teacher rates read from real school tariffication
documents (Варақаи тарифбандӣ / monthly табел) supplied by deputy
principals. Each school's data is keyed by its school number (the same
value ``utils.get_school_number`` returns) and tagged with the pay-scale
period the document belongs to.

Official raise chain (verified against legal sources):

* до 31.08.2025 — old scale (documented by МТМУ №20 tariffication
  for the 2024-2025 school year).
* аз 01.09.2025 — Укази Президент №962 (08.02.2025) ва Қарори Ҳукумат
  №113 (28.02.2025): маоши кормандони томактабӣ ва умумтаълим +30%
  (ҳамаи қиматҳои варақаҳо дақиқан ×1.3 мешаванд).
* аз 01.09.2026 — Укази Президент №1132 (14.01.2026) ва Қарори Ҳукумат
  №72 (26.02.2026): +25% барои ҳамин гурӯҳ, маоши ҳадди ақал 1300 сомонӣ.
  Қиматҳои дақиқи округшудаи ҷадвали нав тасдиқ нашудаанд — ҳар
  пешниҳоди аз ин давра ҳисобшуда ҳамчун «derived» нишон дода мешавад,
  на ҳамчун қимати тасдиқшуда.

Sources (verified identity and period, read-only extraction):

* '3'  — МТМУ №3, табели моҳи августи 2026 (устуни D) — шкалаи 2025-26.
* '11' — МТМУ №11 Зафаробод, «Варакаи тарифбандӣ барои соли хониши
  2025-2026» (ба ҳолати 1 сентябри 2025) — шкалаи 2025-26.
* '20' — МТМУ №20 Зафаробод, варақаи тарифбандӣ барои соли хониши
  2024-2025 — шкалаи кӯҳна (то 01.09.2025).

Only the teacher's first (primary) ставка line is stored. Rates are
strings to keep exact values; they are converted at lookup time.
"""
import re
from decimal import Decimal, ROUND_HALF_UP

from .utils import get_school_number

# ---------------------------------------------------------------------------
# Pay-scale periods and conversion between them.
# ---------------------------------------------------------------------------
PERIOD_PRE_2025 = 'pre-2025'   # то 31.08.2025
PERIOD_2025_26 = '2025-26'     # 01.09.2025 – 31.08.2026 (+30%, Указ №962)
PERIOD_2026_27 = '2026-27'     # аз 01.09.2026 (+25%, Указ №1132)

# Rate expressed on each scale relative to the pre-2025 scale.
_SCALE_INDEX = {
    PERIOD_PRE_2025: Decimal('1'),
    PERIOD_2025_26: Decimal('1.3'),
    PERIOD_2026_27: Decimal('1.625'),   # 1.3 * 1.25
}


def rate_period(year, month):
    """Pay-scale period a tabel month belongs to."""
    if (year, month) >= (2026, 9):
        return PERIOD_2026_27
    if (year, month) >= (2025, 9):
        return PERIOD_2025_26
    return PERIOD_PRE_2025


def _convert(rate, from_period, to_period):
    """Re-express a rate recorded on one scale in another scale's terms."""
    value = Decimal(rate) * _SCALE_INDEX[to_period] / _SCALE_INDEX[from_period]
    return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Name matching — the workbooks (and possibly DB profiles) were typed on
# several keyboards/encodings, so the same letters appear as ь/ы/њ→ҳ,
# ї→ӣ, љ→ҷ, ќ→қ, ѓ→ғ, ў→ӯ, щ→ш and the frequent interchange pairs
# х↔ҳ, у↔ӯ, ч↔ҷ, ж↔ҷ, э↔е, ё↔е. The fold is applied identically to the
# stored names and to profile names, so a fold can only ever merge — the
# uniqueness guard below drops any collision.
# ---------------------------------------------------------------------------
_FOLD = str.maketrans({
    'ь': 'ҳ', 'њ': 'ҳ', 'ы': 'ҳ', 'ї': 'ӣ', 'љ': 'ҷ', 'ќ': 'қ',
    'ѓ': 'ғ', 'ў': 'ӯ', 'щ': 'ш', 'х': 'ҳ', 'у': 'ӯ', 'ч': 'ҷ',
    'ж': 'ҷ', 'ё': 'е', 'э': 'е', 'є': 'е', 'і': 'и',
})

_NON_LETTER = re.compile(r'[^а-яёӯҳқғҷӣўa-z]+')
_TRAILING_DIGITS = re.compile(r'\d.*$')


def normalize_rate_name(name):
    """Normalized token tuple used for teacher matching.

    Birth dates and any digits are cut (tariffication forms append
    'dd.mm.yyyy' after the name), punctuation is dropped, legacy-encoding
    characters are folded, and case is ignored.
    """
    text = _TRAILING_DIGITS.sub('', name or '').casefold()
    text = _NON_LETTER.sub(' ', text.translate(_FOLD))
    return tuple(text.split())


def _name_match(profile_key, entry_key):
    """True when the two names agree on every token of the shorter one.

    Production profiles often store 'Фамилия Имя' while the tariffication
    carries 'Фамилия Имя Отчество' — a prefix match covers that, but a
    one-token name is never enough to identify a person.
    """
    if not profile_key or not entry_key:
        return False
    shorter, longer = sorted((profile_key, entry_key), key=len)
    if len(shorter) < 2:
        return shorter == longer
    return longer[:len(shorter)] == shorter


# ---------------------------------------------------------------------------
# Verified per-school rates. Keys are source-document names; values are
# the recorded ставка as a string ('' entries were dropped at extraction —
# a missing rate is never invented).
# ---------------------------------------------------------------------------
VERIFIED_TARIFFS = {
    # МТМУ №3 — табели августи 2026, устуни D (шкалаи 2025-26).
    '3': {
        'period': PERIOD_2025_26,
        'rates': {
            'Абдухолиқава Малика Низрмжоновна': '1889',
            'Абдухолиқова Улбашой Турдиназаровна': '1889',
            'Авеззода Умедаи Субҳиддин': '1602',
            'Авезова Парвона Авазмаҳмадовна': '1602',
            'Азимова Карима Толибовна': '1602',
            'Арбобиён Дилором': '1602',
            'Арбобова Саврижон Щозибековна': '2393',
            'Аслонова Дилафруз Орзуқуловна': '1889',
            'Ашуров Щуфрон Қурбонивич': '2479',
            'Аюбова Дилором Жамшедовна': '1889',
            'Бердиева Фариза Абдуқодировна': '1889',
            'Буриева Аслия Қамбаровна': '2094',
            'Буронова Гулсара Бозоровна': '1889',
            'Джураева Шоира Хусиновна': '2699',
            'Зокиров Маҳадзокир Собирович': '1889',
            'Илҳина Марина Юрҳевна': '2197',
            'Исмоилзода Нафиса Насрулло': '1602',
            'Камолова Парвина Қобилжоновна': '2094',
            'Кучимова Исмигул Шуҳратовна': '2094',
            'Кучимова Шаҳноза Музаффаровна': '1889',
            'Мамуров Шаҳзоджон Бахтиёрович': '1889',
            'Мансуров Ҳидоят Зикриёевич': '2479',
            'Мансурова Бибиолима Ҳидоятовна': '1803',
            'Муродова Бибиибодат Ҳидоятовна': '2094',
            'Муродова Замира Абдусалимовна': '1602',
            'Муҳиддинова Шоиста Садриддиновна': '2393',
            'Назарова Нигора Холбобоевна': '2094',
            'Назарова Элсевар Гулмуротовна': '1803',
            'Сангинова Рухсора Мунаваровна': '2699',
            'Собирова Ҳуснижаҳон Ниёзбековна': '1702',
            'Тошова Нигора Бобоевна': '1803',
            'Турдибекова Нигора Сатторовна': '2197',
            'Шарипов Фаррух Алижонович': '1889',
            'Қамарова Шохсанам Қамаровна': '2393',
            'Қодирова Саида Абдулхаевна': '2197',
            'Қурбонов Фаромуз Жамшедович': '2479',
            'Қурчиева Мавжуда Аловаддиновна': '2094',
            'Ҳамроқулова Маҳбуба Отақузиевна': '2479',
        },
    },
    # МТМУ №11 Зафаробод — варақаи тарифбандӣ барои соли хониши 2025-2026
    # (ба ҳолати 1 сентябри 2025), устуни H.
    '11': {
        'period': PERIOD_2025_26,
        'rates': {
            'Авалбоева Гулнора': '2699',
            'Авезова Кибриё': '1889',
            'Акрамов Эркин': '1889',
            'Аминова Дилноза': '2094',
            'Бойматова Холида': '1602',
            'Бутаев Абдусамад': '2699',
            'Ғафурзода Хуснорои Каримкул': '1702',
            'Дустмуродов Лоиҳ': '2479',
            'Ҷумаев Умарбой': '2393',
            'Ибрагимова Дилфуза': '2197',
            'Ибрагимова Насиба': '1989',
            'Ибрагимова Озода': '2699',
            'Иброҳимова Обида': '2094',
            'Исоқова Манзура': '2699',
            'Исоқова Насиба': '2197',
            'Каттабеков Отабек Умирзокович': '2479',
            'Кенҷаева Гулсара': '2393',
            'Кенҷаева Дилноза': '1602',
            'Қудратов Фирдавс': '2479',
            'Мавлонова Рузиҷон': '1121',
            'Мавлонова Ҳилола': '1602',
            'Мамарасулов Аҳмад': '1602',
            'Мамарасулова Ойзода': '1602',
            'Муминзода Гулҷаҳон': '2094',
            'Муминов Абдузафар': '2479',
            'Муминова Қурбонгул': '2197',
            'Носирова Азиза': '2393',
            'Ортиқбоева Фируза': '2197',
            'Раҳимов Миразамат': '2197',
            'Раҳмонова Дилфуза': '2197',
            'Табурова Назира': '2699',
            'Урунова Фотима': '2197',
            'Усарова Фарида': '1889',
            'Усмонова Каромат': '2197',
            'Усмонова Шоҳсанам': '1702',
            'Холбекова Чиннигул': '1602',
            'Хушвақтова Гулноза': '1702',
            'Шерматова Гулнора': '1602',
            'Шосаидзода Лолагул': '1889',
            'Шафорова Жасмина': '1889',
            'Шафуров Каримқул': '1803',
            'Ҳаҳорова Ҳулкар': '2094',
            'Қаюмова Зиёда': '2197',
            'Қаюмова Моҳира': '2699',
            'Ҳодиров Фарҳод': '2479',
            'Ҳудратов Бозорбой': '2479',
            'Ҳусейинзода Зарафшон': '2094',
            'Юлдошева Маҳбуба': '2479',
        },
    },
    # МТМУ №20 Зафаробод — варақаи тарифбандӣ барои соли хониши 2024-2025
    # (шкалаи кӯҳна, то +30% аз 01.09.2025), устуни H.
    '20': {
        'period': PERIOD_PRE_2025,
        'rates': {
            'Абдулхамидова Дилором Абдухакимовна': '2076',
            'Арзиева Ҳилола Абдуҳаётовна': '1453',
            'Аҳмедова Баҳринисо Облоқуловна': '2076',
            'Аҳмедова Раҳшона Муҳторовна': '1907',
            'Бердиев Жасур Шукруллоевич': '1690',
            'Бобохонова Замира Темуралиевна': '1841',
            'Зокиров Усар Дусарович': '1907',
            'Зокирова Озода': '1453',
            'Иброҳимова Парвина Исломбоевна': '1453',
            'Калонхӯҷаева Моҳира Додоевна': '1690',
            'Каримова Заррина Бердикуловна': '1611',
            'Кенҷаев Орзиқул Муртазоқулович': '1453',
            'Маҳмудова Дилангиз Баҳриддиновна': '1453',
            'Маҳкамова Дилноза Орифҷоновна': '1841',
            'Мусоева Мавлуда Норқуловна': '1907',
            'Норбекова Ситора Одилчоновна': '1232',
            'Нуруллоева Фируза': '1232',
            'Одинаев Санҷар Зафарбоевич': '1907',
            'Рачабова Манзура Толибовна': '1690',
            'Турдиева Жумагул Исмоиловна': '2076',
            'Хамедова Сайёра Набиҷоновна': '1841',
            'Холбоева Саёҳат Абдулғаффоровна': '1453',
            'Хусанова Замира Мамадқуловна': '2076',
            'Хусанова Ирода Ибрагимовна': '1317',
            'Шарофуллоев Шарофуллоҳ Жа Муҳибович': '2076',
            'Шермонова Уғулой Мамадалиевна': '2076',
            'Шодмонов Фаррух Ортикович': '1453',
            'Шопулатова Зайнура Муртазоевна': '1453',
            'Эшонқулова Муаттар Ҳолмирзаевна': '1690',
            'Ғафурова Раъно Тангирқуловна': '1841',
            'Қурбонбоева Барно Сатторовна': '1841',
            'Қӯзибекова Муҳайё Турсунқуловна': '1453',
            'Ҷаббориён Манучеҳр': '1611',
        },
    },
}


def tariff_data_available(school):
    """True when verified tariffication data exists for this school."""
    return str(get_school_number(school)) in VERIFIED_TARIFFS


def _school_entries(school):
    """[(normalized_key, rate_str)] for the school's verified data."""
    entry = VERIFIED_TARIFFS.get(str(get_school_number(school)))
    if not entry:
        return None, []
    items = {}
    for name, rate in entry['rates'].items():
        key = normalize_rate_name(name)
        # A duplicate normalized key inside the same source would make
        # the rate ambiguous — keep only collisions with identical data.
        if key in items and items[key] != rate:
            items[key] = None
        else:
            items[key] = rate
    return entry, [(k, v) for k, v in items.items()]


def suggested_rate_map(school, year, month, profiles):
    """teacher_id -> suggestion for the given school+month.

    A suggestion is produced only when the match is unambiguous on BOTH
    sides: exactly one profile in the school matches the workbook entry
    and the profile matches exactly one entry. Each hint is::

        {'value': Decimal, 'kind': 'verified'|'derived',
         'period': rate_period(year, month)}

    'verified' means the displayed month sits inside the period the
    source document covers — the rate is the recorded one. 'derived'
    means the value was re-expressed through an official raise factor
    (+30% from 01.09.2025, +25% from 01.09.2026) and still needs the
    school's confirmation.
    """
    entry, entries = _school_entries(school)
    if not entry:
        return {}
    target_period = rate_period(year, month)
    profile_hits = {}
    entry_hits = [0] * len(entries)
    for tp in profiles:
        key = normalize_rate_name(tp.full_name)
        hits = [i for i, (ek, _) in enumerate(entries)
                if _name_match(key, ek)]
        if hits:
            profile_hits[tp.id] = hits
            for i in hits:
                entry_hits[i] += 1
    out = {}
    for pid, hits in profile_hits.items():
        if len(hits) != 1 or entry_hits[hits[0]] != 1:
            continue
        rate = entries[hits[0]][1]
        if rate is None:
            continue
        out[pid] = {
            'value': _convert(rate, entry['period'], target_period),
            'kind': ('verified' if target_period == entry['period']
                     else 'derived'),
            'period': target_period,
        }
    return out
