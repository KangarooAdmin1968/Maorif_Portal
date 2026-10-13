"""Ҳуҷҷатнигорӣ → Табел — automated teacher workload tabel.

Covers: curriculum_hours weekly-hour resolution (annex + explicit
override, never the flat TJC_SUBJECT_HOURS map), teacher attribution and
TeachingGroup handling, grade-band totals, СОАТИ ТАРБИЯВӢ inclusion,
unassigned-load warnings, school isolation/permissions, month selection,
manual-entry persistence, historical-month freezing, and the Excel
export's preserved A–AB structure.
"""
import importlib
import io
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from portal.curriculum_hours import CURRICULUM_HOURS, DEFAULT_WEEKLY_HOURS
from django.db.models import ProtectedError

from portal.models import (
    ClassSubject, Teacher, TeacherProfile, TeacherTabelMonth, TeachingGroup,
)
from portal.tabel import (
    BAND_1_4, BAND_5_9, BAND_10_11, band_key_for_class,
    parse_month_param, teacher_workload,
)
from portal.tests.helpers import (
    MATH, GoldenBase, make_class_subject, make_student,
    make_teacher_profile, make_user,
)

TAJIK = 'ЗАБОНИ ТОҶИКӢ'
TARBIYAVI = 'СОАТИ ТАРБИЯВӢ'
MONTH = '2026-08'
MONTH_Y, MONTH_M = 2026, 8


