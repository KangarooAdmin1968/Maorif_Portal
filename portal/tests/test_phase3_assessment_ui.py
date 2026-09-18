"""Phase 3 tests: assessment-aware teacher journal endpoints.

Covers:
    assessment_save  — create/edit Назорат assessments, auto-numbering,
                       quarter derivation, late АҶМ toggle, lock checks
    save_grade_ajax  — assessment_id/lesson_id validation, component input
                       ('8/9', '8/9/10'), identity/dedup, delete semantics,
                       permission and cross-scope rejection
    legacy behavior  — no assessment_id means the old daily path is intact
"""
import datetime

from django.urls import reverse

from portal.models import Assessment, ClassSubject, Grade, Lesson, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, D_Q2, MATH, TAJIK, GoldenBase, lock_quarter,
    make_class_subject, make_grade, make_quarter_grade,
)
from portal.views import (
    _all_student_gpas, calc_quarter_value, collect_quarter_inputs,
    parse_result_input,
)

AJAX_URL = reverse('save_grade_ajax')
ASSESS_URL = reverse('assessment_save')


def post_grade(client, student, score=None, assessment=None, subject=MATH,
               date=D_Q1, extra=None):
    data = {
        'student_id': student.id,
        'subject': subject,
        'date': date.isoformat() if date else '',
        'type': 'daily',
    }
    if score is not None:
        data['score'] = score
    if assessment is not None:
        data['assessment_id'] = assessment.id
    if extra:
        data.update(extra)
    return client.post(AJAX_URL, data)


def post_assessment(client, cs, date=D_Q1, title='', number=None, is_ajm=False,
                    assessment=None, extra=None):
    data = {
        'class_subject_id': cs.id,
        'date': date.isoformat() if date else '',
        'title': title,
        'is_ajm': '1' if is_ajm else '0',
    }
    if number is not None:
        data['number'] = number
    if assessment is not None:
        data['assessment_id'] = assessment.id
    if extra:
        data.update(extra)
    return client.post(ASSESS_URL, data)


def make_control(cs, date=D_Q1, quarter=1, number=None, is_ajm=False, title=''):
    return Assessment.objects.create(
        class_subject=cs, date=date, quarter=quarter, category='control',
        number=number, title=title, is_ajm=is_ajm,
    )


# ---------------------------------------------------------------------------
# parse_result_input — the server-side parser
# ---------------------------------------------------------------------------


class ParseResultInputTests(GoldenBase):
    def test_single_score(self):
        self.assertEqual(parse_result_input('9'), (9, None))

    def test_two_components(self):
        self.assertEqual(parse_result_input('8/9'), (8.5, [8, 9]))

    def test_three_components(self):
        self.assertEqual(parse_result_input('8/9/10'), (9.0, [8, 9, 10]))

    def test_empty_clears(self):
        self.assertEqual(parse_result_input(''), (None, None))
        self.assertEqual(parse_result_input('   '), (None, None))
        self.assertEqual(parse_result_input(None), (None, None))

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_result_input(' 8/9 '), (8.5, [8, 9]))

    def test_rejects_malformed(self):
        for bad in ('8/x', '0/9', '11/9', '8/', '/9', '8//9', '8/9/10/9',
                    '8.5/9', 'x/9', '8-9', '8;9'):
            with self.assertRaises(ValueError, msg=f'input {bad!r}'):
                parse_result_input(bad)

    def test_rejects_out_of_range_single(self):
        for bad in ('0', '11', 'abc'):
            with self.assertRaises(ValueError, msg=f'input {bad!r}'):
                parse_result_input(bad)

    def test_score_is_never_a_string(self):
        score, _ = parse_result_input('8/9')
        self.assertIsInstance(score, float)
        score, _ = parse_result_input('9')
        self.assertIsInstance(score, int)


# ---------------------------------------------------------------------------
# assessment_save — create / edit / auto-number / locks
# ---------------------------------------------------------------------------


