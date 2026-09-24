"""Tests for get_activity_summary() and its dashboard rendering.

The summary is aggregate-only: distinct authenticated users and
SUM(login_count) for today / current month / last 7 local dates, scoped
by the Phase 3 monitoring scope. Data rows are created directly with
controlled dates; expectations for month/week ranges are recomputed with
a naive ORM oracle so the tests are correct on any calendar date.
"""
import datetime

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from portal.models import DailyActivity
from portal.monitoring import get_activity_summary
from portal.tests.helpers import make_school, make_user


def _row(user, day, logins, school):
    return DailyActivity.objects.create(
        user=user, username=user.username if user else '',
        date=day, category='teacher', school=school,
        login_count=logins,
        first_seen=timezone.now(), last_seen=timezone.now(),
    )


def _oracle(scope_school, start, end):
    qs = DailyActivity.objects.filter(
        user__isnull=False, date__gte=start, date__lte=end)
    if scope_school is not None:
        qs = qs.filter(school=scope_school)
    rows = list(qs.values_list('user_id', 'login_count'))
    return {
        'active_users': len({u for u, _ in rows}),
        'logins': sum(c for _, c in rows),
    }


class ActivitySummaryBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.school_a = make_school('Мактаб №1')
        cls.school_b = make_school('Мактаб №2')
        cls.admin = make_user('admin_test', superuser=True)
        cls.staff_u = make_user('staff_1', staff=True)
        cls.director = make_user('director_1', role='director')
        cls.principal_a = make_user('principal_1', role='principal',
                                  school=cls.school_a)
        cls.zavuch_a = make_user('zavuch_1', role='teacher',
                                 school=cls.school_a)
        cls.teacher_a = make_user('teacher_1', role='teacher',
                                  school=cls.school_a)
        cls.bare = make_user('plain_user')

        cls.act_a1 = make_user('act_a1', role='teacher', school=cls.school_a)
        cls.act_a2 = make_user('act_a2', role='teacher', school=cls.school_a)
        cls.act_b1 = make_user('act_b1', role='teacher', school=cls.school_b)

        today = timezone.localdate()
        cls.today = today
        _row(cls.act_a1, today, 2, cls.school_a)
        _row(cls.act_a2, today, 1, cls.school_a)
        _row(cls.act_b1, today, 1, cls.school_b)
        _row(cls.act_a1, today - datetime.timedelta(days=3), 3,
             cls.school_a)
        _row(cls.act_b1, today - datetime.timedelta(days=10), 5,
             cls.school_b)
        _row(cls.act_a2, today - datetime.timedelta(days=40), 11,
             cls.school_a)
        # Anonymous aggregate row — must never be counted.
        DailyActivity.objects.create(
            user=None, username='', date=today, category='parent_student',
            school=None, login_count=7,
            first_seen=timezone.now(), last_seen=timezone.now(),
        )

    def _summary_expected(self, school=None):
        today = self.today
        month_start = today.replace(day=1)
        week_start = today - datetime.timedelta(days=6)
        exp_today = _oracle(school, today, today)
        exp_month = _oracle(school, month_start, today)
        exp_daily = []
        for i in range(6, -1, -1):
            day = today - datetime.timedelta(days=i)
            day_stats = _oracle(school, day, day)
            exp_daily.append({'date': day, **day_stats})
        return exp_today, exp_month, exp_daily


class ActivitySummaryScopeTests(ActivitySummaryBase):

    def _assert_summary(self, user, school=None):
        summary = get_activity_summary(user)
        self.assertIsNotNone(summary)
        exp_today, exp_month, exp_daily = self._summary_expected(school)
        self.assertEqual(summary['today'], exp_today)
        self.assertEqual(summary['month'], exp_month)
        self.assertEqual(summary['daily'], exp_daily)
        return summary

    def test_superuser_district_totals(self):
        summary = self._assert_summary(self.admin)
        self.assertEqual(summary['today'],
                         {'active_users': 3, 'logins': 4})

    def test_staff_district_totals(self):
        self._assert_summary(self.staff_u)

    def test_director_district_totals(self):
        self._assert_summary(self.director)

    def test_principal_own_school_only(self):
        summary = self._assert_summary(self.principal_a,
                                       school=self.school_a)
        # school A only: act_a1 (2) + act_a2 (1) today
        self.assertEqual(summary['today'],
                         {'active_users': 2, 'logins': 3})

    def test_zavuch_gets_district_aggregate(self):
        # Zavuch may compare across schools; the summary is aggregate-only.
        summary = self._assert_summary(self.zavuch_a)
        self.assertEqual(summary['today'],
                         {'active_users': 3, 'logins': 4})

    def test_teacher_no_summary(self):
        self.assertIsNone(get_activity_summary(self.teacher_a))

    def test_profileless_no_summary(self):
        self.assertIsNone(get_activity_summary(self.bare))

    def test_anonymous_no_summary(self):
        self.assertIsNone(get_activity_summary(AnonymousUser()))
        self.assertIsNone(get_activity_summary(None))