class TabelWorkloadServiceTests(GoldenBase):
    """portal.tabel.teacher_workload — workload computation."""

    def setUp(self):
        super().setUp()
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _row(self, profile):
        rows, _, _ = teacher_workload(self.school_a)
        for r in rows:
            if r['teacher'].id == profile.id:
                return r
        self.fail('teacher not in workload')

    def test_annex_hours_used_not_flat_map(self):
        # ЗАБОНИ ТОҶИКӢ grade 7: annex=2, flat TJC_SUBJECT_HOURS=4.
        self.assertNotEqual(
            CURRICULUM_HOURS['7'][TAJIK], settings.TJC_SUBJECT_HOURS[TAJIK])
        other = make_user('teacher_z', role='teacher', school=self.school_a)
        profile = make_teacher_profile(other, self.school_a, 'Омӯзгор З')
        make_class_subject(self.school_a, '7-А', TAJIK, allocated=profile)
        row = self._row(profile)
        self.assertEqual(
            row['hours'][BAND_5_9], CURRICULUM_HOURS['7'][TAJIK])

    def test_explicit_hours_per_week_wins(self):
        self.cs_a.hours_per_week = 7
        self.cs_a.save()
        row = self._row(self.profile_a)
        self.assertEqual(row['hours'][BAND_5_9], 7)

    def test_band_split_and_total(self):
        make_class_subject(self.school_a, '3-Б', MATH, allocated=self.profile_a)
        make_class_subject(self.school_a, '10-А', TAJIK, allocated=self.profile_a)
        # 7-А МАТЕМАТИКА via cs_a teacher FK -> DEFAULT_WEEKLY_HOURS
        row = self._row(self.profile_a)
        self.assertEqual(row['hours'][BAND_1_4], CURRICULUM_HOURS['3'][MATH])
        self.assertEqual(
            row['hours'][BAND_5_9],
            DEFAULT_WEEKLY_HOURS)  # 7-А МАТЕМАТИКА not in annex
        self.assertEqual(row['hours'][BAND_10_11], CURRICULUM_HOURS['10'][TAJIK])
        self.assertEqual(row['total_hours'], sum(row['hours'].values()))

    def test_allocated_teacher_preferred_over_user_fk(self):
        other = make_user('teacher_x', role='teacher', school=self.school_a)
        profile_b = make_teacher_profile(other, self.school_a, 'Омӯзгор Б')
        cs = make_class_subject(
            self.school_a, '5-А', MATH, teacher=self.teacher_a,
            allocated=profile_b)
        row_b = self._row(profile_b)
        self.assertEqual(row_b['hours'][BAND_5_9], CURRICULUM_HOURS['5'][MATH])
        rows, _, _ = teacher_workload(self.school_a)
        for r in rows:
            if r['teacher'].id == self.profile_a.id:
                for item in r['items']:
                    self.assertNotIn('5-А', item['classes'])

    def test_group_teachers_get_full_hours_each(self):
        other = make_user('teacher_y', role='teacher', school=self.school_a)
        profile_b = make_teacher_profile(other, self.school_a, 'Омӯзгор Б')
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1', teacher=self.profile_a)
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 2', teacher=profile_b)
        expected = DEFAULT_WEEKLY_HOURS  # 7-А МАТЕМАТИКА
        for profile, label in ((self.profile_a, 'Гурӯҳи 1'),
                               (profile_b, 'Гурӯҳи 2')):
            row = self._row(profile)
            self.assertEqual(row['hours'][BAND_5_9], expected)
            self.assertEqual(
                row['items'],
                [{'subject': f'{MATH} — {label}', 'classes': ['7-А']}])

    def test_unassigned_group_listed_not_dropped(self):
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1', teacher=self.profile_a)
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 2', teacher=None)
        _, unassigned, _ = teacher_workload(self.school_a)
        labels = {u['subject'] for u in unassigned}
        self.assertIn(f'{MATH} — Гурӯҳи 2', labels)

    def test_unassigned_class_subject_listed(self):
        _, unassigned, _ = teacher_workload(self.school_a)
        # 7-А auto-seeded subjects have no teacher — all must be reported.
        entries = {(u['class_name'], u['subject']) for u in unassigned}
        self.assertIn(('7-А', TAJIK), entries)
        self.assertIn(('7-А', TARBIYAVI), entries)
        # The assigned cs_a must not appear.
        self.assertNotIn(('7-А', MATH), entries)

    def test_tarbiyavi_counts_in_band_total(self):
        make_class_subject(
            self.school_a, '7-А', TARBIYAVI, allocated=self.profile_a)
        row = self._row(self.profile_a)
        # 1 h/week СОАТИ ТАРБИЯВӢ + 2 h МАТЕМАТИКА default.
        self.assertEqual(row['hours'][BAND_5_9], DEFAULT_WEEKLY_HOURS + 1)

    def test_inactive_subject_ignored(self):
        self.cs_a.is_active = False
        self.cs_a.save()
        rows, _, _ = teacher_workload(self.school_a)
        for r in rows:
            self.assertNotEqual(r['teacher'].id, self.profile_a.id)

    def test_band_key_for_class(self):
        self.assertEqual(band_key_for_class('2-В'), BAND_1_4)
        self.assertEqual(band_key_for_class('9-Г'), BAND_5_9)
        self.assertEqual(band_key_for_class('11-А'), BAND_10_11)
        self.assertIsNone(band_key_for_class(''))

    def test_parse_month_param(self):
        self.assertEqual(parse_month_param('2026-8'), (2026, 8))
        self.assertEqual(parse_month_param('2026-12'), (2026, 12))
        for bad in ('', None, 'abc', '2026-13', '2026-00', '1999-05', '8-2026'):
            self.assertIsNone(parse_month_param(bad), bad)

    def test_unbanded_class_reported_not_dropped(self):
        # '0-А' maps to no 1-4/5-9/10-11 band: the load must be reported.
        make_class_subject(self.school_a, '0-А', TAJIK, allocated=self.profile_a)
        rows, _, unbanded = teacher_workload(self.school_a)
        entry = [u for u in unbanded if u['class_name'] == '0-А']
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]['teacher'], 'Омӯзгор А')
        self.assertGreater(entry[0]['hours'], 0)
        row = self._row(self.profile_a)
        all_classes = {c for it in row['items'] for c in it['classes']}
        self.assertIn('0-А', all_classes)
        self.assertEqual(row['hours'][BAND_1_4], 0)

    def test_cross_school_allocated_teacher_reported(self):
        profile_b = make_teacher_profile(
            self.teacher_b, self.school_b, 'Омӯзгор Б')
        make_class_subject(self.school_a, '8-В', TAJIK, allocated=profile_b)
        rows, unassigned, _ = teacher_workload(self.school_a)
        self.assertFalse(
            any(r['teacher'].id == profile_b.id for r in rows))
        entry = [u for u in unassigned if u['class_name'] == '8-В']
        self.assertEqual(len(entry), 1)
        self.assertIn('мактаби дигар', entry[0]['note'])

    def test_cross_school_group_teacher_reported(self):
        profile_b = make_teacher_profile(
            self.teacher_b, self.school_b, 'Омӯзгор Б')
        cs = make_class_subject(self.school_a, '9-В', TAJIK)
        TeachingGroup.objects.create(
            class_subject=cs, label='Гурӯҳи 1', teacher=profile_b)
        rows, unassigned, _ = teacher_workload(self.school_a)
        self.assertFalse(
            any(r['teacher'].id == profile_b.id for r in rows))
        entry = [u for u in unassigned if u['class_name'] == '9-В']
        self.assertEqual(len(entry), 1)
        self.assertIn('мактаби дигар', entry[0]['note'])
        self.assertIn('Гурӯҳи 1', entry[0]['subject'])

    def test_userless_profile_cannot_capture_null_teacher(self):
        # A profile with user_id=None must never match the
        # ClassSubject.teacher User-FK fallback: a NULL teacher_id would
        # otherwise resolve to the user-less profile via dict.get(None).
        from types import SimpleNamespace
        from portal.tabel import _user_to_profile_map
        real = SimpleNamespace(user_id=self.teacher_a.id)
        ghost = SimpleNamespace(user_id=None)
        m = _user_to_profile_map([ghost, real])
        self.assertIsNone(m.get(None))
        self.assertIs(m[self.teacher_a.id], real)

    def test_null_teacher_assignment_stays_unassigned(self):
        # teacher=NULL and allocated_teacher=NULL -> unassigned, never
        # attributed to any profile.
        make_class_subject(self.school_a, '6-А', TAJIK,
                           teacher=None, allocated=None)
        rows, unassigned, _ = teacher_workload(self.school_a)
        for r in rows:
            for item in r['items']:
                self.assertNotIn('6-А', item['classes'])
        entries = {(u['class_name'], u['subject']) for u in unassigned}
        self.assertIn(('6-А', TAJIK), entries)

    def test_teacher_fk_fallback_still_attributes(self):
        # cs_a (teacher=teacher_a, no allocated_teacher) must keep resolving
        # through the user -> profile fallback map onto profile_a.
        row = self._row(self.profile_a)
        self.assertIn(MATH, [i['subject'] for i in row['items']])


