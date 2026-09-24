"""Tests for DailyActivity recording (Phase 1).

Covers the user_logged_in signal receiver, the shared
record_daily_activity() helper, category derivation order (zavuch before
role — production zavuchs carry role='teacher' + 'zavuch_' username), the
anonymous per-day aggregate row, and login failure isolation.
"""
import datetime
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from portal.middleware import DailyActivityMiddleware, SESSION_FLAG_KEY
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


class DailyActivityMiddlewareTests(DailyActivityBase):
    """DailyActivityMiddleware: session-flag-gated once-per-day recording."""

    def setUp(self):
        self.rf = RequestFactory()
        self.mw = DailyActivityMiddleware(lambda r: HttpResponse('ok'))

    def _request(self, user, session=None):
        request = self.rf.get('/')
        request.user = user
        # A plain dict satisfies the .get()/__setitem__ API the middleware
        # uses and guarantees the flag check itself runs zero queries.
        request.session = {} if session is None else session
        return request

    # -- unit level (RequestFactory) -------------------------------------

    def test_authenticated_request_creates_daily_activity(self):
        self.mw(self._request(self.teacher_a))
        row = self._row(self.teacher_a)
        self.assertEqual(row.date, timezone.localdate())
        self.assertEqual(row.category, 'teacher')
        self.assertEqual(row.login_count, 0)

    def test_request_sets_today_session_flag(self):
        session = {}
        self.mw(self._request(self.teacher_a, session))
        self.assertEqual(
            session[SESSION_FLAG_KEY], timezone.localdate().isoformat())

    def test_second_request_no_new_row(self):
        session = {}
        self.mw(self._request(self.teacher_a, session))
        self.mw(self._request(self.teacher_a, session))
        self.assertEqual(
            DailyActivity.objects.filter(user=self.teacher_a).count(), 1)

    def test_flagged_request_does_not_call_recording(self):
        session = {}
        self.mw(self._request(self.teacher_a, session))
        with mock.patch('portal.middleware.record_daily_activity') as rec:
            self.mw(self._request(self.teacher_a, session))
            rec.assert_not_called()

    def test_yesterday_flag_records_again(self):
        yesterday = (timezone.localdate()
                     - datetime.timedelta(days=1)).isoformat()
        session = {SESSION_FLAG_KEY: yesterday}
        with mock.patch('portal.middleware.record_daily_activity') as rec:
            self.mw(self._request(self.teacher_a, session))
            rec.assert_called_once_with(self.teacher_a, count_login=False)
        self.assertEqual(
            session[SESSION_FLAG_KEY], timezone.localdate().isoformat())

    def test_today_flag_runs_zero_queries(self):
        session = {SESSION_FLAG_KEY: timezone.localdate().isoformat()}
        with self.assertNumQueries(0):
            self.mw(self._request(self.teacher_a, session))

    def test_middleware_does_not_increment_login_count(self):
        self._login(self.teacher_a)  # signal: login_count=1
        self.client.get('/')
        self.assertEqual(self._row(self.teacher_a).login_count, 1)

    def test_login_signal_still_increments_login_count(self):
        self._login(self.teacher_a)
        self.client.get('/')
        self.client.logout()
        self._login(self.teacher_a)
        self.assertEqual(self._row(self.teacher_a).login_count, 2)

    def test_anonymous_request_no_activity(self):
        self.mw(self._request(AnonymousUser()))
        self.assertEqual(DailyActivity.objects.count(), 0)

    def test_recording_failure_still_returns_response(self):
        with mock.patch('portal.middleware.record_daily_activity',
                        side_effect=Exception('boom')):
            resp = self.mw(self._request(self.teacher_a))
        self.assertEqual(resp.status_code, 200)

    def test_presence_updates_last_seen(self):
        record_daily_activity(self.teacher_a, count_login=False)
        row = self._row(self.teacher_a)
        stale = row.last_seen - datetime.timedelta(hours=2)
        DailyActivity.objects.filter(pk=row.pk).update(last_seen=stale)
        self.mw(self._request(self.teacher_a))
        row.refresh_from_db()
        self.assertGreater(row.last_seen, stale)

    # -- integration level (test client through real middleware stack) ----

    def test_client_request_records_and_flags_session(self):
        self.client.force_login(self.teacher_a)  # signal row
        DailyActivity.objects.filter(user=self.teacher_a).delete()
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        row = self._row(self.teacher_a)
        self.assertEqual(row.login_count, 0)  # created by middleware
        self.assertEqual(
            self.client.session[SESSION_FLAG_KEY],
            timezone.localdate().isoformat())

    def test_client_second_request_skips_recording(self):
        self.client.force_login(self.teacher_a)
        DailyActivity.objects.filter(user=self.teacher_a).delete()
        self.client.get('/')
        with mock.patch('portal.middleware.record_daily_activity') as rec:
            self.client.get('/')
            rec.assert_not_called()

    def test_user_last_login_unchanged_by_middleware(self):
        self._login(self.teacher_a)
        last_login = User.objects.get(pk=self.teacher_a.pk).last_login
        self.client.get('/')
        self.assertEqual(
            User.objects.get(pk=self.teacher_a.pk).last_login, last_login)