class AssessmentSaveTests(GoldenBase):
    def setUp(self):
        self.client.force_login(self.teacher_a)

    # 1
    def test_create_control_assessment(self):
        resp = post_assessment(self.client, self.cs_a, title='Диктант')
        self.assertTrue(resp.json()['success'])
        a = Assessment.objects.get(class_subject=self.cs_a)
        self.assertEqual(a.category, 'control')
        self.assertEqual(a.title, 'Диктант')
        self.assertFalse(a.is_ajm)

    # 2
    def test_quarter_derived_from_date(self):
        post_assessment(self.client, self.cs_a, date=D_Q2)
        self.assertEqual(
            Assessment.objects.get(class_subject=self.cs_a).quarter, 2
        )

    # 3
    def test_category_always_control(self):
        post_assessment(self.client, self.cs_a, extra={'category': 'current'})
        self.assertEqual(
            Assessment.objects.get(class_subject=self.cs_a).category, 'control'
        )

    # 4
    def test_double_submit_no_duplicate(self):
        data = {'title': 'Кори назоравӣ', 'number': '3'}
        post_assessment(self.client, self.cs_a, **data)
        post_assessment(self.client, self.cs_a, **data)
        self.assertEqual(
            Assessment.objects.filter(class_subject=self.cs_a).count(), 1
        )

    def test_auto_number_increments(self):
        post_assessment(self.client, self.cs_a)
        post_assessment(self.client, self.cs_a, date=D_Q1_B)
        numbers = sorted(
            Assessment.objects.filter(class_subject=self.cs_a)
            .values_list('number', flat=True)
        )
        self.assertEqual(numbers, [1, 2])

    def test_explicit_number_respected(self):
        post_assessment(self.client, self.cs_a, number='5')
        self.assertEqual(
            Assessment.objects.get(class_subject=self.cs_a).number, 5
        )

    def test_edit_title_number_ajm(self):
        a = make_control(self.cs_a, number=1)
        resp = post_assessment(
            self.client, self.cs_a, assessment=a,
            title='Нав', number='2', is_ajm=True,
        )
        self.assertTrue(resp.json()['success'])
        a.refresh_from_db()
        self.assertEqual((a.title, a.number, a.is_ajm), ('Нав', 2, True))

    def test_edit_preserves_original_date_and_quarter(self):
        # Regression: the UI always posts the viewed journal date; editing
        # must not silently move the assessment to that date/quarter.
        a = make_control(self.cs_a, date=D_Q1, quarter=1, number=1)
        resp = post_assessment(
            self.client, self.cs_a, assessment=a, date=D_Q2,
            title='Таҳрир', number='4', is_ajm=True,
        )
        self.assertTrue(resp.json()['success'])
        a.refresh_from_db()
        self.assertEqual(a.date, D_Q1)
        self.assertEqual(a.quarter, 1)
        self.assertEqual((a.title, a.number, a.is_ajm), ('Таҳрир', 4, True))

    def test_unauthorized_teacher_cannot_create(self):
        self.client.force_login(self.teacher_b)
        resp = post_assessment(self.client, self.cs_a)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            Assessment.objects.filter(class_subject=self.cs_a).exists()
        )

    # 32
    def test_locked_quarter_blocks_create(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_assessment(self.client, self.cs_a, date=D_Q1)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(
            Assessment.objects.filter(class_subject=self.cs_a).exists()
        )

    def test_locked_quarter_blocks_edit(self):
        a = make_control(self.cs_a)
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_assessment(self.client, self.cs_a, assessment=a, is_ajm=True)
        self.assertFalse(resp.json()['success'])
        a.refresh_from_db()
        self.assertFalse(a.is_ajm)


# ---------------------------------------------------------------------------
# save_grade_ajax — assessment-linked writes
# ---------------------------------------------------------------------------


class AssessmentGradeSaveTests(GoldenBase):
    def setUp(self):
        self.client.force_login(self.teacher_a)
        self.a = make_control(self.cs_a, number=1)

    # 5
    def test_single_score(self):
        resp = post_grade(self.client, self.s1, score='9', assessment=self.a)
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.score, 9)
        self.assertIsNone(g.components)

    # 6
    def test_two_components(self):
        post_grade(self.client, self.s1, score='8/9', assessment=self.a)
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.score, 8.5)
        self.assertEqual(g.components, [8, 9])

    # 7
    def test_three_components(self):
        post_grade(self.client, self.s1, score='8/9/10', assessment=self.a)
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.score, 9.0)
        self.assertEqual(g.components, [8, 9, 10])

    # 8-15
    def test_invalid_components_rejected(self):
        for bad in ('8/x', '0/9', '11/9', '8/', '/9', '8//9', '8/9/10/9', '8.5/9'):
            resp = post_grade(self.client, self.s1, score=bad, assessment=self.a)
            self.assertFalse(resp.json()['success'], f'input {bad!r}')
        self.assertFalse(
            Grade.objects.filter(student=self.s1, assessment=self.a).exists()
        )

    # 16 — legacy daily path unchanged
    def test_daily_slash_still_rejected(self):
        resp = post_grade(self.client, self.s1, score='8/9')
        self.assertFalse(resp.json()['success'])
        # No score was stored and no assessment link was created.
        self.assertFalse(
            Grade.objects.filter(student=self.s1).exclude(score=None).exists()
        )
        self.assertFalse(
            Grade.objects.filter(student=self.s1, assessment__isnull=False).exists()
        )

    def test_grade_date_comes_from_assessment(self):
        post_grade(self.client, self.s1, score='9', assessment=self.a, date=D_Q2)
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.date, self.a.date)

    # 17
    def test_repeat_save_converges_to_one_row(self):
        post_grade(self.client, self.s1, score='8', assessment=self.a)
        post_grade(self.client, self.s1, score='8', assessment=self.a)
        post_grade(self.client, self.s1, score='8/9', assessment=self.a)
        self.assertEqual(
            Grade.objects.filter(student=self.s1, assessment=self.a).count(), 1
        )
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.score, 8.5)

    # 18
    def test_two_assessments_separate_rows(self):
        b = make_control(self.cs_a, date=D_Q1_B, number=2)
        post_grade(self.client, self.s1, score='8', assessment=self.a)
        post_grade(self.client, self.s1, score='9', assessment=b)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 2)

    def test_linked_row_coexists_with_daily_row(self):
        post_grade(self.client, self.s1, score='7')
        post_grade(self.client, self.s1, score='9', assessment=self.a)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 2)
        daily = Grade.objects.get(student=self.s1, assessment__isnull=True)
        self.assertIsNone(daily.assessment_id)

    # 19-20
    def test_clearing_removes_only_targeted_grade(self):
        post_grade(self.client, self.s1, score='7')
        post_grade(self.client, self.s1, score='8', assessment=self.a)
        resp = post_grade(self.client, self.s1, score='', assessment=self.a)
        self.assertTrue(resp.json()['success'])
        self.assertFalse(
            Grade.objects.filter(student=self.s1, assessment=self.a).exists()
        )
        # Assessment itself and the daily row survive.
        self.assertTrue(Assessment.objects.filter(pk=self.a.pk).exists())
        self.assertTrue(
            Grade.objects.filter(student=self.s1, assessment__isnull=True).exists()
        )

    # 27
    def test_linked_grade_carries_attendance_behavior(self):
        post_grade(
            self.client, self.s1, score='8', assessment=self.a,
            extra={'attendance': '+', 'behavior_score': '4'},
        )
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual((g.attendance, g.behavior_score), ('+', 4))

    # 33
    def test_locked_quarter_blocks_linked_save(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_grade(self.client, self.s1, score='9', assessment=self.a)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(
            Grade.objects.filter(student=self.s1, assessment=self.a).exists()
        )

    # 28-30 — forged IDs
    def test_cross_school_assessment_rejected(self):
        foreign = make_control(self.cs_b)
        resp = post_grade(self.client, self.s1, score='9', assessment=foreign)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_cross_class_assessment_rejected(self):
        cs_8a = make_class_subject(self.school_a, '8-А', MATH)
        foreign = make_control(cs_8a)
        resp = post_grade(self.client, self.s1, score='9', assessment=foreign)
        self.assertFalse(resp.json()['success'])

    def test_cross_subject_assessment_rejected(self):
        cs_tajik = make_class_subject(self.school_a, '7-А', TAJIK)
        foreign = make_control(cs_tajik)
        resp = post_grade(self.client, self.s1, score='9', assessment=foreign)
        self.assertFalse(resp.json()['success'])

    def test_nonexistent_assessment_rejected(self):
        resp = post_grade(
            self.client, self.s1, score='9',
            extra={'assessment_id': '999999'},
        )
        self.assertFalse(resp.json()['success'])

    # 31 — lesson validation
    def test_cross_class_subject_lesson_rejected(self):
        cs_8a = make_class_subject(self.school_a, '8-А', MATH)
        foreign_lesson = Lesson.objects.create(
            class_subject=cs_8a, date=D_Q1, lesson_number=1,
        )
        resp = post_grade(
            self.client, self.s1, score='9', assessment=self.a,
            extra={'lesson_id': foreign_lesson.id},
        )
        self.assertFalse(resp.json()['success'])

    def test_lesson_without_assessment_rejected(self):
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1,
        )
        resp = post_grade(
            self.client, self.s1, score='9',
            extra={'lesson_id': lesson.id},
        )
        self.assertFalse(resp.json()['success'])

    def test_valid_lesson_accepted(self):
        lesson = Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1,
        )
        resp = post_grade(
            self.client, self.s1, score='9', assessment=self.a,
            extra={'lesson_id': lesson.id},
        )
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, assessment=self.a)
        self.assertEqual(g.lesson_id, lesson.id)


