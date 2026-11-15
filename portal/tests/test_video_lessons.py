"""Видео дарсҳо — the marifat.tj video-lesson directory page.

Open to every authenticated user (teacher, zavuch, maorif staff) per the
maorif director's request; anonymous visitors are redirected to login.
"""
from django.urls import reverse

from portal.tests.helpers import GoldenBase
from portal.video_lessons import VIDEO_LESSON_CLASSES, VIDEO_LESSON_EXTRA


class VideoLessonsTests(GoldenBase):

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse('video_lessons'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r['Location'])

    def test_teacher_sees_all_class_tabs(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons'))
        self.assertEqual(r.status_code, 200)
        for cls in VIDEO_LESSON_CLASSES:
            self.assertContains(r, f'?class={cls}')
        self.assertContains(r, 'marifat.tj')

    def test_class_param_selects_subjects(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons') + '?class=11')
        self.assertEqual(r.status_code, 200)
        for _title, url in VIDEO_LESSON_CLASSES[11]:
            vid = url.split('video_id=')[1].split('&')[0]
            self.assertContains(r, f'video_id={vid}')

    def test_invalid_class_falls_back(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons') + '?class=99')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['selected'], 1)

    def test_zavuch_and_admin_see_page(self):
        self.client.force_login(self.zavuch_a)
        self.assertEqual(
            self.client.get(reverse('video_lessons')).status_code, 200)
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse('video_lessons')).status_code, 200)

    def test_nav_link_visible(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('dashboard'))
        self.assertContains(r, reverse('video_lessons'))

    def test_extra_section_rendered(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons'))
        for _title, url in VIDEO_LESSON_EXTRA:
            vid = url.split('video_id=')[1].split('&')[0]
            self.assertContains(r, f'video_id={vid}')
