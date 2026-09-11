# -*- coding: utf-8 -*-
"""Bicultural (Tajik & Uzbek Cyrillic) gender detection for student names."""
from typing import Optional


def _normalize_cyrillic(text: str) -> str:
    return ' '.join(text.strip().lower().split())


FEMALE_PATRONYMIC_TOKENS = {'қизи', 'кизи', 'духтар'}
FEMALE_PATRONYMIC_ENDINGS = ('овна', 'евна', 'духт', 'қизи', 'кизи', 'кызы')

MALE_PATRONYMIC_TOKENS = {'ўғли', 'угли', 'писар'}
MALE_PATRONYMIC_ENDINGS = ('ович', 'евич', 'пур', 'ўғли', 'угли', 'ӯғли', 'оглы')

FEMALE_SURNAME_ENDINGS = ('ова', 'ева', 'ина')
MALE_SURNAME_ENDINGS = ('ов', 'ев', 'ин')

NEUTRAL_SURNAME_ENDINGS = ('зода', 'зод', 'ӣ', 'ий', 'ён', 'ёни')

MALE_PREFIXES = ('абду', 'абд')

FEMALE_GIVEN_ENDINGS = ('ой', 'хон', 'хони', 'бону', 'бану', 'бегим', 'биби', 'пошша', 'нисо', 'гул', 'гулӣ', 'моҳ', 'гӯша')
FEMALE_GIVEN_NAMES = {
    'мадина', 'нигина', 'фотима', 'фотимаҳон', 'раъно', 'сабрина', 'дилноза', 'дилноз',
    'сурайё', 'зебо', 'зебунисо', 'лола', 'шаҳло', 'муҳайё', 'рухшона', 'нодира',
    'сарвиноз', 'малика', 'маликахон', 'махфират', 'шаҳноз', 'нилуфар', 'гулчехра',
    'гуландом', 'заррина', 'нома', 'назокат', 'парвина', 'саодат', 'садофа',
    'насиба', 'муҳаййина', 'туҳфа', 'шукрона', 'камола', 'нозанин',
    'шукрӣ', 'муборак', 'тунзила', 'ҳусния', 'замира', 'иззат',
    'ситора', 'меҳрона', 'неъмат', 'ҳолпўша', 'розпўша', 'бибихон',
}

MALE_GIVEN_ENDINGS = (
    'бек', 'бой', 'жон', 'ҷон', 'қул', 'қули', 'мирза', 'ботир', 'ёр', 'шоҳ', 'шо', 'дор',
    'беков', 'қурбон', 'хўҷа', 'хоҷа', 'муқим', 'ҷан', 'соз', 'ёр', 'назар',
)
MALE_GIVEN_NAMES = {
    'рустам', 'муҳаммад', 'махмад', 'алишер', 'сардор', 'жасур', 'беҳрӯз',
    'фирдавс', 'далер', 'сино', 'хуршед', 'суҳроб', 'шаҳром', 'ҷамшед', 'акрам',
    'икром', 'улуғбек', 'отабек', 'темур', 'самариддин', 'саид', 'саидӣ',
    'маъруф', 'маҳмуд', 'мурад', 'ҳамид', 'нуриддин', 'сулаймон', 'ваисиддин',
    'озод', 'муслим', 'абдуллоҳ', 'абдулло', 'абдуғаффор', 'абдусалом',
    'абдулҳамид', 'абдулҳақ', 'абдулазиз', 'абдулҳалим', 'шариф', 'умид',
    'мақсуд', 'мусаввир', 'мансур', 'карим', 'анис', 'сухайл', 'баҳодир',
    'шоҳрух', 'маҳмад', 'муҳиддин', 'иброҳим', 'исҳоқ', 'зуҳид', 'суннат',
    'рафиқ', 'сафар', 'қурбон', 'ҳабиб', 'комил', 'мубин', 'шодмон',
    'даврон', 'дилшод', 'ихтиёр', 'давид', 'юнус', 'ҳотам', 'зайниддин',
    'мақбул', 'ҳошим', 'раббим', 'нурали', 'ҳалим', 'саидмурод', 'шоир',
    'саломат', 'зиё', 'зиёд', 'мирзо', 'равшан', 'субҳон', 'ҳофиз',
    'бурҳон', 'шамсиддин', 'рафаэт', 'шароф', 'сафарали', 'тӯра',
    'муртаза', 'шоҳиён', 'муҳриддин', 'раҳим', 'раҳмат', 'файз',
    'абдувоҳид', 'абдусаттор', 'абдуллозода', 'ҷумъа',
}

MALE_EXACT_EXCEPTIONS = {'озод'}


def _has_marker(tokens, tokens_set, endings):
    for token in tokens:
        if token in tokens_set or token.endswith(endings):
            return True
    return False


def _neutral_surname_token(tokens):
    for i, token in enumerate(tokens):
        if token in MALE_EXACT_EXCEPTIONS:
            continue
        if token.endswith(NEUTRAL_SURNAME_ENDINGS):
            return i
    return None


def _gender_stem(token: str) -> str:
    for ending in ('и', 'ӣ'):
        if token.endswith(ending) and len(token) > 1:
            return token[:-1]
    return token


def _is_male(token: str) -> bool:
    if token.startswith(MALE_PREFIXES):
        return True
    stem = _gender_stem(token)
    if (stem.endswith(MALE_GIVEN_ENDINGS) or
            stem in MALE_GIVEN_NAMES or
            token in MALE_GIVEN_NAMES):
        return True
    return False


def _is_female(token: str) -> bool:
    stem = _gender_stem(token)
    if (stem.endswith(FEMALE_GIVEN_ENDINGS) or
            stem in FEMALE_GIVEN_NAMES or
            token in FEMALE_GIVEN_NAMES or
            stem.endswith(('а', 'я'))):
        return True
    return False


def detect_student_gender(full_name: str) -> Optional[str]:
    if not full_name or not full_name.strip():
        return None

    normalized = _normalize_cyrillic(full_name)
    tokens = normalized.split()
    if not tokens:
        return None

    if _has_marker(tokens, FEMALE_PATRONYMIC_TOKENS, FEMALE_PATRONYMIC_ENDINGS):
        return 'F'
    if _has_marker(tokens, MALE_PATRONYMIC_TOKENS, MALE_PATRONYMIC_ENDINGS):
        return 'M'

    for token in tokens:
        if token.endswith(FEMALE_SURNAME_ENDINGS):
            return 'F'
    for token in tokens:
        if token.endswith(MALE_SURNAME_ENDINGS):
            return 'M'

    neutral_idx = _neutral_surname_token(tokens)
    if neutral_idx is not None:
        given_tokens = [t for j, t in enumerate(tokens) if j != neutral_idx]
    else:
        given_tokens = list(tokens)

    for token in given_tokens:
        if _is_female(token):
            return 'F'

    for token in given_tokens:
        if _is_male(token):
            return 'M'

    return None