# ---------------------------------------------------------------------------
# Late АҶМ toggle + Phase 2 integration through the endpoints
# ---------------------------------------------------------------------------


class AjmToggleIntegrationTests(GoldenBase):
    def setUp(self):
        self.client.force_login(self.teacher_a)
        self.a = make_control(self.cs_a, number=1)

    # 21-23
    def test_toggle_off_to_on_moves_pool_no_row_changes(self):
        make_grade(self.s1, score=9, date=D_Q1)
        make_grade(self.s1, score=9, date=D_Q1_B)
        post_grade(self.client, self.s1, score='5', assessment=self.a)
        count_before = Grade.objects.filter(student=self.s1).count()

        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertAlmostEqual(
            calc_quarter_value(current, ajm), (9 + 9 + 5) / 3
        )

        resp = post_assessment(
            self.client, self.cs_a, assessment=self.a, is_ajm=True
        )
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Grade.objects.filter(student=self.s1).count(), count_before
        )
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertAlmostEqual(calc_quarter_value(current, ajm), 7.0)

    # 22
    def test_toggle_on_to_off_restores_current_pool(self):
        self.a.is_ajm = True
        self.a.save()
        make_grade(self.s1, score=9, date=D_Q1)
        post_grade(self.client, self.s1, score='5', assessment=self.a)
        count_before = Grade.objects.filter(student=self.s1).count()

        post_assessment(self.client, self.cs_a, assessment=self.a, is_ajm=False)
        self.assertEqual(
            Grade.objects.filter(student=self.s1).count(), count_before
        )
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(ajm, [])
        self.assertAlmostEqual(calc_quarter_value(current, ajm), 7.0)

    # 24
    def test_multiple_ajm_all_counted(self):
        a2 = make_control(self.cs_a, date=D_Q1_B, number=2, is_ajm=True)
        a3 = make_control(self.cs_a, date=D_Q2, quarter=1, number=3, is_ajm=True)
        self.a.is_ajm = True
        self.a.save()
        post_grade(self.client, self.s1, score='8', assessment=self.a)
        post_grade(self.client, self.s1, score='9', assessment=a2)
        post_grade(self.client, self.s1, score='10', assessment=a3)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(sorted(ajm), [8.0, 9.0, 10.0])

    # 25-26
    def test_component_grade_one_result_and_one_ranking_contribution(self):
        self.a.is_ajm = True
        self.a.save()
        post_grade(self.client, self.s1, score='8/9', assessment=self.a)
        _, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(ajm, [8.5])
        make_grade(self.s1, score=10, date=D_Q1)
        # GPA pools Grade.score: mean(8.5, 10) = 9.25 — components never leak.
        self.assertEqual(_all_student_gpas()[self.s1.id], 9.25)


