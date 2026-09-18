"""Golden-master tests for the grading calculation and entry contracts.

These tests freeze the CURRENT implementation. The future Lesson/Assessment
redesign (Phases 1-5) will be judged against them. Where current behavior is
surprising, the test documents it explicitly rather than "fixing" it.
"""
import datetime

from django.test import TestCase
from django.urls import reverse

from portal.models import Grade, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, D_Q2, D_Q3, MATH, PERIOD, TAJIK,
    GoldenBase, lock_quarter, make_class_subject, make_grade,
    make_quarter_grade, make_student,
)
from portal.views import calc_quarterly, get_date_quarter, get_or_create_grade, is_quarter_locked, std_round


# ---------------------------------------------------------------------------
# A. calc_quarterly / std_round / get_date_quarter
# ---------------------------------------------------------------------------

class CalcQuarterlyGoldenTests(TestCase):
    """Freeze the current quarter/semester/annual/final math."""

    def test_std_round_half_up(self):
        self.assertEqual(std_round(9.5), 10)
        self.assertEqual(std_round(9.4), 9)
        self.assertEqual(std_round(8.5), 9)
        self.assertIsNone(std_round(None))

    def test_get_date_quarter_month_mapping(self):
        self.assertEqual(get_date_quarter(datetime.date(2025, 9, 1)), 1)
        self.assertEqual(get_date_quarter(datetime.date(2025, 11, 30)), 1)
        self.assertEqual(get_date_quarter(datetime.date(2025, 12, 1)), 2)
        self.assertEqual(get_date_quarter(datetime.date(2026, 2, 28)), 2)
        self.assertEqual(get_date_quarter(datetime.date(2026, 3, 1)), 3)
        self.assertEqual(get_date_quarter(datetime.date(2026, 5, 31)), 3)
        self.assertEqual(get_date_quarter(datetime.date(2026, 6, 1)), 4)
        self.assertEqual(get_date_quarter(datetime.date(2026, 8, 31)), 4)

    def test_calc_quarterly_two_quarters_semiannual_mean(self):
        calc = calc_quarterly({1: 8, 2: 10})
        self.assertEqual(calc['semi_annual_1'], 9)
        self.assertFalse(calc['semi_annual_1_proj'])

    def test_calc_quarterly_rounds_half_up(self):
        calc = calc_quarterly({1: 9, 2: 10})
        self.assertEqual(calc['semi_annual_1'], 10)  # 9.5 -> 10

    def test_calc_quarterly_full_year_no_attestation_final_equals_annual(self):
        # GOLDEN: when attestation is absent, final == annual (never halved).
        calc = calc_quarterly({1: 8, 2: 10, 3: 9, 4: 9})
        self.assertEqual(calc['semi_annual_1'], 9)
        self.assertEqual(calc['semi_annual_2'], 9)
        self.assertEqual(calc['annual'], 9)
        self.assertEqual(calc['final'], 9)
        self.assertFalse(calc['annual_proj'])
        self.assertFalse(calc['final_proj'])

    def test_calc_quarterly_attestation_present_final_is_mean(self):
        calc = calc_quarterly({1: 8, 2: 10, 3: 9, 4: 9, 'att': 7})
        self.assertEqual(calc['annual'], 9)
        self.assertEqual(calc['final'], 8)  # (9 + 7) / 2 -> 8

    def test_calc_quarterly_single_quarter_projection_flag(self):
        calc = calc_quarterly({1: 8})
        self.assertEqual(calc['semi_annual_1'], 8)
        self.assertTrue(calc['semi_annual_1_proj'])
        self.assertIsNone(calc['semi_annual_2'])
        self.assertEqual(calc['annual'], 8)
        self.assertTrue(calc['annual_proj'])
        self.assertEqual(calc['final'], 8)
        self.assertTrue(calc['final_proj'])

    def test_calc_quarterly_live_flags_mark_projection(self):
        # Same quarters filled, but flagged live -> projection markers set.
        calc = calc_quarterly({1: 8, 2: 8}, live_flags={2})
        self.assertEqual(calc['semi_annual_1'], 8)
        self.assertTrue(calc['semi_annual_1_proj'])
        self.assertTrue(calc['annual_proj'])
        self.assertTrue(calc['final_proj'])

    def test_calc_quarterly_empty_map_all_none(self):
        calc = calc_quarterly({})
        for key in ('semi_annual_1', 'semi_annual_2', 'annual', 'final'):
            self.assertIsNone(calc[key])
        for key in ('semi_annual_1_proj', 'semi_annual_2_proj',
                    'annual_proj', 'final_proj'):
            self.assertFalse(calc[key])

    def test_calc_quarterly_attestation_only_produces_no_final(self):
        # Current behavior: attestation without any annual value yields no
        # final result at all.
        calc = calc_quarterly({'att': 9})
        self.assertIsNone(calc['annual'])
        self.assertIsNone(calc['final'])

    def test_calc_quarterly_partial_second_semester(self):
        calc = calc_quarterly({1: 8, 2: 10, 3: 9})
        self.assertEqual(calc['semi_annual_1'], 9)
        self.assertFalse(calc['semi_annual_1_proj'])
        self.assertEqual(calc['semi_annual_2'], 9)
        self.assertTrue(calc['semi_annual_2_proj'])
        self.assertEqual(calc['annual'], 9)
        self.assertTrue(calc['annual_proj'])


