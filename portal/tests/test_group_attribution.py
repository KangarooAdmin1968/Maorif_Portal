"""Phase 4 tests — teacher attribution, school readiness, workload and
school documents for grouped ClassSubjects.

Fixture: School A, class 7-А, subject МАТЕМАТИКА grouped into:
    Гурӯҳи 1 -> teacher_a (profile_a) -> s1
    Гурӯҳи 2 -> teacher_a2 (profile_a2) -> s2
s_extra has no membership. School B (cs_b, teacher_b) stays ungrouped.
"""
from django.conf import settings
from django.urls import reverse

from portal.curriculum_hours import quarter_min_norm, weekly_hours
from portal.models import (
    ClassSubject, Grade, Lesson, SubjectGroupMembership, TeachingGroup,
)
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_grade, make_student,
    make_teacher_profile, make_user,
)


class AttributionFixture(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cs = cls.cs_a  # 7-А МАТЕМАТИКА
        cls.profile_a = make_teacher_profile(
            cls.teacher_a, cls.school_a, full_name='Омӯзгор А')
        cls.teacher_a2 = make_user(
            'teacher_3', role='teacher', school=cls.school_a)
        cls.profile_a2 = make_teacher_profile(
            cls.teacher_a2, cls.school_a, full_name='Омӯзгор Б')
        cls.s_extra = make_student(cls.school_a, '7-А', 'Наврӯз Одилов')
        cls.g1 = TeachingGroup.objects.create(
            class_subject=cls.cs, label='Гурӯҳи 1', teacher=cls.profile_a)
        cls.g2 = TeachingGroup.objects.create(
            class_subject=cls.cs, label='Гурӯҳи 2', teacher=cls.profile_a2)
        SubjectGroupMembership.objects.create(
            class_subject=cls.cs, group=cls.g1, student=cls.s1)
        SubjectGroupMembership.objects.create(
            class_subject=cls.cs, group=cls.g2, student=cls.s2)

    def _readiness(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('school_readiness_rating'))
        assert resp.status_code == 200
        stats = {d['school'].id: d for d in resp.context['stats']}
        activity = {a['school'].id: a for a in resp.context['activity']}
        return stats, activity

    def _teachers(self, activity=None):
        if activity is None:
            _, activity = self._readiness()
        return {
            t['name']: t for t in activity[self.school_a.id]['teachers']
        }

    def _workload(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('school_documents'))
        assert resp.status_code == 200
        return {w['teacher'].id: w for w in resp.context['workload']}


class ReadinessTab1Tests(AttributionFixture):
    """'Assigned' = fully staffed: every active group must have a teacher."""

    def test_fully_staffed_grouped_cs_counts_assigned(self):
        stats, _ = self._readiness()
        total = ClassSubject.objects.filter(
            school=self.school_a, is_active=True).count()
        self.assertEqual(stats[self.school_a.id]['total'], total)
        self.assertEqual(stats[self.school_a.id]['assigned'], 1)

    def test_group_without_teacher_not_fully_staffed(self):
        self.g2.teacher = None
        self.g2.save()
        stats, _ = self._readiness()
        # cs.teacher=teacher_a is set, but that must NOT staff the grouped CS.
        self.assertEqual(stats[self.school_a.id]['assigned'], 0)

    def test_cs_level_teacher_alone_does_not_staff_grouped_subject(self):
        self.g1.teacher = None
        self.g1.save()
        stats, _ = self._readiness()
        self.assertEqual(stats[self.school_a.id]['assigned'], 0)


class ReadinessAttributionTests(AttributionFixture):
    """Tab 2: per-group teacher attribution."""

    def test_both_group_teachers_appear(self):
        teachers = self._teachers()
        self.assertIn('Омӯзгор А', teachers)
        self.assertIn('Омӯзгор Б', teachers)
        self.assertIn('Гурӯҳи 1', teachers['Омӯзгор А']['subjects'][0])
        self.assertIn('Гурӯҳи 2', teachers['Омӯзгор Б']['subjects'][0])

    def test_grades_done_scoped_per_group(self):
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s1, subject=MATH, score=9)
        make_grade(self.s2, subject=MATH, score=7)
        teachers = self._teachers()
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 2)
        self.assertEqual(teachers['Омӯзгор Б']['grades_done'], 1)

    def test_min_grades_uses_group_size(self):
        norm = quarter_min_norm('7-А', MATH, self.cs)
        teachers = self._teachers()
        # g1 has 1 member (s1); s_extra is unassigned — NOT class size 3.
        self.assertEqual(teachers['Омӯзгор А']['min_grades'], norm * 1)
        self.assertEqual(teachers['Омӯзгор Б']['min_grades'], norm * 1)

    def test_fulfillment_group_scoped(self):
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=7)
        make_grade(self.s2, subject=MATH, score=9)
        norm = quarter_min_norm('7-А', MATH, self.cs)
        teachers = self._teachers()
        self.assertEqual(
            teachers['Омӯзгор А']['fulfillment'],
            round(1 / (norm * 1) * 100, 1))
        self.assertEqual(
            teachers['Омӯзгор Б']['fulfillment'],
            round(2 / (norm * 1) * 100, 1))

    def test_each_group_teacher_gets_full_weekly_hours(self):
        self.cs.hours_per_week = 4
        self.cs.save()
        teachers = self._teachers()
        self.assertEqual(
            teachers['Омӯзгор А']['hours'], weekly_hours('7-А', MATH, self.cs))
        self.assertEqual(
            teachers['Омӯзгор Б']['hours'], weekly_hours('7-А', MATH, self.cs))

    def test_curriculum_not_doubled_at_school_level(self):
        # One canonical CS row — 'total' counts it once; nothing sums
        # teacher workloads into school curriculum totals.
        stats, _ = self._readiness()
        total_cs = ClassSubject.objects.filter(
            school=self.school_a, is_active=True).count()
        self.assertEqual(stats[self.school_a.id]['total'], total_cs)
        self.assertEqual(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А', subject=MATH).count(), 1)

    def test_unassigned_student_in_school_totals_but_no_teacher(self):
        make_grade(self.s_extra, subject=MATH, score=6)
        stats, activity = self._readiness()
        teachers = self._teachers(activity)
        # School-level counts include the unassigned student's grade.
        self.assertEqual(activity[self.school_a.id]['total_grades'], 1)
        # No teacher is credited with it.
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 0)
        self.assertEqual(teachers['Омӯзгор Б']['grades_done'], 0)

    def test_group_without_teacher_no_attribution_row(self):
        self.g2.teacher = None
        self.g2.save()
        make_grade(self.s2, subject=MATH, score=7)
        _, activity = self._readiness()
        teachers = self._teachers(activity)
        self.assertNotIn('Омӯзгор Б', teachers)
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 0)
        # School-level norm still counts the class-wide grade.
        self.assertEqual(activity[self.school_a.id]['total_grades'], 1)

    def test_zero_student_group_no_false_fulfillment(self):
        SubjectGroupMembership.objects.get(
            class_subject=self.cs, student=self.s2).delete()
        teachers = self._teachers()
        self.assertEqual(teachers['Омӯзгор Б']['min_grades'], 0)
        self.assertEqual(teachers['Омӯзгор Б']['fulfillment'], 0.0)
        self.assertEqual(teachers['Омӯзгор Б']['grades_done'], 0)

    def test_same_teacher_on_both_groups(self):
        self.g2.teacher = self.profile_a
        self.g2.save()
        self.cs.hours_per_week = 4
        self.cs.save()
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=9)
        norm = quarter_min_norm('7-А', MATH, self.cs)
        teachers = self._teachers()
        entry = teachers['Омӯзгор А']
        # Both groups' workload and grades attributed; memberships are
        # unique so no student is double-counted.
        self.assertEqual(entry['grades_done'], 2)
        self.assertEqual(entry['min_grades'], norm * 2)
        self.assertEqual(entry['hours'], 8)
        self.assertNotIn('Омӯзгор Б', teachers)

    def test_lesson_group_overrides_membership_attribution(self):
        # A grade written on a group-2 lesson credits group 2's teacher
        # even though the student is currently a group-1 member.
        l_g2 = Lesson.objects.create(
            class_subject=self.cs, date=D_Q1, lesson_number=2, group=self.g2)
        Grade.objects.create(
            student=self.s1, subject=MATH, period='Холҳои ҷорӣ (Онлайн)',
            date=D_Q1, score=9, lesson=l_g2)
        teachers = self._teachers()
        self.assertEqual(teachers['Омӯзгор Б']['grades_done'], 1)
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 0)

    def test_legacy_lesson_less_grade_uses_membership(self):
        # Pre-split rows (lesson=NULL) attribute via current membership.
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=7)
        teachers = self._teachers()
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 1)
        self.assertEqual(teachers['Омӯзгор Б']['grades_done'], 1)

    def test_mid_quarter_split_no_backfill(self):
        # Grades written before grouping keep lesson=NULL — grouping must
        # never rewrite historical rows.
        g_pre = make_grade(self.s1, subject=MATH, score=8)
        Lesson.objects.create(
            class_subject=self.cs, date=D_Q1, lesson_number=1)
        g_pre.refresh_from_db()
        self.assertIsNone(g_pre.lesson_id)
        self.assertIsNone(
            Lesson.objects.get(class_subject=self.cs).group_id)


