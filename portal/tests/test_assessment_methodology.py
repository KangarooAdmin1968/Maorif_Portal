"""Assessment methodology infrastructure (Stage 1).

Covers:
    Assessment.purpose / work_type / summative_scope fields
    work-type catalog semantics (single / paired_grades / mean / converted)
    parse_result_input work-type-aware validation
    test raw-points storage + provisional conversion
    explicit legacy mapping (is_ajm -> summative/intermediate, untyped -> mean)
"""
from django.urls import reverse

from portal.assessment_catalog import (
    ASSESSMENT_PURPOSES, SUMMATIVE_SCOPES, WORK_TYPES,
    assessment_purpose, assessment_scope, result_semantics_for,
    convert_test_points, get_work_type, test_percentage,
)
from portal.models import Assessment, Grade
from portal.tests.helpers import D_Q1, MATH, GoldenBase, lock_quarter
from portal.views import grade_component_result, parse_result_input, result_display

AJAX_URL = reverse('save_grade_ajax')
ASSESS_URL = reverse('assessment_save')


def make_control(cs, date=D_Q1, quarter=1, **kwargs):
    return Assessment.objects.create(
        class_subject=cs, date=date, quarter=quarter,
        category='control', **kwargs,
    )


def post_grade(client, student, assessment, score=None, extra=None):
    data = {
        'student_id': student.id,
        'subject': MATH,
        'type': 'daily',
        'assessment_id': assessment.id,
    }
    if score is not None:
        data['score'] = score
    if extra:
        data.update(extra)
    return client.post(AJAX_URL, data)


def post_assessment(client, cs, extra=None):
    data = {
        'class_subject_id': cs.id,
        'date': D_Q1.isoformat(),
        'title': '',
        'is_ajm': '0',
    }
    if extra:
        data.update(extra)
    return client.post(ASSESS_URL, data)


# ---------------------------------------------------------------------------
# Catalog structure
# ---------------------------------------------------------------------------

class CatalogTests(GoldenBase):
    def test_work_types_have_required_keys(self):
        for code, cfg in WORK_TYPES.items():
            self.assertIn('label', cfg, code)
            self.assertIn('semantics', cfg, code)
            self.assertIn('slash_allowed', cfg, code)
            self.assertIn('period_contribution', cfg, code)

    def test_source_verified_types(self):
        # The 2023 experimental standard: dictation one grade,
        # retelling/essay paired, test points-based.
        self.assertEqual(WORK_TYPES['dictation']['journal_values'], 1)
        self.assertEqual(WORK_TYPES['dictation']['semantics'], 'single')
        self.assertTrue(WORK_TYPES['dictation']['verified'])
        for code in ('retelling', 'essay'):
            self.assertEqual(WORK_TYPES[code]['semantics'], 'paired_grades')
            self.assertEqual(WORK_TYPES[code]['journal_values'], 2)
            self.assertTrue(WORK_TYPES[code]['verified'])
        self.assertEqual(WORK_TYPES['test']['semantics'], 'converted_points')
        self.assertTrue(WORK_TYPES['test']['points_allowed'])

    def test_composition_marked_unverified(self):
        # Slash notation exists in the source but exact cell semantics are
        # unresolved — the entry must not claim verification.
        self.assertFalse(WORK_TYPES['composition']['verified'])

    def test_neutral_codes_only(self):
        for code in WORK_TYPES:
            self.assertEqual(code, code.lower())
            self.assertTrue(code.isascii())
        for p in ASSESSMENT_PURPOSES:
            self.assertTrue(p.isascii())
        for s in SUMMATIVE_SCOPES:
            self.assertTrue(s.isascii())


# ---------------------------------------------------------------------------
# Legacy compatibility mapping
# ---------------------------------------------------------------------------

class LegacyMappingTests(GoldenBase):
    def test_legacy_control_maps_summative(self):
        a = make_control(self.cs_a)
        self.assertEqual(assessment_purpose(a), 'summative')
        self.assertEqual(assessment_scope(a), 'none')

    def test_legacy_ajm_maps_intermediate(self):
        a = make_control(self.cs_a, is_ajm=True)
        self.assertEqual(assessment_purpose(a), 'summative')
        self.assertEqual(assessment_scope(a), 'intermediate')

    def test_current_category_maps_formative(self):
        a = Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1, quarter=1, category='current'
        )
        self.assertEqual(assessment_purpose(a), 'formative')

    def test_explicit_fields_win_over_legacy(self):
        a = make_control(self.cs_a, is_ajm=True, purpose='diagnostic',
                         summative_scope='final')
        self.assertEqual(assessment_purpose(a), 'diagnostic')
        self.assertEqual(assessment_scope(a), 'final')

    def test_untyped_assessment_keeps_mean_semantics(self):
        a = make_control(self.cs_a)
        self.assertEqual(result_semantics_for(a)['semantics'], 'mean')

    def test_typed_assessment_gets_catalog_semantics(self):
        a = make_control(self.cs_a, work_type='retelling')
        self.assertEqual(result_semantics_for(a)['semantics'], 'paired_grades')


