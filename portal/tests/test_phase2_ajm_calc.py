"""Phase 2 tests: category-aware quarter calculation (АТ + АҶМ + components).

Methodology under test:
    АҶЧ = АТ                                  when no АҶМ exists
    АҶЧ = (АТ + АҶМ) / 2                      when both exist
    АҶМ = mean of per-assessment results      each assessment counts ONCE
    component list [8, 9] on one Grade row    -> 8.5 for that row, never
                                                  two contributions
Intermediate arithmetic stays a precise float; std_round is applied only at
the established output boundary (display / QuarterGrade write).
"""
from django.urls import reverse

from portal.models import Assessment, Grade, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, D_Q2, MATH, PERIOD, GoldenBase,
    lock_quarter, make_grade, make_quarter_grade,
)
from portal.views import (
    _all_student_gpas, _split_quarter_grades, calc_quarter_value,
    calculate_school_rankings, collect_quarter_inputs,
    grade_component_result, std_round,
)


def make_assessment(class_subject, date=D_Q1, quarter=1, is_ajm=True,
                    category='control', **kwargs):
    return Assessment.objects.create(
        class_subject=class_subject, date=date, quarter=quarter,
        category=category, is_ajm=is_ajm, **kwargs,
    )


def mk_grade(student, score, date=D_Q1, subject=MATH, components=None,
             assessment=None):
    """Create a Grade row with optional component list / assessment link."""
    return Grade.objects.create(
        student=student, subject=subject, score=score, period=PERIOD,
        date=date, components=components, assessment=assessment,
    )


# ---------------------------------------------------------------------------
# grade_component_result
# ---------------------------------------------------------------------------


class ComponentResultTests(GoldenBase):
    def test_components_8_9_mean_8_5(self):
        g = mk_grade(self.s1, score=8.5, components=[8, 9])
        self.assertEqual(grade_component_result(g), 8.5)

    def test_components_8_9_10_mean_9(self):
        g = mk_grade(self.s1, score=9, components=[8, 9, 10])
        self.assertEqual(grade_component_result(g), 9.0)

    def test_components_take_precedence_over_score(self):
        # The methodology reads the component list when it is valid.
        g = mk_grade(self.s1, score=5, components=[8, 9])
        self.assertEqual(grade_component_result(g), 8.5)

    def test_null_components_fall_back_to_score(self):
        g = mk_grade(self.s1, score=7, components=None)
        self.assertEqual(grade_component_result(g), 7)

    def test_empty_components_fall_back_to_score(self):
        g = mk_grade(self.s1, score=7, components=[])
        self.assertEqual(grade_component_result(g), 7)

    def test_malformed_components_fall_back_to_score(self):
        for bad in ('8/9', {'a': 1}, [8, 'x'], [True, 9], 8):
            g = mk_grade(self.s1, score=7, components=bad)
            self.assertEqual(grade_component_result(g), 7, f'input {bad!r}')

    def test_component_result_is_precise_not_prematurely_rounded(self):
        g = mk_grade(self.s1, score=9, components=[8, 9])
        self.assertEqual(grade_component_result(g), 8.5)


# ---------------------------------------------------------------------------
# collect_quarter_inputs — pool membership
# ---------------------------------------------------------------------------


class QuarterPoolTests(GoldenBase):
    def test_legacy_null_assessment_rows_are_current(self):
        for score in (8, 9, 10):
            mk_grade(self.s1, score=score)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(sorted(current), [8.0, 9.0, 10.0])
        self.assertEqual(ajm, [])

    def test_non_ajm_assessment_link_counts_as_current(self):
        a = make_assessment(self.cs_a, is_ajm=False)
        mk_grade(self.s1, score=6, assessment=a)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [6.0])
        self.assertEqual(ajm, [])

    def test_ajm_link_leaves_current_pool(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=6, assessment=a)
        mk_grade(self.s1, score=9)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [9.0])
        self.assertEqual(ajm, [6.0])

    def test_ajm_other_quarter_not_counted(self):
        a = make_assessment(self.cs_a, quarter=2, date=D_Q2, is_ajm=True)
        mk_grade(self.s1, score=6, assessment=a)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [])
        self.assertEqual(ajm, [])
        _, ajm_q2 = collect_quarter_inputs(self.s1, MATH, 2)
        self.assertEqual(ajm_q2, [6.0])

    def test_same_assessment_multiple_rows_counted_once(self):
        # Defensive: duplicate Grade rows for one assessment still yield a
        # single per-assessment result (the mean of its row results).
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8, assessment=a)
        mk_grade(self.s1, score=10, assessment=a)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(ajm, [9.0])

    def test_mixed_pools(self):
        mk_grade(self.s1, score=8)                                    # legacy
        mk_grade(self.s1, score=10, assessment=make_assessment(
            self.cs_a, is_ajm=False))                                 # current
        mk_grade(self.s1, score=8.5, assessment=make_assessment(
            self.cs_a, is_ajm=True))                                  # АҶМ 1
        mk_grade(self.s1, score=9, assessment=make_assessment(
            self.cs_a, date=D_Q1_B, is_ajm=True))                     # АҶМ 2
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(sorted(current), [8.0, 10.0])
        self.assertEqual(sorted(ajm), [8.5, 9.0])


