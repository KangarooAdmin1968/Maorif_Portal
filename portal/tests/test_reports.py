"""Ҳисоботлар — live role-scoped reports tab.

Covers: scope (district vs own school), section completeness for both
report kinds, teacher-norm bucketing, and the multi-sheet Excel export.
"""
import io

from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook

from portal.reports import (
    build_report_workbook, collect_district_report, collect_school_report,
    report_period_options, resolve_period,
)
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_grade,
    make_student, make_teacher_profile,
)


class ReportPeriodTests(GoldenBase):
    def test_options_cover_month_quarter_semester_year(self):
        values = {v for v, _ in report_period_options()}
        self.assertIn('q2', values)
        self.assertIn('m11', values)
        self.assertIn('s1', values)
        self.assertIn('all', values)

    def test_month_resolves_to_its_quarter(self):
        # November sits inside quarter 2.
        start, end, label, value, quarter = resolve_period('m11')
        self.assertEqual(quarter, 2)
        self.assertEqual((start.month, end.month), (11, 11))


class SchoolReportTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, 'Омӯзгор Якка')
        make_grade(self.s1, subject=MATH, score=8, date=D_Q1)

    def test_collect_school_report_shape(self):
        data = collect_school_report(self.school_a, 'q2')
        self.assertEqual(data['kind'], 'school')
        self.assertEqual(data['school'], self.school_a)
        for key in ('teacher_rows', 'journal', 'excellent', 'ungraded',
                    'home_students', 'movements', 'class_ranking',
                    'school_students', 'fulfillment', 'students'):
            self.assertIn(key, data)

    def test_teacher_row_status_labels(self):
        data = collect_school_report(self.school_a, 'q2')
        names = {t['name'] for t in data['teacher_rows']}
        self.assertIn('Омӯзгор Якка', names)
        for t in data['teacher_rows']:
            self.assertIn(t['status'], ('Иҷро шуд', 'Қафо монд',
                                        'Баҳо нест', 'Бебаҳо бор'))

    def test_teacher_flagged_when_student_has_no_marks(self):
        """Aggregate quota is not enough — a teacher whose students
        include bebaho (no mark at all in the current quarter) must
        not read «Иҷро шуд», and the row must name where the gap is."""
        # s2 (7-А) has no marks anywhere; teacher_a teaches her МАТЕМАТИКА.
        data = collect_school_report(self.school_a, 'q2')
        row = next(t for t in data['teacher_rows']
                   if t['name'] == 'Омӯзгор Якка')
        self.assertIn('7-А МАТЕМАТИКА', row['missing_detail'])
        self.assertNotEqual(row['status'], 'Иҷро шуд')


