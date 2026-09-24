"""Controlled 'Фани нав' workflow tests (entry: Тақсимоти дарсҳо).

Covers the multi-step subject wizard on top of the canonical registry:
entry-point button, subject selection, duplicate protection, school/class
scoping, preview + double confirmation, availability save, reopening an
existing configuration, the deputy removal-request flow and the admin
approve/reject review.
"""
from django.urls import reverse

from portal.models import (
    ClassSubject, Grade, QuarterGrade, Subject, SubjectAvailability,
    SubjectDeactivationRequest,
)
from portal.tests.helpers import (
    CUSTOM_SUBJECT, GoldenBase, MATH, make_grade, make_quarter_grade,
    make_deactivation_request, make_student,
)
from portal.utils import ensure_class_subjects, registry_subjects_for
from portal.views import _can_manage_subjects

UZBEK = 'ЗАБОН ВА АДАБИЁТИ ӮЗБЕК'
REGISTRY_URL = reverse('subject_registry')
ALLOC_URL = reverse('lesson_allocation')


def _seed_registry_subject(school, class_name, name=UZBEK):
    """Configure `name` for one school/class through the registry pathway."""
    subj, _ = Subject.objects.get_or_create(name=name)
    SubjectAvailability.objects.update_or_create(
        subject=subj, school=school, class_name=class_name,
        defaults={'is_active': True},
    )
    ensure_class_subjects(school, class_name)
    return subj


class EntryPointTests(GoldenBase):
    """Part 1: '➕ Фани нав' lives inside Тақсимоти дарсҳо."""

    def test_button_visible_for_zavuch(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(ALLOC_URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '➕ Фани нав')
        self.assertContains(r, REGISTRY_URL)

    def test_button_visible_for_superuser(self):
        self.client.force_login(self.admin)
        r = self.client.get(ALLOC_URL)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '➕ Фани нав')

    def test_manage_subjects_permission_flags(self):
        self.assertTrue(_can_manage_subjects(self.zavuch_a))
        self.assertTrue(_can_manage_subjects(self.admin))
        self.assertTrue(_can_manage_subjects(self.director))
        self.assertTrue(_can_manage_subjects(self.principal_a))
        self.assertFalse(_can_manage_subjects(self.teacher_a))

    def test_teacher_cannot_open_allocation_or_registry(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(ALLOC_URL)
        self.assertEqual(r.status_code, 302)
        r = self.client.get(REGISTRY_URL)
        self.assertEqual(r.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(REGISTRY_URL)
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r['Location'])


class SelectStepTests(GoldenBase):
    """Part 2-3: searchable canonical list first, creation only if absent."""

    def test_select_step_shows_tajik_ui(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(REGISTRY_URL)
        for s in (
            'Феҳристи фанҳо', 'Фанро интихоб кунед', 'Ҷустуҷӯи фан',
            'Фан дар рӯйхат нест', '➕ Фани нав эҷод кардан', 'Номи фан',
        ):
            self.assertContains(r, s)

    def test_select_step_lists_official_and_registry_subjects(self):
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        self.client.force_login(self.zavuch_a)
        r = self.client.get(REGISTRY_URL)
        self.assertContains(r, MATH)
        self.assertContains(r, UZBEK)

    def test_choose_existing_subject_shows_classes_step(self):
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'pick_name': UZBEK})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Синфҳоро интихоб кунед')
        self.assertContains(r, 'Муассиса')
        self.assertContains(r, self.school_a.name)

    def test_choose_new_subject_shows_classes_step(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': CUSTOM_SUBJECT})
        self.assertContains(r, 'Синфҳоро интихоб кунед')
        self.assertContains(r, CUSTOM_SUBJECT)
        # Nothing is persisted before preview + confirmation.
        self.assertFalse(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())
        self.assertFalse(SubjectAvailability.objects.exists())

    def test_normalized_duplicate_rejected(self):
        Subject.objects.create(name=UZBEK)
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject',
            'new_name': '  забон   ва адабиёти   ӯзбек '}, follow=True)
        self.assertContains(
            r, 'Ин фан аллакай дар рӯйхат мавҷуд аст.')
        self.assertEqual(Subject.objects.filter(name=UZBEK).count(), 1)

    def test_official_name_rejected_as_new(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': 'математика'}, follow=True)
        self.assertContains(
            r, 'Ин фан аллакай дар рӯйхат мавҷуд аст.')
        self.assertFalse(Subject.objects.filter(name=MATH).exists())

    def test_unknown_pick_rejected(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'pick_name': 'ФАНИ НОМАЪЛУМ'})
        self.assertFalse(Subject.objects.filter(name='ФАНИ НОМАЪЛУМ').exists())


