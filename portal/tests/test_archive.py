"""🗄️ Бойгонӣ ва истисно — two distinct student statuses.

«Рафт» archives the Student row (is_archived): it leaves every roster,
journal, norm, ranking and count, while Grade/QuarterGrade history and
the student_detail page survive. «Дар давлати дигар муваққатан таҳсил
мекунад» (StudentExemption) keeps the student on the roster and in the
school count, marks them blue in the journals and removes them from the
norm/ungraded denominators only.
"""
import datetime

from django.urls import reverse

from portal.models import Grade, QuarterGrade, Student, StudentExemption, StudentMovement

from .helpers import D_Q1, GoldenBase, MATH, make_grade, make_quarter_grade


class ArchiveLifecycleTests(GoldenBase):
    """remove_student → soft-delete; roster/queries hide the row."""

    def _remove(self, student):
        self.client.force_login(self.zavuch_a)
        return self.client.post(
            reverse('remove_student', args=[self.school_a.id, '7-А']),
            {'student_id': student.id})

    def test_remove_archives_and_preserves_history(self):
        make_grade(self.s2, score=9)
        make_quarter_grade(self.s2, quarter=1, grade=9)
        self._remove(self.s2)

        row = Student.all_objects.get(id=self.s2.id)
        self.assertTrue(row.is_archived)
        self.assertIsNotNone(row.archived_at)
        # Grades and quarter results survive untouched.
        self.assertEqual(Grade.objects.filter(student=row).count(), 1)
        self.assertEqual(
            QuarterGrade.objects.filter(student=row).count(), 1)
        # Departure movement recorded.
        m = StudentMovement.objects.get(
            school=self.school_a, movement_type='departure')
        self.assertEqual(m.student_name, 'Мадина Каримова')
        # Default manager hides the row.
        self.assertFalse(Student.objects.filter(id=self.s2.id).exists())

    def test_archived_hidden_from_class_roster_and_count(self):
        self._remove(self.s2)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        self.assertEqual(resp.context['total_count'], 1)
        self.assertEqual(
            [s.id for s in resp.context['students']], [self.s1.id])
        self.assertEqual(
            [s.id for s in resp.context['archived_students']],
            [self.s2.id])

    def test_archived_absent_from_grade_entry_journal(self):
        self._remove(self.s2)
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse(
            'grade_entry',
            args=[self.school_a.id, '7-А', 'Математика']))
        self.assertEqual(
            [s.id for s in resp.context['students']], [self.s1.id])

    def test_archived_grades_excluded_from_aggregates(self):
        # An archived student's stored grade must not feed nazorat/GPA
        # aggregates — otherwise a departed student would keep skewing
        # the school's current statistics.
        make_grade(self.s2, score=10)
        self._remove(self.s2)
        from portal.utils import school_gpa
        self.client.force_login(self.zavuch_a)
        # Only s1 (no grades) remains -> GPA is 0, not the archived 10.
        self.assertEqual(school_gpa(self.school_a), 0.0)

    def test_archived_student_detail_stays_readable(self):
        self._remove(self.s2)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('student_detail', args=[self.s2.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Бойгонӣ')

    def test_save_grade_to_archived_student_rejected(self):
        self._remove(self.s2)
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s2.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'type': 'daily', 'score': '8'})
        self.assertFalse(resp.json()['success'])
        self.assertEqual(
            Grade.objects.filter(student_id=self.s2.id).count(), 0)


