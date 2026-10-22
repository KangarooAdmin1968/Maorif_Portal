"""Re-adding a subject after an approved deactivation.

An approved SubjectDeactivationRequest used to stay 'approved' forever, so
ensure_class_subjects() (run on every class page load) disabled the row
again right after any explicit re-add. An explicit re-add now marks the
matching approved request 'reversed' (kept for history); page loads and
ensure_class_subjects() never reverse anything themselves.
"""
from django.urls import reverse

from portal.models import (
    ClassSubject, Subject, SubjectAvailability, SubjectDeactivationRequest,
)
from portal.tests.helpers import (
    GoldenBase, make_deactivation_request, make_student,
)
from portal.utils import ensure_class_subjects

TECH = 'ТЕХНОЛОГИЯ'
IT = 'ТЕХНОЛОГИЯИ ИТТИЛООТӢ'


class ReaddAfterApprovedDeactivationTests(GoldenBase):

    def setUp(self):
        super().setUp()
        make_student(self.school_a, '5-А', 'Хонандаи Якум')
        make_student(self.school_a, '5-Б', 'Хонандаи Дуюм')
        for cn in ('5-А', '5-Б'):
            ensure_class_subjects(self.school_a, cn)
        self.tech = self._cs('5-А', TECH)
        self.it = self._cs('5-А', IT)
        self.it.teacher = self.teacher_a
        self.it.save()
        self.req = self._approve(self.tech)
        # Same subject removed in another class: must stay removed.
        self.other_tech = self._cs('5-Б', TECH)
        self.other_req = self._approve(self.other_tech)

    def _cs(self, class_name, subject):
        return ClassSubject.objects.get(
            school=self.school_a, class_name=class_name, subject=subject)

    def _approve(self, cs):
        req = make_deactivation_request(cs, self.zavuch_a, status='pending')
        self.client.force_login(self.admin)
        self.client.post(reverse('review_deactivation_request'),
                         {'request_id': req.pk, 'action': 'approve'})
        req.refresh_from_db()
        self.assertEqual(req.status, 'approved')
        return req

    def _load_class_page(self, class_name='5-А'):
        self.client.force_login(self.admin)
        r = self.client.get(
            reverse('class_detail', args=[self.school_a.id, class_name]))
        self.assertEqual(r.status_code, 200)

    def _readd_class_page(self, subject='Технология'):
        self.client.force_login(self.zavuch_a)
        return self.client.post(
            reverse('add_remove_subject', args=[self.school_a.id, '5-А']),
            {'action': 'add', 'subject': subject})

    def _assert_untouched_others(self):
        self.other_tech.refresh_from_db()
        self.other_req.refresh_from_db()
        self.assertFalse(self.other_tech.is_active)
        self.assertEqual(self.other_req.status, 'approved')
        self.it.refresh_from_db()
        self.assertTrue(self.it.is_active)
        self.assertEqual(self.it.teacher, self.teacher_a)
        self.assertEqual(self.it.subject, IT)

    def test_approved_deactivation_disables_subject(self):
        self._load_class_page()
        self.tech.refresh_from_db()
        self.assertFalse(self.tech.is_active)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, 'approved')

    def test_page_load_and_ensure_never_reverse(self):
        self._load_class_page()
        ensure_class_subjects(self.school_a, '5-А')
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'approved')
        self.assertFalse(self.tech.is_active)

    def test_class_page_readd_reverses_matching_request(self):
        self._readd_class_page()
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'reversed')
        self.assertEqual(self.req.get_status_display(),
                         'Бекор гардид — фан барқарор шуд')
        self.assertTrue(self.tech.is_active)
        # History preserved: the original request row and reviewer remain.
        self.assertTrue(SubjectDeactivationRequest.objects.filter(
            pk=self.req.pk, reviewed_by=self.admin).exists())
        self._assert_untouched_others()

    def test_subsequent_class_page_load_keeps_subject_and_teacher(self):
        self._readd_class_page()
        tech = self._cs('5-А', TECH)
        tech.teacher = self.teacher_a
        tech.save()
        self._load_class_page()
        ensure_class_subjects(self.school_a, '5-А')
        tech.refresh_from_db()
        self.assertTrue(tech.is_active)
        self.assertEqual(tech.teacher, self.teacher_a)
        self._assert_untouched_others()

    def test_readd_prevents_duplicates_and_registry_row(self):
        self._readd_class_page()
        self._readd_class_page('  технология ')
        self._load_class_page()
        self.assertEqual(ClassSubject.objects.filter(
            school=self.school_a, class_name='5-А', subject=TECH).count(), 1)
        self.assertEqual(self._cs('5-А', TECH).pk, self.tech.pk)
        self.assertFalse(Subject.objects.filter(name=TECH).exists())
        self.assertFalse(Subject.objects.filter(name=IT).exists())

    def test_information_technology_unchanged(self):
        before = ClassSubject.objects.filter(subject=IT).order_by('pk').values()
        before = list(before)
        self._readd_class_page()
        self._load_class_page()
        after = list(ClassSubject.objects.filter(subject=IT).order_by('pk').values())
        self.assertEqual(before, after)
        self.assertFalse(SubjectDeactivationRequest.objects.filter(
            class_subject__subject=IT).exists())

    def test_new_request_allowed_after_reversal(self):
        self._readd_class_page()
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('request_deactivation'),
                         {'cs_id': self.tech.pk, 'reason': 'санҷиш'})
        self.assertTrue(SubjectDeactivationRequest.objects.filter(
            class_subject=self.tech, status='pending').exists())

    def test_registry_wizard_readd_reverses_request(self):
        self.client.force_login(self.admin)
        payload = {
            'action': 'save', 'ack': 'yes', 'pick_name': 'Технология',
            'scope': 'classes', 'schools': [self.school_a.id],
            'classes': ['5-А'],
        }
        self.client.post(reverse('subject_registry'), payload)
        self.client.post(reverse('subject_registry'), payload)
        self._load_class_page()
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'reversed')
        self.assertTrue(self.tech.is_active)
        self.assertLessEqual(Subject.objects.filter(name=TECH).count(), 1)
        self.assertFalse(SubjectAvailability.objects.filter(
            subject__name=TECH, school=self.school_a, class_name='5-Б',
            is_active=True).exists())
        self._assert_untouched_others()

    def test_admin_add_existing_row_reverses_request(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('admin:portal_classsubject_add'), {
            'school': self.school_a.id, 'class_name': '5-А',
            'subject': 'Технология', 'is_active': 'on'})
        self._load_class_page()
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'reversed')
        self.assertTrue(self.tech.is_active)
        self.assertEqual(ClassSubject.objects.filter(
            school=self.school_a, class_name='5-А', subject=TECH).count(), 1)
        self._assert_untouched_others()

    def _allocation_page_readd(self, user, school_field=True):
        """Walk the real 'Тақсимоти дарсҳо' → '➕ Фани нав' wizard flow."""
        self.client.force_login(user)
        alloc = f'{reverse("lesson_allocation")}?school_id={self.school_a.id}'
        r = self.client.get(alloc)
        self.assertContains(r, reverse('subject_registry'))
        url = reverse('subject_registry')
        base = {'pick_name': TECH, 'scope': 'classes', 'classes': ['5-А']}
        if school_field:
            base['schools'] = [self.school_a.id]
        r = self.client.post(url, {'action': 'choose_subject', 'pick_name': TECH})
        self.assertEqual(r.context['step'], 'classes')
        self.assertIn('5-А', r.context['class_choices'])
        for step in ('preview', 'confirm'):
            r = self.client.post(url, dict(base, action=step))
            self.assertEqual(r.context['step'], step)
        r = self.client.post(url, dict(base, action='save', ack='yes'))
        self.assertEqual(r.status_code, 302)
        return self.client.get(alloc if user.is_superuser else reverse('lesson_allocation'))

    def _assert_allocation_readd_fixed(self, page):
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'reversed')
        self.assertTrue(self.tech.is_active)
        self.assertIn(self.tech, list(page.context['class_subjects']))
        self._load_class_page()
        self.tech.refresh_from_db()
        self.assertTrue(self.tech.is_active)
        self.assertEqual(ClassSubject.objects.filter(
            school=self.school_a, class_name='5-А', subject=TECH).count(), 1)
        self.assertLessEqual(Subject.objects.filter(name=TECH).count(), 1)
        self.assertFalse(Subject.objects.filter(name=IT).exists())
        self.assertTrue(SubjectDeactivationRequest.objects.filter(
            pk=self.req.pk, reviewed_by=self.admin).exists())
        self._assert_untouched_others()

    def test_allocation_page_superuser_readd(self):
        page = self._allocation_page_readd(self.admin)
        self._assert_allocation_readd_fixed(page)

    def test_allocation_page_zavuch_readd(self):
        page = self._allocation_page_readd(self.zavuch_a, school_field=False)
        self._assert_allocation_readd_fixed(page)

    def test_admin_reactivate_existing_row_reverses_request(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse('admin:portal_classsubject_change', args=[self.tech.pk]), {
                'school': self.school_a.id, 'class_name': '5-А',
                'subject': TECH, 'is_active': 'on'})
        self._load_class_page()
        self.req.refresh_from_db()
        self.tech.refresh_from_db()
        self.assertEqual(self.req.status, 'reversed')
        self.assertTrue(self.tech.is_active)
        self._assert_untouched_others()


class CancelledStatusChoiceTests(GoldenBase):
    """'cancelled' (owner withdrew a pending request) is a declared status,
    distinct from 'reversed' (an approved removal undone by a re-add)."""

    def setUp(self):
        super().setUp()
        self.req = make_deactivation_request(
            self.cs_a, self.zavuch_a, status='pending')
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('cancel_deactivation_request'),
                         {'request_id': self.req.pk})
        self.req.refresh_from_db()

    def test_cancelled_is_valid_and_labelled(self):
        self.assertEqual(self.req.status, 'cancelled')
        self.req.full_clean()
        self.assertEqual(self.req.get_status_display(), 'Бекор шуд')
        self.assertNotEqual(self.req.get_status_display(),
                            dict(SubjectDeactivationRequest._meta.get_field(
                                'status').choices)['reversed'])

    def test_admin_change_form_keeps_cancelled_selected(self):
        # Without the choice the select had no matching option, so saving
        # the admin form silently submitted the first option ('pending').
        self.client.force_login(self.admin)
        r = self.client.get(reverse(
            'admin:portal_subjectdeactivationrequest_change',
            args=[self.req.pk]))
        self.assertContains(r, '<option value="cancelled" selected>Бекор шуд</option>',
                            html=True)