# ---------------------------------------------------------------------------
# Legacy compatibility + journal page context
# ---------------------------------------------------------------------------


class LegacyAndContextTests(GoldenBase):
    def setUp(self):
        self.client.force_login(self.teacher_a)

    # 34
    def test_legacy_payload_still_works(self):
        resp = post_grade(self.client, self.s1, score='9')
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1)
        self.assertIsNone(g.assessment_id)
        self.assertIsNone(g.components)

    # 35 — replayed pending payload (same shape as localStorage params)
    def test_pending_payload_with_assessment_replays(self):
        a = make_control(self.cs_a, number=1)
        params = {
            'student_id': self.s1.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'type': 'daily',
            'assessment_id': a.id, 'score': '8/9',
        }
        resp = self.client.post(AJAX_URL, params)
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, assessment=a)
        self.assertEqual((g.score, g.components), (8.5, [8, 9]))

    # 36
    def test_official_quartergrade_survives_journal_load(self):
        make_quarter_grade(self.s1, quarter=1, grade=10)
        a = make_control(self.cs_a, number=1, is_ajm=True)
        post_grade(self.client, self.s1, score='8', assessment=a)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['quarter_grades'][self.s1.id][1], 10)
        self.assertNotIn(1, resp.context['live_quarter_flags'][self.s1.id])
        self.assertEqual(
            QuarterGrade.objects.get(student=self.s1, quarter=1).grade, 10
        )

    def test_journal_context_exposes_control_assessments(self):
        a = make_control(self.cs_a, number=1, title='Диктант', is_ajm=True)
        post_grade(self.client, self.s1, score='8/9', assessment=a)
        resp = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '7-А', MATH]
        ))
        self.assertEqual(resp.status_code, 200)
        items = resp.context['control_assessments']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], a.id)
        self.assertTrue(items[0]['is_ajm'])
        self.assertIn('АҶМ', items[0]['label'])
        self.assertEqual(
            resp.context['assessment_grade_map'][a.id][self.s1.id], '8/9'
        )
