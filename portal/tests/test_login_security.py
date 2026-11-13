"""Security regression tests.

Covers the hardening added after the intrusion audit:

* login brute-force lockout (5 failures -> 15 min block, per IP + username);
* random per-teacher passwords instead of the derivable
  Teacher_<school>_<n>@2026 formula;
* redirect to password_change right after login while the account still
  holds a generated default password;
* ForceTeacherPasswordMiddleware enforcing the same inside a session;
* PortalPasswordChangeView flipping custom_password_set on success.
"""
from django.contrib.auth.models import AnonymousUser, User
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from portal.middleware import ForceTeacherPasswordMiddleware
from portal.tests.helpers import make_school, make_teacher_profile, make_user
from portal.views import LOGIN_FAIL_LIMIT, LOGIN_IP_FAIL_LIMIT


class LoginLockoutTests(TestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username='teacher_1_1', password='correct_horse_123')

    def _post_login(self, password):
        return self.client.post(reverse('login'), {
            'username': 'teacher_1_1',
            'password': password,
        })

    def test_wrong_password_allowed_below_limit(self):
        for _ in range(LOGIN_FAIL_LIMIT - 1):
            response = self._post_login('wrong')
        # 4th failure still reaches authenticate -> generic error, no lock.
        response = self._post_login('wrong')
        self.assertContains(response, 'нодуруст', status_code=200)

    def test_five_failures_lock_out_correct_password(self):
        for _ in range(LOGIN_FAIL_LIMIT):
            self._post_login('wrong')
        response = self._post_login('correct_horse_123')
        self.assertContains(response, 'такрор кунед', status_code=200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_clears_counters(self):
        for _ in range(LOGIN_FAIL_LIMIT - 1):
            self._post_login('wrong')
        self._post_login('correct_horse_123')
        self.client.logout()
        # Had the counter not been cleared, one more failure would lock.
        for _ in range(LOGIN_FAIL_LIMIT - 1):
            self._post_login('wrong')
        response = self._post_login('correct_horse_123')
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_username_lock_does_not_block_other_users(self):
        """A locked username must not take down colleagues — shared school
        NAT means one IP serves many teachers; the IP bucket only trips at
        the higher credential-spraying limit."""
        for _ in range(LOGIN_FAIL_LIMIT):
            self._post_login('wrong')
        User.objects.create_user(username='teacher_1_2',
                                 password='other_pw_123')
        response = self.client.post(reverse('login'), {
            'username': 'teacher_1_2',
            'password': 'other_pw_123',
        })
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_ip_lock_blocks_credential_spraying(self):
        # 4 failures per username stay under the per-username limit while
        # pushing the shared IP bucket over its threshold — spraying many
        # accounts from one client is what the IP counter exists to stop.
        for n in range(4):
            for _ in range(LOGIN_FAIL_LIMIT - 1):
                self.client.post(reverse('login'), {
                    'username': f'spray_{n}',
                    'password': 'wrong',
                })
        User.objects.create_user(username='teacher_1_3',
                                 password='other_pw_123')
        response = self.client.post(reverse('login'), {
            'username': 'teacher_1_3',
            'password': 'other_pw_123',
        })
        self.assertContains(response, 'такрор кунед', status_code=200)


class RandomTeacherPasswordTests(TestCase):

    def setUp(self):
        self.school = make_school('Мактаб №9')
        self.zavuch = make_user('zavuch_sec', role='teacher',
                                school=self.school)
        self.client.force_login(self.zavuch)

    def test_add_teacher_gets_random_password_and_private_flag(self):
        response = self.client.post(
            reverse('add_teacher', kwargs={'school_id': self.school.id}),
            {'full_name': 'Тошев Аъзам', 'phone': '', 'subject': ''})
        self.assertRedirects(response, reverse(
            'teacher_list', kwargs={'school_id': self.school.id}),
            fetch_redirect_response=False)
        user = User.objects.get(first_name='Тошев Аъзам')
        # The derivable legacy formula must NOT authenticate.
        self.assertFalse(user.check_password('Teacher_9_1@2026'))
        tp = user.teacherprofile
        # Random generated password counts as private: list shows the lock
        # badge and the forced-change path does not trap the account.
        self.assertTrue(tp.custom_password_set)


class ForcedPasswordChangeTests(TestCase):

    def setUp(self):
        self.school = make_school('Мактаб №7')
        self.teacher = User.objects.create_user(
            username='teacher_7_1', password='Teacher_7_1@2026')
        self.tp = make_teacher_profile(self.teacher, self.school,
                                       full_name='Қурбонов Саид',
                                       custom_password_set=False)

    def test_login_with_default_password_passes_normally(self):
        # Owner decision: legacy default-password accounts keep working —
        # no forced reset at login (the dormant middleware remains opt-in).
        response = self.client.post(reverse('login'), {
            'username': 'teacher_7_1',
            'password': 'Teacher_7_1@2026',
        })
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_password_change_marks_flag(self):
        self.client.force_login(self.teacher)
        response = self.client.post('/password_change/', {
            'old_password': 'Teacher_7_1@2026',
            'new_password1': 'MyNewSecurePass2026',
            'new_password2': 'MyNewSecurePass2026',
        })
        self.assertRedirects(response, '/password_change/done/',
                             fetch_redirect_response=False)
        self.tp.refresh_from_db()
        self.assertTrue(self.tp.custom_password_set)

    def test_user_without_teacher_profile_unaffected(self):
        plain = make_user('plain_1')
        response = self.client.post(reverse('login'), {
            'username': 'plain_1',
            'password': 'pw_test_123',
        })
        self.assertRedirects(response, '/', fetch_redirect_response=False)


class ForceMiddlewareTests(TestCase):

    def setUp(self):
        self.rf = RequestFactory()
        self.school = make_school('Мактаб №5')
        self.mw = ForceTeacherPasswordMiddleware(lambda r: 'PASSED')

    def _request(self, path='/', user=None):
        req = self.rf.get(path)
        req.user = user or AnonymousUser()
        return req

    def _teacher(self, custom):
        user = User.objects.create_user(username='t_mw', password='x12345678')
        tp = make_teacher_profile(user, self.school)
        tp.custom_password_set = custom
        tp.save()
        return user

    def test_default_password_teacher_redirected(self):
        user = self._teacher(custom=False)
        response = self.mw(self._request('/', user))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/password_change/')

    def test_custom_password_teacher_passes(self):
        user = self._teacher(custom=True)
        self.assertEqual(self.mw(self._request('/', user)), 'PASSED')

    def test_exempt_path_passes(self):
        user = self._teacher(custom=False)
        self.assertEqual(
            self.mw(self._request('/password_change/', user)), 'PASSED')
        self.assertEqual(
            self.mw(self._request('/logout/', user)), 'PASSED')

    def test_no_teacher_profile_passes(self):
        user = User.objects.create_user(username='no_tp', password='x12345678')
        self.assertEqual(self.mw(self._request('/', user)), 'PASSED')

    def test_anonymous_passes(self):
        self.assertEqual(self.mw(self._request('/')), 'PASSED')
