"""Phase 1 schema regression tests: Lesson, Assessment, and Grade links.

The 0013 migration is additive-only. These tests prove that:
  - existing Grade rows remain valid with lesson=NULL / assessment=NULL,
  - multiple Lessons for the same ClassSubject on the same date coexist
    (distinguished by lesson_number),
  - multiple Assessments coexist within one quarter/category,
  - deleting a Lesson/Assessment un-links but never deletes a Grade,
  - Grade.score stays the single authoritative combined score,
  - the offline daily-save contract and the teacher activity list are
    unchanged.
"""
from django.db import IntegrityError, transaction
from django.urls import reverse

from portal.models import Assessment, Grade, Lesson
from portal.tests.helpers import (
    D_Q1, D_Q1_B, MATH, GoldenBase, make_grade, make_teacher_profile,
)


def make_lesson(class_subject, date=D_Q1, lesson_number=1, topic=''):
    return Lesson.objects.create(
        class_subject=class_subject,
        date=date,
        lesson_number=lesson_number,
        topic=topic,
    )


def make_assessment(class_subject, date=D_Q1, quarter=1, category='control',
                    lesson=None, number=None, title=''):
    return Assessment.objects.create(
        class_subject=class_subject,
        lesson=lesson,
        date=date,
        quarter=quarter,
        category=category,
        number=number,
        title=title,
    )


# ---------------------------------------------------------------------------
# Lesson model
# ---------------------------------------------------------------------------


