"""⚠️ Рафтор — list of students with «Ғайриқаноатбахш» conduct.

Same semantics as Student.behavior_status: the mean of the latest
stored behavior_score per subject (default 5 for subjects with no
record) must drop below 3 for the student to appear. Access mirrors
«Назорат»: zavuch/principal → own school; superuser/staff/director →
all academic schools + picker.
"""
import datetime

from django.urls import reverse

from portal.models import ClassSubject
from portal.tests.helpers import (
    D_Q1, MATH, GoldenBase, make_grade, make_user,
)


class ConductListTests(GoldenBase):

    def _make_bad(self, student, score=1):
        """Behavior `score` in every active subject of the class → the
        default-5 padding cannot lift the average back over 3."""
        for subj in ClassSubject.objects.filter(
                school=student.school, class_name=student.class_name,
                is_active=True).values_list('subject', flat=True):
            make_grade(student, subj, behavior=score, date=D_Q1)

    # --- access ---------------------------------------------------------

    def test_anonymous_redirected(self):
        resp = self.client.get(reverse('conduct_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith('/login/'))

    def test_regular_teacher_blocked(self):
        self.client.force_login(self.teacher_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertRedirects(resp, '/', fetch_redirect_response=False)

    def test_zavuch_sees_own_school_only(self):
        self._make_bad(self.s1)
        self._make_bad(self.s3)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Ҷаҳонгир Назаров')
        # 'Интихоби муассиса' would false-positive — the parent-portal
        # modal in base.html prints it on every page; check the picker's
        # own select id instead.
        self.assertNotContains(resp, 'conduct-school-select')

    def test_principal_sees_own_school_only(self):
        self._make_bad(self.s1)
        self._make_bad(self.s3)
        self.client.force_login(self.principal_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertNotContains(resp, 'Ҷаҳонгир Назаров')

    def test_director_sees_all_schools_and_picker(self):
        self._make_bad(self.s1)
        self._make_bad(self.s3)
        self.client.force_login(self.director)
        resp = self.client.get(reverse('conduct_list'))
        self.assertContains(resp, 'Фирӯз Раҳимов')
        self.assertContains(resp, 'Ҷаҳонгир Назаров')
        self.assertContains(resp, 'conduct-school-select')

    def test_superuser_picker_filters_one_school(self):
        self._make_bad(self.s1)
        self._make_bad(self.s3)
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse('conduct_list') + f'?school_id={self.school_b.id}')
        self.assertNotContains(resp, 'Фирӯз Раҳимов')
        self.assertContains(resp, 'Ҷаҳонгир Назаров')

    # --- conduct math ---------------------------------------------------

    def test_one_bad_subject_not_enough(self):
        # behavior=2 in МАТЕМАТИКА, default 5 elsewhere → avg stays >3.
        make_grade(self.s1, MATH, behavior=2, date=D_Q1)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_latest_score_wins(self):
        # A newer good mark erases the old bad one per subject.
        for subj in ClassSubject.objects.filter(
                school=self.s1.school, class_name=self.s1.class_name,
                is_active=True).values_list('subject', flat=True):
            make_grade(self.s1, subj, behavior=1, date=D_Q1)
            make_grade(self.s1, subj, behavior=5,
                       date=D_Q1 + datetime.timedelta(days=1))
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertNotContains(resp, 'Фирӯз Раҳимов')

    def test_bad_subjects_listed_with_scores(self):
        self._make_bad(self.s1, score=2)
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('conduct_list'))
        self.assertContains(resp, 'Ғайриқаноатбахш')
        self.assertContains(resp, f'{MATH} (2)')
