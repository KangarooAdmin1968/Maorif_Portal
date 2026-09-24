"""Canonical subject registry tests (Subject + SubjectAvailability).

Covers: canonical identity/dedup, school/class scoping, the 'Ҳама муассисаҳо'
scope, ensure_class_subjects() compatibility, class picker visibility,
safe removal (no grade loss), and permission boundaries.
"""
from django.test import TestCase
from django.urls import reverse

from portal.models import (
    ClassSubject, Grade, Subject, SubjectAvailability,
)
from portal.tests.helpers import (
    CUSTOM_SUBJECT, D_Q1, GoldenBase, MATH, make_class_subject, make_grade,
    make_student,
)
from portal.utils import (
    ensure_class_subjects, official_subjects, official_subjects_for,
    registry_subjects_for,
)

UZBEK = 'ЗАБОН ВА АДАБИЁТИ ӮЗБЕК'


class CanonicalSubjectTests(GoldenBase):
    """Canonical identity: one Subject row per name, idempotent adds."""

    def _add(self, user, school, class_name, subject):
        self.client.force_login(user)
        return self.client.post(
            reverse('add_remove_subject', args=[school.id, class_name]),
            {'action': 'add', 'subject': subject},
        )

    def test_add_creates_canonical_subject_and_availability(self):
        self._add(self.zavuch_a, self.school_a, '7-А', CUSTOM_SUBJECT)
        self.assertTrue(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school=self.school_a,
            class_name='7-А', is_active=True,
        ).exists())
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=CUSTOM_SUBJECT, is_active=True,
        ).exists())

    def test_duplicate_add_is_idempotent(self):
        self._add(self.zavuch_a, self.school_a, '7-А', CUSTOM_SUBJECT)
        self._add(self.zavuch_a, self.school_a, '7-А', CUSTOM_SUBJECT)
        self.assertEqual(Subject.objects.filter(name=CUSTOM_SUBJECT).count(), 1)
        self.assertEqual(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT).count(), 1)
        self.assertEqual(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=CUSTOM_SUBJECT).count(), 1)

    def test_same_subject_two_schools_shares_canonical_row(self):
        self._add(self.zavuch_a, self.school_a, '7-А', CUSTOM_SUBJECT)
        # second school via its own zavuch
        from portal.tests.helpers import make_user
        zavuch_b = make_user('zavuch_2', role='teacher', school=self.school_b)
        self._add(zavuch_b, self.school_b, '7-Б', CUSTOM_SUBJECT)
        self.assertEqual(Subject.objects.filter(name=CUSTOM_SUBJECT).count(), 1)
        self.assertEqual(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT).count(), 2)

    def test_official_subject_does_not_enter_registry(self):
        self._add(self.zavuch_a, self.school_a, '7-А', MATH)
        self.assertFalse(Subject.objects.filter(name=MATH).exists())


class ApplicabilityTests(GoldenBase):
    """registry_subjects_for / official_subjects_for scoping rules."""

    def _avail(self, name=UZBEK, school=None, class_name='', active=True):
        subj = Subject.objects.create(name=name)
        return SubjectAvailability.objects.create(
            subject=subj, school=school, class_name=class_name,
            is_active=active,
        )

    def test_class_scoped_availability(self):
        self._avail(school=self.school_a, class_name='7-А')
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_a, '7-Б'))
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_b, '7-Б'))

    def test_school_wide_availability(self):
        self._avail(school=self.school_a, class_name='')
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '9-Д'))
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_b, '7-Б'))

    def test_global_availability(self):
        self._avail(school=None)
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        self.assertIn(UZBEK, registry_subjects_for(self.school_b, '7-Б'))

    def test_exact_class_optout_beats_school_wide(self):
        subj = Subject.objects.create(name=UZBEK)
        SubjectAvailability.objects.create(
            subject=subj, school=self.school_a, class_name='')
        SubjectAvailability.objects.create(
            subject=subj, school=self.school_a, class_name='7-А',
            is_active=False)
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '8-А'))

    def test_inactive_subject_not_applicable(self):
        subj = Subject.objects.create(name=UZBEK, is_active=False)
        SubjectAvailability.objects.create(
            subject=subj, school=self.school_a, class_name='7-А')
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))

    def test_registry_joins_official_set(self):
        self._avail(school=self.school_a, class_name='7-А')
        self.assertIn(UZBEK, official_subjects_for(self.school_a, '7-А'))
        self.assertNotIn(UZBEK, official_subjects_for(self.school_b, '7-Б'))


