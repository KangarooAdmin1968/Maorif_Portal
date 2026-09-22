"""Phase 1 schema tests for TeachingGroup / SubjectGroupMembership /
Lesson.group — additive storage only; no UI or permission wiring yet.
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from portal.models import (
    ClassSubject, Lesson, SubjectGroupMembership, TeachingGroup,
)
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_student,
    make_teacher_profile,
)


class TeachingGroupModelTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs_math = ClassSubject.objects.get(
            school=cls.school_a, class_name='7-А', subject=MATH)
        cls.cs_b = ClassSubject.objects.get(
            school=cls.school_b, class_name='7-Б', subject=MATH)

    def test_create_group(self):
        g = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1')
        self.assertEqual(g.class_subject_id, self.cs_math.id)
        self.assertTrue(g.is_active)
        self.assertIsNone(g.teacher)

    def test_label_unique_within_class_subject(self):
        TeachingGroup.objects.create(class_subject=self.cs_math, label='Гурӯҳи 1')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeachingGroup.objects.create(
                    class_subject=self.cs_math, label='Гурӯҳи 1')

    def test_same_label_allowed_on_other_class_subject(self):
        TeachingGroup.objects.create(class_subject=self.cs_math, label='Гурӯҳи 1')
        g = TeachingGroup.objects.create(
            class_subject=self.cs_b, label='Гурӯҳи 1')
        self.assertIsNotNone(g.pk)

    def test_teacher_nullable(self):
        g = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1', teacher=None)
        self.assertIsNone(g.teacher_id)

    def test_teacher_profile_assignment(self):
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        g = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1', teacher=profile)
        self.assertEqual(g.teacher_id, profile.id)

    def test_teacher_from_other_school_rejected_by_clean(self):
        profile = make_teacher_profile(self.teacher_b, self.school_b)
        g = TeachingGroup(
            class_subject=self.cs_math, label='Гурӯҳи 1', teacher=profile)
        with self.assertRaises(ValidationError):
            g.full_clean()

    def test_group_teacher_set_null_on_profile_delete(self):
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        g = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1', teacher=profile)
        profile.delete()
        g.refresh_from_db()
        self.assertIsNone(g.teacher_id)

    def test_deactivating_group_keeps_class_subject(self):
        g = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1', is_active=False)
        g.refresh_from_db()
        self.assertFalse(g.is_active)
        self.assertTrue(
            ClassSubject.objects.filter(pk=self.cs_math.pk, is_active=True)
            .exists())

    def test_class_subject_uniqueness_unchanged(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ClassSubject.objects.create(
                    school=self.school_a, class_name='7-А', subject=MATH)


class SubjectGroupMembershipTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs_math = ClassSubject.objects.get(
            school=cls.school_a, class_name='7-А', subject=MATH)
        cls.cs_tajik = make_class_subject(cls.school_a, '7-А', 'ЗАБОНИ ТОҶИКӢ')
        cls.cs_b = ClassSubject.objects.get(
            school=cls.school_b, class_name='7-Б', subject=MATH)
        cls.g1 = TeachingGroup.objects.create(
            class_subject=cls.cs_math, label='Гурӯҳи 1')
        cls.g2 = TeachingGroup.objects.create(
            class_subject=cls.cs_math, label='Гурӯҳи 2')
        cls.g_tajik = TeachingGroup.objects.create(
            class_subject=cls.cs_tajik, label='Гурӯҳи 1')
        cls.g_b = TeachingGroup.objects.create(
            class_subject=cls.cs_b, label='Гурӯҳи 1')

    def _member(self, cs, group, student):
        return SubjectGroupMembership.objects.create(
            class_subject=cs, group=group, student=student)

    def test_create_membership(self):
        m = self._member(self.cs_math, self.g1, self.s1)
        self.assertEqual(m.group_id, self.g1.id)
        self.assertEqual(m.student_id, self.s1.id)

    def test_student_cannot_join_two_groups_same_subject(self):
        self._member(self.cs_math, self.g1, self.s1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._member(self.cs_math, self.g2, self.s1)

    def test_student_can_join_groups_of_different_subjects(self):
        self._member(self.cs_math, self.g1, self.s1)
        m = self._member(self.cs_tajik, self.g_tajik, self.s1)
        self.assertIsNotNone(m.pk)

    def test_same_student_in_different_school_class_subject(self):
        # Same subject name, different school/class — different ClassSubject.
        self._member(self.cs_math, self.g1, self.s1)
        m = self._member(self.cs_b, self.g_b, self.s3)
        self.assertIsNotNone(m.pk)

    def test_cross_subject_group_rejected_by_clean(self):
        m = SubjectGroupMembership(
            class_subject=self.cs_math, group=self.g_b, student=self.s1)
        with self.assertRaises(ValidationError):
            m.full_clean()

    def test_student_from_other_class_rejected_by_clean(self):
        m = SubjectGroupMembership(
            class_subject=self.cs_math, group=self.g1, student=self.s3)
        with self.assertRaises(ValidationError):
            m.full_clean()

    def test_membership_deleted_with_group(self):
        m = self._member(self.cs_math, self.g1, self.s1)
        self.g1.delete()
        self.assertFalse(
            SubjectGroupMembership.objects.filter(pk=m.pk).exists())

    def test_membership_deleted_with_student(self):
        m = self._member(self.cs_math, self.g1, self.s1)
        self.s1.delete()
        self.assertFalse(
            SubjectGroupMembership.objects.filter(pk=m.pk).exists())

    def test_membership_deleted_with_class_subject(self):
        m = self._member(self.cs_math, self.g1, self.s1)
        self.cs_math.delete()
        self.assertFalse(
            SubjectGroupMembership.objects.filter(pk=m.pk).exists())


class LessonGroupFieldTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs_math = ClassSubject.objects.get(
            school=cls.school_a, class_name='7-А', subject=MATH)
        cls.cs_b = ClassSubject.objects.get(
            school=cls.school_b, class_name='7-Б', subject=MATH)
        cls.group = TeachingGroup.objects.create(
            class_subject=cls.cs_math, label='Гурӯҳи 1')
        cls.group_b = TeachingGroup.objects.create(
            class_subject=cls.cs_b, label='Гурӯҳи 1')

    def _lesson(self, cs=None, number=1, group=None):
        return Lesson.objects.create(
            class_subject=cs or self.cs_math, date=D_Q1,
            lesson_number=number, group=group)

    def test_group_nullable_default(self):
        lesson = self._lesson()
        self.assertIsNone(lesson.group_id)

    def test_lesson_can_reference_group(self):
        lesson = self._lesson(group=self.group)
        self.assertEqual(lesson.group_id, self.group.id)
        self.assertIn(lesson, self.group.lessons.all())

    def test_cross_subject_group_rejected_by_clean(self):
        lesson = Lesson(
            class_subject=self.cs_math, date=D_Q1, lesson_number=1,
            group=self.group_b)
        with self.assertRaises(ValidationError):
            lesson.full_clean()

    def test_group_delete_sets_lesson_group_null(self):
        lesson = self._lesson(group=self.group)
        self.group.delete()
        lesson.refresh_from_db()
        self.assertIsNone(lesson.group_id)
        self.assertTrue(Lesson.objects.filter(pk=lesson.pk).exists())

    def test_lesson_numbering_uniqueness_unchanged(self):
        self._lesson(number=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._lesson(number=1)
        # Same number on a different date still fine.
        self._lesson(number=2)

    def test_existing_lessons_behave_without_groups(self):
        # Backward compat: lessons created before/without groups unchanged.
        l1 = self._lesson(number=1)
        l2 = self._lesson(number=2)
        self.assertIsNone(l1.group_id)
        self.assertIsNone(l2.group_id)
        self.assertEqual(l1.lesson_number, 1)
        self.assertEqual(l2.lesson_number, 2)