# ---------------------------------------------------------------------------
# parse_result_input — work-type-aware component semantics
# ---------------------------------------------------------------------------

class ParseResultInputTests(GoldenBase):
    def test_single_value_all_types(self):
        for wt in (None, 'dictation', 'retelling', 'test'):
            score, comps = parse_result_input('9', work_type=wt)
            self.assertEqual(score, 9)
            self.assertIsNone(comps)

    def test_legacy_pair_still_means(self):
        score, comps = parse_result_input('8/9')
        self.assertEqual(score, 8.5)
        self.assertEqual(comps, [8, 9])

    def test_legacy_triple_still_means(self):
        score, comps = parse_result_input('8/9/10')
        self.assertEqual(score, 9.0)
        self.assertEqual(comps, [8, 9, 10])

    def test_dictation_rejects_slash(self):
        with self.assertRaises(ValueError):
            parse_result_input('8/9', work_type='dictation')

    def test_retelling_pair_structured(self):
        score, comps = parse_result_input('8/9', work_type='retelling')
        # Grade.score keeps the aggregation scalar for contribution;
        # the pair itself is the journal result.
        self.assertEqual(score, 8.5)
        self.assertEqual(
            comps,
            [{'aspect': 'content', 'value': 8},
             {'aspect': 'literacy', 'value': 9}],
        )

    def test_retelling_rejects_triple(self):
        with self.assertRaises(ValueError):
            parse_result_input('8/9/10', work_type='retelling')

    def test_essay_pair(self):
        score, comps = parse_result_input('7/8', work_type='essay')
        self.assertEqual(comps[1], {'aspect': 'literacy', 'value': 8})

    def test_test_rejects_slash_in_score(self):
        # Points travel via dedicated params, not the score field.
        with self.assertRaises(ValueError):
            parse_result_input('12/20', work_type='test')

    def test_empty_clears(self):
        self.assertEqual(parse_result_input('', work_type='retelling'), (None, None))
        self.assertEqual(parse_result_input(None), (None, None))


# ---------------------------------------------------------------------------
# Test model — raw points -> percentage -> 10-point score
# ---------------------------------------------------------------------------

class TestModelTests(GoldenBase):
    def test_percentage(self):
        self.assertEqual(test_percentage(12, 20), 60.0)
        self.assertIsNone(test_percentage(None, 20))
        self.assertIsNone(test_percentage(5, 0))

    def test_conversion_returns_score(self):
        score = convert_test_points(18, 20)  # 90%
        self.assertIsNotNone(score)
        self.assertTrue(1 <= score <= 10)
        self.assertEqual(score, convert_test_points(18, 20))

    def test_conversion_monotonic(self):
        scores = [convert_test_points(p, 20) for p in range(21)]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(scores[0], 1)
        self.assertEqual(scores[20], 10)


# ---------------------------------------------------------------------------
# assessment_save — methodology fields
# ---------------------------------------------------------------------------

class AssessmentSaveMethodologyTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_create_with_work_type(self):
        resp = post_assessment(self.client, self.cs_a, extra={'work_type': 'dictation'})
        data = resp.json()
        self.assertTrue(data['success'])
        a = Assessment.objects.get(pk=data['assessment']['id'])
        self.assertEqual(a.work_type, 'dictation')
        self.assertEqual(a.purpose, 'summative')  # control -> summative default

    def test_create_with_purpose_and_scope(self):
        resp = post_assessment(self.client, self.cs_a, extra={
            'work_type': 'test', 'purpose': 'summative',
            'summative_scope': 'intermediate',
        })
        a = Assessment.objects.get(pk=resp.json()['assessment']['id'])
        self.assertEqual(a.work_type, 'test')
        self.assertEqual(a.summative_scope, 'intermediate')

    def test_invalid_work_type_rejected(self):
        resp = post_assessment(self.client, self.cs_a, extra={'work_type': 'bogus'})
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.exists())

    def test_invalid_purpose_rejected(self):
        resp = post_assessment(self.client, self.cs_a, extra={'purpose': 'bogus'})
        self.assertFalse(resp.json()['success'])

    def test_invalid_scope_rejected(self):
        resp = post_assessment(self.client, self.cs_a, extra={'summative_scope': 'bogus'})
        self.assertFalse(resp.json()['success'])

    def test_legacy_create_still_works(self):
        resp = post_assessment(self.client, self.cs_a)
        data = resp.json()
        self.assertTrue(data['success'])
        a = Assessment.objects.get(pk=data['assessment']['id'])
        self.assertEqual(a.work_type, '')

    def test_edit_updates_work_type(self):
        a = make_control(self.cs_a)
        resp = post_assessment(self.client, self.cs_a, extra={
            'assessment_id': a.id, 'work_type': 'essay',
        })
        self.assertTrue(resp.json()['success'])
        a.refresh_from_db()
        self.assertEqual(a.work_type, 'essay')


