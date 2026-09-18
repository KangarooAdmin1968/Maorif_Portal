"""Golden-master tests for the permission model and school isolation.

Freeze the CURRENT role semantics:
    superuser -> everything
    director  -> view all schools; edit requires profile.school == school
    principal -> own school only
    zavuch    -> own school (role=='zavuch' OR username prefix 'zavuch_')
    teacher   -> own school + assigned ClassSubjects only
"""
from django.test import TestCase
from django.urls import reverse

from portal.models import (
    ClassSubject, Grade, normalize_class_name, normalize_subject,
)
from portal.tests.helpers import (
    D_Q1, MATH, TAJIK,
    GoldenBase, make_class_subject, make_school, make_student,
    make_teacher_profile, make_user,
)
from portal.utils import is_academic_school, is_non_graded
from portal.views import (
    _is_regular_teacher,
    _is_zavuch,
    can_edit_grade_journal,
    can_view_grade_journal,
    get_user_role,
    has_school_access,
)


class HelperContractTests(TestCase):
    """Freeze the small predicate functions everything else depends on."""

    def test_get_user_role_defaults_to_teacher(self):
        self.assertEqual(get_user_role(make_user('plain')), 'teacher')

    def test_get_user_role_reads_userprofile(self):
        school = make_school('Мактаб №77')
        u = make_user('p1', role='principal', school=school)
        self.assertEqual(get_user_role(u), 'principal')

    def test_is_zavuch_by_username_prefix(self):
        u = make_user('zavuch_9', role='teacher')
        self.assertTrue(_is_zavuch(u))

    def test_is_zavuch_by_role_value(self):
        u = make_user('regular_name', role='zavuch')
        self.assertTrue(_is_zavuch(u))

    def test_is_zavuch_false_for_plain_teacher(self):
        u = make_user('teacher_9', role='teacher')
        self.assertFalse(_is_zavuch(u))

    def test_is_regular_teacher_excludes_zavuch_username(self):
        # Real zavuch accounts store role='teacher'; the username prefix is
        # what exempts them from teacher restrictions.
        self.assertFalse(_is_regular_teacher(make_user('zavuch_9', role='teacher')))
        self.assertTrue(_is_regular_teacher(make_user('teacher_9', role='teacher')))

    def test_normalize_subject_merges_typo_variants(self):
        self.assertEqual(normalize_subject('ОИҲ'), 'ОИХ')
        self.assertEqual(normalize_subject('OIX'), 'ОИХ')
        self.assertEqual(normalize_subject('одх'), 'ОИХ')
        self.assertEqual(normalize_subject('  математика '), 'МАТЕМАТИКА')

    def test_normalize_class_name_variants(self):
        self.assertEqual(normalize_class_name('10a'), '10-А')
        self.assertEqual(normalize_class_name('7_б'), '7-Б')
        self.assertEqual(normalize_class_name('5 Д'), '5-Д')
        self.assertEqual(normalize_class_name('10-A'), '10-А')  # latin A -> Cyrillic А

    def test_is_non_graded_levels(self):
        for cn, expected in (('0-Б', True), ('1-А', True), ('2-А', False),
                             ('7-А', False), ('10-А', False), ('11-Д', False)):
            self.assertEqual(is_non_graded(cn), expected, cn)

    def test_is_academic_school(self):
        self.assertTrue(is_academic_school(make_school('A1', type='Мактаб')))
        self.assertTrue(is_academic_school(make_school('A2', type='Литсей')))
        self.assertFalse(is_academic_school(
            make_school('Идора', type='Идораи маориф')))
        self.assertFalse(is_academic_school(
            make_school('Том', type='Муассисаи давлатии таълимии томактабӣ')))