class DocumentsWorkloadTests(AttributionFixture):
    """Ҳуҷҷатнигорӣ: per-group-teacher workload lines with group labels."""

    def test_grouped_subject_emits_labeled_items_per_teacher(self):
        workload = self._workload()
        wa = workload[self.profile_a.id]
        wb = workload[self.profile_a2.id]
        self.assertEqual(
            wa['items'], [{'subject': 'МАТЕМАТИКА — Гурӯҳи 1', 'classes': ['7-А']}])
        self.assertEqual(
            wb['items'], [{'subject': 'МАТЕМАТИКА — Гурӯҳи 2', 'classes': ['7-А']}])

    def test_each_group_teacher_gets_full_hours(self):
        self.cs.hours_per_week = 4
        self.cs.save()
        workload = self._workload()
        self.assertEqual(workload[self.profile_a.id]['total_hours'], 4)
        self.assertEqual(workload[self.profile_a2.id]['total_hours'], 4)

    def test_explicit_hours_resolution_unchanged(self):
        # Falls back to the settings recommendation when hours_per_week NULL.
        workload = self._workload()
        self.assertEqual(
            workload[self.profile_a.id]['total_hours'],
            settings.TJC_SUBJECT_HOURS[MATH])

    def test_teacher_less_group_produces_no_workload_row(self):
        self.g2.teacher = None
        self.g2.save()
        workload = self._workload()
        self.assertNotIn(self.profile_a2.id, workload)

    def test_cs_not_duplicated(self):
        workload = self._workload()
        # Still one canonical CS row; labels differ, source row is single.
        self.assertEqual(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А', subject=MATH).count(), 1)
        self.assertEqual(len(workload[self.profile_a.id]['items']), 1)


class UngroupedBaselineTests(GoldenBase):
    """Ungrouped subjects: readiness + documents output unchanged."""

    def _readiness(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('school_readiness_rating'))
        assert resp.status_code == 200
        return resp.context

    def test_ungrouped_readiness_unchanged(self):
        make_teacher_profile(self.teacher_a, self.school_a, full_name='Омӯзгор А')
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=9)
        ctx = self._readiness()
        stats = {d['school'].id: d for d in ctx['stats']}
        activity = {a['school'].id: a for a in ctx['activity']}
        total = ClassSubject.objects.filter(
            school=self.school_a, is_active=True).count()
        self.assertEqual(stats[self.school_a.id]['total'], total)
        self.assertEqual(stats[self.school_a.id]['assigned'], 1)
        norm = quarter_min_norm('7-А', MATH, self.cs_a)
        teachers = {
            t['name']: t for t in activity[self.school_a.id]['teachers']}
        # Whole-class attribution: both students, both grades.
        self.assertEqual(teachers['Омӯзгор А']['grades_done'], 2)
        self.assertEqual(teachers['Омӯзгор А']['min_grades'], norm * 2)
        self.assertEqual(
            teachers['Омӯзгор А']['subjects'], ['МАТЕМАТИКА: 7-А'])

    def test_ungrouped_documents_unchanged(self):
        profile_a = make_teacher_profile(
            self.teacher_a, self.school_a, full_name='Омӯзгор А')
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('school_documents'))
        self.assertEqual(resp.status_code, 200)
        workload = {w['teacher'].id: w for w in resp.context['workload']}
        self.assertEqual(
            workload[profile_a.id]['items'],
            [{'subject': MATH, 'classes': ['7-А']}])
        # school_documents resolves via settings.TJC_SUBJECT_HOURS (5 for
        # МАТЕМАТИКА), not the curriculum_hours annex fallback (2).
        self.assertEqual(
            workload[profile_a.id]['total_hours'],
            settings.TJC_SUBJECT_HOURS[MATH])