class TabelPageTests(GoldenBase):
    """GET /schools/documents/tabel/."""

    def setUp(self):
        super().setUp()
        self.url = reverse('workload_tabel')
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _row(self, response, profile):
        for r in response.context['rows']:
            if r['teacher'].id == profile.id:
                return r
        self.fail('teacher not in tabel rows')

    def test_zavuch_sees_workload(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        row = self._row(r, self.profile_a)
        self.assertEqual(row['hours'][BAND_5_9], DEFAULT_WEEKLY_HOURS)
        self.assertFalse(row['frozen'])
        self.assertContains(r, 'Омӯзгор А')

    def test_regular_teacher_denied(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('dashboard'), r.url)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r.url)

    def test_staff_can_view_but_not_edit(self):
        staff = make_user('staff_a', role='teacher',
                          school=self.school_a, staff=True)
        self.client.force_login(staff)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="rate_')
        self.assertNotContains(r, 'Сабт кардан')

    def test_unassigned_loads_warned(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertGreater(r.context['unassigned_count'], 0)
        self.assertContains(r, 'сарбории таъйинногарда')

    def test_month_selection(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url, {'month': '2026-08'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['month_name'], 'Август')
        self.assertEqual(r.context['month_value'], '2026-08')

    def test_invalid_month_falls_back_with_warning(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url, {'month': '13-99'})
        self.assertEqual(r.status_code, 200)
        msgs = [str(m) for m in r.context['messages']]
        self.assertTrue(any('Моҳи нодуруст' in m for m in msgs))

    def test_school_isolation(self):
        zavuch_b = make_user('zavuch_2', role='teacher', school=self.school_b)
        profile_b = make_teacher_profile(self.teacher_b, self.school_b, 'Омӯзгор Б')
        self.client.force_login(zavuch_b)
        r = self.client.get(self.url)
        rows = r.context['rows']
        self.assertEqual([x['teacher'].id for x in rows], [profile_b.id])
        for u in r.context['unassigned']:
            self.assertEqual(u['class_name'], '7-Б')
        # The save form only carries school-B teacher ids.
        self.assertNotContains(r, f'rate_{self.profile_a.id}')

    def test_superuser_can_pick_school(self):
        self.client.force_login(self.admin)
        r = self.client.get(self.url, {'school_id': self.school_a.id})
        self.assertEqual(r.status_code, 200)
        ids = [x['teacher'].id for x in r.context['rows']]
        self.assertIn(self.profile_a.id, ids)

    def test_no_school_message_for_schoolless_user(self):
        # A gate-passing user without a school sees the empty state.
        self.client.force_login(self.admin)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'ба муассиса вобаста')

    def test_unbanded_loads_warned(self):
        make_class_subject(self.school_a, '0-А', TAJIK, allocated=self.profile_a)
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertEqual(r.context['unbanded_count'], 1)
        self.assertContains(r, 'берун аз синфҳои 1–11')
        self.assertContains(r, '0-А')

    def test_empty_bands_render_blank_not_zero(self):
        # profile_a only teaches 5-9 hours; empty bands must be blank.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url)
        self.assertNotContains(r, '<td>0</td>')

    def test_documents_page_null_teacher_not_attributed(self):
        # The Ҳуҷҷатнигорӣ workload page uses the same user->profile
        # fallback map: NULL-teacher subjects must not surface under any
        # teacher there either (7-А ЗАБОНИ ТОҶИКӢ is seeded unassigned).
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('school_documents'))
        self.assertEqual(r.status_code, 200)
        for w in r.context['workload']:
            for it in w['items']:
                if it['subject'] == TAJIK:
                    self.assertNotIn('7-А', it['classes'])


