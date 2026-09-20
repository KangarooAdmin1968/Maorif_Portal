"""Neutral assessment-methodology catalog for Maorif Portal.

Internal codes are deliberately language-neutral; Tajik strings live in the
'label' display field only. Nothing here claims official status beyond what
the sources support:

- 'verified': True entries implement rules from the 2023 Ministry
  experimental standard for grades 2 and 5 ("Меъёри баҳогузории 10-хола").
  That document is experimental and grade-scoped — do not generalize its
  rules to every grade without the adopted nationwide regulation.
- 'verified': False entries are project assumptions awaiting official
  confirmation. They exist so the system is configurable, not because the
  rule is established.

Country localization: a future country config overrides this module's data
(WORK_TYPES, TEST_CONVERSION_BANDS, period formulas) without touching the
Grade/Lesson schema.
"""

# ---------------------------------------------------------------------------
# Assessment purpose axis (nationwide conceptual model)
# ---------------------------------------------------------------------------
PURPOSE_DIAGNOSTIC = 'diagnostic'
PURPOSE_FORMATIVE = 'formative'
PURPOSE_SUMMATIVE = 'summative'

ASSESSMENT_PURPOSES = (PURPOSE_DIAGNOSTIC, PURPOSE_FORMATIVE, PURPOSE_SUMMATIVE)

# How far a summative result reaches. 'intermediate' is the neutral code the
# legacy is_ajm flag maps onto; 'attestation' covers final exam results.
SUMMATIVE_SCOPES = (
    'none',
    'intermediate',
    'final',
    'quarter',
    'semester',
    'annual',
    'attestation',
)

# ---------------------------------------------------------------------------
# Component semantics — how a multi-value result becomes journal grade(s)
# ---------------------------------------------------------------------------
SEMANTIC_SINGLE = 'single'               # exactly one journal grade
SEMANTIC_PAIRED = 'paired_grades'        # two independent journal grades
SEMANTIC_MEAN = 'mean'                   # components combined by arithmetic mean
SEMANTIC_RUBRIC = 'rubric'               # combined by a per-type rubric (future)
SEMANTIC_CONVERTED = 'converted_points'  # raw points -> % -> scale conversion

COMPONENT_SEMANTICS = (
    SEMANTIC_SINGLE,
    SEMANTIC_PAIRED,
    SEMANTIC_MEAN,
    SEMANTIC_RUBRIC,
    SEMANTIC_CONVERTED,
)

# Component aspect codes for paired-grade works. 'content' and 'literacy' are
# the two journal grades the 2023 standard assigns to нақли хаттӣ / эссе.
ASPECT_CONTENT = 'content'
ASPECT_LITERACY = 'literacy'

