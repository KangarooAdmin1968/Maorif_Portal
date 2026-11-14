"""Таҳсил дар хона — home-education assignments.

A home-education student stays formally attached to their class but is
taught through an is_home TeachingGroup: hidden from the class journal
(«Дар хона таҳсил мегирад» exemption), reachable through the assigned
teacher's own home journal, and credited with the group's explicit
«Соат дар як ҳафта» in workload/tabel as «Таҳсил дар хона — ФАН».

Fixture: School A, class 7-А, МАТЕМАТИКА assigned to teacher_a (the
class teacher); teacher_home gets an is_home group containing s_home.
"""
import datetime

from django.urls import reverse

from portal.curriculum_hours import group_weekly_hours, group_min_norm
from portal.exemptions import home_student_ids
from portal.models import (
    ClassSubject, Student, StudentExemption, SubjectGroupMembership,
    TeachingGroup,
)
from portal.tabel import teacher_workload
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_grade, make_student,
    make_teacher_profile, make_user,
)
from portal.views import visible_students_for, teacher_group_for


class HomeFixture(GoldenBase):
    """7-А МАТЕМАТИКА: class teacher_a + one home group for teacher_home."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs_math = cls.cs_a  # 7-А МАТЕМАТИКА, teacher=teacher_a
        cls.s_home = make_student(cls.school_a, '7-А', 'Аҳмад Ҳофизов')
        cls.teacher_home = make_user(
            'teacher_home', role='teacher', school=cls.school_a)
        cls.profile_home = make_teacher_profile(
            cls.teacher_home, cls.school_a, full_name='Омӯзгори хонагӣ')
        cls.home_group = TeachingGroup.objects.create(
            class_subject=cls.cs_math, label='Таҳсил дар хона',
            teacher=cls.profile_home, is_home=True, hours_per_week=2)
        SubjectGroupMembership.objects.create(
            class_subject=cls.cs_math, group=cls.home_group,
            student=cls.s_home)
        StudentExemption.objects.create(
            student=cls.s_home, reason='home', start_date=D_Q1 - datetime.timedelta(days=60))


class HomeRosterTests(HomeFixture):
    """Roster scoping: home students leave the class journal, enter the
    home journal, and never break the regular teacher's view."""

    def test_class_teacher_journal_hides_home_student(self):
        visible = visible_students_for(self.teacher_a, self.cs_math)
        self.assertNotIn(self.s_home, {s.id for s in visible})
        self.assertIn(self.s1.id, {s.id for s in visible})

    def test_class_teacher_keeps_full_access(self):
        # A home-only group must NOT flip the CS into group mode — the
        # class teacher still sees every non-home student.
        visible = visible_students_for(self.teacher_a, self.cs_math)
        self.assertEqual(
            {s.id for s in visible},
            {self.s1.id, self.s2.id})

    def test_home_teacher_sees_only_home_students(self):
        visible = visible_students_for(self.teacher_home, self.cs_math)
        self.assertEqual({s.id for s in visible}, {self.s_home.id})

    def test_grouped_cs_home_teacher_still_scoped(self):
        # Canonical groups + a home group: each teacher sees only their
        # own members.
        profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')
        g1 = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1',
            teacher=profile_a)
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_math, group=g1, student=self.s1)
        visible_class = visible_students_for(self.teacher_a, self.cs_math)
        self.assertEqual({s.id for s in visible_class}, {self.s1.id})
        visible_home = visible_students_for(self.teacher_home, self.cs_math)
        self.assertEqual({s.id for s in visible_home}, {self.s_home.id})

    def test_home_student_never_in_ungrouped_teacher_view(self):
        # A teacher with NO group on a home-only CS must still not see
        # the home student.
        other = make_user('teacher_x', role='teacher', school=self.school_a)
        visible = visible_students_for(other, self.cs_math)
        self.assertNotIn(self.s_home.id, {s.id for s in visible})


