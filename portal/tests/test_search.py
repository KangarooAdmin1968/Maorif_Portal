"""Tests for the role-aware student search (/search/).

Covers school scoping per role, teacher class restrictions, the tolerant
phone-keyboard name matching (iregex + equivalence groups), anonymous
school requirement, result limits and the zero-search-query guarantees.
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from portal.tests.helpers import (
    GoldenBase, make_class_subject, make_student, make_user,
)
from portal.views import _search_name_pattern, _teacher_permitted_class_names

SEARCH_URL = reverse('search')


def _regexp_queries(ctx):
    return [q for q in ctx.captured_queries if 'REGEXP' in q['sql'].upper()]


class SearchViewTests(GoldenBase):
    """Role scoping and query-shape for GET /search/."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Extra fixture students inside School A (permitted class 7-А).
        cls.abdul = make_student(cls.school_a, '7-А', 'Абдулло Раҳмонов')
        cls.qodir = make_student(cls.school_a, '7-А', 'Қодирова Мадина')
        cls.ghulom = make_student(cls.school_a, '7-А', 'Ғуломов Раҳим')
        cls.sodir = make_student(cls.school_a, '7-А', 'Содирова Ситора')
        # Class 8-Б exists at School A but teacher_a is NOT assigned to it.
        cls.other_class_student = make_student(
            cls.school_a, '8-Б', 'Абдулҳақ Юлдошев')
        cls.staff = make_user('staff_1', role='teacher',
                              school=cls.school_a, staff=True)

    def _names(self, response):
        return [r['full_name'] for r in response.context['results']]

    # --- basic page behaviour ---------------------------------------------

    def test_search_page_renders_anonymous(self):
        r = self.client.get(SEARCH_URL)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['searched'])
        self.assertEqual(r.context['results'], [])

    def test_nav_contains_search_link(self):
        r = self.client.get(reverse('dashboard'))
        self.assertContains(r, '/search/')

    def test_partial_name_search(self):
        r = self.client.get(SEARCH_URL,
                            {'school': self.school_a.id, 'q': 'Раҳмо'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))

    # --- zavuch ------------------------------------------------------------

    def test_zavuch_sees_only_own_school(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertEqual(r.context['selected_school'], self.school_a)
        self.assertNotIn('Ҷаҳонгир Назаров', self._names(r))
        r = self.client.get(SEARCH_URL, {'q': 'Раҳмонов'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))

    def test_zavuch_cannot_widen_scope_via_url(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(SEARCH_URL, {
            'q': 'Назаров', 'school': self.school_b.id})
        # school param is clamped to the zavuch's own school
        self.assertEqual(r.context['selected_school'], self.school_a)
        self.assertNotIn('Ҷаҳонгир Назаров', self._names(r))

    # --- teacher ------------------------------------------------------------

    def test_teacher_sees_only_own_school(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertNotIn('Ҷаҳонгир Назаров', self._names(r))
        r = self.client.get(SEARCH_URL, {'q': 'Раҳмонов'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))

    def test_teacher_results_limited_to_permitted_classes(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(SEARCH_URL, {'q': 'Абдул'})
        names = self._names(r)
        self.assertIn('Абдулло Раҳмонов', names)          # 7-А — assigned
        self.assertNotIn('Абдулҳақ Юлдошев', names)        # 8-Б — not assigned

    def test_teacher_cannot_search_unpermitted_class(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(SEARCH_URL, {
            'q': 'Абдул', 'class_name': '8-Б'})
        self.assertTrue(r.context['class_blocked'])
        self.assertEqual(r.context['results'], [])

    def test_teacher_permitted_class_filter_works(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(SEARCH_URL, {
            'q': 'Абдул', 'class_name': '7-А'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))
        self.assertNotIn('Абдулҳақ Юлдошев', self._names(r))

    def test_teacher_class_dropdown_only_permitted(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(SEARCH_URL)
        self.assertEqual(r.context['class_choices'], ['7-А'])

    def test_permitted_class_names_helper_matches_class_list(self):
        # Same set the class_list view computes through _teacher_can_access_cs.
        self.assertEqual(
            _teacher_permitted_class_names(self.teacher_a, self.school_a),
            {'7-А'},
        )

    # --- privileged scopes ---------------------------------------------------

    def test_admin_searches_all_academic_schools(self):
        self.client.force_login(self.admin)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertIn('Ҷаҳонгир Назаров', self._names(r))
        r = self.client.get(SEARCH_URL, {'q': 'Абдулҳақ'})
        self.assertIn('Абдулҳақ Юлдошев', self._names(r))

    def test_staff_searches_all_academic_schools(self):
        self.client.force_login(self.staff)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertIn('Ҷаҳонгир Назаров', self._names(r))

    def test_director_searches_all_academic_schools(self):
        self.client.force_login(self.director)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertIn('Ҷаҳонгир Назаров', self._names(r))

    def test_principal_sees_own_school(self):
        self.client.force_login(self.principal_a)
        r = self.client.get(SEARCH_URL, {'q': 'Назаров'})
        self.assertEqual(r.context['selected_school'], self.school_a)
        self.assertNotIn('Ҷаҳонгир Назаров', self._names(r))
        r = self.client.get(SEARCH_URL, {'q': 'Раҳмонов'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))

    def test_privileged_school_filter(self):
        self.client.force_login(self.admin)
        r = self.client.get(SEARCH_URL, {
            'q': 'Абдул', 'school': self.school_b.id})
        self.assertEqual(r.context['results'], [])

    # --- anonymous / parent ---------------------------------------------------

    def test_anonymous_requires_school(self):
        r = self.client.get(SEARCH_URL, {'q': 'Абдул'})
        self.assertTrue(r.context['need_school'])
        self.assertFalse(r.context['searched'])
        self.assertEqual(r.context['results'], [])

    def test_anonymous_no_global_search(self):
        # Even with a query, no Student search runs without a school.
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(SEARCH_URL, {'q': 'Абдул'})
        self.assertEqual(_regexp_queries(ctx), [])
        self.assertEqual(r.context['results'], [])

    def test_anonymous_school_scoped_search(self):
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'q': 'Абдул'})
        names = self._names(r)
        self.assertIn('Абдулло Раҳмонов', names)
        self.assertIn('Абдулҳақ Юлдошев', names)
        r = self.client.get(SEARCH_URL, {
            'school': self.school_b.id, 'q': 'Абдул'})
        self.assertEqual(r.context['results'], [])

    def test_anonymous_class_filter(self):
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'class_name': '8-Б', 'q': 'Абдул'})
        names = self._names(r)
        self.assertEqual(names, ['Абдулҳақ Юлдошев'])

    # --- filter validation -----------------------------------------------------

    def test_unknown_school_rejected(self):
        r = self.client.get(SEARCH_URL, {'school': 999999, 'q': 'Абдул'})
        self.assertTrue(r.context['school_error'])
        self.assertEqual(r.context['results'], [])

    def test_invalid_class_rejected(self):
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'class_name': '99-Я', 'q': 'Абдул'})
        self.assertTrue(r.context['class_blocked'])
        self.assertEqual(r.context['results'], [])

    def test_class_filter_normalization(self):
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'class_name': '7а', 'q': 'Абдул'})
        self.assertIn('Абдулло Раҳмонов', self._names(r))

    def test_empty_query_runs_no_search(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(SEARCH_URL, {'school': self.school_a.id})
        self.assertFalse(r.context['searched'])
        self.assertEqual(_regexp_queries(ctx), [])

    def test_whitespace_only_query(self):
        r = self.client.get(SEARCH_URL,
                            {'school': self.school_a.id, 'q': '   '})
        self.assertFalse(r.context['searched'])

    def test_long_query_truncated(self):
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'q': 'А' * 500})
        self.assertEqual(len(r.context['q']), 100)

    # --- result limit ------------------------------------------------------------

    def test_result_limit_30(self):
        for i in range(35):
            make_student(self.school_a, '7-А', f'Абдулзода Тест{i:02d}')
        r = self.client.get(SEARCH_URL, {
            'school': self.school_a.id, 'q': 'Абдулзода'})
        self.assertEqual(len(r.context['results']), 30)

    # --- case-insensitive Cyrillic ------------------------------------------------

    def test_cyrillic_case_insensitive(self):
        expected = {'Абдулло Раҳмонов', 'Абдулҳақ Юлдошев'}
        for q in ('Абдул', 'абдул', 'АБДУЛ', 'аБдУл'):
            with self.subTest(q=q):
                r = self.client.get(SEARCH_URL, {
                    'school': self.school_a.id, 'q': q})
                self.assertTrue(
                    expected.issubset(set(self._names(r))))

    # --- phone-keyboard substitutions ----------------------------------------------

    def _found(self, q):
        r = self.client.get(SEARCH_URL,
                            {'school': self.school_a.id, 'q': q})
        return set(self._names(r))

    def test_latin_q_finds_tajik_q(self):
        for q in ('Qodirova', 'qodirova', 'Kodirova', 'ҚОДИРОВА'):
            with self.subTest(q=q):
                self.assertIn('Қодирова Мадина', self._found(q))

    def test_g_g_substitution(self):
        for q in ('Ghulomov', 'Гуломов', 'ҒУЛОМОВ', 'gulomov'):
            with self.subTest(q=q):
                self.assertIn('Ғуломов Раҳим', self._found(q))

    def test_u_yu_for_uk_u(self):
        make_student(self.school_a, '7-А', 'Рӯстам Шарифов')
        for q in ('Rustam', 'Рустам', 'Ustam'):
            with self.subTest(q=q):
                self.assertIn('Рӯстам Шарифов', self._found(q))

    def test_i_for_ii(self):
        make_student(self.school_a, '7-А', 'Шаҳрӣ Валиева')
        for q in ('Shahri', 'Шахри', 'shahri'):
            with self.subTest(q=q):
                self.assertIn('Шаҳрӣ Валиева', self._found(q))

    def test_h_j_substitution(self):
        make_student(self.school_a, '7-А', 'Ҷумъа Ҳабибов')
        for q in ('Juma', 'Жума', 'ҶУМА'):
            with self.subTest(q=q):
                self.assertIn('Ҷумъа Ҳабибов', self._found(q))
        # ҳ / h / х family: Ҳабибов reachable from all three
        for q in ('Habibov', 'Хабибов', 'ҲАБИБОВ'):
            with self.subTest(q=q):
                self.assertIn('Ҷумъа Ҳабибов', self._found(q))

    def test_substitution_not_overbroad(self):
        # 'Кодиров' must reach Қодирова but never Содирова.
        found = self._found('Кодиров')
        self.assertIn('Қодирова Мадина', found)
        self.assertNotIn('Содирова Ситора', found)
        # 'ch' maps to ч only — it must not reach Ҷ.
        make_student(self.school_a, '7-А', 'Ҷумъа Нозимов')
        self.assertNotIn('Ҷумъа Нозимов', self._found('Chuma'))

    def test_omitted_apostrophe(self):
        make_student(self.school_a, '7-А', 'Саъдулло Мирзоев')
        for q in ('Sadullo', 'Садулло', "Саъдулло"):
            with self.subTest(q=q):
                self.assertIn('Саъдулло Мирзоев', self._found(q))

    # --- performance boundaries -----------------------------------------------------

    def test_dashboard_runs_no_search_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_regexp_queries(ctx), [])

    def test_search_executes_single_student_query(self):
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(SEARCH_URL, {'q': 'Абдул'})
        self.assertEqual(len(_regexp_queries(ctx)), 1)

    # --- parent portal unchanged ------------------------------------------------------

    def test_parent_portal_still_available(self):
        r = self.client.get(reverse('dashboard'))
        self.assertContains(r, 'parent-portal-data')
        self.assertContains(r, 'Барои волидон')
        r = self.client.get(reverse('class_detail',
                                    args=[self.school_a.id, '7-А']))
        self.assertEqual(r.status_code, 200)
        r = self.client.get(reverse('student_detail', args=[self.abdul.id]))
        self.assertEqual(r.status_code, 200)


class SearchPatternTests(GoldenBase):
    """Unit-level checks on the generated regex — no request needed."""

    def test_pattern_is_regex_safe(self):
        # Metacharacters in the query can never break out of the pattern.
        pattern = _search_name_pattern('a+b(c)d.*[x]')
        import re
        rx = re.compile(pattern, re.IGNORECASE)
        self.assertIsNone(rx.search('aaa'))
        self.assertIsNotNone(rx.search('a+b(c)d.*[x]'))

    def test_pattern_case_insensitive_cyrillic(self):
        import re
        rx = re.compile(_search_name_pattern('абдул'), re.IGNORECASE)
        self.assertIsNotNone(rx.search('АБДУЛЛО'))
        self.assertIsNotNone(rx.search('АбдУлҳақ'))
