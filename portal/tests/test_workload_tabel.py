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
import re
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

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
    MATH, GoldenBase, make_class_subject, make_school, make_student,
    make_teacher_profile, make_user,
)

TAJIK = 'ЗАБОНИ ТОҶИКӢ'
TARBIYAVI = 'СОАТИ ТАРБИЯВӢ'
MONTH = '2026-08'
MONTH_Y, MONTH_M = 2026, 8

# --- Formula evaluator ----------------------------------------------------
# openpyxl stores formulas without calculated results; this evaluator
# verifies their arithmetic semantics without a spreadsheet engine. It
# supports exactly the grammar the tabel emits: cell refs, numbers, the
# postfix % operator, + - * /, parentheses, SUM(range) and the
# IF(ref="","",expr) guard — evaluated lazily like Excel, so a guarded
# branch never computes on blank operands.

_FORMULA_TOKEN = re.compile(
    r'\s*(IF\(|SUM\(|\$?[A-Z]{1,2}\d+|""|\d+(?:\.\d+)?|[+\-*/%(),=:])')


class TabelFormulaEvaluator:
    """Evaluate exported tabel formulas against worksheet cells.

    ``value(coord)`` returns a Decimal for numeric results, '' for the
    IF-guard blank, or the raw cell value for literal cells.
    """

    def __init__(self, ws):
        self._ws = ws
        self._cache = {}

    def value(self, coord):
        if coord not in self._cache:
            raw = self._ws[coord].value
            self._cache[coord] = (
                self._eval_text(raw[1:])
                if isinstance(raw, str) and raw.startswith('=') else raw)
        return self._cache[coord]

    def _num(self, value):
        if value in (None, ''):
            return Decimal(0)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _eval_text(self, text):
        # Save/restore so evaluating a referenced cell mid-parse (e.g. AB6
        # pulling in K6) does not corrupt the outer token stream.
        outer = getattr(self, '_tokens', None), getattr(self, '_pos', 0)
        self._tokens = _FORMULA_TOKEN.findall(text)
        self._pos = 0
        result = self._expr()
        assert self._pos == len(self._tokens), 'unparsed formula tail'
        self._tokens, self._pos = outer
        return result

    def _eval_tokens(self, tokens):
        outer_tokens, outer_pos = self._tokens, self._pos
        self._tokens, self._pos = tokens, 0
        result = self._expr()
        assert self._pos == len(tokens), 'unparsed formula tail'
        self._tokens, self._pos = outer_tokens, outer_pos
        return result

    def _peek(self, offset=0):
        index = self._pos + offset
        return self._tokens[index] if index < len(self._tokens) else None

    def _pop(self):
        token = self._peek()
        self._pos += 1
        return token

    def _expr(self):
        if self._peek() == 'IF(':
            return self._if()
        if self._peek() == '""':
            self._pop()
            return ''
        if (re.fullmatch(r'\$?[A-Z]{1,2}\d+', self._peek() or '')
                and self._peek(1) == '='):
            ref = self._pop()
            self._pop()  # '='
            lit = self._pop()
            raw = self.value(ref.lstrip('$'))
            if lit == '""':
                return raw in (None, '')
            return self._num(raw) == Decimal(lit)
        return self._additive()

    def _if(self):
        self._pop()  # 'IF('
        # Split the three arguments on top-level commas and evaluate only
        # the taken branch — Excel-style lazy evaluation.
        parts, depth = [[]], 0
        while True:
            token = self._pop()
            assert token is not None, 'unbalanced IF('
            if token in ('IF(', 'SUM(', '('):
                depth += 1
            elif token == ')':
                if depth == 0:
                    break
                depth -= 1
            if token == ',' and depth == 0:
                parts.append([])
            else:
                parts[-1].append(token)
        assert len(parts) == 3, 'IF() needs 3 arguments'
        branch = parts[1] if self._eval_tokens(parts[0]) else parts[2]
        return self._eval_tokens(branch)

    def _additive(self):
        value = self._term()
        while self._peek() in ('+', '-'):
            op = self._pop()
            rhs = self._term()
            value = value + rhs if op == '+' else value - rhs
        return value

    def _term(self):
        value = self._postfix()
        while self._peek() in ('*', '/'):
            op = self._pop()
            rhs = self._postfix()
            value = value * rhs if op == '*' else value / rhs
        return value

    def _postfix(self):
        value = self._num(self._atom())
        while self._peek() == '%':
            self._pop()
            value /= Decimal(100)
        return value

    def _atom(self):
        token = self._pop()
        if token == '(':
            value = self._expr()
            assert self._pop() == ')'
            return value
        if token == 'SUM(':
            cells = self._range()
            return sum(
                (self._num(self.value(c)) for c in cells), Decimal(0))
        if re.fullmatch(r'\$?[A-Z]{1,2}\d+', token):
            return self.value(token.lstrip('$'))
        return Decimal(token)

    def _range(self):
        m_start = re.fullmatch(r'([A-Z]+)(\d+)', self._pop())
        assert self._pop() == ':'
        m_end = re.fullmatch(r'([A-Z]+)(\d+)', self._pop())
        assert self._pop() == ')'
        cols = range(column_index_from_string(m_start.group(1)),
                     column_index_from_string(m_end.group(1)) + 1)
        rows = range(int(m_start.group(2)), int(m_end.group(2)) + 1)
        return [f'{get_column_letter(c)}{r}' for c in cols for r in rows]


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

    def test_payroll_columns_have_guarded_formulas(self):
        wb = self._export({'month': MONTH})
        ws = wb.active
        for col in ('H', 'I', 'J', 'K', 'O', 'S', 'W', 'AB'):
            for row in range(6, 9):
                self.assertTrue(
                    str(ws[f'{col}{row}'].value).startswith(
                        f'=IF(D{row}='),
                    f'{col}{row} must carry the guarded payroll formula')
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
        # flat regardless of how many teachers/months exist. School A has
        # no verified-tariff data, so the hint path adds no query here.
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


