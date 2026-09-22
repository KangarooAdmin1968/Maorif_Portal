"""Regression tests for the "Тақсимоти дарсҳо" save-payload fix.

Background: the allocation page used to submit every row of every class
(2 fields per ClassSubject plus fields leaked from nested forms), which
exceeded settings.DATA_UPLOAD_MAX_NUMBER_FIELDS (default 1000) on large
schools and produced django.core.exceptions.TooManyFieldsSent -> HTTP 400.

Two changes under test:

1. The page now posts only dirty controls (JS disables untouched inputs
   at submit time). The server must therefore handle sparse POSTs: only
   submitted alloc_*/hours_* keys are applied, missing rows untouched.
2. The invalid nested <form> elements inside #alloc-form were removed;
   deactivation is driven by plain buttons, so no row button can ever
   submit the allocation form (previously the first row's button could).
"""
import re

from django.test import TestCase
from django.urls import reverse

from portal.curriculum_hours import weekly_hours
from portal.models import ClassSubject, SubjectDeactivationRequest
from portal.tests.helpers import (
    MATH, GoldenBase, make_class_subject, make_teacher_profile,
)


def _hours_input_classes(content, cs_id):
    """Return the CSS class attribute of the hours input for a ClassSubject."""
    m = re.search(
        rf'name="hours_{cs_id}"\s+class="([^"]*)"',
        content,
    )
    return m.group(1) if m else None


def _alloc_form_body(content):
    """Return the markup between the alloc-form tag and its </form>."""
    start = content.index('id="alloc-form"')
    end = content.index('</form>', start)
    return content[start:end]


class SparseAllocationSaveTests(GoldenBase):
    """POST /schools/allocation/save/ with only changed fields."""

    def setUp(self):
        super().setUp()
        self.url = reverse('save_lesson_allocation')
        self.profile = make_teacher_profile(self.teacher_a, self.school_a)
        self.other = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject='АЛГЕБРА')

    def _post(self, data, user=None):
        self.client.force_login(user or self.zavuch_a)
        return self.client.post(self.url, data)

    def test_normal_allocation_save_still_works(self):
        r = self._post({f'alloc_{self.cs_a.id}': str(self.profile.id)})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.allocated_teacher_id, self.profile.id)
        self.assertEqual(self.cs_a.teacher_id, self.teacher_a.id)

    def test_only_submitted_rows_are_processed(self):
        # Give the untouched row state that must survive a sparse POST.
        self.other.allocated_teacher = self.profile
        self.other.teacher = self.teacher_a
        self.other.hours_per_week = 5
        self.other.save()
        r = self._post({f'hours_{self.cs_a.id}': '1'})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 1)
        self.assertEqual(self.other.hours_per_week, 5)
        self.assertEqual(self.other.allocated_teacher_id, self.profile.id)
        self.assertEqual(self.other.teacher_id, self.teacher_a.id)

    def test_changing_teacher(self):
        r = self._post({f'alloc_{self.cs_a.id}': str(self.profile.id)})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.allocated_teacher_id, self.profile.id)

    def test_changing_hours(self):
        r = self._post({f'hours_{self.cs_a.id}': '4'})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 4)

    def test_changing_both(self):
        r = self._post({
            f'alloc_{self.cs_a.id}': str(self.profile.id),
            f'hours_{self.cs_a.id}': '3',
        })
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.allocated_teacher_id, self.profile.id)
        self.assertEqual(self.cs_a.hours_per_week, 3)

    def test_clearing_teacher(self):
        self.cs_a.allocated_teacher = self.profile
        self.cs_a.save()
        r = self._post({f'alloc_{self.cs_a.id}': ''})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.allocated_teacher_id)
        self.assertIsNone(self.cs_a.teacher_id)

    def test_clearing_hours(self):
        self.cs_a.hours_per_week = 4
        self.cs_a.save()
        r = self._post({f'hours_{self.cs_a.id}': ''})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week)

    def test_noop_save_when_nothing_changed(self):
        self.cs_a.allocated_teacher = self.profile
        self.cs_a.hours_per_week = 3
        self.cs_a.save()
        r = self._post({})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.allocated_teacher_id, self.profile.id)
        self.assertEqual(self.cs_a.hours_per_week, 3)


