# -*- coding: utf-8 -*-
"""Bicultural (Tajik & Uzbek Cyrillic) gender detection for student names."""
from typing import Optional


def _normalize_cyrillic(text: str) -> str:
    """Lowercase and trim the name for rule matching.

    Tajik/Uzbek-specific characters (ғ, ӣ, қ, ӯ, ҳ, ҷ, ў) are left intact;
    we only collapse whitespace and casefold.
    """
    return ' '.join(text.strip().lower().split())


# Highest-priority patronymic / direct markers.
FEMALE_PATRONYMIC_TOKENS = {'қизи', 'кизи', 'духтар'}
FEMALE_PATRONYMIC_ENDINGS = ('овна', 'евна', 'духт', 'қизи', 'кизи', 'кызы')

MALE_PATRONYMIC_TOKENS = {'ўғли', 'угли', 'писар'}
MALE_PATRONYMIC_ENDINGS = ('ович', 'евич', 'пур', 'ўғли', 'угли', 'ӯғли', 'оглы')

# Family-name endings (Tajik/Russian Cyrillic).
FEMALE_SURNAME_ENDINGS = ('ова', 'ева', 'ина')
MALE_SURNAME_ENDINGS = ('ов', 'ев', 'ин')

# Neutral family-name suffixes: decide by the given name.
NEUTRAL_SURNAME_ENDINGS = ('зода', 'зод', 'ӣ')

# Given-name morpheme indicators.
FEMALE_GIVEN_ENDINGS = ('ой', 'хон', 'бону', 'бегим', 'биби', 'пошша', 'нисо', 'гул', 'моҳ')
FEMALE_GIVEN_NAMES = {
    'мадина', 'нигина', 'фотима', 'раъно', 'сабрина', 'дилноза',
    'сурайё', 'зебо', 'лола', 'шаҳло', 'муҳайё', 'рухшона',
    'нодира', 'сарвиноз',
}

MALE_GIVEN_ENDINGS = ('бек', 'бой', 'жон', 'ҷон', 'қул', 'мирза', 'ботир', 'ёр', 'шоҳ', 'шо', 'дор')
MALE_GIVEN_NAMES = {
    'рустам', 'муҳаммад', 'алишер', 'сардор', 'жасур', 'беҳрӯз',
    'фирдавс', 'далер', 'сино', 'хуршед', 'суҳроб', 'шаҳром',
    'ҷамшед', 'акрам', 'икром', 'улуғбек', 'отабек', 'темур',
}


def _token_matches_any(token: str, endings: tuple) -> bool:
    return token.endswith(endings)


def _has_marker(tokens, tokens_set, endings):
    for token in tokens:
        if token in tokens_set or token.endswith(endings):
            return True
    return False


def detect_student_gender(full_name: str) -> Optional[str]:
    """Return 'M', 'F', or None for a student's full name."""
    if not full_name or not full_name.strip():
        return None

    normalized = _normalize_cyrillic(full_name)
    tokens = normalized.split()
    if not tokens:
        return None

    # 1. Patronymic / direct markers (highest priority).
    if _has_marker(tokens, FEMALE_PATRONYMIC_TOKENS, FEMALE_PATRONYMIC_ENDINGS):
        return 'F'
    if _has_marker(tokens, MALE_PATRONYMIC_TOKENS, MALE_PATRONYMIC_ENDINGS):
        return 'M'

    # 2. Surname endings.
    for token in tokens:
        if token.endswith(FEMALE_SURNAME_ENDINGS):
            return 'F'
    for token in tokens:
        if token.endswith(MALE_SURNAME_ENDINGS):
            return 'M'

    # 3. Neutral surnames: identify the likely family-name token, then examine the rest.
    neutral_idx = None
    for i, token in enumerate(tokens):
        if token.endswith(NEUTRAL_SURNAME_ENDINGS):
            neutral_idx = i
            break

    if neutral_idx is not None:
        given_tokens = [t for j, t in enumerate(tokens) if j != neutral_idx]
    else:
        given_tokens = list(tokens)

    for token in given_tokens:
        if (token.endswith(FEMALE_GIVEN_ENDINGS) or
                token in FEMALE_GIVEN_NAMES or
                token.endswith(('а', 'я'))):
            return 'F'

    for token in given_tokens:
        if token.endswith(MALE_GIVEN_ENDINGS) or token in MALE_GIVEN_NAMES:
            return 'M'

    return None