# ---------------------------------------------------------------------------
# calc_quarter_value — the АҶЧ formula
# ---------------------------------------------------------------------------


class QuarterValueTests(GoldenBase):
    # TEST 1
    def test_at_only_no_ajm(self):
        for score in (8, 9, 10):
            mk_grade(self.s1, score=score)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(calc_quarter_value(current, ajm), 9.0)

    # TEST 2
    def test_at_plus_one_ajm(self):
        mk_grade(self.s1, score=9)
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, assessment=a)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertAlmostEqual(calc_quarter_value(current, ajm), 8.75)
        self.assertEqual(std_round(calc_quarter_value(current, ajm)), 9)

    # TEST 3
    def test_at_plus_multiple_ajm(self):
        mk_grade(self.s1, score=9)
        for score in (8.5, 9, 10):
            mk_grade(self.s1, score=score,
                     assessment=make_assessment(self.cs_a, is_ajm=True))
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        # AJM = mean(8.5, 9, 10) = 9.1667; АҶЧ = (9 + 9.1667) / 2
        self.assertAlmostEqual(
            calc_quarter_value(current, ajm), (9 + (8.5 + 9 + 10) / 3) / 2
        )

    # TEST 4 (pool level — one assessment, two components, counted once)
    def test_two_component_assessment_counts_once(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, components=[8, 9], assessment=a)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(ajm, [8.5])

    # TEST 5 (pool level — one assessment, three components, counted once)
    def test_three_component_assessment_counts_once(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=9, components=[8, 9, 10], assessment=a)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(ajm, [9.0])

    # TEST 6
    def test_multiple_component_assessments_averaged_as_assessments(self):
        a1 = make_assessment(self.cs_a, is_ajm=True)
        a2 = make_assessment(self.cs_a, date=D_Q1_B, is_ajm=True)
        a3 = make_assessment(self.cs_a, date=D_Q2, quarter=1, is_ajm=True)
        mk_grade(self.s1, score=8.5, components=[8, 9], assessment=a1)
        mk_grade(self.s1, score=10, assessment=a2)
        mk_grade(self.s1, score=6.5, components=[6, 7], assessment=a3)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        # Per-assessment results: 8.5, 10, 6.5 -> mean 25/3.
        # Naive component pooling would give mean(8,9,10,6,7) = 8.0.
        self.assertEqual(len(ajm), 3)
        self.assertAlmostEqual(sum(ajm) / len(ajm), 25 / 3)
        self.assertNotAlmostEqual(sum(ajm) / len(ajm), 8.0)

    # TEST 7
    def test_no_ajm_means_at_not_halved(self):
        mk_grade(self.s1, score=9)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        value = calc_quarter_value(current, ajm)
        self.assertEqual(value, 9.0)
        self.assertNotEqual(value, 4.5)

    # TEST 8 — late АҶМ marking recalculates without touching Grade rows
    def test_late_ajm_marking_recalculates(self):
        mk_grade(self.s1, score=9)
        mk_grade(self.s1, score=9, date=D_Q1_B)
        a = make_assessment(self.cs_a, is_ajm=False)
        mk_grade(self.s1, score=5, assessment=a)
        count_before = Grade.objects.filter(student=self.s1).count()

        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        # is_ajm=False -> the row sits in the current pool: mean(9, 9, 5)
        self.assertAlmostEqual(
            calc_quarter_value(current, ajm), (9 + 9 + 5) / 3
        )

        a.is_ajm = True
        a.save()
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        # is_ajm=True -> AT = 9, AJM = 5 -> (9 + 5) / 2
        self.assertAlmostEqual(calc_quarter_value(current, ajm), 7.0)
        self.assertEqual(
            Grade.objects.filter(student=self.s1).count(), count_before
        )

    # TEST 9 — unmarking returns the row to the current pool
    def test_unmarking_ajm_restores_at_only(self):
        mk_grade(self.s1, score=9)
        mk_grade(self.s1, score=9, date=D_Q1_B)
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=5, assessment=a)
        count_before = Grade.objects.filter(student=self.s1).count()

        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertAlmostEqual(calc_quarter_value(current, ajm), 7.0)

        a.is_ajm = False
        a.save()
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertAlmostEqual(
            calc_quarter_value(current, ajm), (9 + 9 + 5) / 3
        )
        self.assertEqual(ajm, [])
        self.assertEqual(
            Grade.objects.filter(student=self.s1).count(), count_before
        )

    # АҶМ-only quarter: no crash, no zero-AT substitution, no halving.
    def test_ajm_only_no_at_returns_ajm(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, assessment=a)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        value = calc_quarter_value(current, ajm)
        self.assertEqual(value, 8.5)

    def test_neither_pool_returns_none(self):
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertIsNone(calc_quarter_value(current, ajm))

    # TEST 13
    def test_precise_float_intermediate(self):
        mk_grade(self.s1, score=8)
        mk_grade(self.s1, score=9, date=D_Q1_B)
        mk_grade(self.s1, score=10, date=D_Q2, assessment=None)
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, components=[8, 9], assessment=a)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        # AT = mean(8, 9) = 8.5; AJM = 8.5; АҶЧ = 8.5 (precise, unrounded).
        self.assertEqual(calc_quarter_value(current, ajm), 8.5)


