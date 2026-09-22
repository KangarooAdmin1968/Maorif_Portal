"""Non-graded class UI contract + sticker pseudo-subject reactivation.

Non-graded classes (e.g. 1-А) have no numeric/quarter grading, so
class_detail must NOT show the subject grading selector — the class-wide
sticker journal is the only entry point. The underlying ClassSubject rows
and teacher assignments must be left fully intact (they drive lesson
allocation and permission checks). sticker_entry must reactivate an
existing inactive 'СТИКЕРҲО' ClassSubject row (the ensure_class_subjects
sweep deactivates it as non-official) without creating duplicates —
while still honoring an approved deactivation.
"""
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from portal.models import ClassSubject, Grade
from portal.tests.helpers import (
    D_Q1, GoldenBase, make_class_subject, make_deactivation_request,
    make_grade, make_student,
)

STICKER_SUBJECT = 'СТИКЕРҲО'  # normalize_subject('Стикерҳо')


class NonGradedClassUiTests(GoldenBase):
    """1-А shows only the class-wide sticker button — no grading selector."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kid = make_student(cls.school_a, '1-А', 'Сабрина Юсупова')
        cls.assigned = make_class_subject(
            cls.school_a, '1-А', 'АЛИФБО', teacher=cls.teacher_a)

    def _url(self, class_name='1-А'):
        return reverse('class_detail', args=[self.school_a.id, class_name])

    def test_non_graded_class_detail_hides_subject_menu(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)
        body = r.content.decode('utf-8')
        self.assertNotIn('id="subject-menu"', body)
        self.assertNotIn('Интихоби фан', body)
        self.assertNotIn('/grade/', body)

    def test_non_graded_class_detail_shows_sticker_button(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        self.assertIn('stickers/', r.content.decode('utf-8'))

    def test_graded_class_shows_subject_menu(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url('7-А'))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode('utf-8')
        self.assertIn('id="subject-menu"', body)
        self.assertNotIn('stickers/', body)

    def test_no_subject_scoped_sticker_url(self):
        self.client.force_login(self.admin)
        r = self.client.get(
            f'/school/{self.school_a.id}/class/1-А/stickers/АЛИФБО/')
        self.assertEqual(r.status_code, 404)
        with self.assertRaises(NoReverseMatch):
            reverse('sticker_entry_subject',
                    args=[self.school_a.id, '1-А', 'АЛИФБО'])

    def test_teacher_without_assignment_redirected(self):
        # teacher_b has no assignment in school A's 1-А — the existing
        # class-level gate still applies (redirect, not an empty page).
        self.client.force_login(self.teacher_b)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 302)

    def test_anonymous_sees_no_subject_menu(self):
        r = self.client.get(self._url())
        self.assertNotIn('id="subject-menu"', r.content.decode('utf-8'))

    def test_teacher_assignment_preserved_in_non_graded_class(self):
        # Hiding the grading UI must not deactivate or detach the teacher's
        # ClassSubject assignment — it still drives allocation and access.
        self.client.force_login(self.admin)
        self.client.get(self._url())
        self.client.get(
            reverse('sticker_entry', args=[self.school_a.id, '1-А']))
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='1-А', subject='АЛИФБО')
        self.assertTrue(cs.is_active)
        self.assertEqual(cs.teacher_id, self.teacher_a.id)

    def test_assigned_teacher_keeps_class_access(self):
        # The assignment still grants the teacher the class-wide sticker
        # journal and any existing activity/ranking-eligibility checks that
        # read active ClassSubject assignments.
        self.client.force_login(self.teacher_a)
        r = self.client.get(
            reverse('sticker_entry', args=[self.school_a.id, '1-А']))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='1-А', is_active=True,
            teacher=self.teacher_a).exists())


class StickerReactivationTests(GoldenBase):
    """sticker_entry reactivates an inactive 'СТИКЕРҲО' row safely."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kid = make_student(cls.school_a, '1-А', 'Сабрина Юсупова')
        make_class_subject(cls.school_a, '1-А', 'АЛИФБО',
                           teacher=cls.teacher_a)

    def _url(self):
        return reverse('sticker_entry', args=[self.school_a.id, '1-А'])

    def test_inactive_sticker_row_reactivated(self):
        cs = make_class_subject(self.school_a, '1-А', STICKER_SUBJECT,
                                is_active=False)
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)

    def test_no_duplicate_sticker_row_created(self):
        cs = make_class_subject(self.school_a, '1-А', STICKER_SUBJECT,
                                is_active=False)
        self.client.force_login(self.admin)
        self.client.get(self._url())
        self.assertEqual(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='1-А',
                subject=STICKER_SUBJECT).count(), 1)
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)

    def test_approved_deactivation_not_overridden(self):
        cs = make_class_subject(self.school_a, '1-А', STICKER_SUBJECT,
                                is_active=False)
        make_deactivation_request(cs, self.zavuch_a, status='approved')
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        # The journal still opens (Grade rows are string-keyed), but the
        # deliberately approved deactivation is left alone.
        self.assertEqual(r.status_code, 200)
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)

    def test_sticker_journal_still_works(self):
        # Teacher with a class-wide assignment reaches the page unchanged.
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)
        self.assertIn(STICKER_SUBJECT, r.content.decode('utf-8'))

    def test_class_wide_sticker_save_scoped_to_pseudo_subject(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
            'date': D_Q1.isoformat(), 'type': 'daily', 'sticker': '⭐',
        })
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(
            student=self.kid, subject=STICKER_SUBJECT).sticker, '⭐')
