"""Golden-master tests for the sticker journal, non-graded classes,
Excel import, student identity/gender, and ClassSubject seeding."""
import datetime
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from portal.models import ClassSubject, Grade, QuarterGrade, Student
from portal.tests.helpers import (
    D_Q1, MATH, PERIOD, CUSTOM_SUBJECT, TAJIK,
    GoldenBase, make_class_subject, make_deactivation_request,
    make_grade, make_quarter_grade, make_school, make_student,
)
from portal.utils import ensure_class_subjects, is_non_graded
from portal.views import QUALITATIVE_CHOICES, _move_student

STICKER_SUBJECT = 'СТИКЕРҲО'  # normalize_subject('Стикерҳо')


# ---------------------------------------------------------------------------
# Sticker journal — non-graded classes
# ---------------------------------------------------------------------------

class StickerFlowGoldenTests(GoldenBase):
    """The sticker journal uses the pseudo-subject 'Стикерҳо' on plain Grade
    rows. It must stay independent of any future Assessment semantics."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kid = make_student(cls.school_a, '1-А', 'Сабрина Юсупова')
        # Class-wide permission: any active assignment inside the non-graded
        # class grants sticker-journal access.
        make_class_subject(cls.school_a, '1-А', 'АЛИФБО',
                           teacher=cls.teacher_a)

    def _url(self, class_name='1-А'):
        return reverse('sticker_entry', args=[self.school_a.id, class_name])

    def test_sticker_entry_non_graded_200(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 200)

    def test_sticker_entry_creates_pseudo_subject_classsubject(self):
        self.client.force_login(self.teacher_a)
        self.client.get(self._url())
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='1-А', subject=STICKER_SUBJECT)
        self.assertFalse(cs.is_default)
        self.assertTrue(cs.is_active)

    def test_sticker_entry_graded_class_redirects(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self._url('7-А'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('class', r.url)

    def test_sticker_save_contract(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
            'date': D_Q1.isoformat(), 'type': 'daily', 'sticker': '⭐',
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(
            student=self.kid, subject=STICKER_SUBJECT).sticker, '⭐')

    def test_sticker_all_four_symbols_accepted(self):
        self.client.force_login(self.teacher_a)
        for i, sym in enumerate(('⭐', '☀️', '🌸', '📖')):
            d = D_Q1 + datetime.timedelta(days=i)
            r = self.client.post(reverse('save_grade_ajax'), {
                'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
                'date': d.isoformat(), 'type': 'daily', 'sticker': sym,
            })
            self.assertTrue(r.json()['success'], sym)

    def test_sticker_invalid_symbol_not_saved(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
            'date': D_Q1.isoformat(), 'type': 'daily', 'sticker': '🔥',
        })
        self.assertTrue(r.json()['success'])  # invalid -> NULL -> row deleted
        self.assertFalse(Grade.objects.filter(student=self.kid).exists())

    def test_sticker_attendance_and_behavior_same_row(self):
        self.client.force_login(self.teacher_a)
        self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
            'date': D_Q1.isoformat(), 'type': 'daily',
            'attendance': '+', 'behavior_score': '5', 'sticker': '📖',
        })
        g = Grade.objects.get(student=self.kid, date=D_Q1)
        self.assertEqual(g.attendance, '+')
        self.assertEqual(g.behavior_score, 5)
        self.assertEqual(g.sticker, '📖')

    def test_sticker_teacher_without_class_assignment_403(self):
        self.client.force_login(self.teacher_b)
        r = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.kid.id, 'subject': STICKER_SUBJECT,
            'date': D_Q1.isoformat(), 'type': 'daily', 'sticker': '⭐',
        })
        self.assertEqual(r.status_code, 403)

    def test_sticker_entry_anonymous_redirects_login(self):
        r = self.client.get(self._url())
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)


# ---------------------------------------------------------------------------
# Non-graded classes — qualitative marks share the score field
# ---------------------------------------------------------------------------

class NonGradedGoldenTests(GoldenBase):

    def test_is_non_graded_contract(self):
        self.assertTrue(is_non_graded('1-А'))
        self.assertTrue(is_non_graded('0-Б'))
        self.assertFalse(is_non_graded('7-А'))

    def test_student_is_graded_property(self):
        kid = make_student(self.school_a, '1-А', 'Сабрина Юсупова')
        self.assertFalse(kid.is_graded)
        self.assertTrue(self.s1.is_graded)

    def test_qualitative_choices_constant(self):
        labels = dict(QUALITATIVE_CHOICES)
        self.assertEqual(labels['1'], 'Ғайб')
        self.assertEqual(labels['2'], 'Ҳозир')
        self.assertEqual(labels['5'], 'Аъло')

    def test_qualitative_score_stored_in_score_field(self):
        # Non-graded classes store the qualitative mark as a plain score int.
        kid = make_student(self.school_a, '1-А', 'Сабрина Юсупова')
        make_class_subject(self.school_a, '1-А', 'АЛИФБО',
                           teacher=self.teacher_a)
        self.client.force_login(self.teacher_a)
        r = self.client.post(reverse('save_grade_ajax'), {
            'student_id': kid.id, 'subject': 'АЛИФБО',
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '5',
        })
        self.assertTrue(r.json()['success'])
        self.assertEqual(Grade.objects.get(student=kid).score, 5)


# ---------------------------------------------------------------------------
# Excel import — current inline implementation in views.import_excel
# ---------------------------------------------------------------------------

class ExcelImportGoldenTests(GoldenBase):
    """Live contract of views.import_excel:
    - 'Ному насаб' column is required; other columns become subject grades;
    - Grade rows are update_or_create(student, subject, period) with NO date
      in the lookup -> one row per subject, date defaults to import day;
    - re-import is idempotent (same row updated);
    - BASELINE RISK: the view checks login only, NOT school access.
    """

    def _xlsx(self, rows):
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return SimpleUploadedFile(
            'import.xlsx', buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def _post(self, file, school=None):
        url = reverse('import_excel', args=[(school or self.school_a).id])
        return self.client.post(url, {'excel': file})

    def test_import_creates_students_and_grade_rows(self):
        f = self._xlsx([
            ['Ному насаб', 'Синф', 'Математика'],
            ['Аҳмад Қосимов', '7-Б', 8],
        ])
        self.client.force_login(self.admin)
        self._post(f)
        st = Student.objects.get(
            id=f'{self.school_a.name}__7-Б__Аҳмад Қосимов')
        g = Grade.objects.get(student=st, subject=MATH)
        self.assertEqual(g.score, 8)
        self.assertEqual(g.period, PERIOD)
        # GOLDEN: imported grades carry the import day's date.
        self.assertEqual(g.date, datetime.date.today())

    def test_import_reimport_idempotent_single_row(self):
        self.client.force_login(self.admin)
        self._post(self._xlsx([
            ['Ному насаб', 'Синф', 'Математика'],
            ['Аҳмад Қосимов', '7-Б', 8],
        ]))
        self._post(self._xlsx([
            ['Ному насаб', 'Синф', 'Математика'],
            ['Аҳмад Қосимов', '7-Б', 10],
        ]))
        grades = Grade.objects.filter(
            student__full_name='Аҳмад Қосимов', subject=MATH)
        self.assertEqual(grades.count(), 1)
        self.assertEqual(grades.first().score, 10)

    def test_import_skips_out_of_range_and_non_numeric(self):
        f = self._xlsx([
            ['Ному насаб', 'Синф', 'Математика', 'Забони тоҷикӣ'],
            ['Аҳмад Қосимов', '7-Б', 15, 'abc'],
        ])
        self.client.force_login(self.admin)
        self._post(f)
        st = Student.objects.get(full_name='Аҳмад Қосимов')
        self.assertFalse(Grade.objects.filter(student=st).exists())

    def test_import_ignores_numbered_columns(self):
        f = self._xlsx([
            ['№ мактаб', '№ синф', 'Ному насаб', 'Синф', 'Математика'],
            ['', '', 'Аҳмад Қосимов', '7-Б', 9],
        ])
        self.client.force_login(self.admin)
        self._post(f)
        st = Student.objects.get(full_name='Аҳмад Қосимов')
        subjects = list(Grade.objects.filter(student=st)
                        .values_list('subject', flat=True))
        self.assertEqual(subjects, [MATH])

    def test_import_missing_name_column_redirects(self):
        f = self._xlsx([['Ҳуҷҷат', 'Синф'], ['x', '7-Б']])
        self.client.force_login(self.admin)
        r = self._post(f)
        self.assertEqual(r.status_code, 302)
        self.assertIn('classes', r.url)

    def test_import_requires_login(self):
        r = self._post(self._xlsx([['Ному насаб', 'Синф'], ['x', '7-Б']]))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    def test_import_cross_school_no_access_check_baseline_risk(self):
        # GOLDEN BASELINE RISK (documented, not endorsed): import_excel
        # enforces @login_required only — a School B teacher CAN currently
        # write students/grades into School A. Frozen here so Phase >0 can
        # deliberately close it.
        f = self._xlsx([
            ['Ному насаб', 'Синф', 'Математика'],
            ['Аҳмад Қосимов', '7-Б', 8],
        ])
        self.client.force_login(self.teacher_b)
        r = self._post(f, school=self.school_a)
        self.assertEqual(r.status_code, 302)  # succeeded, not 403
        self.assertTrue(Student.objects.filter(
            full_name='Аҳмад Қосимов', school=self.school_a).exists())


# ---------------------------------------------------------------------------
# Student identity, gender, class-subject seeding
# ---------------------------------------------------------------------------

class StudentIdentityGoldenTests(GoldenBase):

    def test_student_pk_format(self):
        self.assertEqual(
            self.s1.id, f'{self.school_a.name}__7-А__Фирӯз Раҳимов')

    def test_student_save_normalizes_class_name(self):
        st = make_student(self.school_a, '8б', 'Абдулло Ҳакимов')
        self.assertEqual(st.class_name, '8-Б')
        self.assertIn('8-Б', st.id)

    def test_move_student_relinks_grades_and_rebuilds_pk(self):
        make_grade(self.s1, score=8, date=D_Q1)
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=9)
        old_id = self.s1.id
        moved = _move_student(self.school_a, self.s1, '8-В')
        self.assertEqual(moved.id, f'{self.school_a.name}__8-В__Фирӯз Раҳимов')
        self.assertFalse(Student.objects.filter(id=old_id).exists())
        self.assertEqual(Grade.objects.get(student=moved).score, 8)
        qg = QuarterGrade.objects.get(student=moved)
        self.assertEqual(qg.class_name, '8-В')
        self.assertEqual(qg.grade, 9)

    def test_student_save_seeds_default_class_subjects(self):
        # Creating a student in a fresh class seeds the grade-level
        # TJC_SUBJECTS as ClassSubject rows (is_default=True).
        # Grade 5 curriculum contains 'Математика' (grade 7 splits it into
        # Алгебра/Геометрия).
        school = make_school('Мактаб №55')
        make_student(school, '5-В', 'Озода Мирзоева')
        subjects = set(ClassSubject.objects.filter(
            school=school, class_name='5-В').values_list('subject', flat=True))
        self.assertIn(MATH, subjects)
        self.assertIn(TAJIK, subjects)
        self.assertTrue(all(cs.is_default for cs in ClassSubject.objects.filter(
            school=school, class_name='5-В')))


class GenderGoldenTests(GoldenBase):
    """Gender is display/statistics data today — freeze detector behavior."""

    def test_gender_auto_detect_male_patronymic(self):
        # Fixture students were auto-detected at creation time.
        self.assertEqual(self.s1.gender, 'M')

    def test_gender_auto_detect_female_patronymic(self):
        self.assertEqual(self.s2.gender, 'F')

    def test_gender_unknown_name_stays_null(self):
        st = make_student(self.school_a, '7-А', 'Дарт Вейдер')
        self.assertIn(st.gender, (None, ''))

    def test_manual_gender_preserved_on_save(self):
        st = make_student(self.school_a, '7-А', 'Аҳмад Ҷалилов', gender='F')
        self.assertEqual(st.gender, 'F')
        st.save()  # resave — detector must not override manual value
        self.assertEqual(st.gender, 'F')

    def test_gender_blank_redetects_on_save(self):
        self.s1.gender = None
        self.s1.save()
        self.assertEqual(self.s1.gender, 'M')


class StudentDerivedMetricsTests(GoldenBase):

    def test_attendance_percentage_no_journal_days_is_100(self):
        self.assertEqual(self.s1.attendance_percentage, 100.0)

    def test_attendance_percentage_counts_class_journal_days(self):
        # One journal day in the class; student absent -> 0%.
        make_grade(self.s2, score=8, date=D_Q1, attendance='+')
        make_grade(self.s1, date=D_Q1, attendance='-')
        self.assertEqual(self.s1.attendance_percentage, 0.0)

    def test_behavior_status_labels(self):
        self.assertEqual(self.s1.behavior_status, 'Маълумот нест')
        make_grade(self.s1, score=8, date=D_Q1, behavior=5)
        self.assertEqual(self.s1.behavior_status, 'Намунавӣ')

    def test_total_excused_unexcused(self):
        make_grade(self.s1, date=D_Q1, attendance='-')
        make_grade(self.s1, date=D_Q1, attendance='+',
                   subject='ЗАБОНИ ТОҶИКӢ')
        self.assertEqual(self.s1.total_unexcused, 1)
        self.assertEqual(self.s1.total_excused, 1)


# ---------------------------------------------------------------------------
# ensure_class_subjects — seeding + the non-official deactivation rule that
# Phase 6 (School #5 whitelist) is scheduled to change
# ---------------------------------------------------------------------------

class EnsureClassSubjectsGoldenTests(GoldenBase):

    def test_seeds_official_subjects_for_class_level(self):
        # Grade 7 curriculum: Алгебра/Геометрия (no 'Математика').
        school = make_school('Мактаб №66')
        ensure_class_subjects(school, '7-Г')
        subjects = set(ClassSubject.objects.filter(
            school=school, class_name='7-Г').values_list('subject', flat=True))
        self.assertIn('АЛГЕБРА', subjects)
        self.assertIn('ГЕОМЕТРИЯ', subjects)
        self.assertNotIn(MATH, subjects)
        # Grade 5 curriculum: 'Математика' is official.
        ensure_class_subjects(school, '5-Г')
        subjects5 = set(ClassSubject.objects.filter(
            school=school, class_name='5-Г').values_list('subject', flat=True))
        self.assertIn(MATH, subjects5)

    def test_idempotent_second_run(self):
        ensure_class_subjects(self.school_a, '7-А')
        before = ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А').count()
        ensure_class_subjects(self.school_a, '7-А')
        after = ClassSubject.objects.filter(
            school=self.school_a, class_name='7-А').count()
        self.assertEqual(before, after)

    def test_deactivates_non_official_subject_current_behavior(self):
        # GOLDEN (changes in Phase 6): any ClassSubject outside the grade's
        # official TJC_SUBJECTS list is deactivated by ensure_class_subjects.
        custom = make_class_subject(
            self.school_a, '7-А', CUSTOM_SUBJECT, is_active=True)
        ensure_class_subjects(self.school_a, '7-А')
        custom.refresh_from_db()
        self.assertFalse(custom.is_active)

    def test_preserves_approved_deactivation(self):
        # GOLDEN: the preserve/clear-teacher branch inside ensure_class_subjects
        # only executes for subjects in the official list — the deactivation
        # request must be on an official subject (Тоҷикӣ) to hit it.
        cs = make_class_subject(self.school_a, '7-А', TAJIK,
                                teacher=self.teacher_a)
        make_deactivation_request(cs, self.zavuch_a, status='approved')
        ensure_class_subjects(self.school_a, '7-А')
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)
        self.assertIsNone(cs.teacher)
        self.assertIsNone(cs.allocated_teacher)

    def test_non_official_subject_keeps_teacher_on_bulk_deactivation(self):
        # GOLDEN quirk: a non-official subject is deactivated by the bulk
        # `.exclude(subject__in=official).update(is_active=False)` at the end
        # of ensure_class_subjects — a queryset update that does NOT clear
        # teacher/allocated_teacher the way the per-row preserve branch does.
        cs = make_class_subject(self.school_a, '7-А', CUSTOM_SUBJECT,
                                teacher=self.teacher_a, is_active=True)
        ensure_class_subjects(self.school_a, '7-А')
        cs.refresh_from_db()
        self.assertFalse(cs.is_active)
        self.assertEqual(cs.teacher, self.teacher_a)

    def test_pending_request_does_not_deactivate(self):
        cs = ClassSubject.objects.get(
            school=self.school_a, class_name='7-А', subject=TAJIK)
        make_deactivation_request(cs, self.zavuch_a, status='pending')
        ensure_class_subjects(self.school_a, '7-А')
        cs.refresh_from_db()
        self.assertTrue(cs.is_active)
