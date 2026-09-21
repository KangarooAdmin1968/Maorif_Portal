"""Weekly lesson hours ("Ҳафтада соат") on the lesson-allocation page.

Covers the deputy-principal flow: display of recommended/saved values,
quick-button and manual values, batch save, server-side validation,
permission and school isolation, persistence, and the single storage
field (ClassSubject.hours_per_week — consumed by curriculum_hours).
"""
import re

from django.test import TestCase
from django.urls import reverse

from portal.curriculum_hours import (
    CURRICULUM_HOURS, DEFAULT_WEEKLY_HOURS, MAX_WEEKLY_HOURS,
    quarter_min_norm, weekly_hours,
)
from portal.models import ClassSubject
from portal.tests.helpers import (
    MATH, GoldenBase, make_class_subject, make_student,
)


def _hours_input_classes(content, cs_id):
    """Return the CSS class attribute of the hours input for a ClassSubject."""
    m = re.search(
        rf'name="hours_{cs_id}"\s+class="([^"]*)"',
        content,
    )
    return m.group(1) if m else None


class WeeklyHoursDisplayTests(GoldenBase):
    """GET /schools/allocation/ — rendering of the Ҳафтада соат column."""

    def setUp(self):
        super().setUp()
        self.url = reverse('lesson_allocation')

    def test_page_shows_recommended_hours_muted(self):
        # cs_a: МАТЕМАТИКА in grade 7 — not in the annex table for grade 7,
        # so the fallback recommendation (2) is shown in muted style.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        classes = _hours_input_classes(content, self.cs_a.id)
        self.assertIsNotNone(classes)
        self.assertIn('hours-suggested', classes)
        self.assertContains(r, f'name="hours_{self.cs_a.id}"')
        self.assertContains(r, f'value="{DEFAULT_WEEKLY_HOURS}"')

    def test_page_shows_annex_value_for_official_subject(self):
        # Grade-5 МАТЕМАТИКА is in the annex table with 5 h/week.
        make_student(self.school_a, '5-В', 'Ҷасур Юлдошев')
        cs5 = ClassSubject.objects.get(
            school=self.school_a, class_name='5-В', subject=MATH)
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        content = r.content.decode()
        classes = _hours_input_classes(content, cs5.id)
        self.assertIn('hours-suggested', classes)
        m = re.search(rf'name="hours_{cs5.id}"\s+class="[^"]*"\s+value="(\d+)"', content)
        self.assertEqual(m.group(1), str(CURRICULUM_HOURS['5'][MATH]))

    def test_quick_buttons_rendered(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        for n in (1, 2, 3):
            self.assertContains(r, f'class="hours-quick" data-hours="{n}"')

    def test_saved_value_renders_confirmed_not_muted(self):
        self.cs_a.hours_per_week = 3
        self.cs_a.save()
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        classes = _hours_input_classes(r.content.decode(), self.cs_a.id)
        self.assertIsNotNone(classes)
        self.assertNotIn('hours-suggested', classes)

    def test_unsaved_changes_protection_wired(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertContains(r, 'id="unsaved-modal"')
        self.assertContains(r, 'beforeunload')
        self.assertContains(r, 'unsaved-leave')
        self.assertContains(r, 'unsaved-save')

    def test_teacher_cannot_open_allocation_page(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('dashboard'), r.url)


class WeeklyHoursSaveTests(GoldenBase):
    """POST /schools/allocation/save/ — batch save of hours_<id> values."""

    def setUp(self):
        super().setUp()
        self.save_url = reverse('save_lesson_allocation')

    def _post_hours(self, cs, value, user=None, extra=None):
        self.client.force_login(user or self.zavuch_a)
        data = {f'hours_{cs.id}': str(value)}
        if extra:
            data.update(extra)
        return self.client.post(self.save_url, data)

    def test_quick_value_1_saves(self):
        self._post_hours(self.cs_a, 1)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 1)

    def test_quick_value_2_saves(self):
        self._post_hours(self.cs_a, 2)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 2)

    def test_quick_value_3_saves(self):
        self._post_hours(self.cs_a, 3)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 3)

    def test_manual_value_4_and_5_save(self):
        for value in (4, 5):
            self._post_hours(self.cs_a, value)
            self.cs_a.refresh_from_db()
            self.assertEqual(self.cs_a.hours_per_week, value)

    def test_change_existing_value(self):
        self.cs_a.hours_per_week = 2
        self.cs_a.save()
        self._post_hours(self.cs_a, 3)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 3)

    def test_clearing_input_restores_recommendation(self):
        self.cs_a.hours_per_week = 4
        self.cs_a.save()
        self._post_hours(self.cs_a, '')
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week)

    def test_batch_save_multiple_subjects(self):
        other = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject='АЛГЕБРА')
        self.client.force_login(self.zavuch_a)
        r = self.client.post(self.save_url, {
            f'hours_{self.cs_a.id}': '1',
            f'hours_{other.id}': '3',
        })
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 1)
        self.assertEqual(other.hours_per_week, 3)

    def test_batch_save_combines_hours_and_teacher_allocation(self):
        from portal.tests.helpers import make_teacher_profile
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        self.client.force_login(self.zavuch_a)
        r = self.client.post(self.save_url, {
            f'hours_{self.cs_a.id}': '2',
            f'alloc_{self.cs_a.id}': str(profile.id),
        })
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 2)
        self.assertEqual(self.cs_a.allocated_teacher_id, profile.id)

    # --- accepting an unchanged recommendation -----------------------------

    def test_accepting_unchanged_recommendation_persists_it(self):
        # cs_a starts with no explicit value; its muted recommendation (2) is
        # submitted unchanged on Save and must become an explicit saved value.
        self.assertIsNone(self.cs_a.hours_per_week)
        recommended = weekly_hours('7-А', MATH, self.cs_a)
        self._post_hours(self.cs_a, recommended)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, recommended)
        # After save the value renders confirmed (dark), not muted.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('lesson_allocation'))
        classes = _hours_input_classes(r.content.decode(), self.cs_a.id)
        self.assertNotIn('hours-suggested', classes)

    def test_save_persists_every_displayed_value(self):
        # Simulates the real page submit: every rendered hours input is posted,
        # so each accepted recommendation becomes an explicit saved value.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('lesson_allocation'))
        pairs = re.findall(
            r'name="hours_(\d+)"\s+class="[^"]*"\s+value="(\d+)"',
            r.content.decode())
        self.assertGreater(len(pairs), 1)
        r = self.client.post(
            self.save_url, {f'hours_{k}': v for k, v in pairs})
        self.assertEqual(r.status_code, 302)
        for cs_id, value in pairs:
            self.assertEqual(
                ClassSubject.objects.get(id=cs_id).hours_per_week, int(value))

    def test_hours_save_does_not_touch_teacher_fields(self):
        # Posting only hours must not alter the teacher assignment fields.
        self.assertEqual(self.cs_a.teacher_id, self.teacher_a.id)
        self._post_hours(self.cs_a, 3)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 3)
        self.assertEqual(self.cs_a.teacher_id, self.teacher_a.id)
        self.assertIsNone(self.cs_a.allocated_teacher_id)

    # --- server-side validation ------------------------------------------

    def _assert_rejected(self, raw):
        self._post_hours(self.cs_a, raw)
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week, raw)

    def test_rejects_non_numeric(self):
        self._assert_rejected('abc')

    def test_rejects_zero(self):
        self._assert_rejected('0')

    def test_rejects_negative(self):
        self._assert_rejected('-2')

    def test_rejects_decimal(self):
        self._assert_rejected('2.5')

    def test_rejects_above_domain_cap(self):
        self._assert_rejected(str(MAX_WEEKLY_HOURS + 1))

    def test_accepts_domain_cap(self):
        self._post_hours(self.cs_a, MAX_WEEKLY_HOURS)
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, MAX_WEEKLY_HOURS)

    def test_invalid_row_does_not_block_valid_rows(self):
        other = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject='АЛГЕБРА')
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            f'hours_{self.cs_a.id}': 'xyz',
            f'hours_{other.id}': '2',
        })
        self.cs_a.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week)
        self.assertEqual(other.hours_per_week, 2)

    # --- permissions & isolation ------------------------------------------

    def test_teacher_cannot_save_hours(self):
        r = self._post_hours(self.cs_a, 3, user=self.teacher_a)
        self.assertEqual(r.status_code, 302)
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.hours_per_week)

    def test_zavuch_cannot_set_other_school_hours(self):
        self._post_hours(self.cs_b, 5)
        self.cs_b.refresh_from_db()
        self.assertIsNone(self.cs_b.hours_per_week)

    def test_superuser_can_save_with_school_id(self):
        self.client.force_login(self.admin)
        r = self.client.post(self.save_url, {
            'school_id': str(self.school_b.id),
            f'hours_{self.cs_b.id}': '4',
        })
        self.assertEqual(r.status_code, 302)
        self.cs_b.refresh_from_db()
        self.assertEqual(self.cs_b.hours_per_week, 4)

    # --- persistence / source of truth ------------------------------------

    def test_saved_value_persists_and_is_authoritative(self):
        self._post_hours(self.cs_a, 3)
        self.cs_a.refresh_from_db()
        # The saved override wins over the annex table everywhere the
        # canonical resolver is used (norms, readiness hours).
        self.assertEqual(
            weekly_hours(self.cs_a.class_name, self.cs_a.subject, self.cs_a), 3)
        self.assertEqual(
            quarter_min_norm(self.cs_a.class_name, self.cs_a.subject, self.cs_a),
            7)  # 3 h/week band
        # And survives a fresh page load as a confirmed (non-muted) value.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('lesson_allocation'))
        classes = _hours_input_classes(r.content.decode(), self.cs_a.id)
        self.assertNotIn('hours-suggested', classes)
        m = re.search(
            rf'name="hours_{self.cs_a.id}"\s+class="[^"]*"\s+value="(\d+)"',
            r.content.decode())
        self.assertEqual(m.group(1), '3')

    def test_page_load_does_not_overwrite_saved_value(self):
        self.cs_a.hours_per_week = 4
        self.cs_a.save()
        self.client.force_login(self.zavuch_a)
        self.client.get(reverse('lesson_allocation'))
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.hours_per_week, 4)


class WeeklyHoursStorageTests(TestCase):
    """No duplicate storage: exactly one hours field on ClassSubject."""

    def test_single_hours_field(self):
        fields = [
            f.name for f in ClassSubject._meta.get_fields()
            if 'hour' in f.name.lower()
        ]
        self.assertEqual(fields, ['hours_per_week'])

    def test_resolver_falls_back_to_annex_when_unset(self):
        # Without an explicit value the annex/default recommendation applies.
        cs = ClassSubject(class_name='7-А', subject='ФИЗИКА')
        self.assertEqual(
            weekly_hours('7-А', 'ФИЗИКА', cs), CURRICULUM_HOURS['7']['ФИЗИКА'])
        cs = ClassSubject(class_name='7-А', subject='НОМАЪЛУМ')
        self.assertEqual(
            weekly_hours('7-А', 'НОМАЪЛУМ', cs), DEFAULT_WEEKLY_HOURS)