# ---------------------------------------------------------------------------
# B. get_or_create_grade — date-keyed merge semantics (Phase 4 will make this
#    lesson-aware; this suite freezes the CURRENT date-only behavior)
# ---------------------------------------------------------------------------

class GetOrCreateGradeGoldenTests(GoldenBase):
    """CURRENT contract: one logical Grade per (student, subject, period,
    date). Two 'lessons' on the same day are NOT distinguishable today."""

    def test_same_student_subject_date_returns_same_row(self):
        g1, created1 = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=D_Q1,
            defaults={'score': 8},
        )
        g2, created2 = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=D_Q1,
            defaults={'score': 10},
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(g1.pk, g2.pk)
        self.assertEqual(Grade.objects.filter(
            student=self.s1, subject=MATH, date=D_Q1).count(), 1)

    def test_repeated_save_no_unintended_duplicate(self):
        for _ in range(3):
            get_or_create_grade(
                student=self.s1, subject=MATH, period=PERIOD, date=D_Q1,
                defaults={'score': 7},
            )
        self.assertEqual(Grade.objects.filter(
            student=self.s1, subject=MATH, date=D_Q1).count(), 1)

    def test_different_dates_are_separate_rows(self):
        get_or_create_grade(student=self.s1, subject=MATH, period=PERIOD,
                            date=D_Q1, defaults={'score': 8})
        get_or_create_grade(student=self.s1, subject=MATH, period=PERIOD,
                            date=D_Q1_B, defaults={'score': 9})
        self.assertEqual(Grade.objects.filter(
            student=self.s1, subject=MATH).count(), 2)

    def test_duplicate_rows_merged_field_by_field(self):
        # When duplicates already exist, the first row wins and receives any
        # non-empty fields from the extras; extras are deleted.
        first = make_grade(self.s1, score=8, date=D_Q1)
        extra = make_grade(self.s1, score=9, date=D_Q1,
                           attendance='+', behavior=3)
        merged, created = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=D_Q1,
        )
        self.assertFalse(created)
        self.assertEqual(merged.pk, first.pk)
        merged.refresh_from_db()
        # First row's score wins; empty fields filled from the duplicate.
        self.assertEqual(merged.score, 8)
        self.assertEqual(merged.attendance, '+')
        self.assertEqual(merged.behavior_score, 3)
        self.assertFalse(Grade.objects.filter(pk=extra.pk).exists())

    def test_score_attendance_behavior_sticker_combination(self):
        g, _ = get_or_create_grade(
            student=self.s1, subject=MATH, period=PERIOD, date=D_Q1,
            defaults={'score': 9, 'attendance': '-',
                      'behavior_score': 4, 'sticker': '⭐'},
        )
        self.assertEqual(g.score, 9)
        self.assertEqual(g.attendance, '-')
        self.assertEqual(g.behavior_score, 4)
        self.assertEqual(g.sticker, '⭐')


# ---------------------------------------------------------------------------
# C. save_grade_ajax — the offline-sync server contract
# ---------------------------------------------------------------------------