class SchoolAccessMatrixTests(GoldenBase):
    """has_school_access: the first line of school isolation."""

    def test_superuser_has_access_to_all_schools(self):
        self.assertTrue(has_school_access(self.admin, self.school_a))
        self.assertTrue(has_school_access(self.admin, self.school_b))

    def test_director_has_access_to_all_schools(self):
        self.assertTrue(has_school_access(self.director, self.school_a))
        self.assertTrue(has_school_access(self.director, self.school_b))

    def test_principal_own_school_only(self):
        self.assertTrue(has_school_access(self.principal_a, self.school_a))
        self.assertFalse(has_school_access(self.principal_a, self.school_b))

    def test_zavuch_own_school_only(self):
        self.assertTrue(has_school_access(self.zavuch_a, self.school_a))
        self.assertFalse(has_school_access(self.zavuch_a, self.school_b))

    def test_teacher_own_school_only(self):
        self.assertTrue(has_school_access(self.teacher_a, self.school_a))
        self.assertFalse(has_school_access(self.teacher_a, self.school_b))


class JournalPermissionMatrixTests(GoldenBase):
    """can_edit_grade_journal / can_view_grade_journal — exact current rules."""

    def test_superuser_edit_anywhere(self):
        self.assertTrue(can_edit_grade_journal(
            self.admin, self.school_a, '7-А', MATH))
        self.assertTrue(can_edit_grade_journal(
            self.admin, self.school_b, '7-Б', MATH))

    def test_director_edit_requires_profile_school_match(self):
        # GOLDEN: can_edit_grade_journal checks `profile.school == school`
        # BEFORE the role branches — a district director with school=None can
        # VIEW every school (has_school_access + can_view role bypass) but
        # cannot edit any journal. The "master access" comment in views.py
        # is unreachable without a profile school.
        self.assertTrue(has_school_access(self.director, self.school_a))
        self.assertTrue(can_view_grade_journal(
            self.director, self.school_a, '7-А', MATH))
        self.assertFalse(can_edit_grade_journal(
            self.director, self.school_a, '7-А', MATH))
        self.assertFalse(can_edit_grade_journal(
            self.director, self.school_b, '7-Б', MATH))

    def test_director_with_school_edits_own_school_only(self):
        # GOLDEN: a director attached to a school edits that school only —
        # the profile.school check also blocks them from other schools.
        d = make_user('director_2', role='director', school=self.school_a)
        self.assertTrue(can_edit_grade_journal(
            d, self.school_a, '7-А', MATH))
        self.assertFalse(can_edit_grade_journal(
            d, self.school_b, '7-Б', MATH))

    def test_principal_edit_own_school_only(self):
        self.assertTrue(can_edit_grade_journal(
            self.principal_a, self.school_a, '7-А', MATH))
        self.assertFalse(can_edit_grade_journal(
            self.principal_a, self.school_b, '7-Б', MATH))

    def test_zavuch_edit_own_school_only(self):
        self.assertTrue(can_edit_grade_journal(
            self.zavuch_a, self.school_a, '7-А', MATH))
        self.assertFalse(can_edit_grade_journal(
            self.zavuch_a, self.school_b, '7-Б', MATH))

    def test_teacher_edit_assigned_subject_via_teacher_field(self):
        self.assertTrue(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', MATH))

    def test_teacher_edit_assigned_subject_via_allocated_teacher(self):
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        make_class_subject(self.school_a, '7-А', TAJIK, allocated=profile)
        self.assertTrue(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', TAJIK))

    def test_teacher_edit_unassigned_subject_denied(self):
        # teacher_a is only assigned to МАТЕМАТИКА; TAJIK exists (auto-seeded)
        # but has no teacher assignment.
        self.assertFalse(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', TAJIK))

    def test_teacher_edit_inactive_assignment_denied(self):
        self.cs_a.is_active = False
        self.cs_a.save()
        self.assertFalse(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', MATH))

    def test_teacher_cross_school_edit_denied(self):
        # TEST 8 core: School B teacher must not touch School A journals.
        self.assertFalse(can_edit_grade_journal(
            self.teacher_b, self.school_a, '7-А', MATH))

    def test_teacher_cross_school_view_denied(self):
        self.assertFalse(can_view_grade_journal(
            self.teacher_b, self.school_a, '7-А', MATH))

    def test_teacher_non_graded_class_wide_permission(self):
        # GOLDEN RULE: in non-graded classes (grades 0-1) ANY active subject
        # assignment inside the class grants journal access — needed because
        # the sticker journal uses the pseudo-subject 'Стикерҳо'.
        make_student(self.school_a, '1-А', 'Сабрина Юсупова')
        make_class_subject(self.school_a, '1-А', 'АЛИФБО', teacher=self.teacher_a)
        self.assertTrue(can_edit_grade_journal(
            self.teacher_a, self.school_a, '1-А', 'СТИКЕРҲО'))
        self.assertTrue(can_edit_grade_journal(
            self.teacher_a, self.school_a, '1-А', 'АЛИФБО'))

    def test_teacher_unassigned_in_non_graded_class_denied(self):
        make_student(self.school_a, '1-В', 'Ҷасур Комилов')
        self.assertFalse(can_edit_grade_journal(
            self.teacher_a, self.school_a, '1-В', 'АЛИФБО'))

    def test_view_mirrors_edit_for_teacher(self):
        self.assertTrue(can_view_grade_journal(
            self.teacher_a, self.school_a, '7-А', MATH))
        self.assertFalse(can_view_grade_journal(
            self.teacher_a, self.school_a, '7-А', TAJIK))

    def test_anonymous_cannot_edit(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(can_edit_grade_journal(
            AnonymousUser(), self.school_a, '7-А', MATH))


class EndpointIsolationTests(GoldenBase):
    """Endpoint-level enforcement of the matrix above."""

    def setUp(self):
        super().setUp()
        self.ajax_url = reverse('save_grade_ajax')

    # --- save_grade_ajax ---------------------------------------------------

    def test_save_grade_ajax_cross_school_post_403(self):
        # TEST 8: School B teacher tries to write into School A's journal.
        self.client.force_login(self.teacher_b)
        r = self.client.post(self.ajax_url, {
            'student_id': self.s1.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8',
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse(r.json()['success'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_save_grade_ajax_unassigned_subject_403(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(self.ajax_url, {
            'student_id': self.s1.id, 'subject': TAJIK,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8',
        })
        self.assertEqual(r.status_code, 403)

    def test_save_grade_ajax_assigned_subject_allowed(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(self.ajax_url, {
            'student_id': self.s1.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8',
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['success'])

    def test_save_grade_ajax_zavuch_own_school_allowed(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(self.ajax_url, {
            'student_id': self.s1.id, 'subject': TAJIK,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8',
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['success'])

    def test_save_grade_ajax_zavuch_cross_school_403(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(self.ajax_url, {
            'student_id': self.s3.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8',
        })
        self.assertEqual(r.status_code, 403)

    # --- grade_entry GET ----------------------------------------------------

    def _journal_url(self, school, class_name, subject):
        return reverse('grade_entry', args=[school.id, class_name, subject])

    def test_grade_entry_superuser_200(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._journal_url(self.school_a, '7-А', 'Математика'))
        self.assertEqual(r.status_code, 200)

    def test_grade_entry_teacher_assigned_200(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._journal_url(self.school_a, '7-А', 'Математика'))
        self.assertEqual(r.status_code, 200)

    def test_grade_entry_teacher_wrong_school_redirects_own_class_list(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._journal_url(self.school_b, '7-Б', 'Математика'))
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'/school/{self.school_a.id}/', r.url)

    def test_grade_entry_teacher_unassigned_subject_redirects(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._journal_url(self.school_a, '7-А', 'Забони тоҷикӣ'))
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'/school/{self.school_a.id}/', r.url)

    def test_grade_entry_zavuch_200(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self._journal_url(self.school_a, '7-А', 'Математика'))
        self.assertEqual(r.status_code, 200)

    def test_grade_entry_anonymous_redirects_login(self):
        r = self.client.get(self._journal_url(self.school_a, '7-А', 'Математика'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    # --- teacher-facing listing filters (recent change, now frozen) ---------

    def test_class_list_teacher_filtered_to_assigned_classes(self):
        make_student(self.school_a, '8-Б', 'Шаҳром Одилов')  # not assigned
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('class_list', args=[self.school_a.id]))
        names = [c['class_name'] for c in r.context['class_stats']]
        self.assertEqual(names, ['7-А'])

    def test_class_list_admin_sees_all_classes(self):
        make_student(self.school_a, '8-Б', 'Шаҳром Одилов')
        self.client.force_login(self.admin)
        r = self.client.get(reverse('class_list', args=[self.school_a.id]))
        names = [c['class_name'] for c in r.context['class_stats']]
        self.assertEqual(names, ['7-А', '8-Б'])

    def test_class_list_zavuch_sees_all_classes(self):
        make_student(self.school_a, '8-Б', 'Шаҳром Одилов')
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('class_list', args=[self.school_a.id]))
        names = [c['class_name'] for c in r.context['class_stats']]
        self.assertEqual(names, ['7-А', '8-Б'])

    def test_class_list_anonymous_public_view(self):
        r = self.client.get(reverse('class_list', args=[self.school_a.id]))
        self.assertEqual(r.status_code, 200)

    def test_class_list_teacher_wrong_school_redirects(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('class_list', args=[self.school_b.id]))
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'/school/{self.school_a.id}/', r.url)

    def _make_grade5_class(self):
        """Grade 5 uses 'Математика' in TJC_SUBJECTS (grade 7 uses
        Алгебра/Геометрия instead), so a '5-В' assignment is official."""
        make_student(self.school_a, '5-В', 'Ҷасур Юлдошев')
        make_class_subject(self.school_a, '5-В', MATH, teacher=self.teacher_a)

    def test_class_detail_teacher_sees_only_assigned_subjects(self):
        self._make_grade5_class()
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('class_detail',
                                    args=[self.school_a.id, '5-В']))
        subjects = [cs.subject for cs in r.context['subjects']]
        self.assertEqual(subjects, [MATH])

    def test_class_detail_admin_sees_all_official_subjects(self):
        self._make_grade5_class()
        self.client.force_login(self.admin)
        r = self.client.get(reverse('class_detail',
                                    args=[self.school_a.id, '5-В']))
        subjects = [cs.subject for cs in r.context['subjects']]
        self.assertIn(MATH, subjects)
        self.assertIn(TAJIK, subjects)
        self.assertGreater(len(subjects), 1)

    def test_class_detail_visit_deactivates_non_official_assignment(self):
        # GOLDEN — the School-№5 blocker: МАТЕМАТИКА is not an official
        # grade-7 subject, so class_detail's ensure_class_subjects call
        # deactivates the teacher's assignment as a side effect and their
        # journal permission disappears with it.
        self.assertTrue(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', MATH))
        self.client.force_login(self.admin)
        self.client.get(reverse('class_detail',
                                args=[self.school_a.id, '7-А']))
        self.assertFalse(can_edit_grade_journal(
            self.teacher_a, self.school_a, '7-А', MATH))
        self.assertFalse(ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=MATH).is_active)

    def test_class_detail_teacher_unassigned_class_redirects(self):
        make_student(self.school_a, '8-Б', 'Шаҳром Одилов')
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('class_detail',
                                    args=[self.school_a.id, '8-Б']))
        self.assertEqual(r.status_code, 302)

    # --- parent portal surface ----------------------------------------------

    def test_student_detail_public_anonymous_200(self):
        r = self.client.get(reverse('student_detail', args=[self.s1.id]))
        self.assertEqual(r.status_code, 200)