class ActivitySummarySemanticsTests(ActivitySummaryBase):

    def test_today_users_are_distinct(self):
        # act_a1 has 2 logins today but counts once as active user.
        summary = get_activity_summary(self.admin)
        self.assertEqual(summary['today']['active_users'], 3)

    def test_today_logins_are_summed(self):
        summary = get_activity_summary(self.admin)
        self.assertEqual(summary['today']['logins'], 4)

    def test_month_totals_match_oracle(self):
        summary = get_activity_summary(self.admin)
        _, exp_month, _ = self._summary_expected()
        self.assertEqual(summary['month'], exp_month)
        self.assertGreaterEqual(summary['month']['logins'],
                                summary['today']['logins'])

    def test_weekly_table_groups_by_local_date(self):
        summary = get_activity_summary(self.admin)
        daily = summary['daily']
        self.assertEqual(len(daily), 7)
        dates = [d['date'] for d in daily]
        self.assertEqual(dates[6], self.today)
        self.assertEqual(
            dates,
            [self.today - datetime.timedelta(days=i)
             for i in range(6, -1, -1)])
        by_date = {d['date']: d for d in daily}
        self.assertEqual(
            by_date[self.today], {'date': self.today,
                                  'active_users': 3, 'logins': 4})
        day3 = self.today - datetime.timedelta(days=3)
        self.assertEqual(
            by_date[day3], {'date': day3, 'active_users': 1, 'logins': 3})

    def test_empty_days_are_zero(self):
        summary = get_activity_summary(self.admin)
        day1 = self.today - datetime.timedelta(days=1)
        row = next(d for d in summary['daily'] if d['date'] == day1)
        self.assertEqual(row['active_users'], 0)
        self.assertEqual(row['logins'], 0)

    def test_anonymous_rows_excluded(self):
        # The anonymous row has login_count=7 today; it must not appear.
        summary = get_activity_summary(self.admin)
        self.assertEqual(summary['today']['logins'], 4)

    def test_school_scoped_weekly(self):
        summary = get_activity_summary(self.principal_a)
        by_date = {d['date']: d for d in summary['daily']}
        self.assertEqual(by_date[self.today]['active_users'], 2)
        self.assertEqual(by_date[self.today]['logins'], 3)


class ActivitySummaryPerformanceTests(ActivitySummaryBase):

    def test_district_summary_uses_two_queries(self):
        with self.assertNumQueries(2):
            get_activity_summary(self.admin)

    def test_query_count_independent_of_school_count(self):
        for i in range(10, 25):
            s = make_school(f'Мактаб №{i}')
            u = make_user(f'act_extra_{i}', role='teacher', school=s)
            _row(u, self.today, 1, s)
        with self.assertNumQueries(2):
            summary = get_activity_summary(self.admin)
        self.assertEqual(summary['today']['active_users'], 3 + 15)

    def test_principal_summary_uses_two_queries(self):
        # userprofile.school is already loaded on real requests (the
        # user_role context processor and base.html nav touch it); warm it
        # here to mirror that.
        _ = self.principal_a.userprofile.school
        with self.assertNumQueries(2):
            get_activity_summary(self.principal_a)


class ActivitySummaryDashboardTests(ActivitySummaryBase):

    def test_dashboard_renders_activity_section(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('monitoring_dashboard'))
        self.assertEqual(resp.status_code, 200)
        activity = resp.context['activity']
        # force_login fires user_logged_in → the admin's own login is a
        # real DailyActivity row; recompute expectations with the oracle.
        exp_today, _, _ = self._summary_expected()
        self.assertEqual(activity['today'], exp_today)
        self.assertEqual(len(activity['daily']), 7)
        self.assertContains(resp, 'Фаъолияти имрӯз')
        self.assertContains(resp, 'Фаъолият дар моҳи ҷорӣ')
        self.assertContains(resp, 'Фаъолияти 7 рӯзи охир')

    def test_dashboard_summary_has_no_user_data(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('monitoring_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'act_a1')
        self.assertNotContains(resp, 'act_b1')

    def test_school_table_unchanged(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('monitoring_dashboard'))
        schools = {r['school'].id for r in resp.context['stats']}
        self.assertEqual(schools, {self.school_a.id, self.school_b.id})
        self.assertContains(resp, 'Муассиса')
        self.assertContains(resp, 'Миқдори хонандагон')