class TabelPayrollFormulaTests(GoldenBase):
    """Stage 2B.1 — payroll formulas H–K, O, S, W, AB in the export.

    Formula strings are asserted verbatim and their arithmetic is checked
    with TabelFormulaEvaluator; openpyxl itself computes nothing.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse('workload_tabel_export')
        self.save_url = reverse('workload_tabel_save')
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')

    def _export(self, params=None):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.url, params or {'month': MONTH})
        self.assertEqual(r.status_code, 200)
        return load_workbook(io.BytesIO(r.content))

    def _sheet(self, params=None):
        ws = self._export(params).active
        return ws, TabelFormulaEvaluator(ws)

    def test_formula_strings_all_three_block_rows(self):
        ws, _ = self._sheet()
        bodies = {
            'H': 'D{r}/20*E{r}',
            'I': 'D{r}/18*F{r}',
            'J': 'D{r}/18*G{r}',
            'K': 'SUM(H{r}:J{r})',
            'O': '(D{r}/80*L{r})+(D{r}/72*M{r})+(D{r}/72*N{r})',
            'S': '(D{r}/80*P{r})+(D{r}/72*Q{r})+(D{r}/72*R{r})',
            'W': '(D{r}/18*20%*U{r})+(D{r}/18*20%*V{r})+(D{r}/20*20%*T{r})',
            'AB': 'K{r}-O{r}+S{r}+W{r}+X{r}+Y{r}+Z{r}+AA{r}',
        }
        for r in (6, 7, 8):  # all three rows of the teacher block
            for col, body in bodies.items():
                self.assertEqual(
                    ws[f'{col}{r}'].value,
                    f'=IF(D{r}="","",{body.format(r=r)})',
                    f'{col}{r}')

    def test_second_teacher_formulas_reference_own_row(self):
        other = make_user('teacher_z', role='teacher', school=self.school_a)
        profile_z = make_teacher_profile(other, self.school_a, 'Омӯзгор З')
        make_class_subject(self.school_a, '9-В', MATH, allocated=profile_z)
        ws, _ = self._sheet()
        # 'Омӯзгор А' sorts before 'Омӯзгор З' -> blocks at rows 6 and 9.
        self.assertEqual(ws['B9'].value, 'Омӯзгор З')
        self.assertEqual(ws['H9'].value, '=IF(D9="","",D9/20*E9)')
        self.assertEqual(ws['AB9'].value,
                         '=IF(D9="","",K9-O9+S9+W9+X9+Y9+Z9+AA9)')

    def test_base_pay_and_total_evaluate(self):
        self.profile_a.monthly_rate = Decimal('2479')
        self.profile_a.save()
        ws, ev = self._sheet()
        # cs_a: 7-А МАТЕМАТИКА -> F6=DEFAULT_WEEKLY_HOURS, E6/G6 blank.
        rate = Decimal('2479')
        self.assertEqual(ev.value('H6'), Decimal(0))
        self.assertEqual(ev.value('I6'), rate / 18 * DEFAULT_WEEKLY_HOURS)
        self.assertEqual(ev.value('J6'), Decimal(0))
        self.assertEqual(ev.value('K6'), rate / 18 * DEFAULT_WEEKLY_HOURS)

    def test_decimal_inputs_evaluate(self):
        self.profile_a.monthly_rate = Decimal('2479')
        self.profile_a.save()
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'missed_5_9_{self.profile_a.id}': '3',
            f'missed_10_11_{self.profile_a.id}': '1.5',
            f'substitution_1_4_{self.profile_a.id}': '2.5',
            f'notebook_5_9_{self.profile_a.id}': '4',
            f'allowance_class_head_{self.profile_a.id}': '100',
        })
        ws, ev = self._sheet()
        rate = Decimal('2479')
        self.assertEqual(ws['M6'].value, 3)
        self.assertEqual(ws['N6'].value, 1.5)
        self.assertEqual(ws['P6'].value, 2.5)
        self.assertEqual(ws['U6'].value, 4)
        self.assertEqual(
            ev.value('O6'),
            rate / 72 * 3 + rate / 72 * Decimal('1.5'))
        self.assertEqual(ev.value('S6'), rate / 80 * Decimal('2.5'))
        # W: notebook check pays 20% of the hourly rate per band.
        self.assertEqual(
            ev.value('W6'), rate / 18 * Decimal('0.2') * 4)
        expected_ab = (ev.value('K6') - ev.value('O6') + ev.value('S6')
                       + ev.value('W6') + Decimal(100))
        self.assertEqual(ev.value('AB6'), expected_ab)

    def test_zero_hours_row_payroll_evaluates(self):
        # A teacher with a saved month but no allocation still exports:
        # bands blank -> base pay 0; AB reflects manual amounts only.
        # (Web-page inclusion of zero-hour employees is a later stage.)
        other = make_user('teacher_z', role='teacher', school=self.school_a)
        profile_z = make_teacher_profile(other, self.school_a, 'Омӯзгор З')
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'rate_{profile_z.id}': '1602',
            f'allowance_admin_{profile_z.id}': '500',
        })
        ws, ev = self._sheet()
        self.assertEqual(ws['B9'].value, 'Омӯзгор З')
        self.assertIsNone(ws['F9'].value)
        self.assertEqual(ev.value('K9'), Decimal(0))
        self.assertEqual(ev.value('AB9'), Decimal(500))

    def test_missing_rate_yields_blank_results(self):
        # profile_a has allocations but no monthly_rate -> D6 empty.
        ws, ev = self._sheet()
        self.assertIsNone(ws['D6'].value)
        for col in ('H', 'I', 'J', 'K', 'O', 'S', 'W', 'AB'):
            self.assertEqual(ev.value(f'{col}6'), '', f'{col}6')
            self.assertEqual(ev.value(f'{col}8'), '', f'{col}8')

    def test_missing_monthly_inputs_compute_zero_not_error(self):
        self.profile_a.monthly_rate = Decimal('2479')
        self.profile_a.save()
        ws, ev = self._sheet()
        # No TeacherTabelMonth: L–N, P–R, T–V, X–AA are all blank.
        rate = Decimal('2479')
        self.assertEqual(ev.value('O6'), Decimal(0))
        self.assertEqual(ev.value('S6'), Decimal(0))
        self.assertEqual(ev.value('W6'), Decimal(0))
        self.assertEqual(ev.value('AB6'), rate / 18 * DEFAULT_WEEKLY_HOURS)

    def test_manual_allowances_feed_ab(self):
        self.profile_a.monthly_rate = Decimal('2000')
        self.profile_a.save()
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'allowance_class_head_{self.profile_a.id}': '300',
            f'allowance_admin_{self.profile_a.id}': '50',
            f'allowance_methodical_{self.profile_a.id}': '20',
            f'allowance_language_{self.profile_a.id}': '10',
        })
        ws, ev = self._sheet()
        expected = (Decimal('2000') / 18 * DEFAULT_WEEKLY_HOURS
                    + Decimal('380'))
        self.assertEqual(ev.value('AB6'), expected)

    def test_workbook_recalculates_on_open(self):
        wb = self._export({'month': MONTH})
        self.assertTrue(wb.calculation.fullCalcOnLoad)
        # openpyxl wrote formulas without cached values — data_only
        # returns None, proving no stale results are shipped.
        r = self.client.get(self.url, {'month': MONTH})
        wb2 = load_workbook(io.BytesIO(r.content), data_only=True)
        self.assertIsNone(wb2.active['K6'].value)

    def test_formula_cells_keep_money_format_and_ac_empty(self):
        ws, _ = self._sheet()
        for col in ('H', 'I', 'J', 'K', 'O', 'S', 'W', 'AB'):
            self.assertEqual(ws[f'{col}6'].number_format, '0.00', col)
        for row in range(1, ws.max_row + 1):
            self.assertIsNone(ws.cell(row=row, column=29).value)

    def test_missing_rate_with_allowances_all_blank(self):
        # Allowances entered but no rate -> every payroll cell stays
        # blank; the entered allowance itself is preserved.
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'allowance_class_head_{self.profile_a.id}': '300',
        })
        ws, ev = self._sheet()
        self.assertIsNone(ws['D6'].value)
        self.assertEqual(ws['X6'].value, 300)
        for col in ('H', 'I', 'J', 'K', 'O', 'S', 'W', 'AB'):
            self.assertEqual(ev.value(f'{col}6'), '', f'{col}6')

    def test_zero_hours_with_missed_deduction(self):
        # Rate + missed hours but zero teaching hours: K=0, O>0, and AB
        # applies -O + allowances exactly per the approved formula.
        other = make_user('teacher_z', role='teacher', school=self.school_a)
        profile_z = make_teacher_profile(other, self.school_a, 'Омӯзгор З')
        self.client.force_login(self.zavuch_a)
        self.client.post(self.save_url, {
            'month': MONTH,
            f'rate_{profile_z.id}': '2000',
            f'missed_5_9_{profile_z.id}': '4',
            f'allowance_admin_{profile_z.id}': '100',
        })
        ws, ev = self._sheet()
        self.assertEqual(ws['B9'].value, 'Омӯзгор З')
        self.assertIsNone(ws['E9'].value)
        self.assertIsNone(ws['F9'].value)
        self.assertIsNone(ws['G9'].value)
        self.assertEqual(ev.value('K9'), Decimal(0))
        expected_o = Decimal('2000') / 72 * 4
        self.assertGreater(ev.value('O9'), Decimal(0))
        self.assertEqual(ev.value('O9'), expected_o)
        self.assertEqual(
            ev.value('AB9'),
            ev.value('K9') - expected_o + ev.value('S9')
            + ev.value('W9') + Decimal(100))


class TabelTariffSuggestionTests(GoldenBase):
    """Verified-tariff hints from tariff_rates.suggested_rate_map.

    Fixture schools mirror the real tariffication sources: 'МТМУ №3'
    (Aug-2026 tabel, 2025-26 scale) and 'МТМУ №20' (2024-25 tariffication,
    pre-raise scale).
    """

    def setUp(self):
        super().setUp()
        self.url = reverse('workload_tabel')
        self.save_url = reverse('workload_tabel_save')
        self.export_url = reverse('workload_tabel_export')
        self.school3 = make_school('Муассисаи таҳсилоти умумии №3')
        self.zavuch3 = make_user('zavuch_3', role='teacher',
                                 school=self.school3)
        self.teacher3 = make_user('teacher_3', role='teacher',
                                  school=self.school3)
        # 'Қурбонов Фаромуз Жамшедович' -> 2479 in the Aug-2026 tabel.
        self.profile3 = make_teacher_profile(
            self.teacher3, self.school3, 'Қурбонов Фаромуз Жамшедович')
        make_class_subject(self.school3, '7-А', MATH,
                           allocated=self.profile3)

    def _row(self, response, profile):
        for r in response.context['rows']:
            if r['teacher'].id == profile.id:
                return r
        self.fail('teacher not in tabel rows')

    def test_verified_rate_prefills_same_period_input(self):
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_hint']['kind'], 'verified')
        self.assertEqual(row['rate_input'], Decimal('2479'))
        self.assertContains(
            r, f'name="rate_{self.profile3.id}" value="2479"')
        self.assertContains(r, 'аз тарификатсия')

    def test_prefilled_verified_rate_saves_and_snapshots(self):
        # The deputy accepts the pre-filled value by simply saving.
        self.client.force_login(self.zavuch3)
        r = self.client.post(self.save_url, {
            'month': '2026-08',
            f'rate_{self.profile3.id}': '2479',
            f'missed_5_9_{self.profile3.id}': '2',
        })
        self.assertEqual(r.status_code, 302)
        self.profile3.refresh_from_db()
        self.assertEqual(self.profile3.monthly_rate, Decimal('2479'))
        row = TeacherTabelMonth.objects.get(
            teacher=self.profile3, year=2026, month=8)
        self.assertEqual(row.rates_snapshot[0]['rate'], '2479')

    def test_derived_hint_for_2026_27_never_prefilled(self):
        # +25% from 01.09.2026 is law, but the rounded absolute values
        # are unverified — the hint is text only, the input stays empty.
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-10'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_hint']['kind'], 'derived')
        self.assertEqual(row['rate_hint']['value'], Decimal('3099'))
        self.assertIsNone(row['rate_input'])
        self.assertContains(r, f'name="rate_{self.profile3.id}" value=""')
        self.assertContains(r, 'аз 01.09.2026')

    def test_derived_hint_for_pre_2025_month(self):
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2025-06'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_hint']['kind'], 'derived')
        # 2479 / 1.3 = 1906.92… -> 1907 (old-scale grid point).
        self.assertEqual(row['rate_hint']['value'], Decimal('1907'))
        self.assertIsNone(row['rate_input'])

    def test_manual_rate_wins_over_suggestion(self):
        # A manually stored rate is never overwritten — but when it
        # differs from the verified workbook value the hint flags the
        # discrepancy so the deputy can review it.
        self.profile3.monthly_rate = Decimal('2479')
        self.profile3.save(update_fields=['monthly_rate'])
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_input'], Decimal('2479'))
        self.assertIsNone(row['rate_hint'])  # stored == verified -> quiet
        self.profile3.monthly_rate = Decimal('1500')
        self.profile3.save(update_fields=['monthly_rate'])
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_input'], Decimal('1500'))
        self.assertEqual(row['rate_hint']['kind'], 'verified')
        self.assertEqual(row['rate_hint']['value'], Decimal('2479'))

    def test_stale_rate_flags_derived_hint(self):
        # An old-scale stored rate on a 2026-27 month gets the
        # 'confirm the new scale' hint next to the stored value.
        self.profile3.monthly_rate = Decimal('2479')
        self.profile3.save(update_fields=['monthly_rate'])
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-10'})
        row = self._row(r, self.profile3)
        self.assertEqual(row['rate_input'], Decimal('2479'))
        self.assertEqual(row['rate_hint']['kind'], 'derived')
        self.assertEqual(row['rate_hint']['value'], Decimal('3099'))

    def test_frozen_month_shows_no_hint(self):
        self.client.force_login(self.zavuch3)
        self.client.post(self.save_url, {
            'month': '2026-08',
            f'rate_{self.profile3.id}': '2100',
            f'missed_5_9_{self.profile3.id}': '1',
        })
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, self.profile3)
        self.assertTrue(row['frozen'])
        self.assertIsNone(row['rate_hint'])
        # The recorded 2100 wins over the verified 2479.
        self.assertEqual(row['display_rate'], '2100')

    def test_unmatched_teacher_gets_no_hint(self):
        other = make_user('teacher_9', role='teacher', school=self.school3)
        profile = make_teacher_profile(other, self.school3, 'Ҳакимов Аброр')
        make_class_subject(self.school3, '8-Б', TAJIK, allocated=profile)
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, profile)
        self.assertIsNone(row['rate_hint'])
        self.assertIsNone(row['rate_input'])
        self.assertContains(r, f'name="rate_{profile.id}" value=""')

    def test_duplicate_profile_names_get_no_hint(self):
        # Two same-name profiles in one school -> the workbook entry is
        # ambiguous and must not suggest for either.
        other = make_user('teacher_8', role='teacher', school=self.school3)
        dup = make_teacher_profile(
            other, self.school3, 'Қурбонов Фаромуз Жамшедович')
        make_class_subject(self.school3, '9-В', TAJIK, allocated=dup)
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        self.assertIsNone(self._row(r, self.profile3)['rate_hint'])
        self.assertIsNone(self._row(r, dup)['rate_hint'])

    def test_ambiguous_prefix_match_skipped(self):
        # 'Назарова Нигора' and 'Назарова Нигора Холбобоевна' both
        # prefix-match the single entry -> hint suppressed for both.
        u1 = make_user('teacher_10', role='teacher', school=self.school3)
        p1 = make_teacher_profile(u1, self.school3, 'Назарова Нигора')
        u2 = make_user('teacher_11', role='teacher', school=self.school3)
        p2 = make_teacher_profile(
            u2, self.school3, 'Назарова Нигора Холбобоевна')
        make_class_subject(self.school3, '5-А', TAJIK, allocated=p1)
        make_class_subject(self.school3, '6-А', TAJIK, allocated=p2)
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        self.assertIsNone(self._row(r, p1)['rate_hint'])
        self.assertIsNone(self._row(r, p2)['rate_hint'])

    def test_no_cross_school_hint(self):
        # The same name under a different school number gets nothing.
        zavuch_b = make_user('zavuch_2', role='teacher',
                             school=self.school_b)
        profile_b = make_teacher_profile(
            self.teacher_b, self.school_b, 'Қурбонов Фаромуз Жамшедович')
        self.client.force_login(zavuch_b)
        r = self.client.get(self.url, {'month': '2026-08'})
        row = self._row(r, profile_b)
        self.assertIsNone(row['rate_hint'])
        self.assertIsNone(row['rate_input'])

    def test_short_name_prefix_match_and_periods(self):
        # Production 'Фамилия Имя' (with ҳ) prefix-matches the
        # tariffication 'Фамилия Имя Отчество' (with х) for МТМУ №20.
        school20 = make_school('Муассисаи таҳсилоти умумии №20')
        zavuch20 = make_user('zavuch_20', role='teacher', school=school20)
        teacher20 = make_user('teacher_20', role='teacher', school=school20)
        profile = make_teacher_profile(
            teacher20, school20, 'Абдулҳамидова Дилором')
        make_class_subject(school20, '3-А', TAJIK, allocated=profile)
        self.client.force_login(zavuch20)
        # Recorded period (pre-Sept-2025): verified 2076 prefills.
        r = self.client.get(self.url, {'month': '2024-10'})
        row = self._row(r, profile)
        self.assertEqual(row['rate_hint']['kind'], 'verified')
        self.assertEqual(row['rate_input'], Decimal('2076'))
        # Post-raise periods: derived only, never prefilled.
        r = self.client.get(self.url, {'month': '2025-10'})
        row = self._row(r, profile)
        self.assertEqual(row['rate_hint']['kind'], 'derived')
        self.assertEqual(row['rate_hint']['value'], Decimal('2699'))
        self.assertIsNone(row['rate_input'])
        r = self.client.get(self.url, {'month': '2026-10'})
        row = self._row(r, profile)
        self.assertEqual(row['rate_hint']['kind'], 'derived')
        # 2076 * 1.3 * 1.25 = 3373.5 -> 3374
        self.assertEqual(row['rate_hint']['value'], Decimal('3374'))
        self.assertIsNone(row['rate_input'])

    def test_birthdate_suffix_in_profile_name_still_matches(self):
        # Free the workbook entry first — the setUp profile would
        # otherwise make it ambiguous together with the new one.
        self.profile3.full_name = 'Ҳакимов Аброр'
        self.profile3.save(update_fields=['full_name'])
        teacher = make_user('teacher_12', role='teacher',
                            school=self.school3)
        profile = make_teacher_profile(
            teacher, self.school3,
            'Қурбонов Фаромуз Жамшедович 12.03.1985')
        make_class_subject(self.school3, '4-Г', TAJIK, allocated=profile)
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.url, {'month': '2026-08'})
        self.assertEqual(
            self._row(r, profile)['rate_hint']['kind'], 'verified')

    def test_export_never_writes_suggestion(self):
        # Column D carries stored rates only — a suggestion must not
        # leak into the official export.
        self.client.force_login(self.zavuch3)
        r = self.client.get(self.export_url, {'month': '2026-08'})
        ws = load_workbook(io.BytesIO(r.content)).active
        self.assertEqual(ws['B6'].value, 'Қурбонов Фаромуз Жамшедович')
        self.assertIsNone(ws['D6'].value)