class LessonModelTests(GoldenBase):
    def test_two_lessons_same_class_subject_and_date_coexist(self):
        make_lesson(self.cs_a, D_Q1, lesson_number=1)
        make_lesson(self.cs_a, D_Q1, lesson_number=2)
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 2
        )

    def test_three_lessons_same_day_coexist(self):
        for n in (1, 2, 3):
            make_lesson(self.cs_a, D_Q1, lesson_number=n)
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 3
        )

    def test_duplicate_lesson_number_rejected(self):
        make_lesson(self.cs_a, D_Q1, lesson_number=1)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                make_lesson(self.cs_a, D_Q1, lesson_number=1)
        self.assertEqual(
            Lesson.objects.filter(class_subject=self.cs_a, date=D_Q1).count(), 1
        )

    def test_same_lesson_number_different_dates_coexist(self):
        make_lesson(self.cs_a, D_Q1, lesson_number=1)
        make_lesson(self.cs_a, D_Q1_B, lesson_number=1)
        self.assertEqual(Lesson.objects.filter(class_subject=self.cs_a).count(), 2)

    def test_lesson_links_to_classsubject(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        self.assertEqual(lesson.class_subject, self.cs_a)
        self.assertEqual(lesson.class_subject.subject, MATH)


# ---------------------------------------------------------------------------
# Assessment model
# ---------------------------------------------------------------------------


class AssessmentModelTests(GoldenBase):
    def test_multiple_assessments_same_quarter_and_category(self):
        make_assessment(self.cs_a, D_Q1, quarter=1, category='control')
        make_assessment(self.cs_a, D_Q1_B, quarter=1, category='control')
        self.assertEqual(
            Assessment.objects.filter(
                class_subject=self.cs_a, quarter=1, category='control'
            ).count(),
            2,
        )

    def test_jori_and_nazorat_categories_coexist(self):
        make_assessment(self.cs_a, D_Q1, quarter=1, category='current')
        make_assessment(self.cs_a, D_Q1, quarter=1, category='control')
        self.assertEqual(
            Assessment.objects.filter(class_subject=self.cs_a, quarter=1).count(), 2
        )

    def test_assessment_optional_lesson_link(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        linked = make_assessment(self.cs_a, D_Q1, quarter=1, lesson=lesson)
        unlinked = make_assessment(self.cs_a, D_Q1_B, quarter=1, lesson=None)
        self.assertEqual(linked.lesson, lesson)
        self.assertIsNone(unlinked.lesson)

    def test_is_ajm_defaults_false(self):
        assessment = make_assessment(self.cs_a, D_Q1, quarter=1)
        self.assertFalse(assessment.is_ajm)

    def test_lesson_delete_nulls_assessment_but_preserves_row(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        assessment = make_assessment(self.cs_a, D_Q1, quarter=1, lesson=lesson)
        lesson.delete()
        assessment.refresh_from_db()
        self.assertIsNone(assessment.lesson)


# ---------------------------------------------------------------------------
# Grade nullable links
# ---------------------------------------------------------------------------


class GradeLinkTests(GoldenBase):
    def test_existing_grade_without_links_is_valid(self):
        g = make_grade(self.s1, score=8)
        g.refresh_from_db()
        self.assertIsNone(g.lesson)
        self.assertIsNone(g.assessment)
        self.assertEqual(g.score, 8)

    def test_grade_can_reference_lesson_and_assessment(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        assessment = make_assessment(self.cs_a, D_Q1, quarter=1, lesson=lesson)
        g = make_grade(self.s1, score=9)
        g.lesson = lesson
        g.assessment = assessment
        g.save()
        g.refresh_from_db()
        self.assertEqual(g.lesson, lesson)
        self.assertEqual(g.assessment, assessment)

    def test_deleting_lesson_nulls_grade_but_preserves_row(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        g = make_grade(self.s1, score=7)
        g.lesson = lesson
        g.save()
        lesson.delete()
        g.refresh_from_db()
        self.assertIsNone(g.lesson)
        self.assertEqual(g.score, 7)
        self.assertTrue(Grade.objects.filter(pk=g.pk).exists())

    def test_deleting_assessment_nulls_grade_but_preserves_row(self):
        assessment = make_assessment(self.cs_a, D_Q1, quarter=1)
        g = make_grade(self.s1, score=6)
        g.assessment = assessment
        g.save()
        assessment.delete()
        g.refresh_from_db()
        self.assertIsNone(g.assessment)
        self.assertEqual(g.score, 6)
        self.assertTrue(Grade.objects.filter(pk=g.pk).exists())

    def test_score_remains_authoritative_with_links(self):
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=2)
        assessment = make_assessment(self.cs_a, D_Q1, quarter=1, lesson=lesson)
        g = make_grade(self.s1, score=10)
        g.lesson = lesson
        g.assessment = assessment
        g.save()
        g.refresh_from_db()
        self.assertEqual(g.score, 10)
        # One assessment -> one Grade row per student; components are NOT
        # modeled as sibling rows in Phase 1.
        self.assertEqual(Grade.objects.filter(assessment=assessment).count(), 1)


# ---------------------------------------------------------------------------
# Protected contracts unchanged
# ---------------------------------------------------------------------------


class RegressionContractTests(GoldenBase):
    def test_offline_daily_save_contract_unchanged(self):
        """Legacy localStorage payload (no lesson_id/assessment_id) still saves."""
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id,
            'subject': MATH,
            'date': D_Q1.isoformat(),
            'type': 'daily',
            'score': '8',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertEqual(g.score, 8)
        self.assertIsNone(g.lesson)
        self.assertIsNone(g.assessment)

    def test_offline_quarterly_save_contract_unchanged(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id,
            'subject': MATH,
            'quarter': '1',
            'type': 'quarterly',
            'score': '9',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['semi_annual_1'], 9)

    def test_teacher_activity_list_returns_teacher_rows(self):
        """school_readiness_rating still renders the allocated-teacher drilldown."""
        tp = make_teacher_profile(self.teacher_a, self.school_a)
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('school_readiness_rating'))
        self.assertEqual(resp.status_code, 200)
        activity = resp.context['activity']
        school_a_row = next(
            a for a in activity if a['school'].id == self.school_a.id
        )
        teacher_names = [t['name'] for t in school_a_row['teachers']]
        self.assertIn(tp.full_name, teacher_names)

    def test_moving_student_preserves_grade_links(self):
        from portal.views import _move_student
        lesson = make_lesson(self.cs_a, D_Q1, lesson_number=1)
        g = make_grade(self.s1, score=8)
        g.lesson = lesson
        g.save()
        moved = _move_student(self.school_a, self.s1, '7-А', 'Фирӯз Раҳимов Ҷ.')
        g.refresh_from_db()
        self.assertEqual(g.student, moved)
        self.assertEqual(g.lesson, lesson)
        self.assertEqual(g.score, 8)