class RestoreTests(GoldenBase):
    """Re-enrollment paths — no duplicates, history survives."""

    def _archive(self, student):
        student.is_archived = True
        student.archived_at = datetime.datetime(2026, 1, 10,
                                                tzinfo=datetime.timezone.utc)
        student.save(update_fields=['is_archived', 'archived_at'])

    def test_restore_student_reactivates_same_class(self):
        self._archive(self.s2)
        self.client.force_login(self.zavuch_a)
        resp = self.client.post(
            reverse('restore_student', args=[self.school_a.id, '7-А']),
            {'student_id': self.s2.id})
        self.assertEqual(resp.status_code, 302)
        row = Student.objects.get(id=self.s2.id)
        self.assertFalse(row.is_archived)
        self.assertIsNone(row.archived_at)
        self.assertTrue(StudentMovement.objects.filter(
            school=self.school_a, movement_type='arrival',
            student_name='Мадина Каримова').exists())
        # No duplicate row.
        self.assertEqual(
            Student.all_objects.filter(
                school=self.school_a, full_name='Мадина Каримова').count(),
            1)

    def test_add_student_same_id_reactivates_archived_row(self):
        make_grade(self.s2, score=7)
        self._archive(self.s2)
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_student', args=[self.school_a.id, '7-А']),
            {'full_name': 'Мадина Каримова'})
        self.assertFalse(Student.objects.get(id=self.s2.id).is_archived)
        # Same pk, history still linked.
        self.assertEqual(
            Grade.objects.filter(student_id=self.s2.id).count(), 1)
        self.assertEqual(
            Student.all_objects.filter(
                school=self.school_a, full_name='Мадина Каримова').count(),
            1)

    def test_add_student_archived_other_class_moves_and_restores(self):
        # Student archived in 7-А re-enrolled into 7-Б: _move_student
        # re-keys the row, grades follow, no duplicate remains.
        make_grade(self.s2, score=8)
        self._archive(self.s2)
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_student', args=[self.school_a.id, '7-Б']),
            {'full_name': 'Мадина Каримова'})
        rows = Student.all_objects.filter(
            school=self.school_a, full_name='Мадина Каримова')
        self.assertEqual(rows.count(), 1)
        moved = rows.get()
        self.assertEqual(moved.class_name, '7-Б')
        self.assertFalse(moved.is_archived)
        self.assertEqual(Grade.objects.filter(student=moved).count(), 1)

    def test_add_student_duplicate_active_still_warns(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(
            reverse('add_student', args=[self.school_a.id, '7-А']),
            {'full_name': 'Фирӯз Раҳимов'})
        self.assertEqual(
            Student.all_objects.filter(
                school=self.school_a, full_name='Фирӯз Раҳимов').count(),
            1)


class ExemptionTests(GoldenBase):
    """«Дар давлати дигар муваққатан таҳсил мекунад» — roster stays."""

    def _edit(self, student, **extra):
        self.client.force_login(self.zavuch_a)
        data = {'student_id': student.id, 'full_name': student.full_name,
                'class_name': '7-А'}
        data.update(extra)
        return self.client.post(
            reverse('edit_student', args=[self.school_a.id, '7-А']), data)

    def test_edit_student_sets_exemption(self):
        self._edit(self.s2, exempt_abroad='on',
                   exempt_document='Фармон №15', exempt_end_date='')
        ex = StudentExemption.objects.get(student_id=self.s2.id)
        self.assertEqual(ex.reason, 'abroad')
        self.assertEqual(ex.document, 'Фармон №15')
        self.assertIsNone(ex.end_date)
        self.assertIsNone(ex.cancelled_at)
        self.assertEqual(ex.created_by, self.zavuch_a)

    def test_edit_student_uncheck_cancels_exemption(self):
        self._edit(self.s2, exempt_abroad='on', exempt_document='Ҳуҷҷат')
        self._edit(self.s2)
        ex = StudentExemption.objects.get(student_id=self.s2.id)
        self.assertIsNotNone(ex.cancelled_at)
        self.assertEqual(ex.cancelled_by, self.zavuch_a)

    def test_exempt_student_stays_on_roster_and_count(self):
        self._edit(self.s2, exempt_abroad='on')
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        # Count unchanged — the exemption does NOT shrink the roster.
        self.assertEqual(resp.context['total_count'], 2)
        flagged = {s.id for s in resp.context['students']
                   if getattr(s, 'exemption', None)}
        self.assertEqual(flagged, {self.s2.id})

    def test_exempt_excluded_from_journal_norm_denominator(self):
        from portal.views import _journal_norm_status
        students = list(Student.objects.filter(
            school=self.school_a, class_name='7-А'))
        base = _journal_norm_status(
            '7-А', MATH, self.cs_a, students, datetime.date.today())
        with_exempt = _journal_norm_status(
            '7-А', MATH, self.cs_a, students, datetime.date.today(),
            exempt_ids={self.s2.id})
        # Roster unchanged; the norm denominator shrinks by one student.
        self.assertEqual(base['students'], with_exempt['students'])
        self.assertEqual(with_exempt['students_exempt'], 1)
        self.assertEqual(
            base['required'], with_exempt['required'] + base['norm'])

    def test_expired_exemption_no_longer_exempts(self):
        StudentExemption.objects.create(
            student=self.s2, reason='abroad',
            start_date=datetime.date(2025, 9, 1),
            end_date=datetime.date(2025, 10, 1))
        from portal.exemptions import active_exemption_map
        today = datetime.date.today()
        self.assertNotIn(
            self.s2.id, active_exemption_map([self.s2.id], today=today))
        # But it WAS active inside its window.
        inside = datetime.date(2025, 9, 15)
        self.assertIn(
            self.s2.id, active_exemption_map([self.s2.id], today=inside))

    def test_archived_exempt_student_still_hidden(self):
        # Both flags at once: archive wins — the roster must not leak the
        # student even though an exemption row exists.
        StudentExemption.objects.create(
            student=self.s2, reason='abroad',
            start_date=datetime.date(2025, 9, 1))
        self.s2.is_archived = True
        self.s2.save(update_fields=['is_archived'])
        self.assertFalse(Student.objects.filter(id=self.s2.id).exists())
