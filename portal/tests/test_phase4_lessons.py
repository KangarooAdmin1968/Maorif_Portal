"""Phase 4A tests: multiple independent same-subject lessons per day.

Scope: backend write-path only.
    lesson_save / lesson_delete   — creation + block-when-data-exists delete
    save_grade_ajax               — lesson_id without assessment_id (Ҷорӣ)
    mode exclusivity              — lesson-scoped for new records,
                                   date-wide for legacy lesson-less ones
    assessment_save               — lesson/date consistency validation
    quarter pooling               — every scored Grade row counts once

Methodology frozen here:
    same student + same subject + same date + Lesson 1
    is fully independent from
    same student + same subject + same date + Lesson 2.
"""
from django.urls import reverse

from portal.models import Assessment, ClassSubject, Grade, Lesson, QuarterGrade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, MATH, PERIOD, TAJIK, GoldenBase, lock_quarter,
    make_class_subject, make_grade, make_quarter_grade,
)
from portal.views import calc_quarter_value, collect_quarter_inputs

AJAX_URL = reverse('save_grade_ajax')
ASSESS_URL = reverse('assessment_save')
LESSON_SAVE_URL = reverse('lesson_save')
LESSON_DELETE_URL = reverse('lesson_delete')


def make_lesson(class_subject, date=D_Q1, lesson_number=1, topic=''):
    return Lesson.objects.create(
        class_subject=class_subject, date=date,
        lesson_number=lesson_number, topic=topic,
    )


def make_control(cs, date=D_Q1, quarter=1, number=None, is_ajm=False,
                 title='', lesson=None):
    return Assessment.objects.create(
        class_subject=cs, date=date, quarter=quarter, category='control',
        number=number, title=title, is_ajm=is_ajm, lesson=lesson,
    )


def post_lesson(client, cs, date=D_Q1, number=None, topic='', extra=None):
    data = {
        'class_subject_id': cs.id,
        'date': date.isoformat() if date else '',
        'topic': topic,
    }
    if number is not None:
        data['lesson_number'] = number
    if extra:
        data.update(extra)
    return client.post(LESSON_SAVE_URL, data)


def post_grade(client, student, score=None, assessment=None, lesson=None,
               subject=MATH, date=D_Q1, extra=None):
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
    if lesson is not None:
        data['lesson_id'] = lesson.id
    if extra:
        data.update(extra)
    return client.post(AJAX_URL, data)


def post_assessment(client, cs, date=D_Q1, title='', number=None,
                    is_ajm=False, assessment=None, lesson=None, extra=None):
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
    if lesson is not None:
        data['lesson_id'] = lesson.id
    if extra:
        data.update(extra)
    return client.post(ASSESS_URL, data)


# ---------------------------------------------------------------------------
# lesson_save — creation, numbering, idempotency, permission, lock
# ---------------------------------------------------------------------------


class LessonSaveTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def test_create_lesson_1(self):
        resp = post_lesson(self.client, self.cs_a)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['lesson']['lesson_number'], 1)
        self.assertEqual(data['lesson']['date'], D_Q1.isoformat())
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 1
        )

    def test_create_lesson_2_same_date(self):
        post_lesson(self.client, self.cs_a)
        resp = post_lesson(self.client, self.cs_a)
        self.assertEqual(resp.json()['lesson']['lesson_number'], 2)

    def test_create_lesson_3_same_date(self):
        for _ in range(2):
            post_lesson(self.client, self.cs_a)
        resp = post_lesson(self.client, self.cs_a)
        self.assertEqual(resp.json()['lesson']['lesson_number'], 3)
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 3
        )

    def test_auto_number_skips_existing(self):
        make_lesson(self.cs_a, D_Q1, 1)
        make_lesson(self.cs_a, D_Q1, 3)
        resp = post_lesson(self.client, self.cs_a)
        self.assertEqual(resp.json()['lesson']['lesson_number'], 4)

    def test_explicit_lesson_number(self):
        resp = post_lesson(self.client, self.cs_a, number='5')
        self.assertTrue(resp.json()['success'])
        self.assertEqual(resp.json()['lesson']['lesson_number'], 5)

    def test_duplicate_lesson_number_idempotent(self):
        first = post_lesson(self.client, self.cs_a, number='1').json()['lesson']
        second = post_lesson(self.client, self.cs_a, number='1').json()['lesson']
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 1
        )

    def test_invalid_lesson_number_rejected(self):
        for bad in ('0', '-1', 'abc'):
            resp = post_lesson(self.client, self.cs_a, number=bad)
            self.assertFalse(resp.json()['success'], f'number {bad!r}')
        self.assertFalse(Lesson.objects.filter(class_subject=self.cs_a).exists())

    def test_same_number_other_date_allowed(self):
        post_lesson(self.client, self.cs_a, date=D_Q1, number='1')
        resp = post_lesson(self.client, self.cs_a, date=D_Q1_B, number='1')
        self.assertTrue(resp.json()['success'])
        self.assertEqual(Lesson.objects.filter(class_subject=self.cs_a).count(), 2)

    def test_cross_school_rejected(self):
        self.client.force_login(self.teacher_b)
        resp = post_lesson(self.client, self.cs_a)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Lesson.objects.filter(class_subject=self.cs_a).exists())

    def test_cross_class_rejected(self):
        cs_8a = make_class_subject(self.school_a, '8-А', MATH)
        resp = post_lesson(self.client, cs_8a)
        self.assertEqual(resp.status_code, 403)

    def test_cross_subject_rejected(self):
        cs_tajik = make_class_subject(self.school_a, '7-А', TAJIK)
        resp = post_lesson(self.client, cs_tajik)
        self.assertEqual(resp.status_code, 403)

    def test_inactive_classsubject_rejected(self):
        self.cs_a.is_active = False
        self.cs_a.save()
        resp = post_lesson(self.client, self.cs_a)
        self.assertFalse(resp.json()['success'])

    def test_locked_quarter_blocks_creation(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_lesson(self.client, self.cs_a)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Lesson.objects.filter(class_subject=self.cs_a).exists())

    def test_anonymous_redirects(self):
        self.client.logout()
        resp = post_lesson(self.client, self.cs_a)
        self.assertEqual(resp.status_code, 302)

    def test_get_method_not_allowed(self):
        resp = self.client.get(LESSON_SAVE_URL)
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# lesson_delete — block-when-data-exists
# ---------------------------------------------------------------------------


class LessonDeleteTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.lesson = make_lesson(self.cs_a, D_Q1, 1)

    def _delete(self, lesson=None):
        return self.client.post(
            LESSON_DELETE_URL, {'lesson_id': (lesson or self.lesson).id}
        )

    def test_empty_lesson_deleted(self):
        resp = self._delete()
        self.assertTrue(resp.json()['success'])
        self.assertFalse(Lesson.objects.filter(pk=self.lesson.pk).exists())

    def test_lesson_with_grade_blocked(self):
        post_grade(self.client, self.s1, score='9', lesson=self.lesson)
        resp = self._delete()
        self.assertFalse(resp.json()['success'])
        self.assertTrue(Lesson.objects.filter(pk=self.lesson.pk).exists())
        self.assertTrue(Grade.objects.filter(lesson=self.lesson).exists())

    def test_lesson_with_assessment_blocked(self):
        make_control(self.cs_a, lesson=self.lesson)
        resp = self._delete()
        self.assertFalse(resp.json()['success'])
        self.assertTrue(Lesson.objects.filter(pk=self.lesson.pk).exists())
        # The linked assessment is untouched — no silent SET_NULL cleanup.
        self.assertEqual(
            Assessment.objects.filter(lesson=self.lesson).count(), 1
        )

    def test_locked_quarter_blocks_delete(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = self._delete()
        self.assertFalse(resp.json()['success'])
        self.assertTrue(Lesson.objects.filter(pk=self.lesson.pk).exists())

    def test_cross_school_delete_rejected(self):
        self.client.force_login(self.teacher_b)
        resp = self._delete()
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Lesson.objects.filter(pk=self.lesson.pk).exists())

    def test_nonexistent_lesson_rejected(self):
        resp = self.client.post(LESSON_DELETE_URL, {'lesson_id': '999999'})
        self.assertFalse(resp.json()['success'])


# ---------------------------------------------------------------------------
# Daily grade isolation — the core Phase 4 requirement
# ---------------------------------------------------------------------------


class LessonGradeIsolationTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)
        self.l2 = make_lesson(self.cs_a, D_Q1, 2)

    def test_two_lessons_same_day_coexist(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        rows = Grade.objects.filter(student=self.s1, subject=MATH, date=D_Q1)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            rows.get(lesson=self.l1).score, 8
        )
        self.assertEqual(
            rows.get(lesson=self.l2).score, 10
        )

    def test_three_lessons_same_day_coexist(self):
        l3 = make_lesson(self.cs_a, D_Q1, 3)
        for lesson, score in ((self.l1, 7), (self.l2, 8), (l3, 9)):
            post_grade(self.client, self.s1, score=str(score), lesson=lesson)
        self.assertEqual(
            Grade.objects.filter(student=self.s1, date=D_Q1).count(), 3
        )

    def test_saving_lesson_1_updates_only_lesson_1(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        post_grade(self.client, self.s1, score='9', lesson=self.l1)
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(lesson=self.l1).score, 9)
        self.assertEqual(rows.get(lesson=self.l2).score, 10)

    def test_saving_lesson_2_updates_only_lesson_2(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        post_grade(self.client, self.s1, score='5', lesson=self.l2)
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.get(lesson=self.l1).score, 8)
        self.assertEqual(rows.get(lesson=self.l2).score, 5)

    def test_clearing_lesson_1_keeps_lesson_2(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        resp = post_grade(self.client, self.s1, score='', lesson=self.l1)
        self.assertTrue(resp.json()['success'])
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().lesson_id, self.l2.id)
        self.assertEqual(rows.get().score, 10)

    def test_response_echoes_lesson_identity(self):
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l2)
        data = resp.json()
        self.assertEqual(data['lesson_id'], self.l2.id)
        self.assertEqual(data['lesson_number'], 2)

    def test_legacy_response_shape_unchanged(self):
        resp = post_grade(self.client, self.s1, score='9')
        self.assertEqual(set(resp.json()), {'success', 'saved'})

    def test_cross_school_lesson_rejected(self):
        foreign = make_lesson(self.cs_b, D_Q1, 1)
        resp = post_grade(self.client, self.s1, score='9', lesson=foreign)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_cross_class_lesson_rejected(self):
        cs_8a = make_class_subject(self.school_a, '8-А', MATH)
        foreign = make_lesson(cs_8a, D_Q1, 1)
        resp = post_grade(self.client, self.s1, score='9', lesson=foreign)
        self.assertFalse(resp.json()['success'])

    def test_cross_subject_lesson_rejected(self):
        cs_tajik = make_class_subject(self.school_a, '7-А', TAJIK)
        foreign = make_lesson(cs_tajik, D_Q1, 1)
        resp = post_grade(self.client, self.s1, score='9', lesson=foreign)
        self.assertFalse(resp.json()['success'])

    def test_lesson_date_mismatch_rejected(self):
        other_day = make_lesson(self.cs_a, D_Q1_B, 1)
        resp = post_grade(self.client, self.s1, score='9', date=D_Q1, lesson=other_day)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_nonexistent_lesson_rejected(self):
        resp = post_grade(self.client, self.s1, score='9', extra={'lesson_id': '999999'})
        self.assertFalse(resp.json()['success'])

    def test_locked_quarter_blocks_lesson_save(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l1)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())


# ---------------------------------------------------------------------------
# Lesson-scoped Ҷорӣ / Назорат exclusivity
# ---------------------------------------------------------------------------


class LessonModeExclusivityTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)
        self.l2 = make_lesson(self.cs_a, D_Q1, 2)

    def test_control_on_lesson_1_blocks_current_on_lesson_1(self):
        make_control(self.cs_a, date=D_Q1, lesson=self.l1)
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l1)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Grade.objects.filter(lesson=self.l1).exists())

    def test_control_on_lesson_1_allows_current_on_lesson_2(self):
        make_control(self.cs_a, date=D_Q1, lesson=self.l1)
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l2)
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, lesson=self.l2)
        self.assertEqual(g.score, 9)
        self.assertIsNone(g.assessment_id)

    def test_control_on_lesson_2_allows_current_on_lesson_1(self):
        make_control(self.cs_a, date=D_Q1, lesson=self.l2)
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l1)
        self.assertTrue(resp.json()['success'])

    def test_legacy_lessonless_assessment_still_blocks_legacy_daily(self):
        # Backward compatibility: a Phase 3 lesson-less assessment keeps the
        # old date-wide protection for the lesson-less slot.
        make_control(self.cs_a, date=D_Q1)
        resp = post_grade(self.client, self.s1, score='9')
        self.assertFalse(resp.json()['success'])

    def test_legacy_lessonless_assessment_does_not_block_lessons(self):
        # Lesson-aware records are lesson-scoped; the legacy assessment owns
        # only the lesson-less slot of that date.
        make_control(self.cs_a, date=D_Q1)
        resp = post_grade(self.client, self.s1, score='9', lesson=self.l2)
        self.assertTrue(resp.json()['success'])

    def test_lesson_assessment_does_not_block_legacy_daily(self):
        # A lesson-scoped control must not hijack the whole date: the
        # lesson-less Ҷорӣ slot stays open.
        make_control(self.cs_a, date=D_Q1, lesson=self.l1)
        resp = post_grade(self.client, self.s1, score='9')
        self.assertTrue(resp.json()['success'])

    def test_both_lessons_can_be_control(self):
        a1 = make_control(self.cs_a, date=D_Q1, lesson=self.l1, number=1)
        a2 = make_control(self.cs_a, date=D_Q1, lesson=self.l2, number=2)
        post_grade(self.client, self.s1, score='8', assessment=a1)
        post_grade(self.client, self.s1, score='9', assessment=a2)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 2)
        self.assertEqual(
            Grade.objects.get(student=self.s1, lesson=self.l1).score, 8
        )
        self.assertEqual(
            Grade.objects.get(student=self.s1, lesson=self.l2).score, 9
        )


# ---------------------------------------------------------------------------
# Legacy safety — unlinked rows stay isolated from linked rows
# ---------------------------------------------------------------------------


class LegacyIsolationTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)

    def test_legacy_save_still_works(self):
        resp = post_grade(self.client, self.s1, score='9')
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1)
        self.assertIsNone(g.lesson_id)
        self.assertIsNone(g.assessment_id)

    def test_legacy_save_does_not_merge_lesson_row(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        resp = post_grade(self.client, self.s1, score='9')
        self.assertTrue(resp.json()['success'])
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(lesson=self.l1).score, 8)
        self.assertEqual(rows.get(lesson__isnull=True).score, 9)

    def test_legacy_clear_does_not_touch_lesson_row(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='9')
        post_grade(self.client, self.s1, score='')
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().lesson_id, self.l1.id)

    def test_assessment_row_never_merged_into_legacy(self):
        # A non-АҶМ control grade and a legacy daily row on the same date
        # stay two distinct rows (assessment lives on D_Q1_B so the legacy
        # exclusivity guard does not fire for D_Q1 writes).
        a = make_control(self.cs_a, date=D_Q1_B)
        post_grade(self.client, self.s1, score='8', assessment=a)
        post_grade(self.client, self.s1, score='9', date=D_Q1)
        self.assertEqual(Grade.objects.filter(student=self.s1).count(), 2)
        linked = Grade.objects.get(student=self.s1, assessment=a)
        legacy = Grade.objects.get(student=self.s1, assessment__isnull=True)
        self.assertEqual((linked.score, legacy.score), (8, 9))

    def test_grade_entry_post_skips_linked_rows(self):
        # The batch Сабт form must not hijack a lesson-linked row: its
        # lookup is pinned to lesson=None/assessment=None.
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        resp = self.client.post(
            reverse('grade_entry', args=[self.school_a.id, '7-А', MATH]),
            {'date': D_Q1.isoformat(), f'score_{self.s1.id}': '9'},
        )
        self.assertEqual(resp.status_code, 302)
        rows = Grade.objects.filter(student=self.s1, date=D_Q1)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.get(lesson=self.l1).score, 8)
        self.assertEqual(rows.get(lesson__isnull=True).score, 9)


# ---------------------------------------------------------------------------
# grade_entry — lesson-scoped view context (Phase 4B)
# ---------------------------------------------------------------------------


class GradeEntryLessonContextTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)
        self.l2 = make_lesson(self.cs_a, D_Q1, 2)
        self.url = reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А', 'subject': MATH,
        })

    def _get(self, **params):
        return self.client.get(self.url, params)

    def test_lessons_listed_in_context(self):
        resp = self._get(date=D_Q1.isoformat())
        self.assertEqual(resp.status_code, 200)
        lessons = resp.context['lessons']
        self.assertEqual([l['lesson_number'] for l in lessons], [1, 2])

    def test_selected_lesson_filters_daily_grades(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        resp = self._get(date=D_Q1.isoformat(), lesson=self.l1.id)
        self.assertEqual(resp.context['daily_grades'].get(self.s1.id), 8)
        resp = self._get(date=D_Q1.isoformat(), lesson=self.l2.id)
        self.assertEqual(resp.context['daily_grades'].get(self.s1.id), 10)

    def test_no_lesson_shows_legacy_slot(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='9')
        resp = self._get(date=D_Q1.isoformat())
        self.assertIsNone(resp.context['selected_lesson'])
        self.assertEqual(resp.context['daily_grades'].get(self.s1.id), 9)

    def test_ln_param_resolves_lesson_number(self):
        resp = self._get(date=D_Q1.isoformat(), ln='2')
        self.assertEqual(resp.context['selected_lesson'].id, self.l2.id)

    def test_ln_missing_falls_back_to_legacy(self):
        resp = self._get(date=D_Q1_B.isoformat(), ln='2')
        self.assertIsNone(resp.context['selected_lesson'])

    def test_foreign_lesson_param_ignored(self):
        foreign = make_lesson(self.cs_b, D_Q1, 1)
        resp = self._get(date=D_Q1.isoformat(), lesson=foreign.id)
        self.assertIsNone(resp.context['selected_lesson'])

    def test_lesson_mode_flag_control_only_for_its_lesson(self):
        make_control(self.cs_a, date=D_Q1, lesson=self.l1)
        resp = self._get(date=D_Q1.isoformat(), lesson=self.l1.id)
        self.assertTrue(resp.context['date_is_control'])
        resp = self._get(date=D_Q1.isoformat(), lesson=self.l2.id)
        self.assertFalse(resp.context['date_is_control'])
        resp = self._get(date=D_Q1.isoformat())
        self.assertFalse(resp.context['date_is_control'])

    def test_empty_lesson_marks_has_data_false(self):
        resp = self._get(date=D_Q1.isoformat())
        l1 = next(l for l in resp.context['lessons'] if l['lesson_number'] == 1)
        self.assertFalse(l1['has_data'])
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        resp = self._get(date=D_Q1.isoformat())
        l1 = next(l for l in resp.context['lessons'] if l['lesson_number'] == 1)
        self.assertTrue(l1['has_data'])

    def test_batch_post_writes_lesson_slot(self):
        resp = self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            'lesson_id': self.l1.id,
            f'score_{self.s1.id}': '7',
        })
        self.assertEqual(resp.status_code, 302)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertEqual(g.lesson_id, self.l1.id)
        self.assertEqual(g.score, 7)

    def test_batch_post_foreign_lesson_rejected(self):
        foreign = make_lesson(self.cs_b, D_Q1, 1)
        resp = self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            'lesson_id': foreign.id,
            f'score_{self.s1.id}': '7',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_batch_post_wrong_date_lesson_rejected(self):
        other_day = make_lesson(self.cs_a, D_Q1_B, 1)
        resp = self.client.post(self.url, {
            'date': D_Q1.isoformat(),
            'lesson_id': other_day.id,
            f'score_{self.s1.id}': '7',
        })
        self.assertEqual(resp.status_code, 400)

    def test_batch_post_lesson_scoped_control_block(self):
        make_control(self.cs_a, date=D_Q1, lesson=self.l1)
        self.client.post(self.url, {
            'date': D_Q1.isoformat(), 'lesson_id': self.l1.id,
            f'score_{self.s1.id}': '7',
        })
        self.assertFalse(Grade.objects.filter(lesson=self.l1).exists())
        # The other lesson's slot is unaffected.
        self.client.post(self.url, {
            'date': D_Q1.isoformat(), 'lesson_id': self.l2.id,
            f'score_{self.s1.id}': '7',
        })
        self.assertTrue(Grade.objects.filter(lesson=self.l2).exists())


