"""School-eligibility filtering: non-school institutions (kindergartens,
preschools, education departments) must not appear in school-facing areas.

Rules frozen here:
  - Dashboard statistics count only general-education schools.
  - Parent/student selector, school list, allocation selectors and journals
    exclude non-school institutions.
  - can_view/can_edit_grade_journal deny non-schools server-side.
  - No records are deleted — non-school rows remain in the DB.
"""
import io

import pandas as pd
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from portal.models import (
    School, Student, Teacher, TeacherProfile, UserProfile,
)
from portal.tests.helpers import (
    make_class_subject, make_school, make_student, make_user, MATH,
)
from portal.utils import (
    academic_school_ids, academic_schools, is_academic_school,
    schools_leaderboard,
)
from portal.views import (
    _get_monitoring_stats, can_edit_grade_journal, can_view_grade_journal,
)


class SchoolEligibilityBase(TestCase):
    """Fixture: two real schools plus one of each non-school type."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school('Мактаб №100', type='Мактаб')
        cls.litsey = make_school('Литсей №1', type='Литсей')
        cls.kinder = make_school('Кӯдакистони №1-и ноҳия', type='Кӯдакистон')
        cls.preschool = make_school(
            'МДТТ №2', type='Муассисаи давлатии таълимии томактабӣ')
        cls.idora = make_school(
            'Идораи маорифи ноҳия', type='Идораи маориф')
        cls.non_schools = [cls.kinder, cls.preschool, cls.idora]

        cls.st = make_student(cls.school, '5-А', 'Шахзод Тошев')
        cls.k_st = make_student(cls.kinder, '0-А', 'Кудаки Тест')
        Teacher.objects.create(school=cls.school, name='Омӯзгор Мактаб')
        Teacher.objects.create(school=cls.kinder, name='Мураббии Кӯдакистон')

        cls.admin = make_user('admin_e', superuser=True)
        cls.teacher = make_user('teacher_e', role='teacher', school=cls.school)
        make_class_subject(cls.school, '5-А', MATH, teacher=cls.teacher)
        make_class_subject(cls.kinder, '0-А', MATH, teacher=cls.teacher)


class EligibilityPredicateTests(SchoolEligibilityBase):

    def test_real_school_types_are_eligible(self):
        self.assertTrue(is_academic_school(self.school))
        self.assertTrue(is_academic_school(self.litsey))

    def test_non_school_types_are_not_eligible(self):
        for s in self.non_schools:
            self.assertFalse(is_academic_school(s), s.name)

    def test_academic_schools_helper(self):
        ids = {s.id for s in academic_schools()}
        self.assertIn(self.school.id, ids)
        self.assertIn(self.litsey.id, ids)
        for s in self.non_schools:
            self.assertNotIn(s.id, ids)
        self.assertEqual(ids, academic_school_ids())


class DashboardStatsTests(SchoolEligibilityBase):

    def test_school_count_excludes_non_schools(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_schools'], 2)  # school + litsey only

    def test_student_count_excludes_non_school_students(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_students'], 1)

    def test_teacher_count_excludes_non_school_teachers(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_teachers'], 1)

    def test_dashboard_schools_list_excludes_non_schools(self):
        r = self.client.get(reverse('dashboard'))
        ids = {s.id for s in r.context['schools']}
        self.assertEqual(ids, {self.school.id, self.litsey.id})
        dd = {s.id for s in r.context['schools_dropdown']}
        for s in self.non_schools:
            self.assertNotIn(s.id, dd)

    def test_stats_internally_consistent(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_schools'], len(academic_school_ids()))
        self.assertEqual(
            r.context['total_students'],
            Student.objects.filter(school_id__in=academic_school_ids()).count())
        self.assertEqual(
            r.context['total_teachers'],
            Teacher.objects.filter(school_id__in=academic_school_ids()).count())

    def test_school_count_is_data_driven_not_hardcoded(self):
        """The school count must follow the actual eligible records — adding a
        real school raises it, adding a kindergarten does not."""
        r = self.client.get(reverse('dashboard'))
        before = r.context['total_schools']
        make_school('Мактаб №200', type='Мактаб')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_schools'], before + 1)
        make_school('Кӯдакистони №9', type='Кӯдакистон')
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.context['total_schools'], before + 1)


class ParentPortalSelectorTests(SchoolEligibilityBase):

    def test_parent_selector_excludes_non_schools(self):
        r = self.client.get(reverse('dashboard'))
        data = r.context['parent_portal_data']
        ids = {s['id'] for s in data['schools']}
        self.assertIn(self.school.id, ids)
        for s in self.non_schools:
            self.assertNotIn(s.id, ids)
            self.assertNotIn(str(s.id), data['classes'])

    def test_parent_selector_shows_real_school_classes(self):
        r = self.client.get(reverse('dashboard'))
        data = r.context['parent_portal_data']
        self.assertIn('5-А', data['classes'][str(self.school.id)])


class SchoolListTests(SchoolEligibilityBase):

    def test_school_list_excludes_non_schools(self):
        r = self.client.get(reverse('school_list'))
        ids = {s.id for s in r.context['schools']}
        self.assertEqual(ids, {self.school.id, self.litsey.id})

    def test_class_list_non_school_redirects(self):
        for s in self.non_schools:
            r = self.client.get(reverse('class_list', args=[s.id]))
            self.assertRedirects(
                r, reverse('school_list'), fetch_redirect_response=False)

    def test_class_list_real_school_ok(self):
        r = self.client.get(reverse('class_list', args=[self.school.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '5-А')

    def test_teacher_list_non_school_redirects(self):
        self.client.force_login(self.admin)
        for s in self.non_schools:
            r = self.client.get(reverse('teacher_list', args=[s.id]))
            self.assertRedirects(
                r, reverse('school_list'), fetch_redirect_response=False)


class LessonAllocationTests(SchoolEligibilityBase):

    def setUp(self):
        self.client.force_login(self.admin)

    def test_allocation_selector_excludes_non_schools(self):
        r = self.client.get(reverse('lesson_allocation'))
        self.assertEqual(r.status_code, 200)
        ids = {s.id for s in r.context['all_schools']}
        self.assertIn(self.school.id, ids)
        for s in self.non_schools:
            self.assertNotIn(s.id, ids)

    def test_allocation_rejects_non_school_id_param(self):
        r = self.client.get(
            reverse('lesson_allocation') + f'?school_id={self.kinder.id}')
        self.assertNotEqual(r.context['school'].id, self.kinder.id)

    def test_save_allocation_rejects_non_school(self):
        r = self.client.post(reverse('save_lesson_allocation'),
                             {'school_id': str(self.kinder.id)})
        self.assertRedirects(
            r, reverse('dashboard'), fetch_redirect_response=False)


class JournalAccessTests(SchoolEligibilityBase):
    """Server-side: no journal workflow may treat a non-school as a school."""

    def test_can_view_edit_deny_non_schools(self):
        for s in self.non_schools:
            self.assertFalse(
                can_view_grade_journal(self.admin, s, '0-А', MATH), s.name)
            self.assertFalse(
                can_edit_grade_journal(self.admin, s, '0-А', MATH), s.name)

    def test_grade_entry_non_school_denied(self):
        self.client.force_login(self.admin)
        for s in self.non_schools:
            r = self.client.get(reverse(
                'grade_entry', args=[s.id, '0-А', MATH]))
            self.assertEqual(r.status_code, 403)

    def test_monthly_journal_non_school_denied(self):
        self.client.force_login(self.admin)
        for s in self.non_schools:
            r = self.client.get(reverse(
                'monthly_journal', args=[s.id, '0-А', MATH]))
            self.assertEqual(r.status_code, 403)

    def test_grade_entry_real_school_ok(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse(
            'grade_entry', args=[self.school.id, '5-А', MATH]))
        self.assertEqual(r.status_code, 200)


class ReportsAndRankingsTests(SchoolEligibilityBase):

    def test_schools_leaderboard_excludes_non_schools(self):
        ids = {row['school'].id for row in schools_leaderboard()}
        for s in self.non_schools:
            self.assertNotIn(s.id, ids)
        self.assertIn(self.school.id, ids)

    def test_monitoring_stats_exclude_non_schools(self):
        ids = {row['school'].id for row in _get_monitoring_stats()}
        for s in self.non_schools:
            self.assertNotIn(s.id, ids)


class DirectWriteGuardTests(SchoolEligibilityBase):
    """Server-side: direct POSTs to school-write endpoints must reject
    non-school institutions even when the caller has school access
    (superuser). UI filtering alone is not sufficient."""

    def setUp(self):
        self.client.force_login(self.admin)

    def _xlsx(self, rows):
        buf = io.BytesIO()
        pd.DataFrame(rows).to_excel(buf, index=False)
        buf.seek(0)
        buf.name = 'import.xlsx'
        return buf

    # --- add_student -----------------------------------------------------

    def test_add_student_rejected_for_non_schools(self):
        for s in self.non_schools:
            before = Student.objects.filter(school=s).count()
            r = self.client.post(
                reverse('add_student', args=[s.id, '0-А']),
                {'full_name': 'Хонандаи Нав'})
            self.assertRedirects(
                r, reverse('dashboard'), fetch_redirect_response=False)
            self.assertEqual(Student.objects.filter(school=s).count(), before)

    def test_add_student_works_for_real_school(self):
        r = self.client.post(
            reverse('add_student', args=[self.school.id, '5-А']),
            {'full_name': 'Хонандаи Нав'})
        self.assertRedirects(
            r, reverse('class_detail', args=[self.school.id, '5-А']),
            fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(
            school=self.school, class_name='5-А',
            full_name='Хонандаи Нав').exists())

    # --- add_teacher -----------------------------------------------------

    def test_add_teacher_rejected_for_non_schools(self):
        for s in self.non_schools:
            counts = (
                User.objects.count(),
                TeacherProfile.objects.filter(school=s).count(),
                UserProfile.objects.filter(school=s).count(),
                Teacher.objects.filter(school=s).count(),
            )
            r = self.client.post(
                reverse('add_teacher', args=[s.id]),
                {'full_name': 'Омӯзгори Нав', 'phone': '900', 'subject': MATH})
            self.assertRedirects(
                r, reverse('dashboard'), fetch_redirect_response=False)
            self.assertEqual(counts, (
                User.objects.count(),
                TeacherProfile.objects.filter(school=s).count(),
                UserProfile.objects.filter(school=s).count(),
                Teacher.objects.filter(school=s).count(),
            ))

    def test_add_teacher_works_for_real_school(self):
        r = self.client.post(
            reverse('add_teacher', args=[self.school.id]),
            {'full_name': 'Омӯзгори Нав', 'phone': '900', 'subject': MATH})
        self.assertRedirects(
            r, reverse('teacher_list', args=[self.school.id]),
            fetch_redirect_response=False)
        self.assertTrue(Teacher.objects.filter(
            school=self.school, name='Омӯзгори Нав').exists())
        self.assertTrue(TeacherProfile.objects.filter(
            school=self.school, full_name='Омӯзгори Нав').exists())

    # --- import_excel ----------------------------------------------------

    def test_import_excel_rejected_for_non_schools(self):
        for s in self.non_schools:
            before = Student.objects.filter(school=s).count()
            f = self._xlsx([{'Ному насаб': 'Хонандаи Нав', 'Синф': '0-А'}])
            r = self.client.post(
                reverse('import_excel', args=[s.id]), {'excel': f})
            self.assertRedirects(
                r, reverse('dashboard'), fetch_redirect_response=False)
            self.assertEqual(Student.objects.filter(school=s).count(), before)

    def test_import_excel_works_for_real_school(self):
        f = self._xlsx([{'Ному насаб': 'Хонандаи Воридшуда', 'Синф': '5-А'}])
        r = self.client.post(
            reverse('import_excel', args=[self.school.id]), {'excel': f})
        self.assertRedirects(
            r, reverse('class_list', args=[self.school.id]),
            fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(
            school=self.school, class_name='5-А',
            full_name='Хонандаи Воридшуда').exists())

    # --- sibling write endpoints share the same guard --------------------

    def test_other_write_endpoints_reject_non_schools(self):
        cases = [
            ('add_class', [self.kinder.id], {'class_name': '9-А'}),
            ('add_remove_subject', [self.kinder.id, '0-А'],
             {'action': 'add', 'subject': MATH}),
            ('import_teachers', [self.kinder.id], {}),
            ('transfer_class', [self.idora.id, '0-А'], {'target_class': '1-А'}),
        ]
        for name, args, data in cases:
            r = self.client.post(reverse(name, args=args), data)
            self.assertRedirects(
                r, reverse('dashboard'), fetch_redirect_response=False)


class DataSafetyTests(SchoolEligibilityBase):

    def test_no_records_deleted(self):
        """Non-school institutions, their students and teachers remain in the DB."""
        for s in self.non_schools:
            self.assertTrue(School.objects.filter(id=s.id).exists())
        self.assertTrue(Student.objects.filter(id=self.k_st.id).exists())
        self.assertTrue(
            Teacher.objects.filter(school=self.kinder).exists())
