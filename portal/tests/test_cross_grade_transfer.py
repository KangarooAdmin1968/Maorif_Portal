"""Full-class transfer to existing empty classes.

transfer_class supports two distinct operations:
  A) same-grade merge into a POPULATED class (5-Д -> 5-Г) — unchanged
     Phase 7 behaviour;
  B) full-class transfer to an existing EMPTY class — any grade, any
     letter, same school (5-Д -> 6-Д, 5-Д -> 7-Д, 5-Д -> 5-Е,
     5-Д -> 5-А, 5-Д -> 4-Д) — never a merge, never a creation.

The transfer path must stay atomic, preserve Grade/QuarterGrade history
(QuarterGrade rows keep the class they were earned in), preserve target
ClassSubject configuration, and reuse the Phase 7 reactivation/empty-class
handling.
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from portal import views as portal_views
from portal.models import (
    ClassSubject, Grade, QuarterGrade, Student, SubjectGroupMembership,
    TeachingGroup,
)
from portal.tests.helpers import (
    MATH, make_class_subject, make_deactivation_request, make_grade,
    make_quarter_grade, make_school, make_student, make_teacher_profile,
    make_user,
)
from portal.utils import ensure_class_subjects

RUSSIAN = 'ЗАБОНИ РУСӢ'
CUSTOM = 'ФАНИ ФАЪОЛИЯТ'  # not in TJC_SUBJECTS


class CrossGradeBase(TestCase):
    def setUp(self):
        self.school = make_school('Мактаб №13')
        self.admin = make_user('admin_cg', superuser=True)
        self.teacher_user = make_user(
            'teacher_cg', role='teacher', school=self.school)
        self.teacher_profile = make_teacher_profile(
            self.teacher_user, self.school)
        self.client.force_login(self.admin)

    def post_transfer(self, source, target):
        return self.client.post(
            reverse('transfer_class', args=[self.school.id, source]),
            {'target_class': target, 'new_class_name': ''})

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

    def students_in(self, class_name):
        return Student.objects.filter(
            school=self.school, class_name=class_name)


# ----------------------------------------------------------------------
# Same-grade merge must be unchanged
# ----------------------------------------------------------------------
class SameGradeRegressionTests(CrossGradeBase):
    def test_same_grade_merge_still_works(self):
        for i in range(2):
            make_student(self.school, '5-Д', f'Хонандаи Д-{i}')
        for i in range(2):
            make_student(self.school, '5-Г', f'Хонандаи Г-{i}')

        self.post_transfer('5-Д', '5-Г')

        self.assertFalse(self.students_in('5-Д').exists())
        self.assertEqual(self.students_in('5-Г').count(), 4)
        self.assertFalse(self.cs_rows('5-Д').filter(is_active=True).exists())

    def test_same_grade_populated_target_rejected(self):
        """A populated same-grade class that is not a valid merge target
        is rejected — populated classes are never merged on transfer."""
        make_student(self.school, '5-Г', 'Хонандаи Г')
        make_student(self.school, '5-Д', 'Хонандаи Д')

        self.post_transfer('5-Г', '5-Д')  # lower letter -> higher is invalid

        self.assertEqual(self.students_in('5-Г').count(), 1)
        self.assertEqual(self.students_in('5-Д').count(), 1)


# ----------------------------------------------------------------------
# Cross-grade happy path — any other grade, any direction
# ----------------------------------------------------------------------
class CrossGradeTransferTests(CrossGradeBase):
    def setUp(self):
        super().setUp()
        self.students = [
            make_student(self.school, '5-Д', f'Хонандаи {i}', gender='F')
            for i in range(3)
        ]
        ensure_class_subjects(self.school, '6-Д')  # existing empty target

    def test_empty_target_transfer_succeeds(self):
        resp = self.post_transfer('5-Д', '6-Д')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.students_in('6-Д').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())
        # Identity preserved: new ids carry the target class, names intact.
        names = sorted(
            self.students_in('6-Д').values_list('full_name', flat=True))
        self.assertEqual(names, ['Хонандаи 0', 'Хонандаи 1', 'Хонандаи 2'])
        for st in self.students_in('6-Д'):
            self.assertTrue(st.id.endswith(f'__6-Д__{st.full_name}'))
            self.assertEqual(st.gender, 'F')

    def test_source_class_subjects_deactivated_not_deleted(self):
        self.post_transfer('5-Д', '6-Д')
        rows = self.cs_rows('5-Д')
        self.assertTrue(rows.exists())
        self.assertFalse(rows.filter(is_active=True).exists())

    def test_redirects_to_target_class(self):
        resp = self.post_transfer('5-Д', '6-Д')
        self.assertRedirects(
            resp, reverse('class_detail',
                          args=[self.school.id, '6-Д']),
            fetch_redirect_response=False)

    def test_non_adjacent_grade_transfer_succeeds(self):
        """5-Д -> 7-Д: no 'next grade only' restriction exists."""
        ensure_class_subjects(self.school, '7-Д')
        self.post_transfer('5-Д', '7-Д')
        self.assertEqual(self.students_in('7-Д').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())

    def test_lower_grade_transfer_succeeds(self):
        """5-Д -> 4-Д: transfer down a grade is allowed to an empty class."""
        ensure_class_subjects(self.school, '4-Д')
        self.post_transfer('5-Д', '4-Д')
        self.assertEqual(self.students_in('4-Д').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())

    def test_a_source_transfers_to_empty_target(self):
        """7-А -> 5-А: the -А merge restriction does not block an
        empty-target transfer."""
        ensure_class_subjects(self.school, '5-А')
        for i in range(2):
            make_student(self.school, '7-А', f'Хонандаи А-{i}')
        self.post_transfer('7-А', '5-А')
        self.assertEqual(self.students_in('5-А').count(), 2)
        self.assertFalse(self.students_in('7-А').exists())

    def test_same_grade_empty_higher_letter_succeeds(self):
        """5-Д -> 5-Е: same-grade, higher letter — allowed when target
        is existing and empty (it is not a merge, it is a transfer)."""
        ensure_class_subjects(self.school, '5-Е')
        self.post_transfer('5-Д', '5-Е')
        self.assertEqual(self.students_in('5-Е').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())

    def test_same_grade_empty_a_target_succeeds(self):
        """5-Д -> 5-А (empty): routed through the EMPTY-target transfer
        path — QuarterGrade.class_name keeps the class it was earned in."""
        ensure_class_subjects(self.school, '5-А')
        st = self.students[0]
        q1 = make_quarter_grade(st, class_name='5-Д', subject=MATH,
                                quarter=1, grade=8)

        self.post_transfer('5-Д', '5-А')

        self.assertEqual(self.students_in('5-А').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())
        q1.refresh_from_db()
        self.assertEqual(q1.student_id, st.id.replace('__5-Д__', '__5-А__'))
        self.assertEqual(q1.class_name, '5-Д')
        self.assertEqual(q1.grade, 8)

    def test_same_grade_populated_merge_still_relabels(self):
        """5-Д -> 5-А (populated): the existing merge path applies —
        QuarterGrade.class_name follows the student into 5-А."""
        target_st = make_student(self.school, '5-А', 'Хонандаи А')
        st = self.students[0]
        q1 = make_quarter_grade(st, class_name='5-Д', subject=MATH,
                                quarter=1, grade=8)

        self.post_transfer('5-Д', '5-А')

        self.assertEqual(self.students_in('5-А').count(), 4)
        self.assertFalse(self.students_in('5-Д').exists())
        q1.refresh_from_db()
        self.assertEqual(q1.student_id, st.id.replace('__5-Д__', '__5-А__'))
        self.assertEqual(q1.class_name, '5-А')
        self.assertEqual(q1.grade, 8)
        self.assertTrue(
            Student.objects.filter(id=target_st.id).exists())

    def test_previously_used_now_empty_target_allowed(self):
        """7-Д had students, they moved out — it stays a valid target."""
        st = make_student(self.school, '7-Д', 'Хонандаи Кӯҳна')
        self.client.post(
            reverse('edit_student', args=[self.school.id, '7-Д']),
            {'student_id': st.id, 'full_name': st.full_name,
             'gender': '', 'class_name': '8-А', 'new_class_name': ''})
        self.assertFalse(self.students_in('7-Д').exists())
        # Its ClassSubjects were deactivated when the class emptied out —
        # it is still a pre-existing, eligible target.
        self.assertTrue(self.cs_rows('7-Д').exists())

        self.post_transfer('5-Д', '7-Д')

        self.assertEqual(self.students_in('7-Д').count(), 3)
        self.assertFalse(self.students_in('5-Д').exists())


# ----------------------------------------------------------------------
# Target safety
# ----------------------------------------------------------------------
class TargetSafetyTests(CrossGradeBase):
    def setUp(self):
        super().setUp()
        make_student(self.school, '5-Д', 'Хонандаи Манбаъ')

    def test_populated_target_rejected(self):
        ensure_class_subjects(self.school, '6-Д')
        make_student(self.school, '6-Д', 'Хонандаи Ҳадаф')

        self.post_transfer('5-Д', '6-Д')

        self.assertEqual(self.students_in('5-Д').count(), 1)
        self.assertEqual(self.students_in('6-Д').count(), 1)
        self.assertEqual(
            self.students_in('6-Д').first().full_name, 'Хонандаи Ҳадаф')

    def test_missing_target_rejected(self):
        # 6-В has no ClassSubject rows and no students — it does not exist.
        self.assertFalse(self.cs_rows('6-В').exists())
        self.post_transfer('5-Д', '6-В')
        self.assertEqual(self.students_in('5-Д').count(), 1)
        self.assertFalse(self.students_in('6-В').exists())
        # The transfer itself never creates a target class.
        self.assertFalse(self.cs_rows('6-В').exists())

    def test_same_class_rejected(self):
        self.post_transfer('5-Д', '5-Д')
        self.assertEqual(self.students_in('5-Д').count(), 1)

    def test_other_school_target_rejected(self):
        """A class that exists only at another school is not a target."""
        other = make_school('Мактаб №14')
        ensure_class_subjects(other, '6-Д')
        self.assertFalse(self.cs_rows('6-Д').exists())

        self.post_transfer('5-Д', '6-Д')

        self.assertEqual(self.students_in('5-Д').count(), 1)
        self.assertFalse(self.students_in('6-Д').exists())
        # And it is not offered in the target list either.
        resp = self.client.get(
            reverse('class_detail', args=[self.school.id, '5-Д']))
        self.assertNotIn('6-Д', resp.context['empty_targets'])

    def test_empty_source_rejected(self):
        ensure_class_subjects(self.school, '6-Д')
        Student.objects.filter(school=self.school).delete()
        self.post_transfer('5-Д', '6-Д')
        self.assertFalse(self.students_in('6-Д').exists())


# ----------------------------------------------------------------------
# Atomicity
# ----------------------------------------------------------------------
class RollbackTests(CrossGradeBase):
    def test_failure_mid_move_rolls_back_everything(self):
        make_student(self.school, '5-Д', 'Хонандаи 1')
        make_student(self.school, '5-Д', 'Хонандаи 2')
        make_student(self.school, '5-Д', 'Хонандаи 3')
        ensure_class_subjects(self.school, '6-Д')

        real_move = portal_views._move_student
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise ValueError('санҷиши хатогӣ')
            return real_move(*args, **kwargs)

        with patch('portal.views._move_student', side_effect=flaky):
            self.post_transfer('5-Д', '6-Д')

        # No partial transfer: all students remain in 5-Д, 6-Д stays empty.
        self.assertEqual(self.students_in('5-Д').count(), 3)
        self.assertFalse(self.students_in('6-Д').exists())
        self.assertFalse(
            Student.objects.filter(
                school=self.school, class_name='6-Д').exists())


# ----------------------------------------------------------------------
# ClassSubject preservation and Phase 7 reactivation
# ----------------------------------------------------------------------
class ClassSubjectSafetyTests(CrossGradeBase):
    def setUp(self):
        super().setUp()
        make_student(self.school, '5-Д', 'Хонандаи Манбаъ')
        ensure_class_subjects(self.school, '6-Д')

    def test_target_configuration_preserved(self):
        cs = ClassSubject.objects.get(
            school=self.school, class_name='6-Д', subject=MATH)
        cs.teacher = self.teacher_user
        cs.allocated_teacher = self.teacher_profile
        cs.hours_per_week = 4
        cs.save()
        before = self.cs_snapshot('6-Д')
        self.assertTrue(before)

        self.post_transfer('5-Д', '6-Д')

        for row in self.cs_rows('6-Д'):
            self.assertTrue(row.is_active, row.subject)
        self.assert_snapshot('6-Д', before)

    def test_target_non_curriculum_row_not_dropped(self):
        make_class_subject(self.school, '6-Д', CUSTOM,
                           teacher=self.teacher_user, is_active=True)

        self.post_transfer('5-Д', '6-Д')

        cs = ClassSubject.objects.get(
            school=self.school, class_name='6-Д', subject=CUSTOM)
        self.assertTrue(cs.is_active)
        self.assertEqual(cs.teacher_id, self.teacher_user.id)

    def test_inactive_target_defaults_reactivated(self):
        """Phase 7: rows inactive only because the class was empty revive."""
        cs = ClassSubject.objects.get(
            school=self.school, class_name='6-Д', subject=MATH)
        cs.is_active = False
        cs.save()

        self.post_transfer('5-Д', '6-Д')

        cs.refresh_from_db()
        self.assertTrue(cs.is_active)

    def test_approved_deactivation_stays_inactive(self):
        """Phase 7: intentional opt-outs are never revived by a transfer."""
        cs = ClassSubject.objects.get(
            school=self.school, class_name='6-Д', subject=RUSSIAN)
        cs.is_active = False
        cs.save()
        make_deactivation_request(cs, self.admin, status='approved')

        self.post_transfer('5-Д', '6-Д')

        cs.refresh_from_db()
        self.assertFalse(cs.is_active)


# ----------------------------------------------------------------------
# Grade / QuarterGrade history
# ----------------------------------------------------------------------
class GradeHistoryTests(CrossGradeBase):
    def test_grades_relinked_and_quarters_keep_source_class(self):
        st = make_student(self.school, '5-Д', 'Мадина Каримова')
        ensure_class_subjects(self.school, '6-Д')
        old_id = st.id
        g = make_grade(st, subject=MATH, score=9)
        q1 = make_quarter_grade(st, class_name='5-Д', subject=MATH,
                                quarter=1, grade=8)
        q0 = make_quarter_grade(st, class_name='5-Д', subject=MATH,
                                quarter=0, att=9)

        self.post_transfer('5-Д', '6-Д')

        self.assertFalse(Student.objects.filter(id=old_id).exists())
        new_st = Student.objects.get(school=self.school, class_name='6-Д')

        g.refresh_from_db()
        self.assertEqual(g.student_id, new_st.id)
        self.assertEqual(g.score, 9)

        q1.refresh_from_db()
        q0.refresh_from_db()
        self.assertEqual(q1.student_id, new_st.id)
        self.assertEqual(q0.student_id, new_st.id)
        # Historical rows keep the class they were earned in — a future
        # grade-6 update_or_create(…, class_name='6-Д', quarter=1) must not
        # silently overwrite the grade-5 quarter result.
        self.assertEqual(q1.class_name, '5-Д')
        self.assertEqual(q0.class_name, '5-Д')
        self.assertEqual(q1.grade, 8)
        self.assertEqual(q0.att_grade, 9)

        self.assertEqual(
            Grade.objects.filter(student_id=old_id).count(), 0)
        self.assertEqual(
            QuarterGrade.objects.filter(student_id=old_id).count(), 0)


# ----------------------------------------------------------------------
# TeachingGroup / membership consistency
# ----------------------------------------------------------------------
class TeachingGroupTests(CrossGradeBase):
    def test_memberships_do_not_survive_as_orphans(self):
        st = make_student(self.school, '5-Д', 'Хонандаи Гурӯҳӣ')
        ensure_class_subjects(self.school, '6-Д')
        cs = ClassSubject.objects.get(
            school=self.school, class_name='5-Д', subject=MATH)
        group = TeachingGroup.objects.create(
            class_subject=cs, label='Гурӯҳи 1', teacher=self.teacher_profile)
        old_id = st.id
        SubjectGroupMembership.objects.create(
            class_subject=cs, group=group, student=st)

        self.post_transfer('5-Д', '6-Д')

        # Memberships belong to the source ClassSubject — they are removed
        # with the old student row and never left dangling.
        self.assertEqual(
            SubjectGroupMembership.objects.filter(student_id=old_id).count(), 0)
        moved = Student.objects.get(school=self.school, class_name='6-Д')
        self.assertEqual(
            SubjectGroupMembership.objects.filter(student=moved).count(), 0)
        # Source groups themselves stay intact with their ClassSubject.
        self.assertTrue(
            TeachingGroup.objects.filter(class_subject=cs).exists())


# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------
class PermissionTests(CrossGradeBase):
    def test_regular_teacher_cannot_transfer(self):
        make_student(self.school, '5-Д', 'Хонандаи Манбаъ')
        ensure_class_subjects(self.school, '6-Д')
        self.client.force_login(self.teacher_user)

        self.post_transfer('5-Д', '6-Д')

        self.assertEqual(self.students_in('5-Д').count(), 1)
        self.assertFalse(self.students_in('6-Д').exists())


# ----------------------------------------------------------------------
# Target list in the transfer dialog (class_detail context)
# ----------------------------------------------------------------------
class TargetListTests(CrossGradeBase):
    def setUp(self):
        super().setUp()
        for i in range(3):
            make_student(self.school, '5-Д', f'Хонандаи {i}')
        # Existing empty classes — any grade, any letter.
        ensure_class_subjects(self.school, '5-Е')   # same grade, higher letter
        ensure_class_subjects(self.school, '6-Д')
        ensure_class_subjects(self.school, '7-Д')
        ensure_class_subjects(self.school, '8-Б')
        ensure_class_subjects(self.school, '4-Д')   # lower grade
        # Same-grade empty class that is also merge-eligible — emptiness
        # decides the route, so it belongs to the empty-target group.
        ensure_class_subjects(self.school, '5-В')
        # Populated classes — must never appear.
        make_student(self.school, '6-А', 'Хонандаи 6-А')
        make_student(self.school, '7-Б', 'Хонандаи 7-Б')
        make_student(self.school, '5-Г', 'Хонандаи 5-Г')

    def detail_context(self):
        resp = self.client.get(
            reverse('class_detail', args=[self.school.id, '5-Д']))
        self.assertEqual(resp.status_code, 200)
        return resp.context['empty_targets']

    def test_only_existing_empty_classes_listed(self):
        self.assertEqual(
            sorted(self.detail_context()),
            ['4-Д', '5-В', '5-Е', '6-Д', '7-Д', '8-Б'])

    def test_populated_classes_not_listed(self):
        targets = self.detail_context()
        self.assertNotIn('6-А', targets)
        self.assertNotIn('7-Б', targets)
        self.assertNotIn('5-Г', targets)

    def test_source_not_listed(self):
        self.assertNotIn('5-Д', self.detail_context())

    def test_previously_used_empty_class_appears(self):
        st = Student.objects.get(school=self.school, class_name='7-Б')
        self.client.post(
            reverse('edit_student', args=[self.school.id, '7-Б']),
            {'student_id': st.id, 'full_name': st.full_name,
             'gender': '', 'class_name': '9-А', 'new_class_name': ''})
        self.assertIn('7-Б', self.detail_context())


# ----------------------------------------------------------------------
# Confirmation dialog content
# ----------------------------------------------------------------------
class ConfirmationTextTests(CrossGradeBase):
    def test_confirmation_contains_source_target_and_count(self):
        for i in range(3):
            make_student(self.school, '5-Д', f'Хонандаи {i}')
        ensure_class_subjects(self.school, '7-Д')

        resp = self.client.get(
            reverse('class_detail', args=[self.school.id, '5-Д']))

        self.assertContains(resp, 'Синфи қабулкунанда')
        self.assertContains(resp, 'интиқолшаванда: 3 нафар')
        # The step-1 confirmation question also names the exact count.
        self.assertContains(resp, '(3 нафар)')
        self.assertContains(resp, 'Гузарондан')
        self.assertContains(resp, 'Бекор кардан')
