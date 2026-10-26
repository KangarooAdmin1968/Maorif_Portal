"""assessment_toggle + control-slot journal writes.

The «Н» column marker is a single reversible action:
    mark   — create an auto-numbered untyped control Assessment for the
             (date, lesson) slot and re-link its plain Grade rows to it;
    unmark — detach the linked rows back into plain Ҷорӣ rows and delete
             the (single) Assessment.

Also covered: the shared journal-cell pipeline writes to a control slot's
linked row (a control column in the monthly journal), while typed work
types keep their dedicated daily-journal entry.
"""
import json

from django.urls import reverse

from portal.models import Assessment, Grade
from portal.tests.helpers import (
    D_Q1, D_Q1_B, MATH, GoldenBase, lock_quarter, make_grade,
)

TOGGLE_URL = reverse('assessment_toggle')
MONTHLY_URL = reverse('monthly_save')


def post_toggle(client, cs, date=D_Q1, lesson_id=''):
    return client.post(TOGGLE_URL, {
        'class_subject_id': cs.id,
        'date': date.isoformat(),
        'lesson_id': lesson_id,
    })


def monthly_payload(client, items):
    return client.post(MONTHLY_URL, {'payload': json.dumps(items)})


def item(student, date=D_Q1, lesson_id='', **fields):
    out = {
        'key': f"{student.id}|{date.isoformat()}|{lesson_id}",
        'student_id': student.id,
        'subject': MATH,
        'date': date.isoformat(),
        'lesson_id': lesson_id,
    }
    out.update(fields)
    return out


class ToggleMarkTests(GoldenBase):
    def test_mark_empty_slot_creates_untyped_assessment(self):
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['control'])
        self.assertEqual(data['converted'], 0)
        a = Assessment.objects.get(id=data['assessment']['id'])
        self.assertEqual(a.category, 'control')
        self.assertEqual(a.number, 1)
        self.assertEqual(a.title, '')
        self.assertFalse(a.is_ajm)
        self.assertEqual(a.work_type, '')
        self.assertEqual(a.date, D_Q1)
        self.assertEqual(a.quarter, 1)

    def test_mark_auto_numbers_after_existing(self):
        Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1_B, quarter=1,
            category='control', number=2,
        )
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a)
        a = Assessment.objects.get(id=resp.json()['assessment']['id'])
        self.assertEqual(a.number, 3)

    def test_mark_converts_existing_current_grades(self):
        g1 = make_grade(self.s1, score=9, date=D_Q1,
                        attendance='-', behavior=4)
        make_grade(self.s2, score=7, date=D_Q1)
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['converted'], 2)
        aid = data['assessment']['id']
        g1.refresh_from_db()
        self.assertEqual(g1.assessment_id, aid)
        self.assertEqual(g1.score, 9)
        self.assertEqual(g1.attendance, '-')
        self.assertEqual(g1.behavior_score, 4)
        # The response carries display values for the converted rows.
        self.assertEqual(data['grades'][str(self.s1.id)], '9')
        self.assertEqual(data['grades'][str(self.s2.id)], '7')
        self.assertEqual(data['marks'][str(self.s1.id)]['att'], '-')

    def test_second_toggle_unmarks(self):
        """The same action is a toggle: mark on an empty slot, then the
        second call unmarks it and removes the assessment."""
        self.client.force_login(self.teacher_a)
        post_toggle(self.client, self.cs_a)
        r2 = post_toggle(self.client, self.cs_a).json()
        self.assertTrue(r2['success'])
        self.assertFalse(r2['control'])
        self.assertEqual(Assessment.objects.filter(
            class_subject=self.cs_a, category='control').count(), 0)