# ---------------------------------------------------------------------------
# save_grade_ajax — work-type-aware result writes
# ---------------------------------------------------------------------------

class GradeWriteSemanticsTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_retelling_pair_stored_as_two_grades(self):
        a = make_control(self.cs_a, work_type='retelling')
        resp = post_grade(self.client, self.s1, a, score='8/9')
        data = resp.json()
        self.assertTrue(data['success'])
        g = Grade.objects.get(student=self.s1, assessment=a)
        self.assertEqual(g.score, 8.5)  # aggregation scalar only
        self.assertEqual(
            g.components,
            [{'aspect': 'content', 'value': 8},
             {'aspect': 'literacy', 'value': 9}],
        )
        # Journal display is the pair, never the mean.
        self.assertEqual(data['display'], '8/9')

    def test_dictation_single_only(self):
        a = make_control(self.cs_a, work_type='dictation')
        resp = post_grade(self.client, self.s1, a, score='9')
        self.assertTrue(resp.json()['success'])
        resp = post_grade(self.client, self.s2, a, score='8/9')
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s2).exists())

    def test_untyped_assessment_keeps_mean(self):
        a = make_control(self.cs_a)
        resp = post_grade(self.client, self.s1, a, score='8/9')
        data = resp.json()
        self.assertTrue(data['success'])
        g = Grade.objects.get(student=self.s1, assessment=a)
        self.assertEqual(g.score, 8.5)
        self.assertEqual(g.components, [8, 9])
        # Frozen Phase 3 contract: 'score' carries the mean, 'display'
        # echoes the typed fraction form.
        self.assertEqual(data['score'], 8.5)
        self.assertEqual(data['display'], '8/9')

    def test_retelling_triple_rejected(self):
        a = make_control(self.cs_a, work_type='essay')
        resp = post_grade(self.client, self.s1, a, score='8/9/10')
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_paired_display_helper(self):
        a = make_control(self.cs_a, work_type='retelling')
        post_grade(self.client, self.s1, a, score='8/9')
        g = Grade.objects.get(student=self.s1, assessment=a)
        self.assertEqual(result_display(g), '8/9')
        self.assertEqual(grade_component_result(g), 8.5)


# ---------------------------------------------------------------------------
# Test raw points through the write path
# ---------------------------------------------------------------------------

class TestPointsWriteTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.test_a = make_control(self.cs_a, work_type='test')

    def test_points_stored_and_score_derived(self):
        resp = post_grade(self.client, self.s1, self.test_a, extra={
            'points_achieved': '18', 'points_possible': '20',
        })
        data = resp.json()
        self.assertTrue(data['success'])
        g = Grade.objects.get(student=self.s1, assessment=self.test_a)
        self.assertEqual(g.points_achieved, 18)
        self.assertEqual(g.points_possible, 20)
        self.assertEqual(g.score, convert_test_points(18, 20))
        self.assertEqual(data['percentage'], 90.0)

    def test_points_recoverable_for_recalc(self):
        post_grade(self.client, self.s1, self.test_a, extra={
            'points_achieved': '10', 'points_possible': '20',
        })
        g = Grade.objects.get(student=self.s1, assessment=self.test_a)
        # Raw data survives: a future conversion-table change can re-derive.
        self.assertEqual(test_percentage(g.points_achieved, g.points_possible), 50.0)

    def test_direct_score_still_allowed_for_test(self):
        resp = post_grade(self.client, self.s1, self.test_a, score='9')
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, assessment=self.test_a)
        self.assertEqual(g.score, 9)
        self.assertIsNone(g.points_achieved)

    def test_points_rejected_on_non_test_type(self):
        a = make_control(self.cs_a, work_type='dictation')
        resp = post_grade(self.client, self.s1, a, extra={
            'points_achieved': '10', 'points_possible': '20',
        })
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_invalid_points_rejected(self):
        resp = post_grade(self.client, self.s1, self.test_a, extra={
            'points_achieved': '25', 'points_possible': '20',
        })
        self.assertFalse(resp.json()['success'])

    def test_partial_points_rejected(self):
        resp = post_grade(self.client, self.s1, self.test_a, extra={
            'points_achieved': '10',
        })
        self.assertFalse(resp.json()['success'])

    def test_clearing_resets_points(self):
        post_grade(self.client, self.s1, self.test_a, extra={
            'points_achieved': '18', 'points_possible': '20',
        })
        resp = post_grade(self.client, self.s1, self.test_a, score='')
        self.assertTrue(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_points_on_legacy_untyped_rejected(self):
        a = make_control(self.cs_a)
        resp = post_grade(self.client, self.s1, a, extra={
            'points_achieved': '10', 'points_possible': '20',
        })
        self.assertFalse(resp.json()['success'])