# ---------------------------------------------------------------------------
# assessment_save — lesson attach + validation
# ---------------------------------------------------------------------------


class AssessmentLessonTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)
        self.l2 = make_lesson(self.cs_a, D_Q1, 2)

    def test_assessment_attach_lesson_1(self):
        resp = post_assessment(self.client, self.cs_a, lesson=self.l1)
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Assessment.objects.get(class_subject=self.cs_a).lesson_id, self.l1.id
        )

    def test_assessment_attach_lesson_2_same_date(self):
        post_assessment(self.client, self.cs_a, lesson=self.l1)
        resp = post_assessment(self.client, self.cs_a, lesson=self.l2)
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Assessment.objects.filter(class_subject=self.cs_a).count(), 2
        )
        self.assertEqual(
            Assessment.objects.filter(lesson=self.l2).count(), 1
        )

    def test_assessment_lesson_date_mismatch_rejected(self):
        other_day = make_lesson(self.cs_a, D_Q1_B, 1)
        resp = post_assessment(self.client, self.cs_a, date=D_Q1, lesson=other_day)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.filter(class_subject=self.cs_a).exists())

    def test_assessment_edit_lesson_date_mismatch_rejected(self):
        a = make_control(self.cs_a, date=D_Q1, number=1)
        other_day = make_lesson(self.cs_a, D_Q1_B, 1)
        resp = post_assessment(
            self.client, self.cs_a, assessment=a, lesson=other_day
        )
        self.assertFalse(resp.json()['success'])
        a.refresh_from_db()
        self.assertIsNone(a.lesson_id)

    def test_cross_subject_lesson_rejected_on_assessment(self):
        cs_tajik = make_class_subject(self.school_a, '7-А', TAJIK)
        foreign = make_lesson(cs_tajik, D_Q1, 1)
        resp = post_assessment(self.client, self.cs_a, lesson=foreign)
        self.assertFalse(resp.json()['success'])

    def test_lessonless_assessment_unchanged(self):
        resp = post_assessment(self.client, self.cs_a)
        self.assertTrue(resp.json()['success'])
        self.assertIsNone(
            Assessment.objects.get(class_subject=self.cs_a).lesson_id
        )

    def test_grade_on_lesson_1_not_exposed_to_lesson_2(self):
        a1 = make_control(self.cs_a, date=D_Q1, lesson=self.l1, number=1)
        a2 = make_control(self.cs_a, date=D_Q1, lesson=self.l2, number=2)
        post_grade(self.client, self.s1, score='8', assessment=a1)
        post_grade(self.client, self.s1, score='9', assessment=a2)
        # Forging a1's grade onto l2 is rejected by the scope check.
        resp = post_grade(
            self.client, self.s1, score='10',
            assessment=a1, extra={'lesson_id': self.l2.id},
        )
        self.assertFalse(resp.json()['success'])
        self.assertEqual(
            Grade.objects.get(student=self.s1, assessment=a1).score, 8
        )


# ---------------------------------------------------------------------------
# Quarter pooling — each scored row counts once, lesson-aware or not
# ---------------------------------------------------------------------------


class LessonQuarterPoolingTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.l1 = make_lesson(self.cs_a, D_Q1, 1)
        self.l2 = make_lesson(self.cs_a, D_Q1, 2)

    def test_two_lesson_grades_both_count_toward_at(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10', lesson=self.l2)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(sorted(current), [8.0, 10.0])
        self.assertEqual(ajm, [])
        self.assertEqual(calc_quarter_value(current, ajm), 9.0)

    def test_one_lesson_grade_one_contribution(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        current, _ = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [8.0])

    def test_control_lesson_stays_separate_from_current(self):
        a2 = make_control(self.cs_a, date=D_Q1, lesson=self.l2, is_ajm=True)
        post_grade(self.client, self.s1, score='9', lesson=self.l1)
        post_grade(self.client, self.s1, score='5', assessment=a2)
        current, ajm = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [9.0])
        self.assertEqual(ajm, [5.0])
        # АҶЧ = (АТ 9 + АҶМ 5) / 2 = 7
        self.assertEqual(calc_quarter_value(current, ajm), 7.0)

    def test_legacy_and_lesson_grades_pool_together(self):
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='10')
        current, _ = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(sorted(current), [8.0, 10.0])

    def test_no_same_day_collapse(self):
        # Two scored rows on the same date are never deduplicated by date.
        post_grade(self.client, self.s1, score='8', lesson=self.l1)
        post_grade(self.client, self.s1, score='8', lesson=self.l2)
        current, _ = collect_quarter_inputs(self.s1, MATH, 1)
        self.assertEqual(current, [8.0, 8.0])