class ScopeAndClassTests(GoldenBase):
    """Part 4-5: deputy is bound to own school; explicit class selection."""

    def test_zavuch_bound_to_own_school(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': CUSTOM_SUBJECT})
        self.assertContains(r, self.school_a.name)
        self.assertNotContains(r, 'Ҳама муассисаҳо')
        # Other schools are not offered to a deputy.
        self.assertNotContains(r, self.school_b.name)

    def test_classes_limited_to_own_school(self):
        make_student(self.school_b, '9-Б', 'Хонандаи Дигар')
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': CUSTOM_SUBJECT})
        self.assertContains(r, '7-А')
        self.assertNotContains(r, '9-Б')
        self.assertNotContains(r, '7-Б')

    def test_save_forces_own_school_and_own_classes(self):
        # A crafted POST targeting another school must not leak.
        self.client.force_login(self.zavuch_a)
        self.client.post(REGISTRY_URL, {
            'action': 'save', 'ack': 'yes', 'new_name': CUSTOM_SUBJECT,
            'scope': 'all', 'schools': [str(self.school_b.id)],
            'classes': ['7-Б']})
        self.assertFalse(SubjectAvailability.objects.filter(
            school=self.school_b).exists())
        self.assertFalse(SubjectAvailability.objects.filter(
            school__isnull=True).exists())

    def test_select_all_offered_as_deliberate_action(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': CUSTOM_SUBJECT})
        self.assertContains(r, 'Ҳамаи синфҳо')

    def test_unselected_classes_unaffected(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(REGISTRY_URL, {
            'action': 'save', 'ack': 'yes', 'new_name': CUSTOM_SUBJECT,
            'classes': ['7-А']})
        self.assertTrue(SubjectAvailability.objects.filter(
            school=self.school_a, class_name='7-А', is_active=True).exists())
        self.assertEqual(
            SubjectAvailability.objects.filter(
                school=self.school_a).exclude(class_name='7-А').count(), 0)
        self.assertFalse(ClassSubject.objects.filter(
            school=self.school_a, class_name='1-А',
            subject=CUSTOM_SUBJECT).exists())


class PreviewConfirmSaveTests(GoldenBase):
    """Part 6-8: preview, double confirmation, final save."""

    def _save(self, user=None, follow=False, **data):
        self.client.force_login(user or self.zavuch_a)
        return self.client.post(REGISTRY_URL, data, follow=follow)

    def test_preview_shows_summary_without_saving(self):
        r = self._save(
            action='preview', new_name=CUSTOM_SUBJECT, classes=['7-А'])
        self.assertContains(r, 'Пешнамоиш')
        self.assertContains(r, 'Фан:')
        self.assertContains(r, 'Муассиса:')
        self.assertContains(r, 'Синфҳо:')
        self.assertContains(r, CUSTOM_SUBJECT)
        self.assertContains(r, self.school_a.name)
        self.assertContains(r, '7-А')
        self.assertContains(r, 'Ин фан ба 1 синф илова карда мешавад.')
        self.assertContains(r, '↩ Баргаштан')
        self.assertContains(r, '✅ Тасдиқ кардан')
        self.assertFalse(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())
        self.assertFalse(SubjectAvailability.objects.exists())

    def test_confirm_step_shows_safety_question(self):
        r = self._save(
            action='confirm', new_name=CUSTOM_SUBJECT, classes=['7-А'])
        self.assertContains(
            r, 'Шумо дурустии номи фан ва синфҳои интихобшударо санҷидед?')
        self.assertContains(r, 'Ҳа, тасдиқ мекунам')
        self.assertContains(r, 'Не, баргаштан')
        self.assertFalse(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())

    def test_save_without_ack_lands_on_confirmation(self):
        r = self._save(
            action='save', new_name=CUSTOM_SUBJECT, classes=['7-А'])
        self.assertContains(
            r, 'Шумо дурустии номи фан ва синфҳои интихобшударо санҷидед?')
        self.assertFalse(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())
        self.assertFalse(SubjectAvailability.objects.exists())

    def test_full_wizard_save_creates_availability(self):
        r = self._save(
            action='save', ack='yes', new_name=CUSTOM_SUBJECT,
            classes=['7-А'], follow=True)
        self.assertContains(r, 'Фан бо муваффақият илова карда шуд.')
        self.assertTrue(Subject.objects.filter(name=CUSTOM_SUBJECT).exists())
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=CUSTOM_SUBJECT, school=self.school_a,
            class_name='7-А', is_active=True).exists())
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=CUSTOM_SUBJECT, is_active=True).exists())
        # Returns the deputy to the allocation context where the subject
        # is now visible.
        self.assertContains(r, 'Тақсимоти дарсҳо')
        self.assertContains(r, CUSTOM_SUBJECT)

    def test_existing_subject_selection_creates_no_duplicate(self):
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        make_student(self.school_a, '8-А', 'Хонандаи Нав')
        self._save(action='save', ack='yes', pick_name=UZBEK,
                   classes=['8-А'])
        self.assertEqual(Subject.objects.filter(name=UZBEK).count(), 1)
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_a,
            class_name='8-А', is_active=True).exists())

    def test_official_subject_via_picker(self):
        # An official curriculum subject selected from the list is applied
        # through the same availability pathway.
        self._save(action='save', ack='yes', pick_name=MATH,
                   classes=['7-А'])
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=MATH, school=self.school_a,
            class_name='7-А', is_active=True).exists())
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=MATH, is_active=True).exists())


