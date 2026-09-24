"""Tests for DailyActivity recording (Phase 1).

Covers the user_logged_in signal receiver, the shared
record_daily_activity() helper, category derivation order (zavuch before
role — production zavuchs carry role='teacher' + 'zavuch_' username), the
anonymous per-day aggregate row, and login failure isolation.
"""
import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from portal.models import DailyActivity
from portal.signals import record_daily_activity
from portal.tests.helpers import make_school, make_user


class DailyActivityBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school_a = make_school('Мактаб №1')
        cls.admin = make_user('admin_test', superuser=True)
        cls.director = make_user('director_1', role='director')
        cls.principal_a = make_user('principal_1', role='principal',
                                  school=cls.school_a)
        cls.zavuch_a = make_user('zavuch_1', role='teacher',
                                 school=cls.school_a)
        cls.teacher_a = make_user('teacher_1', role='teacher',
                                  school=cls.school_a)

    def _login(self, user):
        return self.client.login(username=user.username,
                                 password='pw_test_123')

    def _row(self, user):
        return DailyActivity.objects.get(user=user)


class LoginRecordingTests(DailyActivityBase):

    def test_login_creates_daily_activity(self):
        self.assertTrue(self._login(self.teacher_a))
        row = self._row(self.teacher_a)
        self.assertEqual(row.date, timezone.localdate())
        self.assertEqual(row.school, self.school_a)

    def test_login_count_starts_at_one(self):
        self._login(self.teacher_a)
        self.assertEqual(self._row(self.teacher_a).login_count, 1)

    def test_second_login_same_day_no_new_row(self):
        self._login(self.teacher_a)
        self.client.logout()
        self._login(self.teacher_a)
        self.assertEqual(
            DailyActivity.objects.filter(user=self.teacher_a).count(), 1)

    def test_second_login_increments_login_count(self):
        self._login(self.teacher_a)
        self.client.logout()
        self._login(self.teacher_a)
        self.assertEqual(self._row(self.teacher_a).login_count, 2)

    def test_first_seen_unchanged_on_second_login(self):
        self._login(self.teacher_a)
        first_seen = self._row(self.teacher_a).first_seen
        self.client.logout()
        self._login(self.teacher_a)
        self.assertEqual(self._row(self.teacher_a).first_seen, first_seen)

    def test_last_seen_updated_on_second_login(self):
        self._login(self.teacher_a)
        row = self._row(self.teacher_a)
        stale = row.last_seen - datetime.timedelta(hours=2)
        DailyActivity.objects.filter(pk=row.pk).update(last_seen=stale)
        self.client.logout()
        self._login(self.teacher_a)
        row.refresh_from_db()
        self.assertGreater(row.last_seen, stale)

    def test_username_snapshot_stored(self):
        self._login(self.teacher_a)
        self.assertEqual(self._row(self.teacher_a).username, 'teacher_1')

    def test_presence_update_does_not_increment_login_count(self):
        self._login(self.teacher_a)
        record_daily_activity(self.teacher_a, count_login=False)
        self.assertEqual(self._row(self.teacher_a).login_count, 1)

    def test_user_last_login_still_updated(self):
        self.assertTrue(self._login(self.teacher_a))
        self.assertIsNotNone(
            User.objects.get(pk=self.teacher_a.pk).last_login)


class CategoryTests(DailyActivityBase):
    """Category derivation order: auth → staff → zavuch → director →
    principal → teacher. Production zavuchs are role='teacher' with a
    'zavuch_' username, so they must be caught before the role check."""

    def _category(self, user):
        record_daily_activity(user, count_login=False)
        return self._row(user).category

    def test_zavuch_username_with_teacher_role_is_zavuch(self):
        self.assertEqual(self._category(self.zavuch_a), 'zavuch')

    def test_principal_is_principal(self):
        self.assertEqual(self._category(self.principal_a), 'principal')

    def test_director_is_director(self):
        self.assertEqual(self._category(self.director), 'director')

    def test_superuser_is_staff(self):
        self.assertEqual(self._category(self.admin), 'staff')

    def test_is_staff_user_is_staff(self):
        staff = make_user('staff_1', role='teacher', staff=True)
        self.assertEqual(self._category(staff), 'staff')

    def test_regular_teacher_is_teacher(self):
        self.assertEqual(self._category(self.teacher_a), 'teacher')

    def test_profileless_user_is_teacher(self):
        bare = make_user('plain_user')
        row = None
        record_daily_activity(bare, count_login=False)
        row = self._row(bare)
        self.assertEqual(row.category, 'teacher')
        self.assertIsNone(row.school)

    def test_director_without_school_has_null_school(self):
        record_daily_activity(self.director, count_login=False)
        self.assertIsNone(self._row(self.director).school)


class AnonymousActivityTests(TestCase):

    def test_anonymous_category_is_parent_student(self):
        record_daily_activity(None, count_login=True)
        row = DailyActivity.objects.get(user__isnull=True)
        self.assertEqual(row.category, 'parent_student')

    def test_anonymous_one_row_per_day(self):
        record_daily_activity(None, count_login=True)
        record_daily_activity(None, count_login=True)
        self.assertEqual(
            DailyActivity.objects.filter(user__isnull=True).count(), 1)

    def test_anonymous_row_aggregates_count(self):
        record_daily_activity(None, count_login=True)
        record_daily_activity(None, count_login=True)
        row = DailyActivity.objects.get(user__isnull=True)
        self.assertEqual(row.login_count, 2)


class FailureIsolationTests(DailyActivityBase):

    def test_recording_failure_does_not_break_login(self):
        with mock.patch('portal.signals.record_daily_activity',
                        side_effect=Exception('boom')):
            self.assertTrue(self._login(self.teacher_a))
        self.assertEqual(
            DailyActivity.objects.filter(user=self.teacher_a).count(), 0)
