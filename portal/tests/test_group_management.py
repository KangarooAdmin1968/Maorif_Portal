"""Phase 2 tests — TeachingGroup management UI for Admin/Zavuch.

Covers the group-management page/save/delete endpoints plus the pure
distribution helpers in portal.grouping (gender split, balanced split,
assignment validation).
"""
import json
import re

from django.test import TestCase
from django.urls import reverse

from portal.grouping import (
    balanced_split, gender_split, group_stats, validate_assignment,
)
from portal.models import (
    ClassSubject, Lesson, SubjectGroupMembership, TeachingGroup,
)
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_class_subject, make_student,
    make_teacher_profile, make_user,
)


def _json_block(content, element_id):
    m = re.search(
        rf'<script id="{element_id}" type="application/json">(.*?)</script>',
        content, re.S)
    return json.loads(m.group(1)) if m else None


def _fake_students(boys=0, girls=0, unknown=0):
    studs = (
        [{'id': f'b{i}', 'name': f'Бой {i:02d}', 'gender': 'M'}
         for i in range(boys)]
        + [{'id': f'g{i}', 'name': f'Гёрл {i:02d}', 'gender': 'F'}
           for i in range(girls)]
        + [{'id': f'u{i}', 'name': f'Ун {i:02d}', 'gender': ''}
           for i in range(unknown)]
    )
    return studs


class GroupingHelperTests(TestCase):
    """Pure-function tests for the distribution proposals."""

    def test_gender_split_puts_boys_in_g1_girls_in_g2(self):
        g1, g2 = gender_split(_fake_students(boys=3, girls=4))
        self.assertEqual(set(g1), {'b0', 'b1', 'b2'})
        self.assertEqual(set(g2), {'g0', 'g1', 'g2', 'g3'})

    def test_gender_split_exactly_two_groups(self):
        g1, g2 = gender_split(_fake_students(boys=5, girls=5))
        self.assertTrue(set(g1).isdisjoint(g2))

    def test_gender_split_leaves_unknown_gender_unassigned(self):
        g1, g2 = gender_split(_fake_students(boys=2, girls=2, unknown=1))
        self.assertNotIn('u0', g1)
        self.assertNotIn('u0', g2)

    def test_balanced_split_even_per_gender(self):
        g1, g2 = balanced_split(_fake_students(boys=10, girls=8))
        b1 = len([s for s in g1 if s.startswith('b')])
        b2 = len([s for s in g2 if s.startswith('b')])
        f1 = len([s for s in g1 if s.startswith('g')])
        f2 = len([s for s in g2 if s.startswith('g')])
        self.assertEqual((b1, b2), (5, 5))
        self.assertEqual((f1, f2), (4, 4))
        self.assertEqual((len(g1), len(g2)), (9, 9))

    def test_balanced_split_odd_girls(self):
        g1, g2 = balanced_split(_fake_students(girls=7))
        self.assertEqual((len(g1), len(g2)), (4, 3))

    def test_balanced_split_odd_boys(self):
        g1, g2 = balanced_split(_fake_students(boys=9))
        self.assertEqual((len(g1), len(g2)), (5, 4))

    def test_balanced_split_minimizes_total_difference(self):
        # boys 9 -> 5/4, girls 7 -> 4/3. Options: (5+4)/(4+3) = 9/7 vs
        # (5+3)/(4+4) = 8/8 — the minimizer must be picked.
        g1, g2 = balanced_split(_fake_students(boys=9, girls=7))
        self.assertEqual((len(g1), len(g2)), (8, 8))
        b1 = len([s for s in g1 if s.startswith('b')])
        f1 = len([s for s in g1 if s.startswith('g')])
        self.assertEqual((b1, f1), (5, 3))

    def test_balanced_split_covers_all_gendered_students(self):
        studs = _fake_students(boys=6, girls=5)
        g1, g2 = balanced_split(studs)
        self.assertEqual(sorted(g1 + g2), sorted(s['id'] for s in studs))

    def test_balanced_split_leaves_unknown_unassigned(self):
        g1, g2 = balanced_split(_fake_students(boys=4, unknown=2))
        self.assertNotIn('u0', g1 + g2)
        self.assertNotIn('u1', g1 + g2)

    def test_validate_ok(self):
        errors = validate_assignment({'a', 'b'}, ['a'], ['b'])
        self.assertEqual(errors, [])

    def test_validate_duplicate_membership(self):
        self.assertIn(
            'duplicate_membership',
            validate_assignment({'a', 'b'}, ['a'], ['a', 'b']))

    def test_validate_unassigned(self):
        self.assertIn(
            'unassigned',
            validate_assignment({'a', 'b'}, ['a'], []))

    def test_validate_unknown_student(self):
        self.assertIn(
            'unknown_students',
            validate_assignment({'a'}, ['a', 'zz'], []))

    def test_validate_empty_class(self):
        self.assertEqual(validate_assignment(set(), [], []), [])

    def test_group_stats(self):
        studs = {s['id']: s for s in _fake_students(boys=2, girls=1, unknown=1)}
        stats = group_stats(studs, ['b0', 'g0', 'u0'])
        self.assertEqual(
            stats, {'boys': 1, 'girls': 1, 'unknown': 1, 'total': 3})


