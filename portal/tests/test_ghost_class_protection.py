# -*- coding: utf-8 -*-
"""Regression tests for the ghost-class bug.

Production symptom: classes like ``5-%D0%90`` appeared next to the real
``5-А`` — a percent-encoded class name was persisted because a double-
encoded URL (``/class/5-%25D0%2590/``) hit the public ``class_detail``
view, which ran ``ensure_class_subjects()`` on the literal decoded name.
Crawler and user visits then kept re-seeding the ghost via self-generated
double-encoded links.

Fixes under test:
- ``normalize_class_name`` unwraps extra percent-encoding layers.
- ``ensure_class_subjects`` refuses non-canonical class names.
- ``class_detail`` canonical-redirects variant names and only seeds
  subjects for classes that actually exist.
- Listing/context sources merge stored variants onto the canonical class.
"""
from django.test import TestCase
from django.urls import reverse

from portal.models import ClassSubject, Student, normalize_class_name
from portal.utils import ensure_class_subjects
from portal.tests.helpers import GoldenBase, make_student


class NormalizeClassNameEncodingTests(TestCase):

    def test_single_percent_layer_decodes(self):
        self.assertEqual(normalize_class_name('5-%D0%90'), '5-А')

    def test_double_percent_layer_decodes(self):
        self.assertEqual(normalize_class_name('5-%25D0%2590'), '5-А')
        self.assertEqual(normalize_class_name('9-%25D0%2591'), '9-Б')

    def test_canonical_and_latin_forms_unchanged(self):
        self.assertEqual(normalize_class_name('5-А'), '5-А')
        self.assertEqual(normalize_class_name('5-a'), '5-А')
        self.assertEqual(normalize_class_name('5_A'), '5-А')

    def test_garbage_stays_literal_but_non_canonical(self):
        self.assertEqual(normalize_class_name('junk'), 'JUNK')
        self.assertEqual(normalize_class_name(''), '')


class EnsureClassSubjectsGuardTests(GoldenBase):

    def test_garbage_name_seeds_nothing(self):
        created = ensure_class_subjects(self.school_a, 'GARBAGE')
        self.assertEqual(created, 0)
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='GARBAGE').exists())

    def test_mojibake_name_seeds_nothing(self):
        created = ensure_class_subjects(self.school_a, '6-Ð\x90')
        self.assertEqual(created, 0)
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.school_a, class_name__contains='Ð').exists())

    def test_ghost_class_never_maintained(self):
        # A non-canonical class with no students gets no sweep at all.
        cs = ClassSubject.objects.bulk_create([ClassSubject(
            school=self.school_a, class_name='6-Ð\x90',
            subject='ФАНИ ИХТИЁРИЙ', is_active=True, is_default=False)])[0]
        ensure_class_subjects(self.school_a, '6-Ð\x90')
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)

    def test_real_noncanonical_class_with_students_still_maintained(self):
        # Uzbek-track Latin-letter classes are real — the sweep still runs.
        make_student(self.school_a, '5-E', 'Ўзбек Трек Хонанда')
        cs = ClassSubject.objects.create(
            school=self.school_a, class_name='5-E',
            subject='ФАНИ ИХТИЁРИЙ', is_active=True, is_default=False)
        ensure_class_subjects(self.school_a, '5-E')
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)

    def test_encoded_name_resolves_to_real_class(self):
        created = ensure_class_subjects(self.school_a, '5-%D0%90')
        self.assertGreater(created, 0)
        self.assertTrue(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='5-А').exists())
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.school_a, class_name__contains='%').exists())


class ClassDetailGhostTests(GoldenBase):

    def test_double_encoded_url_redirects_to_canonical(self):
        make_student(self.school_a, '5-А', 'Тест Хонанда')
        url = reverse('class_detail', args=[self.school_a.id, '5-%D0%90'])
        self.assertIn('%25D0%2590', url)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r['Location'].endswith(
            f'/school/{self.school_a.id}/class/5-%D0%90/'))
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.school_a, class_name__contains='%').exists())

    def test_variant_name_redirects_to_canonical(self):
        url = reverse('class_detail', args=[self.school_a.id, '5-a'])
        r = self.client.get(url)
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r['Location'].endswith(
            f'/school/{self.school_a.id}/class/5-%D0%90/'))

    def test_nonexistent_canonical_class_never_seeds(self):
        r = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '9-Я']))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='9-Я').exists())

    def test_existing_class_still_backfills_subjects(self):
        # Legacy backfill: a class with students but missing ClassSubject
        # rows must still be re-seeded on page load.
        ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А').delete()
        r = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А').exists())

    def test_archived_only_class_still_seeds(self):
        student = make_student(self.school_a, '6-Г', 'Рафта Хонанда')
        ClassSubject.objects.filter(
            school=self.school_a, class_name='6-Г').delete()
        Student.all_objects.filter(pk=student.pk).update(is_archived=True)
        r = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '6-Г']))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='6-Г').exists())


class GhostMergeInListingsTests(GoldenBase):
    """Stored ghost rows (legacy data) merge onto the canonical class in
    listings — no duplicate chips/rows even before data cleanup."""

    def _make_ghost(self):
        # bulk_create bypasses save() normalization, simulating the
        # malformed rows already persisted in production.
        ClassSubject.objects.bulk_create([
            ClassSubject(
                school=self.school_a, class_name='5-%D0%90',
                subject='МАТЕМАТИКА', is_active=True, is_default=True),
        ])

    def test_parent_portal_collapses_ghost_variant(self):
        make_student(self.school_a, '5-А', 'Тест Хонанда')
        self._make_ghost()
        self.client.force_login(self.admin)
        r = self.client.get(reverse('dashboard'))
        names = r.context['parent_portal_data']['classes'][
            str(self.school_a.id)]
        self.assertEqual(names.count('5-А'), 1)
        self.assertFalse(any('%' in n for n in names))

    def test_class_list_merges_ghost_variant(self):
        make_student(self.school_a, '5-А', 'Тест Хонанда')
        self._make_ghost()
        r = self.client.get(
            reverse('class_list', args=[self.school_a.id]))
        names = [s['class_name'] for s in r.context['class_stats']]
        self.assertEqual(names.count('5-А'), 1)
        self.assertFalse(any('%' in n for n in names))
        row = next(s for s in r.context['class_stats']
                   if s['class_name'] == '5-А')
        self.assertEqual(row['student_count'], 1)
