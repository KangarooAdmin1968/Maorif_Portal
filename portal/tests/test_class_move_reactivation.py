"""Regression tests for the class move/split "Тақсимоти дарсҳо" bug.

Production incident (School No. 8): a class swap through a temporary class
left 6-А and 7-А with students but every ClassSubject row inactive, so both
classes vanished from "Тақсимоти дарсҳо". Root cause: emptying a class
deactivates all its ClassSubjects, but no code path reactivates them when
students return.

The fix must revive rows that were only inactive because the class was
empty, while preserving rows that were intentionally removed (an approved
SubjectDeactivationRequest or an inactive SubjectAvailability opt-out).
"""
from unittest.mock import MagicMock

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.urls import reverse

from portal.admin import ClassSubjectAdmin
from portal.models import ClassSubject, Student, SubjectAvailability
from portal.tests.helpers import (
    MATH, make_deactivation_request, make_school, make_student,
    make_teacher_profile, make_user,
)
from portal.utils import ensure_class_subjects

RUSSIAN = 'ЗАБОНИ РУСӢ'
ENGLISH = 'ЗАБОНИ АНГЛИСӢ'


class ClassMoveBase(TestCase):
    def setUp(self):
        self.school = make_school('Мактаб №20')
        self.admin = make_user('admin_mv', superuser=True)
        self.teacher_user = make_user(
            'teacher_mv', role='teacher', school=self.school)
        self.teacher_profile = make_teacher_profile(
            self.teacher_user, self.school)
        self.client.force_login(self.admin)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def move_student(self, student, target_class):
        return self.client.post(
            reverse('edit_student',
                    args=[self.school.id, student.class_name]),
            {
                'student_id': student.id,
                'full_name': student.full_name,
                'gender': 'M',
                'class_name': target_class,
                'new_class_name': '',
            })

    def move_all(self, source, target):
        for st in list(Student.objects.filter(
                school=self.school, class_name=source).order_by('full_name')):
            self.move_student(st, target)

    def cs_rows(self, class_name):
        return ClassSubject.objects.filter(
            school=self.school, class_name=class_name)

    def cs_snapshot(self, class_name):
        return {
            r.subject: (r.teacher_id, r.allocated_teacher_id,
                        r.hours_per_week, r.is_default)
            for r in self.cs_rows(class_name)
        }

    def assert_snapshot(self, class_name, before):
        rows = {r.subject: r for r in self.cs_rows(class_name)}
        self.assertEqual(set(rows), set(before))
        for subj, (t, a, h, d) in before.items():
            row = rows[subj]
            self.assertEqual(row.teacher_id, t, f'{class_name} {subj}')
            self.assertEqual(row.allocated_teacher_id, a,
                             f'{class_name} {subj}')
            self.assertEqual(row.hours_per_week, h, f'{class_name} {subj}')
            self.assertEqual(row.is_default, d, f'{class_name} {subj}')

    def allocation_page(self):
        return self.client.get(
            reverse('lesson_allocation'), {'school_id': self.school.id})


# ----------------------------------------------------------------------
# TEST 1 — empty class -> students return
# ----------------------------------------------------------------------
class EmptyClassReturnTests(ClassMoveBase):
    def test_returning_students_reactivate_existing_subjects(self):
        make_student(self.school, '5-А', 'Асли Назарова')
        make_student(self.school, '5-А', 'Фирӯз Ҷалилов')
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-А', subject=MATH)
        cs.teacher = self.teacher_user
        cs.allocated_teacher = self.teacher_profile
        cs.hours_per_week = 4
        cs.save()
        before = self.cs_snapshot('5-А')
        self.assertTrue(before)

        # Empty the class — every ClassSubject becomes inactive.
        self.move_all('5-А', '5-Б')
        self.assertFalse(Student.objects.filter(
            school=self.school, class_name='5-А').exists())
        self.assertFalse(self.cs_rows('5-А').filter(is_active=True).exists())

        # Students return — the stored configuration must come back.
        self.move_all('5-Б', '5-А')
        self.assertTrue(Student.objects.filter(
            school=self.school, class_name='5-А').exists())
        for row in self.cs_rows('5-А'):
            self.assertTrue(row.is_active, row.subject)
        self.assert_snapshot('5-А', before)

        # The class is visible again in "Тақсимоти дарсҳо".
        self.assertContains(self.allocation_page(), '5-А')


