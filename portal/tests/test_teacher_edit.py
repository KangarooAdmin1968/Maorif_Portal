"""Regression tests for the teacher edit / password assignment flow.

Covers the cardinality gate in edit_teacher (exactly one TeacherProfile must
match school + old name), password replacement semantics, session
invalidation, the custom_password_set badge flag, and multi-school isolation.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from portal.models import Grade, Teacher, TeacherProfile, UserProfile
from portal.tests.helpers import (
    MATH, make_class_subject, make_grade, make_school, make_student, make_user,
)


class TeacherEditBase(TestCase):
    """One academic school, one teacher with profile+user, an admin, a zavuch."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school('Мактаб №1')
        cls.admin = make_user('admin_edit', superuser=True)
        cls.zavuch = make_user('zavuch_1', role='teacher', school=cls.school)
        cls.tuser = User.objects.create_user(
            username='teacher_1_1', password='Teacher_1_1@2026',
            first_name='Раҳимов Али')
        cls.profile = TeacherProfile.objects.create(
            user=cls.tuser, school=cls.school, full_name='Раҳимов Али')
        UserProfile.objects.create(
            user=cls.tuser, role='teacher', school=cls.school)
        cls.teacher = Teacher.objects.create(
            school=cls.school, name='Раҳимов Али', subject='Математика')

    def _edit(self, pw='BrandNew_2026!', name='Раҳимов Али', user=None):
        self.client.force_login(user or self.admin)
        return self.client.post(
            reverse('edit_teacher', args=[self.school.id]),
            {'teacher_id': str(self.teacher.id), 'full_name': name,
             'phone': '+992900000000', 'subject': 'Математика',
             'new_password': pw})


class PasswordAssignment(TeacherEditBase):

    def test_old_password_fails_new_works(self):
        self.assertTrue(self.client.login(
            username='teacher_1_1', password='Teacher_1_1@2026'))
        self.client.logout()
        self._edit()
        self.tuser.refresh_from_db()
        self.assertFalse(self.tuser.check_password('Teacher_1_1@2026'))
        self.assertTrue(self.tuser.check_password('BrandNew_2026!'))
        self.assertTrue(self.client.login(
            username='teacher_1_1', password='BrandNew_2026!'))

    def test_successive_changes_only_latest_valid(self):
        self._edit('Pass_B_2026!')
        self._edit('Pass_C_2026!')
        self.tuser.refresh_from_db()
        self.assertFalse(self.tuser.check_password('Teacher_1_1@2026'))
        self.assertFalse(self.tuser.check_password('Pass_B_2026!'))
        self.assertTrue(self.tuser.check_password('Pass_C_2026!'))

    def test_zavuch_can_change_password(self):
        self._edit('ZavuchSet_2026!', user=self.zavuch)
        self.tuser.refresh_from_db()
        self.assertTrue(self.tuser.check_password('ZavuchSet_2026!'))
        self.assertFalse(self.tuser.check_password('Teacher_1_1@2026'))

    def test_empty_password_leaves_everything_unchanged(self):
        r = self._edit(pw='   ', name='Раҳимов А.')
        self.assertEqual(r.status_code, 302)
        self.tuser.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertTrue(self.tuser.check_password('Teacher_1_1@2026'))
        self.assertFalse(self.profile.custom_password_set)

    def test_existing_session_invalidated(self):
        teacher_client = self.client_class()
        self.assertTrue(teacher_client.login(
            username='teacher_1_1', password='Teacher_1_1@2026'))
        url = reverse('teacher_list', args=[self.school.id])
        self.assertEqual(teacher_client.get(url).status_code, 200)
        self._edit()
        # Django's session auth hash rotates with the password -> old
        # session is rejected and redirected to login.
        self.assertEqual(teacher_client.get(url).status_code, 302)