class SaveGradeAjaxContractTests(GoldenBase):
    """Freeze the JSON contract consumed by grade_entry.html localStorage sync.

    CURRENT contract details that must not change silently:
    - business errors return HTTP 200 with success=False (permission is the
      only 403);
    - '8.9' is TRUNCATED to 8 (parse_score does int(float(raw)));
    - fields not present in POST are left untouched;
    - a daily row whose fields are all emptied is deleted;
    - quarterly saves return the calc_quarterly payload.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.url = reverse('save_grade_ajax')

    def _post(self, **params):
        return self.client.post(self.url, params)

    # --- daily saves -------------------------------------------------------

    def test_daily_score_creates_grade_contract(self):
        r = self._post(student_id=self.s1.id, subject='Математика',
                       date=D_Q1.isoformat(), type='daily', score='8')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['saved'])
        g = Grade.objects.get(student=self.s1)
        self.assertEqual(g.subject, MATH)  # normalized on save
        self.assertEqual(g.period, PERIOD)
        self.assertEqual(g.date, D_Q1)
        self.assertEqual(g.score, 8)

    def test_daily_attendance_saved(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', attendance='-')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=self.s1).attendance, '-')

    def test_daily_invalid_attendance_becomes_none(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', attendance='x')
        self.assertTrue(r.json()['success'])
        # Invalid value normalizes to NULL; all-null row is deleted.
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 0)

    def test_daily_behavior_saved(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', behavior_score='3')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=self.s1).behavior_score, 3)

    def test_daily_behavior_out_of_range_becomes_none(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', behavior_score='9')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 0)

    def test_daily_sticker_saved(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', sticker='⭐')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=self.s1).sticker, '⭐')

    def test_daily_decimal_score_truncated_to_int(self):
        # GOLDEN QUIRK: ajax '8.9' -> int(8.9) -> 8 (truncation, not rejection).
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8.9')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=self.s1).score, 8)

    def test_daily_comma_decimal_separator_accepted(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8,5')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=self.s1).score, 8)

    def test_daily_score_above_10_business_error_http_200(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='11')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data['success'])
        self.assertIn('Хол', data['message'])
        # GOLDEN QUIRK: get_or_create_grade runs BEFORE parse_score, so a
        # rejected score leaves an all-null Grade row behind.
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertIsNone(g.score)

    def test_daily_score_non_numeric_business_error_http_200(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='abc')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertIsNone(g.score)

    def test_daily_score_zero_business_error(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='0')
        self.assertFalse(r.json()['success'])

    def test_daily_slash_components_rejected_current_behavior(self):
        # GOLDEN: '8/9' cannot currently be entered — parse_score rejects it.
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8/9')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])
        # ...but the empty placeholder row was already created.
        self.assertEqual(Grade.objects.filter(
            student=self.s1, date=D_Q1, score__isnull=True).count(), 1)

    def test_daily_empty_score_deletes_row(self):
        make_grade(self.s1, score=8, date=D_Q1)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='')
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 0)

    def test_unsent_fields_are_preserved(self):
        # Fields absent from the POST body are NOT cleared — this is what
        # makes per-cell offline sync safe.
        make_grade(self.s1, score=8, date=D_Q1)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', attendance='+')
        self.assertTrue(r.json()['success'])
        g = Grade.objects.get(student=self.s1)
        self.assertEqual(g.score, 8)          # untouched
        self.assertEqual(g.attendance, '+')   # updated

    def test_manual_quarter_grade_survives_daily_save(self):
        # GOLDEN (TEST 5): a daily cell save never touches QuarterGrade —
        # official period results are only modified by quarterly saves,
        # the form POST, or calc_quarter_from_daily.
        make_quarter_grade(self.s1, quarter=1, grade=7)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='10')
        self.assertTrue(r.json()['success'])
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 7)

    # --- quarterly saves ---------------------------------------------------

    def test_quarterly_save_creates_quarter_grade_and_returns_calc(self):
        make_quarter_grade(self.s1, quarter=1, grade=8)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='2', score='10')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['saved'])
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=2).grade, 10)
        # The response carries the recalculated calc_quarterly payload —
        # the journal JS renders Нимсола/Солона/Ҷамъбастӣ from these keys.
        for key in ('semi_annual_1', 'semi_annual_2', 'annual', 'final'):
            self.assertIn(key, data)
        self.assertEqual(data['semi_annual_1'], 9)

    def test_quarterly_quarter_zero_writes_attestation(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='0', score='9')
        self.assertTrue(r.json()['success'])
        qg = QuarterGrade.objects.get(student=self.s1, quarter=0)
        self.assertEqual(qg.att_grade, 9)
        self.assertIsNone(qg.grade)

    def test_quarterly_empty_score_deletes_row(self):
        make_quarter_grade(self.s1, quarter=1, grade=8)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='1', score='')
        self.assertTrue(r.json()['success'])
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).exists())

    def test_quarterly_invalid_quarter_error(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='x', score='9')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_quarterly_score_out_of_range_error(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='1', score='12')
        self.assertFalse(r.json()['success'])

    # --- envelope / permission contract ------------------------------------

    def test_missing_student_id_business_error_200(self):
        r = self._post(subject=MATH, date=D_Q1.isoformat(),
                       type='daily', score='8')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_nonexistent_student_business_error_200(self):
        r = self._post(student_id='no_such_id', subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_missing_subject_business_error_200(self):
        r = self._post(student_id=self.s1.id, date=D_Q1.isoformat(),
                       type='daily', score='8')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_unknown_type_business_error(self):
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='bogus', score='8')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_get_method_not_allowed(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 405)

    def test_anonymous_redirects_to_login(self):
        self.client.logout()
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    # --- quarter lock contract ----------------------------------------------

    def test_locked_quarter_blocks_daily_save(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       date=D_Q1.isoformat(), type='daily', score='8')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])
        self.assertIn('баста', r.json()['message'].lower())
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 0)

    def test_locked_quarter_blocks_quarterly_save(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='1', score='9')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['success'])

    def test_lock_scoped_per_quarter_other_quarter_still_saves(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        r = self._post(student_id=self.s1.id, subject=MATH,
                       type='quarterly', quarter='2', score='9')
        self.assertTrue(r.json()['success'])


# ---------------------------------------------------------------------------
# D. calc_quarter_from_daily — bulk quarter fill
# ---------------------------------------------------------------------------

class CalcQuarterFromDailyGoldenTests(GoldenBase):
    """Freeze the current bulk calculation: quarter = plain mean of ALL daily
    scores in that quarter's months. There is no category split today — this
    is the TEST 1/TEST 2 baseline (8,9,10 -> 9, uniformly pooled)."""

    def setUp(self):
        super().setUp()
        self.url = reverse('calc_quarter_from_daily',
                           args=[self.school_a.id, '7-А', 'Математика'])

    def test_three_scores_8_9_10_mean_9(self):
        for score, d in ((8, D_Q1), (9, D_Q1_B), (10, D_Q1_B)):
            make_grade(self.s1, score=score, date=d)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 9)

    def test_all_daily_scores_pooled_uniformly_no_categories(self):
        # GOLDEN: every daily score counts equally — the АҶМ concept does not
        # exist in current code, so nothing is weighted or halved.
        make_grade(self.s1, score=8, date=D_Q1)
        make_grade(self.s1, score=8, date=D_Q1_B)
        make_grade(self.s1, score=10, date=D_Q1_B)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        # (8+8+10)/3 = 8.67 -> 9
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 9)

    def test_multiple_quarters_calculated_separately(self):
        make_grade(self.s1, score=8, date=D_Q1)
        make_grade(self.s1, score=10, date=D_Q2)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 8)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=2).grade, 10)

    def test_rounding_half_up(self):
        make_grade(self.s1, score=9, date=D_Q1)
        make_grade(self.s1, score=10, date=D_Q1_B)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 10)  # 9.5 -> 10

    def test_locked_quarter_is_skipped(self):
        make_grade(self.s1, score=8, date=D_Q1)
        make_grade(self.s1, score=10, date=D_Q2)
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).exists())
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=2).grade, 10)

    def test_manual_quarter_grade_is_overwritten_current_behavior(self):
        # GOLDEN (documented): bulk recalc uses update_or_create — a manually
        # entered official quarter grade IS replaced by the daily mean.
        make_quarter_grade(self.s1, quarter=1, grade=5)
        make_grade(self.s1, score=9, date=D_Q1)
        self.client.force_login(self.admin)
        self.client.post(self.url)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 9)

    def test_requires_edit_permission_403(self):
        self.client.force_login(self.teacher_b)  # other school's teacher
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 403)

    def test_redirects_to_grade_entry(self):
        self.client.force_login(self.admin)
        r = self.client.post(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('grade', r.url)


# ---------------------------------------------------------------------------
# grade_entry form POST — batch save contract (separate from the ajax path)
# ---------------------------------------------------------------------------

class GradeEntryPostContractTests(GoldenBase):
    """The non-ajax form POST path has subtly different score validation than
    save_grade_ajax: non-integer scores are silently dropped (score=None),
    and EMPTY quarter cells DELETE QuarterGrade rows."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse('grade_entry',
                           args=[self.school_a.id, '7-А', 'Математика'])

    def test_batch_post_saves_daily_score(self):
        r = self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            f'score_{self.s1.id}': '8',
            f'attendance_{self.s1.id}': '+',
            f'behavior_{self.s1.id}': '4',
        })
        self.assertEqual(r.status_code, 302)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertEqual(g.score, 8)
        self.assertEqual(g.attendance, '+')
        self.assertEqual(g.behavior_score, 4)

    def test_batch_post_saves_quarter_and_attestation(self):
        self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            f'q_1_{self.s1.id}': '9',
            f'att_{self.s1.id}': '10',
        })
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=1).grade, 9)
        self.assertEqual(QuarterGrade.objects.get(
            student=self.s1, quarter=0).att_grade, 10)

    def test_empty_score_cell_deletes_grade_row(self):
        make_grade(self.s1, score=8, date=D_Q1)
        self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            f'score_{self.s1.id}': '',
            f'attendance_{self.s1.id}': '',
            f'behavior_{self.s1.id}': '',
        })
        self.assertEqual(Grade.objects.filter(
            student=self.s1, date=D_Q1).count(), 0)

    def test_empty_quarter_cell_deletes_quarter_grade_current_behavior(self):
        # GOLDEN (documented): posting the form with a blank quarter cell
        # deletes that student's official QuarterGrade for the quarter.
        make_quarter_grade(self.s1, quarter=1, grade=8)
        self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            f'q_1_{self.s1.id}': '',
        })
        self.assertFalse(QuarterGrade.objects.filter(
            student=self.s1, quarter=1).exists())

    def test_non_integer_score_rejected_silently(self):
        # Unlike the ajax path (which truncates 8.9 -> 8), the form POST
        # treats '8.5' as invalid -> score=None -> row deleted if otherwise
        # empty. Freeze the difference.
        self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            f'score_{self.s1.id}': '8.5',
        })
        self.assertEqual(Grade.objects.filter(
            student=self.s1, date=D_Q1).count(), 0)

    def test_post_cross_school_teacher_redirected_not_403(self):
        # GOLDEN: the role pre-check at the top of grade_entry runs for POST
        # too — a foreign-school teacher gets a 302 redirect to their own
        # class list, not a 403.
        self.client.force_login(self.teacher_b)
        r = self.client.post(self.url, {'date': D_Q1.isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'],
                         '/school/%d/classes/' % self.school_b.id)

    def test_post_unassigned_subject_teacher_redirected(self):
        # GOLDEN: the teacher pre-check calls can_view_grade_journal, which is
        # subject-scoped — a teacher posting to a subject they're not assigned
        # is 302-redirected to their class list. The POST-branch 403 at
        # views.py:1281 is unreachable for regular teachers.
        self.client.force_login(self.teacher_a)
        url = reverse('grade_entry',
                      args=[self.school_a.id, '7-А', 'ЗАБОНИ ТОҶИКӢ'])
        r = self.client.post(url, {'date': D_Q1.isoformat()})
        self.assertEqual(r.status_code, 302)

    def test_post_school_less_director_403(self):
        # GOLDEN: a director with no profile school can VIEW every journal
        # (role bypass inside can_view_grade_journal) but the POST-branch
        # can_edit check requires profile.school == school → 403 on save.
        self.client.force_login(self.director)
        r = self.client.post(self.url, {'date': D_Q1.isoformat()})
        self.assertEqual(r.status_code, 403)


# ---------------------------------------------------------------------------
# QuarterLock scope
# ---------------------------------------------------------------------------

class QuarterLockGoldenTests(GoldenBase):

    def test_is_quarter_locked_exact_match(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.assertTrue(is_quarter_locked(self.school_a, '7-А', MATH, 1))

    def test_lock_does_not_leak_to_other_quarter(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.assertFalse(is_quarter_locked(self.school_a, '7-А', MATH, 2))

    def test_lock_does_not_leak_to_other_subject(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.assertFalse(is_quarter_locked(self.school_a, '7-А', TAJIK, 1))

    def test_lock_does_not_leak_to_other_school(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.assertFalse(is_quarter_locked(self.school_b, '7-А', MATH, 1))

    def test_lock_does_not_leak_to_other_class(self):
        make_student(self.school_a, '8-Б', 'Дилшод Юлдошев')
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.assertFalse(is_quarter_locked(self.school_a, '8-Б', MATH, 1))

    def test_locked_false_does_not_lock(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1, locked=False)
        self.assertFalse(is_quarter_locked(self.school_a, '7-А', MATH, 1))

    def test_none_quarter_never_locked(self):
        self.assertFalse(is_quarter_locked(self.school_a, '7-А', MATH, None))