# ----------------------------------------------------------------------
# TEST 2 — A -> temporary class -> B (School No. 8 scenario)
# ----------------------------------------------------------------------
class SwapViaTemporaryClassTests(ClassMoveBase):
    def test_swap_preserves_destination_configurations(self):
        make_student(self.school, '6-А', 'Хонандаи 6а-1')
        make_student(self.school, '6-А', 'Хонандаи 6а-2')
        make_student(self.school, '7-А', 'Хонандаи 7а-1')
        make_student(self.school, '7-А', 'Хонандаи 7а-2')
        cs = ClassSubject.objects.get(
            school=self.school, class_name='6-А', subject=MATH)
        cs.teacher = self.teacher_user
        cs.allocated_teacher = self.teacher_profile
        cs.hours_per_week = 3
        cs.save()
        before6 = self.cs_snapshot('6-А')
        before7 = self.cs_snapshot('7-А')

        self.move_all('6-А', '7-Б')   # temporary class
        self.move_all('7-А', '6-А')
        self.move_all('7-Б', '7-А')
        self.client.post(
            reverse('delete_class', args=[self.school.id, '7-Б']))

        for cn, before in (('6-А', before6), ('7-А', before7)):
            self.assertTrue(Student.objects.filter(
                school=self.school, class_name=cn).exists(), cn)
            for row in self.cs_rows(cn):
                self.assertTrue(row.is_active, f'{cn} {row.subject}')
            self.assert_snapshot(cn, before)

        # Temporary class is fully gone.
        self.assertFalse(Student.objects.filter(
            school=self.school, class_name='7-Б').exists())
        self.assertFalse(self.cs_rows('7-Б').exists())
        self.assertFalse(ClassSubject.objects.filter(
            school=self.school, class_name='7-Б', is_active=True).exists())

        page = self.allocation_page()
        self.assertContains(page, '6-А')
        self.assertContains(page, '7-А')


# ----------------------------------------------------------------------
# TEST 3 — intentional deactivation must remain deactivated
# ----------------------------------------------------------------------
class IntentionalDeactivationTests(ClassMoveBase):
    def setUp(self):
        super().setUp()
        make_student(self.school, '5-В', 'Хонандаи Аввал')
        make_student(self.school, '5-Г', 'Хонандаи Кӯчонда')

    def arrive(self):
        st = Student.objects.get(school=self.school, class_name='5-Г')
        self.move_student(st, '5-В')

    def test_removed_subject_via_add_remove_stays_inactive(self):
        self.client.post(
            reverse('add_remove_subject', args=[self.school.id, '5-В']),
            {'action': 'remove', 'subject': 'Математика'})
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-В', subject=MATH)
        self.assertFalse(cs.is_active)

        self.arrive()
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)
        self.assertTrue(ClassSubject.objects.get(
            school=self.school, class_name='5-В',
            subject=RUSSIAN).is_active)

    def test_approved_deactivation_request_stays_inactive(self):
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-В', subject=RUSSIAN)
        cs.is_active = False
        cs.save()
        make_deactivation_request(cs, self.admin, status='approved')

        self.arrive()
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)

    def test_superuser_deactivation_stays_inactive(self):
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-В', subject=ENGLISH)
        self.client.post(reverse('deactivate_subject'), {'cs_id': cs.id})
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)

        self.arrive()
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)

    def test_admin_addform_reactivation_clears_optout(self):
        """Admin 'add' on an existing row must clear the stale opt-out so a
        later empty->return cycle can still revive the subject."""
        # 1-2. Intentional removal via the UI -> row inactive + opt-out row.
        self.client.post(
            reverse('add_remove_subject', args=[self.school.id, '5-В']),
            {'action': 'remove', 'subject': 'Математика'})
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-В', subject=MATH)
        self.assertFalse(cs.is_active)
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=MATH, school=self.school,
            class_name='5-В', is_active=False).exists())

        # 3. Admin Add-form reactivation (save_model 'reactivated' branch:
        # a new obj over an existing (school, class, subject) row).
        model_admin = ClassSubjectAdmin(ClassSubject, AdminSite())
        obj = ClassSubject(
            school=self.school, class_name='5-В', subject=MATH)
        model_admin.save_model(MagicMock(), obj, form=None, change=False)

        # 4. Row active again AND the opt-out is gone.
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)
        self.assertFalse(SubjectAvailability.objects.filter(
            subject__name=MATH, school=self.school,
            class_name='5-В', is_active=False).exists())

        # 5-6. Empty -> return cycle must now revive the subject.
        st = Student.objects.get(school=self.school, class_name='5-В')
        self.move_student(st, '5-Г')
        self.assertFalse(self.cs_rows('5-В').filter(is_active=True).exists())
        st = Student.objects.get(
            school=self.school, class_name='5-Г', full_name=st.full_name)
        self.move_student(st, '5-В')
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)

    def test_intentional_removal_survives_empty_return_cycle(self):
        self.client.post(
            reverse('add_remove_subject', args=[self.school.id, '5-В']),
            {'action': 'remove', 'subject': 'Математика'})
        self.move_all('5-В', '5-Г')
        self.move_all('5-Г', '5-В')
        self.assertFalse(ClassSubject.objects.get(
            school=self.school, class_name='5-В', subject=MATH).is_active)
        self.assertTrue(ClassSubject.objects.get(
            school=self.school, class_name='5-В',
            subject=RUSSIAN).is_active)