class RankingUnchangedTests(AttributionFixture):
    """Student-level rankings are membership-agnostic."""

    def test_school_and_class_rankings_identical_after_grouping(self):
        from portal.views import (
            calculate_class_rankings, calculate_school_rankings,
            calculate_subject_rankings,
        )
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=10)
        # Recompute with groups removed — the rows exist; deactivate groups.
        before_school = calculate_school_rankings()
        before_class = calculate_class_rankings()
        before_subject = calculate_subject_rankings()
        TeachingGroup.objects.filter(class_subject=self.cs).update(
            is_active=False)
        after_school = calculate_school_rankings()
        after_class = calculate_class_rankings()
        after_subject = calculate_subject_rankings()
        self.assertEqual(before_school, after_school)
        self.assertEqual(before_class, after_class)
        self.assertEqual(before_subject, after_subject)

    def test_student_counts_once_regardless_of_group(self):
        from portal.views import calculate_school_rankings
        make_grade(self.s1, subject=MATH, score=8)
        make_grade(self.s2, subject=MATH, score=10)
        make_grade(self.s_extra, subject=MATH, score=6)
        data = calculate_school_rankings()
        entry = next(d for d in data if d['school'].id == self.school_a.id)
        # 8 + 10 + 6 = 24 / 3 students = 8.0 — s_extra counts at school
        # level even without a group membership.
        self.assertEqual(entry['gpa'], 8.0)