class TabelSaveTests(GoldenBase):
    """POST /schools/documents/tabel/save/ — manual monthly entries."""

    def setUp(self):
        super().setUp()
        self.url = reverse('workload_tabel_save')
        self.page_url = reverse('workload_tabel')
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _post(self, data, user=None):
        self.client.force_login(user or self.zavuch_a)
        return self.client.post(self.url, data)

    def test_saves_monthly_fields_and_rate(self):
        r = self._post({
            'month': MONTH,
            f'rate_{self.profile_a.id}': '2479',
            f'missed_5_9_{self.profile_a.id}': '3',
            f'substitution_10_11_{self.profile_a.id}': '2.5',
            f'notebook_5_9_{self.profile_a.id}': '9',
            f'allowance_class_head_{self.profile_a.id}': '371.85',
        })
        self.assertEqual(r.status_code, 302)
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(row.missed_5_9, Decimal('3'))
        self.assertEqual(row.substitution_10_11, Decimal('2.5'))
        self.assertEqual(row.notebook_5_9, Decimal('9'))
        self.assertEqual(row.allowance_class_head, Decimal('371.85'))
        self.profile_a.refresh_from_db()
        self.assertEqual(self.profile_a.monthly_rate, Decimal('2479'))

    def test_saved_values_reappear_on_next_visit(self):
        self._post({
            'month': MONTH,
            f'missed_1_4_{self.profile_a.id}': '4',
        })
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        row = self._row_context(r)
        self.assertEqual(row['monthly'].missed_1_4, Decimal('4'))
        self.assertContains(r, f'name="missed_1_4_{self.profile_a.id}" value="4"')

    def _row_context(self, response):
        for r in response.context['rows']:
            if r['teacher'].id == self.profile_a.id:
                return r
        self.fail('teacher not in rows')

    def test_blank_submission_creates_no_row(self):
        self._post({'month': MONTH, f'missed_5_9_{self.profile_a.id}': ''})
        self.assertFalse(TeacherTabelMonth.objects.filter(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M).exists())

    def test_invalid_value_rejected_with_warning(self):
        r = self._post({
            'month': MONTH,
            f'missed_5_9_{self.profile_a.id}': 'abc',
        })
        self.assertEqual(r.status_code, 302)
        self.assertFalse(TeacherTabelMonth.objects.filter(
            teacher=self.profile_a).exists())
        r2 = self.client.get(r.url)
        msgs = [str(m) for m in r2.context['messages']]
        self.assertTrue(any('нодуруст' in m for m in msgs))

    def test_negative_and_overflow_rejected(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '-1'})
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '99999'})
        self.assertFalse(TeacherTabelMonth.objects.filter(
            teacher=self.profile_a).exists())

    def test_clearing_field_sets_null(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '5'})
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': ''})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertIsNone(row.missed_5_9)

    def test_teacher_cannot_save(self):
        r = self._post({
            'month': MONTH,
            f'missed_5_9_{self.profile_a.id}': '3',
        }, user=self.teacher_a)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(TeacherTabelMonth.objects.exists())

    def test_zavuch_cannot_save_other_school(self):
        profile_b = make_teacher_profile(self.teacher_b, self.school_b, 'Омӯзгор Б')
        self._post({
            'month': MONTH,
            'school_id': str(self.school_b.id),
            f'missed_5_9_{profile_b.id}': '3',
        })
        self.assertFalse(TeacherTabelMonth.objects.filter(
            teacher=profile_b).exists())

    def test_months_are_independent(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '3'})
        self._post({'month': '2026-09',
                    f'missed_5_9_{self.profile_a.id}': '7'})
        aug = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=2026, month=8)
        sep = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=2026, month=9)
        self.assertEqual(aug.missed_5_9, Decimal('3'))
        self.assertEqual(sep.missed_5_9, Decimal('7'))

    def test_missing_month_param_rejected(self):
        r = self._post({f'missed_5_9_{self.profile_a.id}': '3'})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('workload_tabel'), r.url)
        self.assertFalse(TeacherTabelMonth.objects.exists())

    def test_saved_month_freezes_workload_snapshot(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '3'})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(
            row.workload_snapshot['hours'][BAND_5_9], DEFAULT_WEEKLY_HOURS)
        # Re-allocation after the save must not rewrite the recorded month.
        self.cs_a.hours_per_week = 9
        self.cs_a.save()
        self.client.force_login(self.zavuch_a)
        r_saved = self.client.get(self.page_url, {'month': MONTH})
        ctx_row = self._row_context(r_saved)
        self.assertTrue(ctx_row['frozen'])
        self.assertEqual(ctx_row['hours'][BAND_5_9], DEFAULT_WEEKLY_HOURS)
        # A different (unsaved) month still shows the live workload.
        r_live = self.client.get(self.page_url, {'month': '2026-09'})
        ctx_live = self._row_context(r_live)
        self.assertFalse(ctx_live['frozen'])
        self.assertEqual(ctx_live['hours'][BAND_5_9], 9)

    def test_snapshot_frozen_on_repeated_save(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '3'})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        frozen_hours = dict(row.workload_snapshot['hours'])
        # Re-allocate, then save the same month again for another field:
        # the frozen snapshot must NOT refresh to the new allocation.
        self.cs_a.hours_per_week = 9
        self.cs_a.save()
        self._post({'month': MONTH,
                    f'notebook_5_9_{self.profile_a.id}': '4'})
        row.refresh_from_db()
        self.assertEqual(row.workload_snapshot['hours'], frozen_hours)
        self.assertEqual(row.notebook_5_9, Decimal('4'))

    def test_month_row_owned_by_school(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '3'})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(row.school_id, self.school_a.id)

    def test_double_save_keeps_single_row(self):
        data = {'month': MONTH, f'missed_5_9_{self.profile_a.id}': '3'}
        self._post(data)
        self._post(data)
        self.assertEqual(
            TeacherTabelMonth.objects.filter(
                teacher=self.profile_a, year=MONTH_Y, month=MONTH_M
            ).count(), 1)

    def test_teacher_transfer_preserves_history(self):
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '3'})
        # The teacher transfers to school B.
        self.profile_a.school = self.school_b
        self.profile_a.save()
        # School A still sees its own frozen record.
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        ctx_row = self._row_context(r)
        self.assertTrue(ctx_row['frozen'])
        self.assertEqual(ctx_row['monthly'].missed_5_9, Decimal('3'))
        # A school-A save cannot touch the departed teacher's row.
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '99'})
        old = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, school=self.school_a)
        self.assertEqual(old.missed_5_9, Decimal('3'))
        # School B records its own row for the same teacher + month.
        zavuch_b = make_user('zavuch_2', role='teacher', school=self.school_b)
        self._post({'month': MONTH,
                    f'missed_5_9_{self.profile_a.id}': '8'}, user=zavuch_b)
        rows = TeacherTabelMonth.objects.filter(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            rows.get(school=self.school_b).missed_5_9, Decimal('8'))

    def test_model_clean_rejects_bad_month(self):
        for bad in (0, 13):
            row = TeacherTabelMonth(
                school=self.school_a, teacher=self.profile_a,
                year=2026, month=bad)
            with self.assertRaises(ValidationError):
                row.full_clean()
        self.assertFalse(TeacherTabelMonth.objects.exists())


