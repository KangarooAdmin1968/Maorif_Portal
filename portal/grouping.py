"""TeachingGroup distribution helpers (Phase 2).

Pure functions over plain student dicts::

    {'id': <student pk>, 'name': <full name>, 'gender': 'M' | 'F' | ''}

They generate *proposals only* — the school's existing journal grouping is
authoritative and nothing is persisted until the Admin/Zavuch confirms the
save in the group-management view.
"""

GROUP_LABELS = ('Гурӯҳи 1', 'Гурӯҳи 2')


def _sort_key(student):
    return ((student.get('name') or '').lower(), str(student.get('id')))


def _ids(students):
    return [s['id'] for s in students]


def _halves(students):
    """Split a sorted list into (ceil-half, floor-half) preserving order."""
    mid = (len(students) + 1) // 2
    return students[:mid], students[mid:]


def gender_split(students):
    """Mode A — boys -> group 1, girls -> group 2.

    Students without a detected gender are deliberately left unassigned so
    the user places them manually before confirming.
    """
    boys = sorted((s for s in students if s.get('gender') == 'M'), key=_sort_key)
    girls = sorted((s for s in students if s.get('gender') == 'F'), key=_sort_key)
    return _ids(boys), _ids(girls)


def balanced_split(students):
    """Mode C — split each gender as evenly as possible, then pick the
    arrangement that minimizes the total group-size difference.

    Deterministic: within each gender the alphabetical first half is the
    "majority" half; on a tie the boys-majority + girls-majority combination
    lands in group 1. Students without a gender stay unassigned.
    """
    boys = sorted((s for s in students if s.get('gender') == 'M'), key=_sort_key)
    girls = sorted((s for s in students if s.get('gender') == 'F'), key=_sort_key)
    b1, b2 = _halves(boys)
    g1, g2 = _halves(girls)
    options = [
        (b1 + g1, b2 + g2),
        (b1 + g2, b2 + g1),
    ]
    best = min(options, key=lambda pair: abs(len(pair[0]) - len(pair[1])))
    return _ids(best[0]), _ids(best[1])


def validate_assignment(student_ids, members_g1, members_g2):
    """Validate a proposed two-group assignment for one ClassSubject.

    Returns a list of error codes (empty = valid):
      'duplicate_within'     — same student twice inside one group list
      'duplicate_membership' — a student appears in both groups
      'unknown_students'     — an id that is not a student of this class
      'unassigned'           — at least one student belongs to neither group
    """
    errors = []
    all_ids = set(student_ids)
    set1, set2 = set(members_g1), set(members_g2)
    if len(members_g1) != len(set1) or len(members_g2) != len(set2):
        errors.append('duplicate_within')
    if set1 & set2:
        errors.append('duplicate_membership')
    if (set1 | set2) - all_ids:
        errors.append('unknown_students')
    if all_ids - (set1 | set2):
        errors.append('unassigned')
    return errors


def group_stats(students_by_id, member_ids):
    """Composition summary for one group (preview/panel display)."""
    boys = girls = unknown = 0
    for sid in member_ids:
        gender = (students_by_id.get(sid) or {}).get('gender')
        if gender == 'M':
            boys += 1
        elif gender == 'F':
            girls += 1
        else:
            unknown += 1
    total = boys + girls + unknown
    return {'boys': boys, 'girls': girls, 'unknown': unknown, 'total': total}