class EditExistingTests(GoldenBase):
    """Part 9: reopening shows current config and never silently resets."""

    def test_reopen_shows_configured_classes(self):
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'pick_name': UZBEK})
        self.assertIn('7-А', r.context['configured'])

    def test_reopen_does_not_destroy_existing_config(self):
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        make_student(self.school_a, '8-А', 'Хонандаи Нав')
        # Deputy reopens and saves only 8-А checked — 7-А must survive.
        self.client.force_login(self.zavuch_a)
        self.client.post(REGISTRY_URL, {
            'action': 'save', 'ack': 'yes', 'pick_name': UZBEK,
            'classes': ['8-А']})
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_a,
            class_name='7-А', is_active=True).exists())
        self.assertTrue(ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А',
            subject=UZBEK, is_active=True).exists())
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_a,
            class_name='8-А', is_active=True).exists())

    def test_pending_request_marked_on_classes_step(self):
        subj = _seed_registry_subject(self.school_a, '7-А', UZBEK)
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=UZBEK)
        make_deactivation_request(cs, self.zavuch_a, status='pending')
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'pick_name': UZBEK})
        self.assertIn('7-А', r.context['pending_removal'])
        self.assertContains(r, 'дар интизорӣ')


class RemovalRequestTests(GoldenBase):
    """Part 10-12: deputy requests removal; admin approves/rejects."""

    def setUp(self):
        super().setUp()
        _seed_registry_subject(self.school_a, '7-А', UZBEK)
        self.cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=UZBEK)

    def test_removal_request_block_offered(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'pick_name': UZBEK})
        self.assertContains(r, 'Дархости хориҷ кардани фан')
        self.assertContains(r, 'Сабаби дархост')
        self.assertContains(r, '📨 Фиристодани дархост')

    def test_zavuch_submits_removal_request_with_reason(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(reverse('request_deactivation'), {
            'subject': UZBEK, 'classes': ['7-А'],
            'reason': 'Ин фан дар синфи мо таҳсил намешавад.'}, follow=True)
        self.assertContains(
            r, 'Дархост барои хориҷ кардани фан фиристода шуд.')
        req = SubjectDeactivationRequest.objects.get(class_subject=self.cs)
        self.assertEqual(req.status, 'pending')
        self.assertEqual(req.requested_by, self.zavuch_a)
        self.assertEqual(req.notes, 'Ин фан дар синфи мо таҳсил намешавад.')

    def test_request_not_duplicated(self):
        make_deactivation_request(self.cs, self.zavuch_a, status='pending')
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('request_deactivation'), {
            'subject': UZBEK, 'classes': ['7-А']})
        self.assertEqual(SubjectDeactivationRequest.objects.filter(
            class_subject=self.cs).count(), 1)

    def test_zavuch_cannot_request_for_other_school(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('request_deactivation'), {
            'cs_id': self.cs_b.id})
        self.assertFalse(SubjectDeactivationRequest.objects.filter(
            class_subject=self.cs_b).exists())

    def test_zavuch_cannot_directly_deactivate(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('deactivate_subject'), {'cs_id': self.cs.id})
        self.cs.refresh_from_db()
        self.assertTrue(self.cs.is_active)
        # Canonical subject untouched too.
        self.assertTrue(Subject.objects.get(name=UZBEK).is_active)

    def test_teacher_cannot_submit_removal_request(self):
        self.client.force_login(self.teacher_a)
        self.client.post(reverse('request_deactivation'), {
            'subject': UZBEK, 'classes': ['7-А']})
        self.assertFalse(SubjectDeactivationRequest.objects.exists())

    def test_admin_sees_pending_request(self):
        req = make_deactivation_request(
            self.cs, self.zavuch_a, status='pending')
        req.notes = 'Сабаби навишташуда'
        req.save()
        self.client.force_login(self.admin)
        r = self.client.get(reverse('deactivation_requests'))
        self.assertContains(r, UZBEK)
        self.assertContains(r, '7-А')
        self.assertContains(r, self.school_a.name)
        self.assertContains(r, 'Сабаби навишташуда')
        self.assertContains(r, 'Дар интизорӣ')
        self.assertContains(r, '✅ Тасдиқ кардан')
        self.assertContains(r, '❌ Рад кардан')

    def test_admin_approve_deactivates_only_requested_scope(self):
        # Same subject also configured for another class and another school.
        _seed_registry_subject(self.school_a, '8-А', UZBEK)
        make_student(self.school_a, '8-А', 'Хонандаи Нав')
        _seed_registry_subject(self.school_b, '7-Б', UZBEK)
        req = make_deactivation_request(
            self.cs, self.zavuch_a, status='pending')
        self.client.force_login(self.admin)
        self.client.post(reverse('review_deactivation_request'), {
            'request_id': req.id, 'action': 'approve'})
        self.cs.refresh_from_db()
        self.assertFalse(self.cs.is_active)
        # Matching availability becomes an explicit opt-out so
        # ensure_class_subjects() cannot re-seed it.
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_a,
            class_name='7-А', is_active=False).exists())
        self.assertNotIn(
            UZBEK, registry_subjects_for(self.school_a, '7-А'))
        # Other scopes and the canonical subject itself stay intact.
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_a,
            class_name='8-А', is_active=True).exists())
        self.assertTrue(SubjectAvailability.objects.filter(
            subject__name=UZBEK, school=self.school_b,
            class_name='7-Б', is_active=True).exists())
        self.assertTrue(Subject.objects.get(name=UZBEK).is_active)

    def test_admin_approve_preserves_history(self):
        make_grade(self.s1, UZBEK, 9)
        make_quarter_grade(self.s1, '7-А', UZBEK, quarter=1, grade=9)
        req = make_deactivation_request(
            self.cs, self.zavuch_a, status='pending')
        self.client.force_login(self.admin)
        self.client.post(reverse('review_deactivation_request'), {
            'request_id': req.id, 'action': 'approve'})
        self.assertTrue(Grade.objects.filter(
            student=self.s1, subject=UZBEK).exists())
        self.assertTrue(QuarterGrade.objects.filter(
            student=self.s1, subject=UZBEK).exists())

    def test_admin_reject_keeps_subject_active(self):
        req = make_deactivation_request(
            self.cs, self.zavuch_a, status='pending')
        self.client.force_login(self.admin)
        self.client.post(reverse('review_deactivation_request'), {
            'request_id': req.id, 'action': 'reject'})
        req.refresh_from_db()
        self.cs.refresh_from_db()
        self.assertEqual(req.status, 'rejected')
        self.assertTrue(self.cs.is_active)