# ---------------------------------------------------------------------------
# Offline storage — lesson-aware keys; legacy key format untouched
# ---------------------------------------------------------------------------


class OfflineLessonKeyTests(GoldenBase):
    """Pin the lesson-scoped localStorage contract in grade_entry.html.

    Lesson 1 and Lesson 2 drafts/pending queues live under different keys
    (suffix _l<lesson_id>) so they can never overwrite each other; the
    legacy suffix-less key keeps working for the lesson-less slot and for
    payloads queued before lesson mode existed.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def _js(self, **params):
        resp = self.client.get(
            reverse('grade_entry', args=[self.school_a.id, '7-А', MATH]),
            params,
        )
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode('utf-8')

    def test_lesson_suffix_appended_to_key(self):
        js = self._js()
        self.assertIn("k += `_l${selectedLessonId}`", js)

    def test_legacy_key_format_unchanged(self):
        js = self._js()
        self.assertIn(
            '`maorif_grade_${schoolId}_${className}_${subjectName}_', js
        )

    def test_pending_payload_carries_lesson_id(self):
        js = self._js()
        self.assertIn('params.lesson_id = selectedLessonId', js)

    def test_lesson_pending_replays_to_same_endpoint(self):
        # syncPending POSTs item.params — which include lesson_id — to the
        # same save endpoint, so lesson-scoped queues replay unchanged.
        js = self._js()
        self.assertIn('new URLSearchParams(item.params)', js)
        self.assertIn('await fetch(saveUrl', js)

    def test_server_accepts_lesson_scoped_payload(self):
        l1 = make_lesson(self.cs_a, D_Q1, 1)
        resp = post_grade(self.client, self.s1, score='8', lesson=l1)
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Grade.objects.get(student=self.s1, date=D_Q1).lesson_id, l1.id
        )


# ---------------------------------------------------------------------------
# "+ Дарси нав" cancel — prompt() null must abort before the POST
# ---------------------------------------------------------------------------


class LessonCreateCancelTests(GoldenBase):
    """createLesson() POSTs lesson_save immediately, so Cancel/Escape on the
    topic prompt must return early — regression guard on the template JS."""

    def test_createlesson_aborts_on_prompt_cancel(self):
        from pathlib import Path
        from django.conf import settings
        src = (Path(settings.BASE_DIR) / 'portal' / 'templates' / 'portal'
               / 'grade_entry.html').read_text(encoding='utf-8')
        fn = src[src.index('function createLesson()'):src.index('function deleteLesson()')]
        self.assertIn('prompt(', fn)
        # The null check sits between the prompt call and the POST.
        self.assertIn('if (topic === null) return;', fn)
        self.assertLess(fn.index('prompt('), fn.index('topic === null'))
        self.assertLess(fn.index('topic === null'), fn.index('fetch('))

    def test_delete_middle_lesson_keeps_siblings(self):
        # Cancelling an empty D2 must never touch D1 or D3.
        self.client.force_login(self.teacher_a)
        l1 = make_lesson(self.cs_a, D_Q1, 1)
        l2 = make_lesson(self.cs_a, D_Q1, 2)
        l3 = make_lesson(self.cs_a, D_Q1, 3)
        post_grade(self.client, self.s1, score='9', lesson=l1)
        post_grade(self.client, self.s1, score='7', lesson=l3)
        resp = self.client.post(LESSON_DELETE_URL, {'lesson_id': l2.id})
        self.assertTrue(resp.json()['success'])
        remaining = list(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1)
            .order_by('lesson_number')
            .values_list('lesson_number', flat=True)
        )
        self.assertEqual(remaining, [1, 3])
        self.assertTrue(Grade.objects.filter(lesson=l1).exists())
        self.assertTrue(Grade.objects.filter(lesson=l3).exists())