class GroupPageAccessTests(GoldenBase):
    """Permission matrix for the group-management endpoints."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.zavuch_b = make_user('zavuch_2', role='teacher', school=cls.school_b)
        cls.page_url = reverse('class_groups', args=[cls.cs_a.id])
        cls.save_url = reverse('save_class_groups', args=[cls.cs_a.id])
        cls.delete_url = reverse('delete_class_groups', args=[cls.cs_a.id])

    def test_admin_can_open_page(self):
        self.client.force_login(self.admin)
        r = self.client.get(self.page_url)
        self.assertEqual(r.status_code, 200)

    def test_zavuch_can_open_page_own_school(self):
        self.client.force_login(self.zavuch_a)
        r = self.client.get(self.page_url)
        self.assertEqual(r.status_code, 200)

    def test_teacher_cannot_open_page(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(self.page_url)
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('/login/', r.url)

    def test_teacher_cannot_save(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(self.save_url, {})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_teacher_cannot_delete(self):
        self.client.force_login(self.teacher_a)
        r = self.client.post(self.delete_url, {})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_zavuch_cannot_manage_other_school(self):
        url = reverse('class_groups', args=[self.cs_b.id])
        self.client.force_login(self.zavuch_a)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 302)
        r = self.client.post(
            reverse('save_class_groups', args=[self.cs_b.id]), {})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(self.page_url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.url)

    def test_inactive_class_subject_rejected(self):
        cs = make_class_subject(self.school_a, '7-А', 'ФАНИ ФАЪОЛИЯТ',
                                is_active=False)
        self.client.force_login(self.admin)
        r = self.client.get(reverse('class_groups', args=[cs.id]))
        self.assertEqual(r.status_code, 302)


class GroupPageContentTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.page_url = reverse('class_groups', args=[cls.cs_a.id])
        cls.boy = make_student(cls.school_a, '7-А', 'Алӣ Бойзода', gender='M')
        cls.girl = make_student(cls.school_a, '7-А', 'Зара Духтарзода',
                                gender='F')

    def test_page_renders_mode_buttons_and_confirm(self):
        self.client.force_login(self.zavuch_a)
        content = self.client.get(self.page_url).content.decode()
        for marker in ('mode-gender', 'mode-journal', 'mode-balanced',
                       'grp-confirm-modal', 'teacher-g1', 'teacher-g2'):
            self.assertIn(marker, content)
        self.assertIn('Оё мехоҳед ин ду гурӯҳро бо чунин таркиб сабт кунед?',
                      content)

    def test_gender_proposal_in_page(self):
        self.client.force_login(self.zavuch_a)
        content = self.client.get(self.page_url).content.decode()
        proposals = _json_block(content, 'grp-proposals-data')
        self.assertIsNotNone(proposals)
        boys = {s.id for s in (self.s1, self.s2, self.boy, self.girl)
                if s.gender == 'M'}
        girls = {s.id for s in (self.s1, self.s2, self.boy, self.girl)
                 if s.gender == 'F'}
        self.assertEqual(set(proposals['gender']['g1']), boys)
        self.assertEqual(set(proposals['gender']['g2']), girls)

    def test_preview_does_not_persist(self):
        self.client.force_login(self.zavuch_a)
        self.client.get(self.page_url)
        self.assertEqual(TeachingGroup.objects.count(), 0)
        self.assertEqual(SubjectGroupMembership.objects.count(), 0)

    def test_existing_groups_prefilled(self):
        g = TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1')
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_a, group=g, student=self.s1)
        self.client.force_login(self.zavuch_a)
        content = self.client.get(self.page_url).content.decode()
        existing = _json_block(content, 'grp-existing-data')
        self.assertIn(self.s1.id, existing['Гурӯҳи 1']['members'])
        self.assertIn('Нест кардани гурӯҳҳо', content)


class GroupSaveTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher_a2 = make_user('teacher_3', role='teacher',
                                   school=cls.school_a)
        cls.pa = make_teacher_profile(cls.teacher_a, cls.school_a)
        cls.pb = make_teacher_profile(cls.teacher_a2, cls.school_a,
                                      full_name='Омӯзгор Дуввум')
        cls.save_url = reverse('save_class_groups', args=[cls.cs_a.id])
        cls.page_url = reverse('class_groups', args=[cls.cs_a.id])

    def _save(self, g1, g2, t1=None, t2=None, user=None, confirm='1'):
        data = {
            'confirm': confirm,
            'teacher_g1': str(t1 if t1 is not None else self.pa.id),
            'teacher_g2': str(t2 if t2 is not None else self.pb.id),
            'members_g1': list(g1),
            'members_g2': list(g2),
        }
        self.client.force_login(user or self.zavuch_a)
        return self.client.post(self.save_url, data)

    def test_create_two_groups_for_one_class_subject(self):
        r = self._save([self.s1.id], [self.s2.id])
        self.assertEqual(r.status_code, 302)
        groups = TeachingGroup.objects.filter(class_subject=self.cs_a)
        self.assertEqual(groups.count(), 2)
        self.assertEqual(
            set(groups.values_list('label', flat=True)),
            {'Гурӯҳи 1', 'Гурӯҳи 2'})
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_a).count(), 2)

    def test_subject_remains_one_row(self):
        self._save([self.s1.id], [self.s2.id])
        self.assertEqual(
            ClassSubject.objects.filter(
                school=self.school_a, class_name='7-А', subject=MATH
            ).count(), 1)
        self.assertFalse(
            ClassSubject.objects.filter(subject__icontains='2').exists())

    def test_teachers_assigned_to_groups(self):
        self._save([self.s1.id], [self.s2.id])
        g1 = TeachingGroup.objects.get(
            class_subject=self.cs_a, label='Гурӯҳи 1')
        g2 = TeachingGroup.objects.get(
            class_subject=self.cs_a, label='Гурӯҳи 2')
        self.assertEqual(g1.teacher_id, self.pa.id)
        self.assertEqual(g2.teacher_id, self.pb.id)

    def test_class_subject_teacher_fields_untouched(self):
        self.cs_a.allocated_teacher = self.pa
        self.cs_a.save()
        self._save([self.s1.id], [self.s2.id])
        self.cs_a.refresh_from_db()
        self.assertEqual(self.cs_a.teacher_id, self.teacher_a.id)
        self.assertEqual(self.cs_a.allocated_teacher_id, self.pa.id)

    def test_missing_confirm_flag_rejected(self):
        r = self._save([self.s1.id], [self.s2.id], confirm='')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)
        self.assertEqual(SubjectGroupMembership.objects.count(), 0)

    def test_missing_teacher_rejected(self):
        r = self._save([self.s1.id], [self.s2.id], t1='')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_teacher_from_other_school_rejected(self):
        foreign = make_teacher_profile(self.teacher_b, self.school_b)
        r = self._save([self.s1.id], [self.s2.id], t2=foreign.id)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_same_student_in_both_groups_rejected(self):
        r = self._save([self.s1.id], [self.s1.id, self.s2.id])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)
        self.assertEqual(SubjectGroupMembership.objects.count(), 0)

    def test_unassigned_student_rejected(self):
        r = self._save([self.s1.id], [])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_unknown_student_rejected(self):
        r = self._save([self.s1.id, 'no-such-student'], [self.s2.id])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_student_from_other_class_rejected(self):
        r = self._save([self.s1.id, self.s3.id], [self.s2.id])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(TeachingGroup.objects.count(), 0)

    def test_rejected_save_leaves_existing_state_untouched(self):
        self._save([self.s1.id], [self.s2.id])
        r = self._save([self.s1.id, 'bad-id'], [])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_a).count(), 2)

    def test_edit_moves_student_between_groups(self):
        self._save([self.s1.id], [self.s2.id])
        r = self._save([], [self.s1.id, self.s2.id])
        self.assertEqual(r.status_code, 302)
        g2 = TeachingGroup.objects.get(
            class_subject=self.cs_a, label='Гурӯҳи 2')
        self.assertEqual(
            set(g2.memberships.values_list('student_id', flat=True)),
            {self.s1.id, self.s2.id})
        # Still exactly one membership per student.
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_a, student=self.s1).count(), 1)

    def test_edit_changes_group_teacher(self):
        self._save([self.s1.id], [self.s2.id])
        self._save([self.s1.id], [self.s2.id], t1=self.pb.id)
        g1 = TeachingGroup.objects.get(
            class_subject=self.cs_a, label='Гурӯҳи 1')
        self.assertEqual(g1.teacher_id, self.pb.id)
        # No duplicate labels after re-save.
        self.assertEqual(
            TeachingGroup.objects.filter(class_subject=self.cs_a).count(), 2)

    def test_same_labels_reusable_on_other_class_subject(self):
        self._save([self.s1.id], [self.s2.id])
        other = make_class_subject(self.school_a, '7-А', 'АЛГЕБРА')
        url = reverse('save_class_groups', args=[other.id])
        self.client.force_login(self.zavuch_a)
        r = self.client.post(url, {
            'confirm': '1',
            'teacher_g1': str(self.pa.id),
            'teacher_g2': str(self.pb.id),
            'members_g1': [self.s1.id],
            'members_g2': [self.s2.id],
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            TeachingGroup.objects.filter(
                class_subject=other, label='Гурӯҳи 1').exists())

    def test_admin_can_save_for_any_school(self):
        url = reverse('save_class_groups', args=[self.cs_b.id])
        self.client.force_login(self.admin)
        profile_b = make_teacher_profile(self.teacher_b, self.school_b)
        r = self.client.post(url, {
            'confirm': '1',
            'teacher_g1': str(profile_b.id),
            'teacher_g2': str(profile_b.id),
            'members_g1': [self.s3.id],
            'members_g2': [],
        })
        self.assertEqual(r.status_code, 302)
        self.assertIn('school_id=', r.url)
        self.assertEqual(
            TeachingGroup.objects.filter(class_subject=self.cs_b).count(), 2)


class GroupDeleteTests(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pa = make_teacher_profile(cls.teacher_a, cls.school_a)
        cls.delete_url = reverse('delete_class_groups', args=[cls.cs_a.id])
        cls.g1 = TeachingGroup.objects.create(
            class_subject=cls.cs_a, label='Гурӯҳи 1', teacher=cls.pa)
        cls.g2 = TeachingGroup.objects.create(
            class_subject=cls.cs_a, label='Гурӯҳи 2')
        SubjectGroupMembership.objects.create(
            class_subject=cls.cs_a, group=cls.g1, student=cls.s1)

    def _delete(self, user=None):
        self.client.force_login(user or self.zavuch_a)
        return self.client.post(self.delete_url)

    def test_safe_delete_without_activity(self):
        r = self._delete()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            TeachingGroup.objects.filter(class_subject=self.cs_a).count(), 0)
        self.assertEqual(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_a).count(), 0)
        # The ClassSubject row itself is never deleted.
        self.assertTrue(ClassSubject.objects.filter(pk=self.cs_a.pk).exists())

    def test_delete_blocked_when_lessons_reference_group(self):
        Lesson.objects.create(
            class_subject=self.cs_a, date=D_Q1, lesson_number=1,
            group=self.g1)
        r = self._delete()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            TeachingGroup.objects.filter(pk=self.g1.pk).exists())
        self.assertTrue(
            SubjectGroupMembership.objects.filter(
                class_subject=self.cs_a).exists())

    def test_teacher_cannot_delete(self):
        r = self._delete(user=self.teacher_a)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            TeachingGroup.objects.filter(class_subject=self.cs_a).exists())


class AllocationEntryPointTests(GoldenBase):
    """The + Гурӯҳ action inside "Тақсимоти дарсҳо"."""

    def _page(self, user):
        self.client.force_login(user)
        r = self.client.get(reverse('lesson_allocation'))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_group_link_shown_to_zavuch(self):
        content = self._page(self.zavuch_a)
        url = reverse('class_groups', args=[self.cs_a.id])
        self.assertIn(url, content)
        self.assertIn('+ Гурӯҳ', content)

    def test_group_link_shown_to_admin(self):
        content = self._page(self.admin)
        self.assertIn(reverse('class_groups', args=[self.cs_a.id]), content)

    def test_grouped_subject_shows_edit_label(self):
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1')
        TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 2')
        content = self._page(self.zavuch_a)
        self.assertIn('✏️ Гурӯҳҳо (2)', content)

    def test_teacher_never_reaches_allocation_page(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('lesson_allocation'))
        self.assertEqual(r.status_code, 302)