class AllocationMarkupTests(GoldenBase):
    """The rendered page must not nest forms inside #alloc-form."""

    def _page(self, user):
        self.client.force_login(user)
        r = self.client.get(reverse('lesson_allocation'))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_no_nested_forms_inside_alloc_form_zavuch(self):
        body = _alloc_form_body(self._page(self.zavuch_a))
        self.assertNotIn('<form', body)

    def test_no_nested_forms_inside_alloc_form_superuser(self):
        body = _alloc_form_body(self._page(self.admin))
        self.assertNotIn('<form', body)

    def test_zavuch_rows_use_plain_deactivation_buttons(self):
        content = self._page(self.zavuch_a)
        body = _alloc_form_body(content)
        n_rows = len(re.findall(r'class="alloc-row"', body))
        buttons = re.findall(
            r'<button type="button" class="btn btn-warning btn-sm '
            r'deactivation-btn" data-cs-id="(\d+)" '
            r'data-url="/schools/allocation/request-deactivation/"',
            body)
        # One plain button per row, including the first row.
        self.assertEqual(len(buttons), n_rows)
        self.assertIn(str(self.cs_a.id), buttons)

    def test_superuser_rows_use_plain_deactivate_buttons(self):
        content = self._page(self.admin)
        body = _alloc_form_body(content)
        n_rows = len(re.findall(r'class="alloc-row"', body))
        buttons = re.findall(
            r'<button type="button" class="btn btn-danger btn-sm '
            r'deactivate-btn" data-cs-id="(\d+)"', body)
        self.assertEqual(len(buttons), n_rows)
        # The standalone POST form lives outside the allocation form.
        self.assertIn('id="deactivate-form"', content)
        self.assertNotIn('id="deactivate-form"', body)

    def test_pending_request_shows_badge_not_button(self):
        make_deactivation = SubjectDeactivationRequest.objects.create(
            class_subject=self.cs_a, requested_by=self.zavuch_a,
            status='pending')
        content = self._page(self.zavuch_a)
        body = _alloc_form_body(content)
        n_rows = len(re.findall(r'class="alloc-row"', body))
        n_buttons = body.count('deactivation-btn')
        self.assertEqual(n_buttons, n_rows - 1)
        self.assertIn('Дархост ирсол шуд', body)
        self.assertTrue(make_deactivation.id)

    def test_dirty_submit_wiring_present(self):
        content = self._page(self.zavuch_a)
        self.assertIn('dirtyControls', content)
        self.assertIn("querySelectorAll('.hours-input, .alloc-select')", content)
        # Hours of the class currently on screen are always submitted so a
        # displayed recommendation is accepted as explicit on save.
        self.assertIn('row.dataset.class === currentClass', content)

    def test_large_page_renders_and_sparse_post_still_saves(self):
        # ~520 ClassSubject rows: the old page posted >1000 fields and hit
        # TooManyFieldsSent. With dirty-only submission a small POST is all
        # the endpoint ever needs — and it must still process it correctly.
        rows = [
            ClassSubject(school=self.school_a, class_name=f'{g}-А',
                         subject=f'ФАН {s:02d}')
            for g in range(1, 41) for s in range(13)
        ]
        ClassSubject.objects.bulk_create(rows)
        self.assertGreater(
            ClassSubject.objects.filter(
                school=self.school_a, is_active=True).count(), 500)
        content = self._page(self.zavuch_a)
        self.assertIn('id="alloc-form"', content)
        self.assertNotIn('<form', _alloc_form_body(content))
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        self.client.force_login(self.zavuch_a)
        r = self.client.post(reverse('save_lesson_allocation'), {
            f'alloc_{self.cs_a.id}': str(profile.id),
            f'hours_{self.cs_a.id}': '2',
        })
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.allocated_teacher_id, profile.id)
        self.assertEqual(self.cs_a.hours_per_week, 2)