class ToggleUnmarkTests(GoldenBase):
    def test_unmark_detaches_rows_and_deletes_assessment(self):
        self.client.force_login(self.teacher_a)
        mark = post_toggle(self.client, self.cs_a).json()
        aid = mark['assessment']['id']
        make_grade(self.s1, score=8, date=D_Q1,
                   attendance='+', behavior=5)
        Grade.objects.filter(
            student=self.s1, subject=MATH, date=D_Q1,
            assessment__isnull=True).update(assessment_id=aid)
        resp = post_toggle(self.client, self.cs_a)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['control'])
        self.assertFalse(Assessment.objects.filter(id=aid).exists())
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertIsNone(g.assessment_id)
        self.assertEqual(g.score, 8)
        self.assertEqual(g.attendance, '+')
        self.assertEqual(g.behavior_score, 5)

    def test_unmark_merges_into_existing_plain_row(self):
        """If a plain row and a linked row coexist for one student, the
        unmark merge keeps the plain row and fills only its empty fields."""
        self.client.force_login(self.teacher_a)
        mark = post_toggle(self.client, self.cs_a).json()
        aid = mark['assessment']['id']
        linked = make_grade(self.s1, score=9, date=D_Q1, attendance='-')
        linked.assessment_id = aid
        linked.save()
        plain = make_grade(self.s1, score=None, date=D_Q1, behavior=3)
        resp = post_toggle(self.client, self.cs_a)
        self.assertTrue(resp.json()['success'])
        plain.refresh_from_db()
        # Plain row keeps its own behavior, gains the missing score/att.
        self.assertEqual(plain.score, 9)
        self.assertEqual(plain.attendance, '-')
        self.assertEqual(plain.behavior_score, 3)
        self.assertFalse(Grade.objects.filter(assessment_id=aid).exists())
        # Exactly one row survives for this student on the slot.
        self.assertEqual(Grade.objects.filter(
            student=self.s1, date=D_Q1).count(), 1)

    def test_unmark_refused_when_multiple_assessments(self):
        Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1, quarter=1,
            category='control', number=1)
        Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1, quarter=1,
            category='control', number=2)
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a)
        self.assertFalse(resp.json()['success'])
        self.assertEqual(Assessment.objects.filter(
            class_subject=self.cs_a, category='control').count(), 2)


class ToggleSafetyTests(GoldenBase):
    def test_locked_quarter_refused(self):
        lock_quarter(self.school_a, quarter=1)
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a).exists())

    def test_other_school_teacher_refused(self):
        self.client.force_login(self.teacher_b)
        resp = post_toggle(self.client, self.cs_a)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a).exists())

    def test_forged_lesson_refused(self):
        # A lesson of a different ClassSubject must not host the toggle.
        other_cs = self.cs_b
        from portal.models import Lesson
        foreign_lesson = Lesson.objects.create(
            class_subject=other_cs, date=D_Q1, lesson_number=1)
        self.client.force_login(self.teacher_a)
        resp = post_toggle(self.client, self.cs_a, lesson_id=foreign_lesson.id)
        self.assertFalse(resp.json()['success'])
        self.assertFalse(Assessment.objects.filter(
            class_subject=self.cs_a).exists())


class MonthlyControlCellTests(GoldenBase):
    """monthly_save writes onto the control slot's linked row."""

    def _mark(self):
        self.client.force_login(self.teacher_a)
        return post_toggle(self.client, self.cs_a).json()['assessment']['id']

    def test_score_write_lands_on_linked_row(self):
        aid = self._mark()
        resp = monthly_payload(self.client, [item(self.s1, score='9')])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 1)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertEqual(g.assessment_id, aid)
        self.assertEqual(g.score, 9)
        # No plain row was left behind in the control slot.
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=D_Q1, assessment__isnull=True).exists())

    def test_attendance_and_behavior_on_linked_row(self):
        aid = self._mark()
        resp = monthly_payload(self.client, [
            item(self.s1, attendance='-', behavior_score='3')])
        self.assertEqual(resp.json()['saved'], 1)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertEqual(g.assessment_id, aid)
        self.assertEqual(g.attendance, '-')
        self.assertEqual(g.behavior_score, 3)

    def test_typed_assessment_slot_rejected(self):
        Assessment.objects.create(
            class_subject=self.cs_a, date=D_Q1, quarter=1,
            category='control', number=1, work_type='dictation')
        self.client.force_login(self.teacher_a)
        resp = monthly_payload(self.client, [item(self.s1, score='9')])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 0)
        self.assertFalse(data['results'][0]['ok'])
        self.assertFalse(Grade.objects.filter(
            student=self.s1, date=D_Q1).exists())

    def test_plain_slot_still_writes_plain_row(self):
        self.client.force_login(self.teacher_a)
        resp = monthly_payload(self.client, [item(self.s1, score='8')])
        self.assertEqual(resp.json()['saved'], 1)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertIsNone(g.assessment_id)
        self.assertEqual(g.score, 8)

    def test_unmark_then_write_goes_to_plain_row(self):
        aid = self._mark()
        post_toggle(self.client, self.cs_a)   # unmark
        resp = monthly_payload(self.client, [item(self.s1, score='7')])
        self.assertEqual(resp.json()['saved'], 1)
        g = Grade.objects.get(student=self.s1, date=D_Q1)
        self.assertIsNone(g.assessment_id)
        self.assertEqual(g.score, 7)
