"""Focused tests for the centralized monitoring-permission helpers
(portal/monitoring.py).

Access matrix under test:

                access  all_schools  own_school  other_school  own_detail
                                                  (aggregate)   other_detail
superuser         T        T            T           T             T
staff             T        T            T           T             T
director          T        T            T           T             T
principal         T        F            T           F             F
zavuch            T        F            T           T(agg)        F
teacher           F        F            F           F             F
profileless       F        F            F           F             F
anonymous         F        F            F           F             F
"""
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from portal.monitoring import (
    can_access_monitoring,
    can_view_all_schools_monitoring,
    can_view_cross_school_aggregate,
    can_view_full_school_detail,
    can_view_school_monitoring,
)
from portal.tests.helpers import make_school, make_user


class MonitoringPermissionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.school_a = make_school('Мактаб №1')
        cls.school_b = make_school('Мактаб №2')
        cls.admin = make_user('admin_test', superuser=True)
        cls.staff = make_user('staff_1', staff=True)
        cls.director = make_user('director_1', role='director')
        cls.principal_a = make_user('principal_1', role='principal',
                                  school=cls.school_a)
        cls.zavuch_a = make_user('zavuch_1', role='teacher',
                                 school=cls.school_a)
        cls.teacher_a = make_user('teacher_1', role='teacher',
                                  school=cls.school_a)
        cls.bare = make_user('plain_user')  # authenticated, no UserProfile
        cls.anon = AnonymousUser()

    # -- district scope: superuser / staff / district director -----------

    def _assert_district_scope(self, user):
        self.assertTrue(can_access_monitoring(user))
        self.assertTrue(can_view_all_schools_monitoring(user))
        self.assertTrue(can_view_school_monitoring(user, self.school_a))
        self.assertTrue(can_view_school_monitoring(user, self.school_b))
        self.assertTrue(can_view_full_school_detail(user, self.school_a))
        self.assertTrue(can_view_full_school_detail(user, self.school_b))
        self.assertTrue(can_view_cross_school_aggregate(user))

    def test_superuser_full_access(self):
        self._assert_district_scope(self.admin)

    def test_staff_full_access(self):
        self._assert_district_scope(self.staff)

    def test_district_director_full_access(self):
        self._assert_district_scope(self.director)

    # -- school scope: principal -----------------------------------------

    def test_principal_own_school(self):
        self.assertTrue(can_access_monitoring(self.principal_a))
        self.assertFalse(can_view_all_schools_monitoring(self.principal_a))
        self.assertTrue(
            can_view_school_monitoring(self.principal_a, self.school_a))
        self.assertTrue(
            can_view_full_school_detail(self.principal_a, self.school_a))

    def test_principal_other_school_denied(self):
        self.assertFalse(
            can_view_school_monitoring(self.principal_a, self.school_b))
        self.assertFalse(
            can_view_full_school_detail(self.principal_a, self.school_b))
        self.assertFalse(can_view_cross_school_aggregate(self.principal_a))

    # -- school_agg scope: zavuch ----------------------------------------

    def test_zavuch_own_school_full_detail(self):
        self.assertTrue(can_access_monitoring(self.zavuch_a))
        self.assertFalse(can_view_all_schools_monitoring(self.zavuch_a))
        self.assertTrue(
            can_view_school_monitoring(self.zavuch_a, self.school_a))
        self.assertTrue(
            can_view_full_school_detail(self.zavuch_a, self.school_a))

    def test_zavuch_other_school_aggregate_only(self):
        # Aggregate comparison is allowed for other schools...
        self.assertTrue(
            can_view_school_monitoring(self.zavuch_a, self.school_b))
        self.assertTrue(can_view_cross_school_aggregate(self.zavuch_a))
        # ...but never individual detail (users, last_login timestamps).
        self.assertFalse(
            can_view_full_school_detail(self.zavuch_a, self.school_b))

    # -- denied scopes ----------------------------------------------------

    def _assert_denied(self, user):
        self.assertFalse(can_access_monitoring(user))
        self.assertFalse(can_view_all_schools_monitoring(user))
        self.assertFalse(can_view_school_monitoring(user, self.school_a))
        self.assertFalse(can_view_school_monitoring(user, self.school_b))
        self.assertFalse(can_view_full_school_detail(user, self.school_a))
        self.assertFalse(can_view_full_school_detail(user, self.school_b))
        self.assertFalse(can_view_cross_school_aggregate(user))

    def test_regular_teacher_denied(self):
        self._assert_denied(self.teacher_a)

    def test_profileless_user_denied(self):
        self._assert_denied(self.bare)

    def test_anonymous_denied(self):
        self._assert_denied(self.anon)

    def test_none_user_denied(self):
        self._assert_denied(None)

    # -- edge cases --------------------------------------------------------

    def test_none_school_denied_for_school_scopes(self):
        for user in (self.principal_a, self.zavuch_a, self.teacher_a):
            self.assertFalse(can_view_school_monitoring(user, None))
            self.assertFalse(can_view_full_school_detail(user, None))
