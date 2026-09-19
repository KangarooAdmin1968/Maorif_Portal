"""School+class-scoped extra subjects: 'Забон ва адабиёти ӯзбек' at School No. 5.

Covers the SCHOOL_CLASS_SUBJECTS configuration in portal/utils.py:

- the Uzbek subject is seeded ONLY for the configured School No. 5 classes;
- the same class names in other schools never receive it;
- other School No. 5 classes never receive it;
- 'Забони давлатӣ' is preserved (not deactivated) where it already exists
  but is never auto-created;
- ensure_class_subjects() is idempotent and never removes the extra;
- Student.save() seeds the extra too;
- class_detail lists the extra and grade_entry can open it.
"""
from django.test import TestCase
from django.urls import reverse

from portal.models import ClassSubject
from portal.tests.helpers import (
    make_class_subject, make_school, make_student, make_user,
)
from portal.utils import ensure_class_subjects, official_subjects_for

UZBEK = 'ЗАБОН ВА АДАБИЁТИ ӮЗБЕК'          # normalize_subject('Забон ва адабиёти ӯзбек')
DAVLATI = 'ЗАБОНИ ДАВЛАТӢ'                 # 'Забони давлатӣ' — must survive, never seeded
UZBEK_CLASSES = ('3-Д', '4-Ғ', '4-Д', '5-Д', '5-Е', '6-Д', '6-Е', '7-Д', '8-Е', '9-Д')


class SchoolScopedSubjectTests(TestCase):
    def setUp(self):
        self.school5 = make_school('МТМУ №5')   # get_school_number -> '5'
        self.other = make_school('Мактаб №20')  # get_school_number -> '20'
        self.admin = make_user('admin_uz', superuser=True)

    def _active(self, school, class_name):
        ensure_class_subjects(school, class_name)
        return set(ClassSubject.objects.filter(
            school=school, class_name=class_name, is_active=True
        ).values_list('subject', flat=True))

    # 1-10 — every configured class gets the subject
    def test_uzbek_subject_added_to_configured_classes(self):
        for c in UZBEK_CLASSES:
            with self.subTest(class_name=c):
                self.assertIn(UZBEK, self._active(self.school5, c))

    # 11 — other School No. 5 classes are untouched
    def test_other_school5_classes_do_not_get_subject(self):
        for c in ('3-А', '4-А', '7-А', '9-В', '10-А', '11-Б'):
            with self.subTest(class_name=c):
                self.assertNotIn(UZBEK, self._active(self.school5, c))

    # 12 — identical class names in another school get nothing
    def test_same_class_names_in_other_school_do_not_get_subject(self):
        for c in UZBEK_CLASSES:
            with self.subTest(class_name=c):
                self.assertNotIn(UZBEK, self._active(self.other, c))

    def test_extra_is_not_curriculum_default(self):
        self._active(self.school5, '3-Д')
        cs = ClassSubject.objects.get(
            school=self.school5, class_name='3-Д', subject=UZBEK
        )
        self.assertFalse(cs.is_default)
        self.assertTrue(cs.is_active)

    # 13 — 'Забони давлатӣ' is preserved where present, never created
    def test_davlati_preserved_when_present(self):
        make_class_subject(self.school5, '3-Д', DAVLATI)
        self.assertIn(DAVLATI, self._active(self.school5, '3-Д'))

    def test_davlati_never_seeded(self):
        # A class where Забони давлатӣ does not exist must not gain it.
        self.assertNotIn(DAVLATI, self._active(self.school5, '3-Д'))

    def test_davlati_preserved_only_for_configured_classes(self):
        make_class_subject(self.school5, '7-А', DAVLATI)
        # Not configured for 7-А — the normal deactivation rule applies.
        self.assertNotIn(DAVLATI, self._active(self.school5, '7-А'))

    # Persistence through the maintenance path
    def test_ensure_rerun_keeps_subject_and_no_duplicates(self):
        for _ in range(3):
            ensure_class_subjects(self.school5, '3-Д')
        rows = ClassSubject.objects.filter(
            school=self.school5, class_name='3-Д', subject=UZBEK
        )
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.first().is_active)

    def test_ensure_reactivates_accidentally_deactivated_extra(self):
        ensure_class_subjects(self.school5, '3-Д')
        ClassSubject.objects.filter(
            school=self.school5, class_name='3-Д', subject=UZBEK
        ).update(is_active=False)
        ensure_class_subjects(self.school5, '3-Д')
        self.assertTrue(
            ClassSubject.objects.get(
                school=self.school5, class_name='3-Д', subject=UZBEK
            ).is_active
        )

    def test_student_creation_seeds_subject(self):
        make_student(self.school5, '3-Д', 'Аҳмадов Фирдавс')
        self.assertTrue(
            ClassSubject.objects.filter(
                school=self.school5, class_name='3-Д',
                subject=UZBEK, is_active=True,
            ).exists()
        )

    def test_student_in_other_school_does_not_seed_subject(self):
        make_student(self.other, '3-Д', 'Аҳмадов Фирдавс')
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.other, class_name='3-Д', subject=UZBEK
            ).exists()
        )

    # 15 — the class page lists it and the journal opens it
    def test_class_detail_lists_uzbek_subject(self):
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse('class_detail', args=[self.school5.id, '3-Д'])
        )
        self.assertEqual(resp.status_code, 200)
        subs = {s.subject for s in resp.context['subjects']}
        self.assertIn(UZBEK, subs)

    def test_class_detail_other_school_does_not_list_it(self):
        ensure_class_subjects(self.other, '3-Д')
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse('class_detail', args=[self.other.id, '3-Д'])
        )
        self.assertEqual(resp.status_code, 200)
        subs = {s.subject for s in resp.context['subjects']}
        self.assertNotIn(UZBEK, subs)

    def test_grade_entry_opens_uzbek_subject(self):
        ensure_class_subjects(self.school5, '3-Д')
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse('grade_entry', args=[self.school5.id, '3-Д', UZBEK])
        )
        self.assertEqual(resp.status_code, 200)

    # 16 — global official set stays clean; scoping is per school+class
    def test_uzbek_not_in_global_official_subjects(self):
        from portal.utils import official_subjects
        self.assertNotIn(UZBEK, official_subjects())
        self.assertIn(UZBEK, official_subjects_for(self.school5, '3-Д'))
        self.assertNotIn(UZBEK, official_subjects_for(self.other, '3-Д'))
        self.assertNotIn(UZBEK, official_subjects_for(self.school5, '7-А'))
