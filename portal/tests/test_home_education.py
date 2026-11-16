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
    D_Q1, MATH, TAJIK, GoldenBase, make_class_subject, make_grade,
    make_student, make_teacher_profile, make_user,
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


class DualRoleTests(GoldenBase):
    """Dual-role teacher: the CS's regular class teacher AND the owner of
    its таҳсил дар хона group on the same ClassSubject.

    Regression: teacher_group_for() alone trapped such a teacher inside
    the home journal — the class roster collapsed to the home students
    and shared class lessons refused her writes. The plain link must
    open the class journal; the home journal stays reachable via
    ?group=<id> (journal_group_for).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs_math = cls.cs_a  # 7-А МАТЕМАТИКА, class teacher = teacher_a
        cls.s_home = make_student(cls.school_a, '7-А', 'Аҳмад Ҳофизов')
        cls.profile_a = make_teacher_profile(
            cls.teacher_a, cls.school_a, full_name='Омӯзгор А')
        cls.home_group = TeachingGroup.objects.create(
            class_subject=cls.cs_math, label='Таҳсил дар хона',
            teacher=cls.profile_a, is_home=True, hours_per_week=2)
        SubjectGroupMembership.objects.create(
            class_subject=cls.cs_math, group=cls.home_group,
            student=cls.s_home)
        StudentExemption.objects.create(
            student=cls.s_home, reason='home',
            start_date=D_Q1 - datetime.timedelta(days=60))

    def _url(self):
        return reverse('grade_entry', args=[self.school_a.id, '7-А', MATH])

    def test_journal_group_resolution(self):
        from portal.views import journal_group_for, _GROUP_INVALID
        # The plain link opens the class journal for the dual-role teacher.
        self.assertIsNone(journal_group_for(self.teacher_a, self.cs_math))
        # ?group=<own> opens the home journal explicitly.
        self.assertEqual(
            journal_group_for(
                self.teacher_a, self.cs_math, self.home_group.id),
            self.home_group)
        # Foreign or malformed ids are rejected, never silently widened.
        self.assertIs(
            journal_group_for(self.teacher_a, self.cs_math, '999999'),
            _GROUP_INVALID)
        self.assertIs(
            journal_group_for(self.teacher_a, self.cs_math, 'x'),
            _GROUP_INVALID)

    def test_pure_home_teacher_resolution_unchanged(self):
        from portal.views import journal_group_for
        th = make_user('teacher_h2', role='teacher', school=self.school_a)
        prof = make_teacher_profile(th, self.school_a)
        g2 = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Таҳсил дар хона 2',
            teacher=prof, is_home=True)
        # Only the home group, no class role -> home journal by default.
        self.assertEqual(journal_group_for(th, self.cs_math), g2)

    def test_class_journal_default_shows_class_roster(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Аҳмад Ҳофизов')

    def test_home_journal_via_group_param(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(self._url() + f'?group={self.home_group.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Аҳмад Ҳофизов')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_foreign_group_param_rejected(self):
        self.client.force_login(self.teacher_a)
        other = make_user('teacher_y', role='teacher', school=self.school_a)
        prof = make_teacher_profile(other, self.school_a)
        foreign = TeachingGroup.objects.create(
            class_subject=self.cs_math, label='Гурӯҳи дигар', teacher=prof)
        resp = self.client.get(self._url() + f'?group={foreign.id}')
        self.assertEqual(resp.status_code, 403)

    def test_monthly_journal_dual_contexts(self):
        self.client.force_login(self.teacher_a)
        url = reverse(
            'monthly_journal', args=[self.school_a.id, '7-А', MATH])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Аҳмад Ҳофизов')
        resp = self.client.get(url + f'?group={self.home_group.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Аҳмад Ҳофизов')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_shared_lesson_usable_by_dual_role(self):
        # The actual blocker: shared (group=NULL) class lessons refused
        # writes while the teacher's only group was home — fixed via the
        # class-teacher fallback in _teacher_can_use_lesson.
        from portal.models import Lesson
        from portal.views import _teacher_can_use_lesson
        lesson = Lesson.objects.create(
            class_subject=self.cs_math, date=D_Q1, lesson_number=1)
        self.assertTrue(_teacher_can_use_lesson(self.teacher_a, lesson))

    def test_class_journal_new_lesson_is_shared(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('lesson_save'), {
            'class_subject_id': self.cs_math.id,
            'date': D_Q1.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)
        from portal.models import Lesson
        lesson = Lesson.objects.get(pk=resp.json()['lesson']['id'])
        self.assertIsNone(lesson.group_id)

    def test_home_journal_lesson_attaches_to_home_group(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('lesson_save'), {
            'class_subject_id': self.cs_math.id,
            'date': D_Q1.isoformat(),
            'group_id': str(self.home_group.id),
        })
        self.assertEqual(resp.status_code, 200)
        from portal.models import Lesson
        lesson = Lesson.objects.get(pk=resp.json()['lesson']['id'])
        self.assertEqual(lesson.group_id, self.home_group.id)

    def test_writes_class_student_on_shared_lesson(self):
        from portal.models import Grade, Lesson
        lesson = Lesson.objects.create(
            class_subject=self.cs_math, date=D_Q1, lesson_number=1)
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id, 'subject': MATH,
            'date': D_Q1.isoformat(), 'score': '9',
            'lesson_id': str(lesson.id), 'type': 'daily',
        })
        data = resp.json()
        self.assertTrue(data['success'], data.get('message'))
        self.assertTrue(Grade.objects.filter(
            student=self.s1, lesson=lesson, score=9).exists())

    def test_class_picker_offers_both_journals(self):
        # The picker only lists official curriculum subjects — МАТЕМАТИКА
        # is not in TJC_SUBJECTS['7'], so mirror the dual role onto the
        # official ЗАБОНИ ТОҶИКӢ row for this UI assertion.
        cs_tajik = make_class_subject(
            self.school_a, '7-А', TAJIK, teacher=self.teacher_a)
        home_t = TeachingGroup.objects.create(
            class_subject=cs_tajik, label='Таҳсил дар хона',
            teacher=self.profile_a, is_home=True, hours_per_week=2)
        self.client.force_login(self.teacher_a)
        resp = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        self.assertEqual(resp.status_code, 200)
        # Plain subject link (class journal) + ?group= entry (home journal)
        self.assertContains(resp, f'?group={home_t.id}')


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


class HomeEditTests(HomeFixture):
    """«Таҳрир» — teacher, hours and name fixes on a home seat."""

    def _seat(self):
        return SubjectGroupMembership.objects.get(
            class_subject=self.cs_math, student=self.s_home)

    def _edit(self, user, membership, **overrides):
        self.client.force_login(user)
        payload = {
            'membership_id': membership.id,
            'teacher_id': self.profile_home.id,
            'hours': '4',
            'student_name': membership.student.full_name,
        }
        payload.update(overrides)
        return self.client.post(reverse('home_education_edit'), payload)

    def test_edit_updates_teacher_and_hours(self):
        other = make_teacher_profile(
            make_user('teacher_h3', role='teacher', school=self.school_a),
            self.school_a, full_name='Омӯзгори дигар')
        resp = self._edit(self.zavuch_a, self._seat(),
                          teacher_id=other.id, hours='4.5')
        self.assertEqual(resp.status_code, 302)
        self.home_group.refresh_from_db()
        self.assertEqual(self.home_group.teacher_id, other.id)
        self.assertEqual(self.home_group.hours_per_week, 4.5)

    def test_edit_bad_hours_rejected(self):
        resp = self._edit(self.zavuch_a, self._seat(), hours='99')
        self.assertEqual(resp.status_code, 302)
        self.home_group.refresh_from_db()
        self.assertEqual(self.home_group.hours_per_week, 2)

    def test_edit_renames_student_and_keeps_seats(self):
        # A second home seat on another subject — the rename must carry
        # every membership, not just the edited row.
        cs_rus = make_class_subject(self.school_a, '7-А', 'ЗАБОНИ РУСӢ')
        home_group_rus = TeachingGroup.objects.create(
            class_subject=cs_rus, label='Таҳсил дар хона',
            teacher=self.profile_home, is_home=True, hours_per_week=1)
        SubjectGroupMembership.objects.create(
            class_subject=cs_rus, group=home_group_rus,
            student=self.s_home)
        grade = make_grade(self.s_home, subject=MATH, score=5)
        old_id = self.s_home.id

        resp = self._edit(self.zavuch_a, self._seat(),
                          student_name='Аҳмад Ҳофизови Точӣ')
        self.assertEqual(resp.status_code, 302)

        new_student = Student.objects.get(
            school=self.school_a, class_name='7-А',
            full_name='Аҳмад Ҳофизови Точӣ')
        self.assertNotEqual(new_student.id, old_id)
        self.assertFalse(Student.all_objects.filter(id=old_id).exists())
        # Every seat follows the renamed record.
        seats = SubjectGroupMembership.objects.filter(student=new_student)
        self.assertEqual(
            {m.class_subject_id for m in seats},
            {self.cs_math.id, cs_rus.id})
        self.assertTrue(all(m.group.is_home for m in seats))
        # Grades and the home exemption follow too.
        grade.refresh_from_db()
        self.assertEqual(grade.student_id, new_student.id)
        self.assertIn(new_student.id, home_student_ids([new_student.id]))

    def test_edit_rejects_existing_name(self):
        resp = self._edit(self.zavuch_a, self._seat(),
                          student_name=self.s1.full_name)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Student.objects.filter(
            school=self.school_a, class_name='7-А',
            full_name='Аҳмад Ҳофизов').exists())

    def test_edit_cross_school_blocked(self):
        zavuch_b = make_user('zavuch_8', role='teacher', school=self.school_b)
        membership = self._seat()
        resp = self._edit(zavuch_b, membership, hours='9')
        self.assertEqual(resp.status_code, 302)
        self.home_group.refresh_from_db()
        self.assertEqual(self.home_group.hours_per_week, 2)

    def test_deactivate_subject_guarded_by_home_seat(self):
        # Removing the fan must not silently hide the registry row —
        # the seat leaves through «Таҳсил дар хона» first.
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse('deactivate_subject'), {'cs_id': self.cs_math.id})
        self.assertEqual(resp.status_code, 302)
        self.cs_math.refresh_from_db()
        self.assertTrue(self.cs_math.is_active)
        self.assertTrue(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_math, student=self.s_home).exists())

    def test_add_reuses_inactive_class_subject(self):
        # The fan was removed from the class allocation (the row still
        # exists, inactive): adding it for home education reactivates the
        # same ClassSubject instead of stacking a duplicate.
        self.cs_math.is_active = False
        self.cs_math.save(update_fields=['is_active'])
        self.client.force_login(self.zavuch_a)
        resp = self.client.post(
            reverse('home_education_add'), {
                'class_name': '7-А', 'subject': MATH,
                'teacher_id': self.profile_home.id, 'hours': '3',
                'student_id': self.s1.id,
            })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А',
                subject=MATH).count(), 1)
        self.cs_math.refresh_from_db()
        self.assertTrue(self.cs_math.is_active)
