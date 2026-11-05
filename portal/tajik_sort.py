"""Tajik-alphabet ordering for student rosters and name lists.

SQLite's default BINARY collation compares raw Unicode codepoints, so
the Tajik-specific letters Ғғ Ӣӣ Ққ Ӯӯ Ҳҳ Ҷҷ (Cyrillic Supplement
block) sort AFTER every basic Cyrillic letter: surnames like
«Ҷуманазарова» or «Ӯзбекова» used to land at the very end of class
rosters — past «Я» — instead of their proper positions, and mid-word
letters misordered too («Аҳмадова» after «Ашурова»).

Sorting in Python with `tajik_key` restores the official 35-letter
order. Rosters are small (a class ≈ 30 students, a whole school a few
hundred), so the cost is microseconds and no schema change or migration
is needed. Nothing here writes to the database — this is display
ordering only.

Rollback: set TAJIK_SORT_ENABLED = False and every call site falls back
to the previous codepoint ordering with this single-line change (or
revert the diff in GitHub Desktop).
"""

TAJIK_SORT_ENABLED = True

# Official Tajik Cyrillic alphabet, in order (35 letters).
TAJIK_ALPHABET = 'абвгғдеёжзиӣйкқлмнопрстуӯфхҳчҷшъэюя'

_ORDER = {ch: i for i, ch in enumerate(TAJIK_ALPHABET)}
_AFTER = len(TAJIK_ALPHABET)


def tajik_key(name):
    """Sort key placing `name` in official Tajik-alphabet order.

    Case-insensitive; runs of whitespace collapse («Алиева  Нафиса»).
    Characters outside the alphabet — Latin letters, digits,
    punctuation — sort after the whole alphabet, ordered among
    themselves by codepoint.
    """
    cleaned = ' '.join(str(name or '').split()).lower()
    return tuple(_ORDER.get(ch, _AFTER + ord(ch)) for ch in cleaned)


def name_key(name):
    """Name sort key honoring TAJIK_SORT_ENABLED.

    Disabled -> the plain string, i.e. the same codepoint ordering
    rosters had before (both SQLite BINARY and Python compare strings
    by codepoint).
    """
    if TAJIK_SORT_ENABLED:
        return tajik_key(name)
    return str(name or '')


def sort_students(students):
    """Materialize `students` as a list in Tajik-alphabet order.

    Accepts a queryset or any iterable of objects with ``full_name``;
    pk breaks exact-name ties so the result is always deterministic.
    Disabled -> the same codepoint order ``.order_by('full_name')``
    used to return.
    """
    if TAJIK_SORT_ENABLED:
        return sorted(students,
                      key=lambda s: (tajik_key(s.full_name), s.pk))
    return sorted(students, key=lambda s: (s.full_name, s.pk))