# ---------------------------------------------------------------------------
# Work-type catalog
# ---------------------------------------------------------------------------
# Fields per entry:
#   label               — Tajik display label (presentation layer only)
#   semantics           — one of COMPONENT_SEMANTICS
#   journal_values      — number of journal grades this work produces
#                         (None = flexible / unresolved)
#   aspects             — aspect codes for paired entries, in grade order
#   slash_allowed       — whether '8/9' fraction input is valid
#   max_components      — max slash parts (None = unrestricted)
#   points_allowed      — whether raw points input is valid (tests)
#   default_purpose     — usual assessment purpose for this work
#   period_contribution — whether results feed period/quarter calculation
#   verified            — True only when backed by the 2023 source
#   source              — provenance note
# ---------------------------------------------------------------------------
WORK_TYPES = {
    'oral': {
        'label': 'Ҷавоби шифоҳӣ',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_FORMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'written': {
        'label': 'Кори хаттӣ',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_FORMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'dictation': {
        # 2023 source: a control dictation receives ONE grade in the
        # notebook and class journal.
        'label': 'Диктант',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': True,
        'source': '2023-experimental (grades 2, 5)',
    },
    'retelling': {
        # 2023 source: TWO journal grades — content then literacy —
        # recorded with fraction notation.
        'label': 'Нақли хаттӣ',
        'semantics': SEMANTIC_PAIRED,
        'journal_values': 2,
        'aspects': (ASPECT_CONTENT, ASPECT_LITERACY),
        'slash_allowed': True,
        'max_components': 2,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': True,
        'source': '2023-experimental (grades 2, 5)',
    },
    'essay': {
        # 2023 source: TWO journal grades — content then literacy —
        # recorded with fraction notation.
        'label': 'Эссе',
        'semantics': SEMANTIC_PAIRED,
        'journal_values': 2,
        'aspects': (ASPECT_CONTENT, ASPECT_LITERACY),
        'slash_allowed': True,
        'max_components': 2,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': True,
        'source': '2023-experimental (grades 2, 5)',
    },
    'composition': {
        # 2023 source: fraction notation exists and content/literacy/
        # spelling/punctuation criteria are discussed, but the exact
        # journal-cell semantics are not explicit. Configured as a content+
        # literacy pair pending confirmation — MUST be re-verified against
        # the official text before relying on it.
        'label': 'Иншо',
        'semantics': SEMANTIC_PAIRED,
        'journal_values': 2,
        'aspects': (ASPECT_CONTENT, ASPECT_LITERACY),
        'slash_allowed': True,
        'max_components': 2,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'provisional — 2023 standard shows slash notation; cell semantics unconfirmed',
    },
    'test': {
        # 2023 source: closed question = 1 pt, open = 2 pts, matching =
        # 4 pts; total -> percentage -> 10-point conversion.
        'label': 'Тест',
        'semantics': SEMANTIC_CONVERTED,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': True,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': True,
        'source': '2023-experimental (grades 2, 5)',
    },
    'practical': {
        'label': 'Кори амалӣ',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_FORMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'laboratory': {
        'label': 'Кори таҷрибавӣ (лабораторӣ)',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_FORMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'project': {
        'label': 'Лоиҳа',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_FORMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'control': {
        'label': 'Кори назоратӣ',
        'semantics': SEMANTIC_SINGLE,
        'journal_values': 1,
        'slash_allowed': False,
        'max_components': 1,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
    'other': {
        # Flexible escape hatch: keeps the Phase 2/3 mean behavior for
        # works not yet in the catalog.
        'label': 'Дигар',
        'semantics': SEMANTIC_MEAN,
        'journal_values': None,
        'slash_allowed': True,
        'max_components': 3,
        'points_allowed': False,
        'default_purpose': PURPOSE_SUMMATIVE,
        'period_contribution': True,
        'verified': False,
        'source': 'project-default',
    },
}


def get_work_type(code):
    """Return the work-type config dict for a code, or None."""
    if not code:
        return None
    return WORK_TYPES.get(code)


def work_type_choices():
    """[(code, Tajik label)] for form/model choices."""
    return [(code, cfg['label']) for code, cfg in WORK_TYPES.items()]


def work_type_label(code):
    cfg = get_work_type(code)
    return cfg['label'] if cfg else ''


# ---------------------------------------------------------------------------
# Legacy compatibility mapping
# ---------------------------------------------------------------------------
# Untyped assessments (work_type='' — every pre-existing row) keep the
# Phase 2/3 semantics exactly: slash input of 2-3 components -> arithmetic
# mean stored in Grade.score. This is an explicit mapping, not silent
# reinterpretation: old rows are never rewritten, they are *read* with these
# semantics.
LEGACY_RESULT_SEMANTICS = {
    'semantics': SEMANTIC_MEAN,
    'journal_values': None,
    'slash_allowed': True,
    'max_components': 3,
    'points_allowed': False,
    'verified': False,
    'source': 'legacy Phase 2/3 behavior',
}


def result_semantics_for(assessment):
    """Resolve the component semantics config for an Assessment.

    Typed assessments use their work-type entry; untyped legacy rows fall
    back to LEGACY_RESULT_SEMANTICS (mean). Returns a config dict.
    """
    if assessment is not None:
        cfg = get_work_type(getattr(assessment, 'work_type', ''))
        if cfg is not None:
            return cfg
    return LEGACY_RESULT_SEMANTICS


def assessment_purpose(assessment):
    """Neutral purpose code for an Assessment, with explicit legacy mapping.

    New rows carry Assessment.purpose. Legacy rows map:
      is_ajm=True        -> summative (scope: intermediate, see below)
      category='control' -> summative
      category='current' -> formative
    """
    purpose = getattr(assessment, 'purpose', '')
    if purpose in ASSESSMENT_PURPOSES:
        return purpose
    return PURPOSE_SUMMATIVE if assessment.category == 'control' else PURPOSE_FORMATIVE


def assessment_scope(assessment):
    """Neutral summative-scope code, mapping legacy is_ajm -> intermediate.

    is_ajm is legacy compatibility only until its official meaning is
    confirmed; the mapping is explicit here rather than scattered in code.
    """
    scope = getattr(assessment, 'summative_scope', '') or 'none'
    if scope != 'none':
        return scope
    return 'intermediate' if getattr(assessment, 'is_ajm', False) else 'none'


# ---------------------------------------------------------------------------
# Test conversion (converted_points semantics)
# ---------------------------------------------------------------------------
# Provisional percentage -> 10-point band table for tests.
#
# NOT verified against the adopted national regulation — the 2023 standard
# specifies that conversion happens but this band table is a configurable
# placeholder pending the official table. Grade rows store raw points so a
# future table swap can re-derive every score.
TEST_CONVERSION_VERIFIED = False

# (minimum_percentage_inclusive, score) — first matching band wins.
TEST_CONVERSION_BANDS = [
    (95.0, 10),
    (85.0, 9),
    (70.0, 8),
    (55.0, 7),
    (40.0, 6),
    (25.0, 5),
    (15.0, 4),
    (5.0, 3),
    (1.0, 2),
    (0.0, 1),
]


def test_percentage(points_achieved, points_possible):
    """Percentage achieved for a test result; None when not computable."""
    if points_achieved is None or points_possible in (None, 0):
        return None
    return (float(points_achieved) / float(points_possible)) * 100.0


def convert_test_percentage(pct, bands=None):
    """Convert a percentage to a 1-10 score via the configured band table."""
    if pct is None:
        return None
    for minimum, score in (bands or TEST_CONVERSION_BANDS):
        if pct >= minimum:
            return score
    return 1


def convert_test_points(points_achieved, points_possible, bands=None):
    """points -> percentage -> 10-point score (provisional table)."""
    return convert_test_percentage(
        test_percentage(points_achieved, points_possible), bands=bands
    )