class CardinalityGate(TeacherEditBase):

    def _messages(self):
        r = self.client.get(
            reverse('teacher_list', args=[self.school.id]))
        return [str(m) for m in r.context['messages']]

    def test_zero_profiles_full_abort(self):
        self.profile.delete()
        r = self._edit(name='Раҳимов А.')
        self.teacher.refresh_from_db()
        self.tuser.refresh_from_db()
        self.assertEqual(self.teacher.name, 'Раҳимов Али')
        self.assertTrue(self.tuser.check_password('Teacher_1_1@2026'))
        msgs = self._messages()
        self.assertTrue(any('ёфт нашуд' in m for m in msgs))
        self.assertFalse(any('таҳрир шуд' in m for m in msgs))

    def test_duplicate_profiles_full_abort(self):
        other = User.objects.create_user(
            username='teacher_1_7', password='Teacher_1_7@2026')
        TeacherProfile.objects.create(
            user=other, school=self.school, full_name='Раҳимов Али')
        self._edit(name='Раҳимов А.', pw='DupPass_2026!')
        self.teacher.refresh_from_db()
        self.tuser.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.teacher.name, 'Раҳимов Али')
        self.assertTrue(self.tuser.check_password('Teacher_1_1@2026'))
        self.assertTrue(other.check_password('Teacher_1_7@2026'))
        msgs = self._messages()
        self.assertTrue(any('такрорӣ' in m for m in msgs))
        self.assertFalse(any('таҳрир шуд' in m for m in msgs))


class MultiSchoolIsolation(TeacherEditBase):

    def test_other_school_account_untouched(self):
        school_b = make_school('Мактаб №2')
        user_b = User.objects.create_user(
            username='teacher_2_1', password='Teacher_2_1@2026',
            first_name='Раҳимов Али')
        TeacherProfile.objects.create(
            user=user_b, school=school_b, full_name='Раҳимов Али')
        Teacher.objects.create(
            school=school_b, name='Раҳимов Али', subject='Математика')

        self._edit('OnlySchool1_2026!')
        self.tuser.refresh_from_db()
        user_b.refresh_from_db()
        self.assertTrue(self.tuser.check_password('OnlySchool1_2026!'))
        self.assertTrue(user_b.check_password('Teacher_2_1@2026'))
        self.assertTrue(self.client.login(
            username='teacher_2_1', password='Teacher_2_1@2026'))


class BadgeFlag(TeacherEditBase):

    def _teacher_ctx(self):
        r = self.client.get(reverse('teacher_list', args=[self.school.id]))
        return [t for t in r.context['teachers'] if t.id == self.teacher.id][0]

    def test_flag_set_and_badge_before_login(self):
        self.client.force_login(self.admin)
        t = self._teacher_ctx()
        self.assertFalse(t.is_password_private)
        self.assertNotIn('Рамзи шахсӣ', t.password_display)
        self._edit()
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.custom_password_set)
        self.assertIsNone(self.tuser.last_login)
        t = self._teacher_ctx()
        self.assertTrue(t.is_password_private)
        self.assertIn('Рамзи шахсӣ', t.password_display)

    def test_last_login_still_drives_badge(self):
        self.tuser.last_login = timezone.now()
        self.tuser.save(update_fields=['last_login'])
        self.client.force_login(self.admin)
        t = self._teacher_ctx()
        self.assertTrue(t.is_password_private)
        self.assertIn('Рамзи шахсӣ', t.password_display)

    def test_default_teacher_shows_generated_password(self):
        self.client.force_login(self.admin)
        t = self._teacher_ctx()
        self.assertFalse(t.is_password_private)
        self.assertEqual(t.password_display, 'Teacher_1_1@2026')


class GradingSafety(TeacherEditBase):

    def test_edit_does_not_touch_grade_data(self):
        student = make_student(self.school, '7-А', 'Фирӯз Раҳимов')
        cs = make_class_subject(
            self.school, '7-А', MATH, teacher=self.tuser)
        grade = make_grade(student, subject=MATH, score='5')
        self._edit()
        grade.refresh_from_db()
        self.assertEqual(grade.score, 5.0)
        self.assertEqual(Grade.objects.filter(student=student).count(), 1)