# ----------------------------------------------------------------------
# TEST 5 — split / merge scenarios
# ----------------------------------------------------------------------
class SplitAndMergeTests(ClassMoveBase):
    def test_split_keeps_both_classes_usable(self):
        for i in range(3):
            make_student(self.school, '5-Г', f'Хонандаи {i}')
        students = list(Student.objects.filter(
            school=self.school, class_name='5-Г').order_by('full_name'))
        for st in students[:2]:
            self.move_student(st, '5-Д')

        # Source still has students -> stays active; destination is usable.
        self.assertTrue(self.cs_rows('5-Г').filter(is_active=True).exists())
        self.assertTrue(self.cs_rows('5-Д').filter(is_active=True).exists())

    def test_merge_closes_source_but_reopens_on_return(self):
        for i in range(2):
            make_student(self.school, '5-Г', f'Хонандаи Г-{i}')
        for i in range(2):
            make_student(self.school, '5-Д', f'Хонандаи Д-{i}')

        # Valid merge: same grade, higher letter -> lower letter.
        self.client.post(
            reverse('transfer_class', args=[self.school.id, '5-Д']),
            {'target_class': '5-Г', 'new_class_name': ''})
        self.assertFalse(Student.objects.filter(
            school=self.school, class_name='5-Д').exists())
        self.assertFalse(self.cs_rows('5-Д').filter(is_active=True).exists())

        # A student later moves back into the closed class -> it reopens.
        st = Student.objects.filter(
            school=self.school, class_name='5-Г').first()
        self.move_student(st, '5-Д')
        self.assertTrue(self.cs_rows('5-Д').filter(is_active=True).exists())

    def test_add_student_reopens_emptied_class(self):
        make_student(self.school, '5-К', 'Хонандаи Ягона')
        self.move_all('5-К', '5-Л')
        self.assertFalse(self.cs_rows('5-К').filter(is_active=True).exists())

        self.client.post(
            reverse('add_student', args=[self.school.id, '5-К']),
            {'full_name': 'Хонандаи Нав'})
        self.assertTrue(self.cs_rows('5-К').filter(is_active=True).exists())


# ----------------------------------------------------------------------
# TEST 6 — ensure_class_subjects() semantics unchanged
# ----------------------------------------------------------------------
class EnsureUnchangedTests(ClassMoveBase):
    def test_ensure_alone_does_not_reactivate_empty_class(self):
        """Reactivation must be arrival-scoped, not a global ensure change."""
        make_student(self.school, '5-Ж', 'Хонандаи Ягона')
        self.move_all('5-Ж', '5-З')
        ensure_class_subjects(self.school, '5-Ж')
        self.assertFalse(self.cs_rows('5-Ж').filter(is_active=True).exists())

    def test_ensure_still_creates_defaults_for_new_class(self):
        ensure_class_subjects(self.school, '6-В')
        self.assertTrue(self.cs_rows('6-В').filter(
            is_active=True, is_default=True).exists())

    def test_ensure_still_reactivates_registry_extras(self):
        """Registry availability subjects keep their existing reactivation."""
        from portal.models import Subject, SubjectAvailability
        subj = Subject.objects.create(name='ФАНИ ИДОВАШУДА')
        SubjectAvailability.objects.create(
            subject=subj, school=self.school, class_name='5-М',
            is_active=True)
        cs = ClassSubject.objects.create(
            school=self.school, class_name='5-М', subject='ФАНИ ИДОВАШУДА',
            is_active=False)
        ensure_class_subjects(self.school, '5-М')
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)