class EnsureCompatibilityTests(GoldenBase):
    """Custom subjects survive the ensure_class_subjects sweep."""

    def test_custom_subject_survives_ensure(self):
        Subject.objects.create(name=UZBEK)
        SubjectAvailability.objects.create(
            subject=Subject.objects.get(name=UZBEK),
            school=self.school_a, class_name='7-А')
        ensure_class_subjects(self.school_a, '7-А')
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=UZBEK, is_active=True).exists())

    def test_ensure_seeds_registry_row_when_missing(self):
        Subject.objects.create(name=UZBEK)
        SubjectAvailability.objects.create(
            subject=Subject.objects.get(name=UZBEK),
            school=self.school_a, class_name='7-А')
        ensure_class_subjects(self.school_a, '7-А')
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=UZBEK)
        self.assertTrue(cs.is_active)
        self.assertFalse(cs.is_default)

    def test_ensure_does_not_seed_in_other_school(self):
        Subject.objects.create(name=UZBEK)
        SubjectAvailability.objects.create(
            subject=Subject.objects.get(name=UZBEK),
            school=self.school_a, class_name='7-А')
        ensure_class_subjects(self.school_b, '7-Б')
        self.assertFalse(ClassSubject.objects.filter(
            school=self.school_b, class_name='7-Б',
            subject=UZBEK).exists())

    def test_ensure_still_seeds_official_curriculum(self):
        from portal.utils import default_subjects_for_class
        from portal.models import normalize_subject
        ensure_class_subjects(self.school_a, '7-А')
        for s in default_subjects_for_class('7-А'):
            self.assertTrue(ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А',
                subject=normalize_subject(s), is_active=True,
                is_default=True).exists(),
                f'default subject missing: {s}')

    def test_approved_deactivation_still_wins(self):
        subj = Subject.objects.create(name=UZBEK)
        SubjectAvailability.objects.create(
            subject=subj, school=self.school_a, class_name='7-А')
        ensure_class_subjects(self.school_a, '7-А')
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=UZBEK)
        from portal.tests.helpers import make_deactivation_request
        make_deactivation_request(cs, self.zavuch_a, status='approved')
        ensure_class_subjects(self.school_a, '7-А')
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)


class RemoveSafetyTests(GoldenBase):
    """Removing availability deactivates only the ClassSubject row."""

    def test_remove_keeps_grades(self):
        make_student(self.school_a, '7-А', 'Тест Хонанда')
        cs = make_class_subject(self.school_a, '7-А', CUSTOM_SUBJECT)
        st = Grade.objects.create(
            student_id=f'{self.school_a.name}__7-А__Тест Хонанда',
            subject=CUSTOM_SUBJECT, score=8, period='Холҳои ҷорӣ (Онлайн)',
            date=D_Q1)
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'remove', 'subject': CUSTOM_SUBJECT})
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)
        self.assertTrue(Grade.objects.filter(id=st.id).exists())

    def test_remove_creates_optout_so_ensure_cannot_readd(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'remove', 'subject': CUSTOM_SUBJECT})
        ensure_class_subjects(self.school_a, '7-А')
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=CUSTOM_SUBJECT)
        self.assertFalse(cs.is_active)
        self.assertNotIn(
            CUSTOM_SUBJECT, registry_subjects_for(self.school_a, '7-А'))