class HomeJournalTests(HomeFixture):
    """Journal endpoints: the home group resolves to its own journal."""

    def _url(self, subject=MATH):
        return reverse('grade_entry', args=[self.school_a.id, '7-А', subject])

    def test_home_teacher_opens_journal(self):
        self.client.force_login(self.teacher_home)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Аҳмад Ҳофизов')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_class_teacher_journal_excludes_home_student(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Аҳмад Ҳофизов')
        self.assertContains(resp, 'Фирӯз Раҳимов')

    def test_home_journal_uses_group_hours_norm(self):
        # 2 h/week -> 5 marks per quarter, not the class subject's 7.
        self.assertEqual(group_weekly_hours(self.home_group, self.cs_math), 2)
        self.assertEqual(group_min_norm(self.home_group, self.cs_math), 5)

    def test_home_journal_page_title(self):
        self.client.force_login(self.teacher_home)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Таҳсил дар хона')

    def test_home_lesson_scoping(self):
        # Home teacher's lessons land on the home group; the class
        # journal never lists them.
        self.client.force_login(self.teacher_home)
        resp = self.client.post(reverse('lesson_save'), {
            'class_subject_id': self.cs_math.id,
            'date': D_Q1.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)
        from portal.models import Lesson
        lesson = Lesson.objects.get(pk=resp.json()['lesson']['id'])
        self.assertEqual(lesson.group_id, self.home_group.id)

        self.client.force_login(self.teacher_a)
        resp = self.client.get(self._url() + f'?date={D_Q1.isoformat()}')
        self.assertNotContains(resp, f'selectLesson(\'{lesson.id}\')')


class HomeExemptionTests(HomeFixture):
    def test_home_student_ids(self):
        self.assertIn(
            self.s_home.id,
            home_student_ids([self.s1.id, self.s_home.id]))
        self.assertNotIn(
            self.s1.id, home_student_ids([self.s1.id, self.s_home.id]))

    def test_reason_label(self):
        ex = StudentExemption.objects.get(student=self.s_home, reason='home')
        self.assertEqual(ex.get_reason_display(), 'Дар хона таҳсил мегирад')


class HomeWorkloadTests(HomeFixture):
    """Workload/tabel: the home group lands as its own «Таҳсил дар хона
    — ФАН» line with the group's explicit hours."""

    def test_teacher_workload_home_row(self):
        rows, unassigned, _ = teacher_workload(self.school_a)
        home_rows = [r for r in rows if r['teacher'].id == self.profile_home.id]
        self.assertEqual(len(home_rows), 1)
        items = home_rows[0]['items']
        self.assertEqual(
            items[0]['subject'], f'Таҳсил дар хона — {MATH}')
        self.assertEqual(home_rows[0]['total_hours'], 2)

    def test_class_teacher_hours_unchanged(self):
        # The home group adds to the home teacher, never doubles the
        # class teacher's load.
        rows, _, _ = teacher_workload(self.school_a)
        user_profile = {r['teacher'].id: r for r in rows}
        profile_a = self.cs_math.allocated_teacher or \
            __import__('portal.models', fromlist=['TeacherProfile'])\
            .TeacherProfile.objects.filter(
                user=self.teacher_a, school=self.school_a).first()
        if profile_a is not None:
            class_row = user_profile[profile_a.id]
            self.assertNotIn(
                f'Таҳсил дар хона — {MATH}',
                [i['subject'] for i in class_row['items']])


class HomeAdminViewTests(HomeFixture):
    """The «Таҳсил дар хона» page and its add/remove endpoints."""

    def _add(self, user, **overrides):
        self.client.force_login(user)
        payload = {
            'class_name': '7-А',
            'subject': MATH,
            'teacher_id': self.profile_home.id,
            'hours': '3',
            'document': 'Фармоиш №12',
        }
        payload.update(overrides)
        return self.client.post(reverse('home_education_add'), payload)

    def test_zavuch_adds_new_named_student(self):
        resp = self._add(self.zavuch_a, new_name='Саид Неъматов')
        self.assertEqual(resp.status_code, 302)
        st = Student.objects.get(
            school=self.school_a, class_name='7-А',
            full_name='Саид Неъматов')
        self.assertIn(st.id, home_student_ids([st.id]))
        membership = SubjectGroupMembership.objects.get(
            class_subject=self.cs_math, student=st)
        self.assertTrue(membership.group.is_home)
        self.assertEqual(membership.group.hours_per_week, 3)
        # Same teacher + subject reuses one home group.
        self.assertEqual(membership.group_id, self.home_group.id)
        # Updating the same assignment bumps the group's hours.
        self.home_group.refresh_from_db()
        self.assertEqual(self.home_group.hours_per_week, 3)

    def test_zavuch_adds_existing_student(self):
        new_profile = make_teacher_profile(
            make_user('teacher_h2', role='teacher', school=self.school_a),
            self.school_a, full_name='Омӯзгори дуввум')
        resp = self._add(
            self.zavuch_a, student_id=self.s2.id,
            teacher_id=new_profile.id)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(self.s2.id, home_student_ids([self.s2.id]))

    def test_student_in_canonical_group_rejected(self):
        profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')
        g1 = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи 1',
            teacher=profile_a)
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_math, group=g1, student=self.s1)
        self._add(self.zavuch_a, student_id=self.s1.id)
        self.assertNotIn(self.s1.id, home_student_ids([self.s1.id]))
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_math, student=self.s1).count(), 1)

    def test_duplicate_membership_rejected(self):
        self._add(self.zavuch_a, student_id=self.s_home.id)
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_math, student=self.s_home).count(), 1)

    def test_teacher_of_other_school_rejected(self):
        profile_b = make_teacher_profile(
            self.teacher_b, self.school_b, full_name='Омӯзгори Б')
        self._add(self.zavuch_a, new_name='Наврӯз Алиев',
                  teacher_id=profile_b.id)
        self.assertFalse(
            Student.objects.filter(full_name='Наврӯз Алиев').exists())

    def test_regular_teacher_cannot_add(self):
        self._add(self.teacher_a, new_name='Наврӯз Алиев')
        self.assertFalse(
            Student.objects.filter(full_name='Наврӯз Алиев').exists())

    def test_remove_cancels_exemption(self):
        membership = SubjectGroupMembership.objects.get(
            class_subject=self.cs_math, student=self.s_home)
        self.client.force_login(self.zavuch_a)
        resp = self.client.post(
            reverse('home_education_remove'),
            {'membership_id': membership.id})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(self.s_home.id, home_student_ids([self.s_home.id]))
        # Empty home group deactivates — history stays.
        self.home_group.refresh_from_db()
        self.assertFalse(self.home_group.is_active)
        # Grade history and the student record are untouched.
        self.assertTrue(
            Student.objects.filter(pk=self.s_home.pk).exists())

    def test_home_education_page_lists_members(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('home_education'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Таҳсил дар хона')
        self.assertContains(resp, 'Аҳмад Ҳофизов')


class HomeIsolationTests(HomeFixture):
    """School isolation: a zavuch manages only their own school's rows."""

    def test_other_school_zavuch_cannot_manage(self):
        zavuch_b = make_user('zavuch_9', role='teacher', school=self.school_b)
        self.client.force_login(zavuch_b)
        resp = self.client.get(reverse('home_education'))
        # School B has no home-education rows — page renders, empty.
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Аҳмад Ҳофизов')
        # And POSTs against School A rows cannot leak: zavuch_b's school
        # context is School B, so the membership lookup fails.
        membership = SubjectGroupMembership.objects.get(
            class_subject=self.cs_math, student=self.s_home)
        resp = self.client.post(
            reverse('home_education_remove'),
            {'membership_id': membership.id})
        self.assertTrue(
            SubjectGroupMembership.objects.filter(pk=membership.pk).exists())

    def test_superuser_school_picker(self):
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse('home_education') + f'?school_id={self.school_a.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Аҳмад Ҳофизов')

    def test_superuser_without_school_gets_picker(self):
        # Landing without ?school_id= must show the school selector,
        # not the "access limited" error — the page exists for picking.
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('home_education'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Интихоби муассиса')
        self.assertNotContains(resp, 'Дастрасӣ маҳдуд аст')

    def test_regular_teacher_still_blocked(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('home_education'))
        self.assertRedirects(resp, '/', fetch_redirect_response=False)


class HomeMonitoringTests(HomeFixture):
    """Denominators: home students leave every class-level count."""

    def test_norm_denominator_excludes_home(self):
        # 7-А has 3 students (s1, s2, s_home); s_home's marks must never
        # inflate the class teacher's norm completion.
        make_grade(self.s1, score=4)
        make_grade(self.s_home, score=5)
        from portal.views import _journal_norm_status
        journal = _journal_norm_status(
            '7-А', MATH, self.cs_math,
            list(visible_students_for(self.teacher_a, self.cs_math)),
            datetime.date.today())
        self.assertEqual(journal['students'], 2)

    def test_quarter_marks_still_flow(self):
        # Home students' marks still land in the official class record:
        # Grade rows keyed to the student's own class feed the standard
        # quarter-documents pipeline — only journals and norm math
        # exclude them.
        g = make_grade(self.s_home, subject=MATH, score=5)
        self.assertEqual(g.student.class_name, '7-А')
        self.assertEqual(g.subject, MATH)