# ---------------------------------------------------------------------------
# Integration: calc_quarter_from_daily + grade_entry live projection
# ---------------------------------------------------------------------------


class CalcWriteIntegrationTests(GoldenBase):
    def _post_calc(self):
        self.client.force_login(self.teacher_a)
        return self.client.post(reverse(
            'calc_quarter_from_daily',
            args=[self.school_a.id, '7-А', MATH],
        ))

    def test_calc_quarter_from_daily_writes_ajm_aware_value(self):
        mk_grade(self.s1, score=9)
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, assessment=a)
        resp = self._post_calc()
        self.assertEqual(resp.status_code, 302)
        qg = QuarterGrade.objects.get(student=self.s1, quarter=1)
        self.assertEqual(qg.grade, 9)  # std_round(8.75)

    def test_calc_quarter_from_daily_no_ajm_unchanged(self):
        for score in (8, 9, 10):
            mk_grade(self.s1, score=score)
        resp = self._post_calc()
        self.assertEqual(resp.status_code, 302)
        qg = QuarterGrade.objects.get(student=self.s1, quarter=1)
        self.assertEqual(qg.grade, 9)

    # TEST 11
    def test_locked_quarter_blocks_calc_write(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        mk_grade(self.s1, score=8)
        mk_grade(self.s1, score=10, date=D_Q2)
        resp = self._post_calc()
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            QuarterGrade.objects.filter(student=self.s1, quarter=1).exists()
        )
        self.assertEqual(
            QuarterGrade.objects.get(student=self.s1, quarter=2).grade, 10
        )

    # TEST 12
    def test_manual_quartergrade_not_overwritten_by_live(self):
        make_quarter_grade(self.s1, quarter=1, grade=10)
        mk_grade(self.s1, score=8)
        mk_grade(self.s1, score=8, date=D_Q1_B)
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]
        ))
        self.assertEqual(resp.status_code, 200)
        # Official QuarterGrade wins; live projection never replaces it.
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 10)
        self.assertNotIn(1, resp.context['live_quarter_flags'][self.s1.id])

    def test_grade_entry_live_projection_ajm_aware(self):
        mk_grade(self.s1, score=8)
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=6, assessment=a)
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]
        ))
        self.assertEqual(resp.status_code, 200)
        # АҶЧ = (8 + 6) / 2 = 7 — projected, so the Ҷ flag stays set.
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 7)
        self.assertIn(1, resp.context['live_quarter_flags'][self.s1.id])


# ---------------------------------------------------------------------------
# TEST 10 — rankings consume Grade.score once, components never leak
# ---------------------------------------------------------------------------


class RankingProtectionTests(GoldenBase):
    def test_components_do_not_create_extra_ranking_contributions(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=8.5, components=[8, 9], assessment=a)
        mk_grade(self.s1, score=10)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 2)
        # Ranking pools Grade.score only: mean(8.5, 10) = 9.25.
        # If [8, 9] leaked as separate rows it would be mean(8.5, 8, 9, 10)
        # = 8.875.
        self.assertEqual(_all_student_gpas()[self.s1.id], 9.25)
        school_a = next(
            r for r in calculate_school_rankings()
            if r['school'].id == self.school_a.id
        )
        self.assertEqual(school_a['gpa'], 9.25)

    def test_three_component_assessment_one_ranking_contribution(self):
        a = make_assessment(self.cs_a, is_ajm=True)
        mk_grade(self.s1, score=9, components=[8, 9, 10], assessment=a)
        self.assertEqual(_all_student_gpas()[self.s1.id], 9.0)
