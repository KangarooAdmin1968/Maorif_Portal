"""Non-graded class subject selection + sticker pseudo-subject reactivation.

class_detail must render the same subject dropdown for non-graded classes
(e.g. 1-А) as for graded ones — grade_entry already supports qualitative
grading there. The sticker button stays alongside. sticker_entry must
reactivate an existing inactive 'СТИКЕРҲО' ClassSubject row (the
ensure_class_subjects sweep deactivates it as non-official) without
creating duplicates — while still honoring an approved deactivation.
"""
from django.test import TestCase
from django.urls import reverse

from portal.models import ClassSubject
from portal.tests.helpers import (
    GoldenBase, make_class_subject, make_deactivation_request, make_student,
)

STICKER_SUBJECT = 'СТИКЕРҲО'  # normalize_subject('Стикерҳо')


class NonGradedSubjectMenuTests(GoldenBase):
    """The subject dropdown must exist for non-graded classes too."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kid = make_student(cls.school_a, '1-А', 'Сабрина Юсупова')
        make_class_subject(cls.school_a, '1-А', 'АЛИФБО',
                           teacher=cls.teacher_a)

    def _url(self, class_name='1-А'):
        return reverse('class_detail', args=[self.school_a.id, class_name])

    def test_non_graded_class_detail_renders_subject_menu(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="subject-menu"', r.content.decode('utf-8'))
        subs = {s.subject for s in r.context['subjects']}
        self.assertIn('АЛИФБО', subs)
        self.assertIn('МАТЕМАТИКА', subs)

    def test_non_graded_menu_links_reach_grade_entry(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        body = r.content.decode('utf-8')
        self.assertIn('/grade/', body)
        # The menu link target is the existing grade_entry route.
        r2 = self.client.get(reverse(
            'grade_entry', args=[self.school_a.id, '1-А', 'АЛИФБО']))
        self.assertEqual(r2.status_code, 200)

    def test_sticker_button_still_shown_for_non_graded(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url())
        self.assertIn('stickers/', r.content.decode('utf-8'))

    def test_graded_class_unchanged(self):
        self.client.force_login(self.admin)
        r = self.client.get(self._url('7-А'))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode('utf-8')
        self.assertIn('id="subject-menu"', body)
        self.assertNotIn('stickers/', body)
        subs = {s.subject for s in r.context['subjects']}
        self.assertNotIn(STICKER_SUBJECT, subs)

    def test_teacher_assignment_filter_unchanged_for_non_graded(self):
        # teacher_a is assigned only to 1-А АЛИФБО — the menu must show
        # exactly the assigned subject, not the whole official list.
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)
        subs = {s.subject for s in r.context['subjects']}
        self.assertEqual(subs, {'АЛИФБО'})

    def test_teacher_without_assignment_redirected(self):
        # teacher_b has no assignment in school A's 1-А — the existing
        # class-level gate still applies (redirect, not an empty menu).
        self.client.force_login(self.teacher_b)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 302)

    def test_anonymous_sees_no_subject_menu(self):
        r = self.client.get(self._url())
        self.assertNotIn('id="subject-menu"', r.content.decode('utf-8'))


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