class ActiveClassRecommendationTests(GoldenBase):
    """Saving with a class on screen accepts its displayed hour
    recommendations as explicit values — without re-submitting the rows
    of every other (hidden) class."""

    def setUp(self):
        super().setUp()
        self.save_url = reverse('save_lesson_allocation')
        self.page_url = reverse('lesson_allocation')

    def _post(self, data):
        self.client.force_login(self.zavuch_a)
        return self.client.post(self.save_url, data)

    def test_unchanged_displayed_recommendation_becomes_explicit(self):
        # cs_a has no explicit value; its muted recommendation is what the
        # page submits for the active class when the user presses Save.
        self.assertIsNone(self.cs_a.hours_per_week)
        recommended = weekly_hours('7-А', MATH, self.cs_a)
        r = self._post({f'hours_{self.cs_a.id}': str(recommended)})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, recommended)

    def test_accepted_recommendation_renders_confirmed_after_reload(self):
        recommended = weekly_hours('7-А', MATH, self.cs_a)
        self._post({f'hours_{self.cs_a.id}': str(recommended)})
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url)
        classes = _hours_input_classes(r.content.decode(), self.cs_a.id)
        self.assertIsNotNone(classes)
        self.assertNotIn('hours-suggested', classes)

    def test_only_active_class_recommendations_are_accepted(self):
        # A second class' rows stay suggested/NULL when they were not on
        # screen — the sparse POST carries only the active class' hours.
        other_class = make_class_subject(
            self.school_a, '5-В', MATH, teacher=self.teacher_a)
        recommended = weekly_hours('7-А', MATH, self.cs_a)
        r = self._post({f'hours_{self.cs_a.id}': str(recommended)})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        other_class.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, recommended)
        self.assertIsNone(other_class.hours_per_week)

    def test_hidden_class_recommendation_still_muted_after_save(self):
        other_class = make_class_subject(
            self.school_a, '5-В', MATH, teacher=self.teacher_a)
        recommended = weekly_hours('7-А', MATH, self.cs_a)
        self._post({f'hours_{self.cs_a.id}': str(recommended)})
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url)
        classes = _hours_input_classes(r.content.decode(), other_class.id)
        self.assertIn('hours-suggested', classes)

    def test_explicit_hours_change_in_active_class(self):
        r = self._post({f'hours_{self.cs_a.id}': '5'})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 5)

    def test_clearing_active_class_hours_restores_null(self):
        self.cs_a.hours_per_week = 4
        self.cs_a.save()
        r = self._post({f'hours_{self.cs_a.id}': ''})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week)


class DeactivationEndpointTests(GoldenBase):
    """Deactivation still works after the nested forms became buttons."""

    def test_zavuch_ajax_request_deactivation(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.post(
            reverse('request_deactivation'), {'cs_id': self.cs_a.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'ok')
        self.assertTrue(SubjectDeactivationRequest.objects.filter(
            class_subject=self.cs_a, requested_by=self.zavuch_a,
            status='pending').exists())

    def test_superuser_direct_deactivation(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse('deactivate_subject'), {'cs_id': self.cs_a.id})
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertFalse(self.cs_a.is_active)


class NoNestedFormInDbPage(TestCase):
    """Guard: 'deactivation-form' markup class is gone entirely."""

    def test_template_has_no_deactivation_form_class(self):
        import pathlib
        tpl = pathlib.Path(
            __file__).parents[1] / 'templates' / 'school' / 'lesson_allocation.html'
        content = tpl.read_text(encoding='utf-8')
        self.assertNotIn('deactivation-form', content)