class TabelExportTests(GoldenBase):
    """GET /schools/documents/tabel/export/ — A–AB structure preserved."""

    def setUp(self):
        super().setUp()
        self.url = reverse('workload_tabel_export')
        self.save_url = reverse('workload_tabel_save')
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _export(self, params=None, user=None):
        self.client.force_login(user or self.zavuch_a)
        r = self.client.get(self.url, params or {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return load_workbook(io.BytesIO(r.content))

    def test_workbook_header_structure_preserved(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        self.assertIn(self.school_a.name, ws['A1'].value)
        self.assertIn('Август', ws['A1'].value)
        self.assertIn('2026', ws['A1'].value)
        self.assertIn('A1:AB1', {str(m) for m in ws.merged_cells.ranges})
        self.assertIn('B2:B4', {str(m) for m in ws.merged_cells.ranges})
        self.assertEqual(ws['B2'].value,
                         'Ному насаб      (рӯз, моҳ, соли таваллуд)')
        self.assertEqual(ws['D2'].value, 'Музди меҳнати моҳона (1 ставка)')
        self.assertEqual(ws['H2'].value, 'Миқдори соатҳои ҳафта аз рӯи фан')
        self.assertEqual(ws['AB2'].value, 'ҳамагӣ музди меҳнат дар як моҳ')
        self.assertEqual(ws['E4'].value, '1-4.')
        self.assertEqual(ws['V4'].value, '10-11.')
        self.assertIn('Роҳбари синф', ws['X4'].value)
        # Index row 5: 0..25 across A..AB.
        for col in range(1, 29):
            self.assertEqual(ws.cell(row=5, column=col).value, col - 1)

    def test_data_row_and_three_row_block(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        self.assertEqual(ws['A6'].value, 1)
        self.assertEqual(ws['B6'].value, 'Омӯзгор А')
        self.assertIn(MATH, ws['C6'].value)
        self.assertIn('A6:A8', {str(m) for m in ws.merged_cells.ranges})
        self.assertIn('B6:B8', {str(m) for m in ws.merged_cells.ranges})
        # 7-А is band 5-9 -> column F; annex misses МАТЕМАТИКА -> default 2.
        self.assertIsNone(ws['E6'].value)
        self.assertEqual(ws['F6'].value, DEFAULT_WEEKLY_HOURS)
        self.assertIsNone(ws['G6'].value)

    def test_payroll_columns_left_blank(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        for col in ('H', 'I', 'J', 'K', 'O', 'S', 'W', 'AB'):
            for row in range(6, 9):
                self.assertIsNone(
                    ws[f'{col}{row}'].value, f'{col}{row} must stay blank')
        # Column AC is never used.
        for row in range(1, ws.max_row + 1):
            self.assertIsNone(ws.cell(row=row, column=29).value)

    def test_manual_monthly_data_exported(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'rate_{self.profile_a.id}': '2479',
            f'missed_5_9_{self.profile_a.id}': '3',
            f'substitution_1_4_{self.profile_a.id}': '1',
            f'notebook_10_11_{self.profile_a.id}': '2',
            f'allowance_admin_{self.profile_a.id}': '500',
        })
        wb = self._export({'month': MONTH})
        ws = wb.active
        self.assertEqual(ws['D6'].value, 2479)
        self.assertEqual(ws['M6'].value, 3)    # missed 5-9
        self.assertEqual(ws['P6'].value, 1)    # substitution 1-4
        self.assertEqual(ws['V6'].value, 2)    # notebook 10-11
        self.assertEqual(ws['Y6'].value, 500)  # allowance admin

    def test_export_teacher_denied(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('dashboard'), r.url)

    def test_export_other_school_isolated(self):
        profile_b = make_teacher_profile(self.teacher_b, self.school_b, 'Омӯзгор Б')
        wb = self._export({'month': MONTH})
        ws = wb.active
        self.assertNotEqual(ws['B6'].value, 'Омӯзгор Б')

    def test_signature_footer_row(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        # One teacher -> single 3-row block -> signature row at row 9.
        self.assertIn('Муовини директор', ws.cell(row=9, column=2).value)
        self.assertIn('Директори муассиса', ws.cell(row=9, column=11).value)

    def test_tarbiyavi_hours_exported_not_paid(self):
        make_class_subject(
            self.school_a, '7-А', TARBIYAVI, allocated=self.profile_a)
        wb = self._export({'month': MONTH})
        ws = wb.active
        # 1 h СОАТИ ТАРБИЯВӢ + 2 h МАТЕМАТИКА -> band 5-9 column F = 3.
        self.assertEqual(ws['F6'].value, 3)
        # The class-head supplement (X) is a manual payroll item, not
        # derived from the тарбиявӣ hour -> stays blank until entered.
        self.assertIsNone(ws['X6'].value)

    def test_template_format_details_preserved(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        # The template formats every data cell H..AB as '0.00'; A..G are
        # General. Margins/paper match the reference workbook (A4, ~10mm).
        self.assertEqual(ws['H6'].number_format, '0.00')
        self.assertEqual(ws['AB6'].number_format, '0.00')
        self.assertEqual(ws['E6'].number_format, 'General')
        self.assertAlmostEqual(ws.page_margins.left, 0.3937, places=3)
        self.assertAlmostEqual(ws.page_margins.bottom, 0.3937, places=3)
        self.assertEqual(ws.page_setup.orientation, 'portrait')
        self.assertEqual(int(ws.page_setup.paperSize), 9)
        self.assertEqual(ws.row_dimensions[4].height, 64)


class TabelIntegrityTests(GoldenBase):
    """Stage 1F.2 — archive-not-delete, PROTECT, name/rate snapshots."""

    def setUp(self):
        super().setUp()
        self.save_url = reverse('workload_tabel_save')
        self.page_url = reverse('workload_tabel')
        self.export_url = reverse('workload_tabel_export')
        self.remove_url = reverse('remove_teacher', args=[self.school_a.id])
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _save_month(self, extra=None):
        data = {'month': MONTH, f'missed_5_9_{self.profile_a.id}': '3'}
        if extra:
            data.update(extra)
        self.client.force_login(self.zavuch_a)
        return self.client.post(self.save_url, data)

    def _roster_teacher(self):
        return Teacher.objects.create(
            school=self.school_a, name='Омӯзгор А')

    def test_teacher_with_history_archived_not_deleted(self):
        self._save_month({f'rate_{self.profile_a.id}': '2479'})
        roster = self._roster_teacher()
        self.client.force_login(self.zavuch_a)
        r = self.client.post(self.remove_url, {'teacher_id': roster.id})
        self.assertEqual(r.status_code, 302)
        self.profile_a.refresh_from_db()
        self.teacher_a.refresh_from_db()
        # Profile + months survive; login deactivated; roster row removed.
        self.assertTrue(self.profile_a.is_archived)
        self.assertFalse(self.teacher_a.is_active)
        self.assertEqual(TeacherTabelMonth.objects.count(), 1)
        self.assertFalse(Teacher.objects.filter(pk=roster.pk).exists())
        # Live attribution cleared on all three channels.
        self.cs_a.refresh_from_db()
        self.assertIsNone(self.cs_a.teacher_id)
        self.assertIsNone(self.cs_a.allocated_teacher_id)

    def test_teacher_without_history_hard_deleted(self):
        roster = self._roster_teacher()
        self.client.force_login(self.zavuch_a)
        self.client.post(self.remove_url, {'teacher_id': roster.id})
        # Legacy behavior: user + profile fully removed.
        self.assertFalse(
            TeacherProfile.objects.filter(pk=self.profile_a.pk).exists())
        from django.contrib.auth.models import User
        self.assertFalse(User.objects.filter(pk=self.teacher_a.pk).exists())

    def test_repeated_remove_is_safe(self):
        self._save_month()
        roster = self._roster_teacher()
        self.client.force_login(self.zavuch_a)
        self.client.post(self.remove_url, {'teacher_id': roster.id})
        # Second removal for a same-name roster entry must not touch the
        # archived profile or its months again.
        roster2 = self._roster_teacher()
        r = self.client.post(self.remove_url, {'teacher_id': roster2.id})
        self.assertEqual(r.status_code, 302)
        self.profile_a.refresh_from_db()
        self.assertTrue(self.profile_a.is_archived)
        self.assertEqual(TeacherTabelMonth.objects.count(), 1)

    def test_protect_blocks_direct_delete(self):
        self._save_month()
        with self.assertRaises(ProtectedError):
            self.teacher_a.delete()
        self.assertEqual(TeacherTabelMonth.objects.count(), 1)
        self.assertTrue(
            TeacherProfile.objects.filter(pk=self.profile_a.pk).exists())

    def test_archived_profile_gets_no_new_month_rows(self):
        self.profile_a.is_archived = True
        self.profile_a.save(update_fields=['is_archived'])
        self._save_month()
        self.assertFalse(TeacherTabelMonth.objects.exists())

    def test_archived_excluded_from_allocation_picker(self):
        self.profile_a.is_archived = True
        self.profile_a.save(update_fields=['is_archived'])
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('lesson_allocation'))
        self.assertNotIn(self.profile_a, r.context['teachers'])

    def test_archived_month_row_renders_read_only(self):
        self._save_month()
        self.profile_a.is_archived = True
        self.profile_a.save(update_fields=['is_archived'])
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        self.assertContains(r, 'Омӯзгор А')
        self.assertNotContains(
            r, f'name="missed_5_9_{self.profile_a.id}"')
        self.assertNotContains(r, f'name="rate_{self.profile_a.id}"')

    def test_name_and_rate_frozen_at_first_save(self):
        self._save_month({f'rate_{self.profile_a.id}': '2479'})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(row.teacher_name, 'Омӯзгор А')
        self.assertEqual(row.rates_snapshot, [
            {'line_no': 1, 'title': '', 'rate': '2479'}])
        # Live profile changes must not rewrite the recorded month.
        self.profile_a.full_name = 'Номунасаби Нав'
        self.profile_a.monthly_rate = Decimal('9999')
        self.profile_a.save()
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        ctx = [x for x in r.context['rows']
               if x['teacher'].id == self.profile_a.id][0]
        self.assertEqual(ctx['display_name'], 'Омӯзгор А')
        self.assertEqual(ctx['display_rate'], '2479')

    def test_snapshot_not_overwritten_by_later_save(self):
        self._save_month({f'rate_{self.profile_a.id}': '2479'})
        self.profile_a.full_name = 'Номунасаби Нав'
        self.profile_a.monthly_rate = Decimal('9999')
        self.profile_a.save()
        self._save_month({f'notebook_5_9_{self.profile_a.id}': '4'})
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile_a, year=MONTH_Y, month=MONTH_M)
        self.assertEqual(row.teacher_name, 'Омӯзгор А')
        self.assertEqual(row.rates_snapshot[0]['rate'], '2479')
        self.assertEqual(row.notebook_5_9, Decimal('4'))

    def test_export_matches_page_snapshot(self):
        self._save_month({f'rate_{self.profile_a.id}': '2479'})
        self.profile_a.full_name = 'Номунасаби Нав'
        self.profile_a.monthly_rate = Decimal('9999')
        self.profile_a.save()
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.export_url, {'month': MONTH})
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws['B6'].value, 'Омӯзгор А')
        self.assertEqual(ws['D6'].value, 2479)

    def test_legacy_row_without_snapshot_falls_back_to_live(self):
        # Rows saved before the snapshot fields existed keep their old
        # behavior: live name and rate, never blank, never invented.
        self.profile_a.monthly_rate = Decimal('1500')
        self.profile_a.save()
        TeacherTabelMonth.objects.create(
            school=self.school_a, teacher=self.profile_a,
            year=MONTH_Y, month=MONTH_M, missed_5_9=Decimal('1'))
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        ctx = [x for x in r.context['rows']
               if x['teacher'].id == self.profile_a.id][0]
        self.assertEqual(ctx['display_name'], 'Омӯзгор А')
        self.assertEqual(ctx['display_rate'], Decimal('1500'))

    def test_transfer_then_archive_keeps_history(self):
        self._save_month()
        self.profile_a.school = self.school_b
        self.profile_a.is_archived = True
        self.profile_a.save()
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url, {'month': MONTH})
        ctx = [x for x in r.context['rows']
               if x['teacher'].id == self.profile_a.id][0]
        self.assertTrue(ctx['frozen'])
        self.assertEqual(ctx['monthly'].school_id, self.school_a.id)

    def test_tabel_page_query_count(self):
        # Snapshot fields ride the existing queries — the count must stay
        # flat regardless of how many teachers/months exist.
        self._save_month()
        self.client.force_login(self.zavuch_a)
        with self.assertNumQueries(11):
            self.client.get(self.page_url, {'month': MONTH})

    def test_readiness_excludes_archived_and_null_key_safe(self):
        # The readiness map must not attribute a NULL/archived-teacher
        # ClassSubject to a user-less or archived profile.
        self.profile_a.is_archived = True
        self.profile_a.save(update_fields=['is_archived'])
        self.client.force_login(self.zavuch_a)
        r = self.client.get(reverse('school_readiness_rating'))
        self.assertEqual(r.status_code, 200)
        for item in r.context['activity']:
            for t in item['teachers']:
                self.assertNotEqual(t['name'], 'Омӯзгор А')

    def test_remove_race_falls_back_to_archive(self):
        # A month row created between the exists() check and user.delete()
        # must trigger the archive path, not an HTTP 500. Simulate the race
        # by making the history check lie while a real month row exists.
        TeacherTabelMonth.objects.create(
            school=self.school_a, teacher=self.profile_a,
            year=MONTH_Y, month=MONTH_M)
        roster = self._roster_teacher()
        self.client.force_login(self.zavuch_a)
        related_mgr = type(self.profile_a.tabel_months)
        with mock.patch.object(related_mgr, 'exists', return_value=False):
            r = self.client.post(self.remove_url, {'teacher_id': roster.id})
        self.assertEqual(r.status_code, 302)
        self.profile_a.refresh_from_db()
        self.teacher_a.refresh_from_db()
        self.assertTrue(self.profile_a.is_archived)
        self.assertFalse(self.teacher_a.is_active)
        self.assertEqual(TeacherTabelMonth.objects.count(), 1)
        self.assertFalse(Teacher.objects.filter(pk=roster.pk).exists())

    def test_migration_0024_backfill_fills_name_only(self):
        mod = importlib.import_module(
            'portal.migrations.0024_backfill_tabel_teacher_name')
        legacy = TeacherTabelMonth.objects.create(
            school=self.school_a, teacher=self.profile_a,
            year=MONTH_Y, month=8)
        preset = TeacherTabelMonth.objects.create(
            school=self.school_a, teacher=self.profile_a,
            year=MONTH_Y, month=7, teacher_name='Таҳририй',
            rates_snapshot=[{'line_no': 1, 'title': '', 'rate': '100.00'}])
        mod.backfill_teacher_name(apps, None)
        legacy.refresh_from_db()
        preset.refresh_from_db()
        # Missing name filled from the live profile; rate never invented.
        self.assertEqual(legacy.teacher_name, self.profile_a.full_name)
        self.assertIsNone(legacy.rates_snapshot)
        # Rows that already carry a snapshot are left untouched.
        self.assertEqual(preset.teacher_name, 'Таҳририй')
        self.assertEqual(preset.rates_snapshot[0]['rate'], '100.00')
