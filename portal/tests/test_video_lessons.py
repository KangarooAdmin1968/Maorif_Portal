"""Видео дарсҳо — the marifat.tj video-lesson directory page.

Open to every authenticated user (teacher, zavuch, maorif staff) per the
maorif director's request; anonymous visitors are redirected to login.
Subjects open an inline playlist whose topics stream marifat.tj files.
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

    def test_class_param_lists_subject_cards(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons') + '?class=11')
        self.assertEqual(r.status_code, 200)
        for title, _url, rows in VIDEO_LESSON_CLASSES[11]:
            self.assertContains(r, title)
            self.assertContains(r, f'{len(rows)} дарс')

    def test_invalid_class_falls_back(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons') + '?class=99')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['selected'], 1)

    def test_subject_view_shows_player_and_lessons(self):
        self.client.force_login(self.teacher_a)
        title, url, rows = VIDEO_LESSON_CLASSES[5][0]
        r = self.client.get(
            reverse('video_lessons') + '?class=5&subject=0')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['subject']['title'], title)
        self.assertContains(r, 'id="vl-video"')
        topic, file = rows[0]
        self.assertContains(r, topic)
        self.assertContains(r, file)
        vid = url.split('video_id=')[1].split('&')[0]
        self.assertContains(r, f'video_id={vid}')  # fallback link

    def test_subject_out_of_range_ignored(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(
            reverse('video_lessons') + '?class=5&subject=99')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context['subject'])

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

    def test_download_endpoint_redirects_to_source(self):
        self.client.force_login(self.teacher_a)
        _title, _url, rows = VIDEO_LESSON_CLASSES[5][0]
        r = self.client.get(
            reverse('video_download', kwargs={'filename': rows[0][1]}))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r['Location'],
            'https://marifat.tj/admin/videos/' + rows[0][1])

    def test_download_endpoint_rejects_bad_filename(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(
            reverse('video_download',
                    kwargs={'filename': 'not_a_video.txt'}))
        self.assertEqual(r.status_code, 400)

    def test_extra_section_rendered(self):
        self.client.force_login(self.teacher_a)
        r = self.client.get(reverse('video_lessons'))
        for _title, url, _rows in VIDEO_LESSON_EXTRA:
            vid = url.split('video_id=')[1].split('&')[0]
            self.assertContains(r, f'video_id={vid}')