class LanguageAuditTests(GoldenBase):
    """Part 13: user-facing Subject Registry strings are Tajik Cyrillic."""

    LATIN_UI_WORDS = ('>Save<', '>Delete<', '>Submit<', '>Cancel<',
                      '>Back<', '>Search<', 'placeholder="Search"')

    def test_registry_template_has_no_latin_ui_strings(self):
        import pathlib
        tpl = pathlib.Path(__file__).parents[1] / 'templates' / 'portal' / \
            'subject_registry.html'
        content = tpl.read_text(encoding='utf-8')
        for w in self.LATIN_UI_WORDS:
            self.assertNotIn(w, content)

    def test_wizard_steps_use_tajik_labels(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(REGISTRY_URL, {
            'action': 'choose_subject', 'new_name': CUSTOM_SUBJECT})
        for s in ('Синфҳоро интихоб кунед', 'Ҳамаи синфҳо', '↩ Баргаштан',
                  'Пешнамоиш', 'Муассиса'):
            self.assertContains(r, s)
        r = self.client.post(REGISTRY_URL, {
            'action': 'preview', 'new_name': CUSTOM_SUBJECT,
            'classes': ['7-А']})
        for s in ('Пешнамоиш', 'Фан:', 'Муассиса:', 'Синфҳо:',
                  '↩ Баргаштан', '✅ Тасдиқ кардан'):
            self.assertContains(r, s)