class DistrictReportTests(GoldenBase):
    def setUp(self):
        super().setUp()
        make_teacher_profile(self.teacher_a, self.school_a)
        make_teacher_profile(self.teacher_b, self.school_b)
        make_grade(self.s1, subject=MATH, score=9, date=D_Q1)
        make_grade(self.s3, subject=MATH, score=6, date=D_Q1)

    def test_collect_district_report_shape(self):
        # The district report serves readiness only from the
        # cron-warmed cache — warm it like production does.
        from django.core.management import call_command
        call_command('refresh_rankings')
        data = collect_district_report('q2')
        self.assertEqual(data['kind'], 'district')
        self.assertEqual(len(data['school_rows']), 2)
        row = {r['school'].id: r for r in data['school_rows']}
        for r in data['school_rows']:
            for key in ('students', 'teachers', 'no_grades', 'met',
                        'not_met', 'journal', 'arrivals', 'departures',
                        'home_students', 'excellent', 'ungraded'):
                self.assertIn(key, r)
        self.assertGreaterEqual(row[self.school_a.id]['teachers'], 1)

    def test_top_and_weak_class_lists(self):
        # Rankings are read-only on the report page — the cron command
        # warms them first, like production.
        from django.core.management import call_command
        call_command('refresh_rankings')
        data = collect_district_report('q2')
        top = {c['class_name'] for c in data['top_classes']}
        weak = {c['class_name'] for c in data['weak_classes']}
        self.assertIn('7-А', top)
        self.assertIn('7-Б', weak)

    def test_district_counts_cold_then_warm(self):
        from portal.reports import report_counts_cache_key
        from portal.views import (
            RANKING_STALE_SUFFIX, _ranking_cache_delete,
            academic_year_start,
        )
        quarter = resolve_period('q2')[4]
        key = report_counts_cache_key(quarter, academic_year_start())
        # The file cache persists between test runs — clear both
        # entries so the cold path is what it is in production before
        # the first cron warm.
        _ranking_cache_delete(key)
        _ranking_cache_delete(key + RANKING_STALE_SUFFIX)
        # Cold cache → '—' cells (None), never a heavy request-side scan.
        data = collect_district_report('q2')
        for r in data['school_rows']:
            self.assertIsNone(r['excellent'])
            self.assertIsNone(r['ungraded'])
        # Warm the per-quarter key like refresh_rankings does.
        from portal.models import School
        from portal.reports import excellent_ungraded_counts
        from portal.utils import is_academic_school
        from portal.views import _ranking_cached
        schools = [s for s in School.objects.all()
                   if is_academic_school(s)]
        _ranking_cached(
            key, lambda: excellent_ungraded_counts(schools, quarter))
        data = collect_district_report('q2')
        # The batched counts must equal the official per-school
        # collectors the Ҳуҷҷатнигорӣ lists use.
        from portal.excellent import collect_excellent_students
        from portal.ungraded import collect_ungraded_students
        for r in data['school_rows']:
            self.assertIsInstance(r['excellent'], int)
            self.assertIsInstance(r['ungraded'], int)
            self.assertEqual(
                r['excellent'],
                len(collect_excellent_students(r['school'], quarter)
                    ['students']))
            self.assertEqual(
                r['ungraded'],
                collect_ungraded_students(r['school'], quarter)
                ['students'])


class ReportViewScopeTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_teacher_cannot_open_reports(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('reports'))
        self.assertEqual(resp.status_code, 302)

    def test_director_sees_district(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['data']['kind'], 'district')

    def test_director_drills_into_school(self):
        self.client.force_login(self.director)
        resp = self.client.get(
            reverse('reports') + f'?school={self.school_a.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['data']['kind'], 'school')
        self.assertEqual(resp.context['data']['school'], self.school_a)

    def test_zavuch_sees_only_own_school(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(
            reverse('reports') + f'?school={self.school_b.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['data']['kind'], 'school')
        self.assertEqual(resp.context['data']['school'], self.school_a)


class ReportExportTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_district_workbook_sheets(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse('reports_export'))
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        names = set(wb.sheetnames)
        for sheet in ('Ҳолати умумӣ', 'Рейтинги мактабҳо',
                      'Синфҳои пешқадам', 'Синфҳои заиф',
                      'Рейтинги фанҳо', 'Хонандагони беҳтарин',
                      'Аълохон ва Бебаҳо', 'Ҳолати журналҳо',
                      'Ҳаракати хонандагон', 'Таҳсил дар хона'):
            self.assertIn(sheet, names)
        # Every sheet prints landscape on one page width.
        for ws in wb.worksheets:
            self.assertEqual(ws.page_setup.orientation, 'landscape')
            self.assertEqual(ws.page_setup.fitToWidth, 1)

    def test_school_workbook_sheets(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('reports_export'))
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        names = set(wb.sheetnames)
        for sheet in ('Меъёри омӯзгорон', 'Ҳолати журналҳо', 'Аълохон',
                      'Бебаҳо', 'Таҳсил дар хона', 'Ҳаракати хонандагон',
                      'Рейтинги синфҳо', 'Хонандагони беҳтарин'):
            self.assertIn(sheet, names)

    def test_teacher_cannot_export(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('reports_export'))
        self.assertEqual(resp.status_code, 302)