class ClassPickerTests(GoldenBase):
    """Custom subject appears in the correct class's subject picker."""

    def test_custom_subject_in_picker_after_add(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        r = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        self.assertContains(r, CUSTOM_SUBJECT)

    def test_custom_subject_reopened_and_stable(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        # simulate several reopen sweeps
        for _ in range(3):
            r = self.client.get(
                reverse('class_detail', args=[self.school_a.id, '7-А']))
            self.assertContains(r, CUSTOM_SUBJECT)
        self.assertEqual(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=CUSTOM_SUBJECT, is_active=True).count(), 1)

    def test_custom_subject_absent_in_other_school_class(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        self.client.force_login(self.admin)
        r = self.client.get(
            reverse('class_detail', args=[self.school_b.id, '7-Б']))
        self.assertNotContains(r, CUSTOM_SUBJECT)


class PermissionTests(GoldenBase):
    """Only managers may mutate subjects; teachers get redirected/403."""

    def test_teacher_cannot_add_subject(self):
        self.client.force_login(self.teacher_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '7-А']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        self.assertFalse(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())

    def test_zavuch_cannot_add_to_other_school(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_remove_subject', args=[self.school_b.id, '7-Б']),
            {'action': 'add', 'subject': CUSTOM_SUBJECT})
        self.assertFalse(ClassSubject.objects.filter(
            school=self.school_b, class_name='7-Б',
            subject=CUSTOM_SUBJECT).exists())

    def test_teacher_forbidden_on_registry_page(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('subject_registry'))
        self.assertEqual(r.status_code, 403)

    def test_superuser_registry_access(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('subject_registry'))
        self.assertEqual(r.status_code, 200)

    def test_zavuch_registry_restricted_to_own_school(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('subject_registry'))
        self.assertEqual(r.status_code, 200)
        # zavuch cannot choose 'all schools' scope
        r2 = self.client.post(reverse('subject_registry'), {
            'name': CUSTOM_SUBJECT, 'scope': 'all',
            'schools': [str(self.school_b.id)],
        })
        self.assertFalse(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school__isnull=True).exists())
        self.assertFalse(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school=self.school_b).exists())
        # own school got it instead
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school=self.school_a).exists())


class RegistryViewTests(GoldenBase):
    """The management page creates canonical subjects with the right scope."""

    def test_admin_creates_global_subject(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('subject_registry'), {
            'name': CUSTOM_SUBJECT, 'scope': 'all'})
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school__isnull=True,
            is_active=True).exists())
        self.assertIn(CUSTOM_SUBJECT,
                      registry_subjects_for(self.school_b, '7-Б'))

    def test_admin_assigns_selected_classes(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('subject_registry'), {
            'name': UZBEK, 'scope': 'classes',
            'schools': [str(self.school_a.id)], 'classes': ['5-Д', '6-Д'],
        })
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '5-Д'))
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '6-Д'))
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        # ensure seeds the ClassSubject rows for the selected classes
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='5-Д',
            subject=UZBEK, is_active=True).exists())
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='6-Д',
            subject=UZBEK, is_active=True).exists())

    def test_uzbek_subject_scenario(self):
        """Exact motivating case: one canonical Uzbek subject, selected
        classes in selected schools only."""
        self.client.force_login(self.admin)
        self.client.post(reverse('subject_registry'), {
            'name': 'Забон ва адабиёти ӯзбек', 'scope': 'classes',
            'schools': [str(self.school_a.id)], 'classes': ['7-А'],
        })
        self.assertEqual(Subject.objects.filter(name=UZBEK).count(), 1)
        self.assertIn(UZBEK, registry_subjects_for(self.school_a, '7-А'))
        self.assertNotIn(UZBEK, registry_subjects_for(self.school_b, '7-Б'))


class RankingRegressionTests(GoldenBase):
    """Adding a custom subject must not disturb existing rankings."""

    def test_rankings_ignore_subject_registry(self):
        from portal.utils import subjects_leaderboard
        make_grade(self.s1, MATH, 9)
        before = subjects_leaderboard()
        Subject.objects.create(name=CUSTOM_SUBJECT)
        SubjectAvailability.objects.create(
            subject=Subject.objects.get(name=CUSTOM_SUBJECT),
            school=self.school_a, class_name='7-А')
        after = subjects_leaderboard()
        self.assertEqual(before, after)
