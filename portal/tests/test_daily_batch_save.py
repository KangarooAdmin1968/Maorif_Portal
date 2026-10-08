"""Daily journal batched-save tests.

The daily journal (grade_entry) stages score/attendance/behavior edits
into the existing localStorage pending queue and flushes them in ONE POST
to daily_save. These tests cover the server contract: per-item results
keyed by input name, identical write semantics to save_grade_ajax /
monthly_save, permission/scope/lock enforcement, partial failures, and
batch atomicity. Assessment-linked and quarterly writes stay on their own
endpoints and are never sent here.
"""
import json
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from portal import views
from portal.models import Grade, SubjectGroupMembership, TeachingGroup
from portal.tests.helpers import (
    D_Q1, D_Q1_B, MATH, TAJIK, GoldenBase, lock_quarter, make_grade,
    make_student, make_teacher_profile,
)
from portal.tests.test_phase4_lessons import make_control, make_lesson

DAILY_SAVE_URL = reverse('daily_save')
D = D_Q1  # 2025-10-15, quarter 1


class DailyBatchSaveTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)

    def _post(self, items):
        return self.client.post(
            DAILY_SAVE_URL, {'payload': json.dumps(items)}
        )

    def _item(self, student=None, field='score', value='8', key=None,
              lesson=None, subject=MATH, date=D):
        sid = student.id if student else self.s1.id
        item = {
            'key': key if key is not None else f'{field}_{sid}',
            'student_id': sid,
            'subject': subject,
            'date': date.isoformat() if date else '',
            # Legacy queue items carry these — the endpoint must ignore them.
            'type': 'daily',
            'lesson_id': str(lesson) if lesson else '',
        }
        item[field] = value
        return item

    def _result(self, data, key):
        return next(r for r in data['results'] if r['key'] == key)

    # -- batch basics --------------------------------------------------------

    def test_multiple_students_one_request(self):
        resp = self._post([
            self._item(self.s1, 'score', '8'),
            self._item(self.s2, 'score', '9'),
        ])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 2)
        self.assertEqual(
            Grade.objects.get(student=self.s1, date=D, subject=MATH).score, 8)
        self.assertEqual(
            Grade.objects.get(student=self.s2, date=D, subject=MATH).score, 9)

    def test_score_attendance_behavior_same_batch(self):
        resp = self._post([
            self._item(self.s1, 'score', '7'),
            self._item(self.s1, 'attendance', '-'),
            self._item(self.s1, 'behavior_score', '4'),
            self._item(self.s2, 'score', '10'),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 4)
        g = Grade.objects.get(student=self.s1, date=D)
        self.assertEqual(g.score, 7)
        self.assertEqual(g.attendance, '-')
        self.assertEqual(g.behavior_score, 4)

    def test_untouched_fields_not_overwritten(self):
        make_grade(self.s1, score=5, attendance='+', behavior=3)
        # Batch writes only the score — attendance/behavior survive.
        resp = self._post([self._item(self.s1, 'score', '9')])
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, date=D)
        self.assertEqual(g.score, 9)
        self.assertEqual(g.attendance, '+')
        self.assertEqual(g.behavior_score, 3)

    # -- validation preserved -------------------------------------------------

    def test_score_range_1_10_enforced_per_item(self):
        resp = self._post([
            self._item(self.s1, 'score', '11'),
            self._item(self.s1, 'score', '0', key='score_zero'),
            self._item(self.s2, 'score', '7'),
        ])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 1)
        self.assertFalse(self._result(data, f'score_{self.s1.id}')['ok'])
        self.assertFalse(self._result(data, 'score_zero')['ok'])
        self.assertTrue(self._result(data, f'score_{self.s2.id}')['ok'])
        self.assertIn('Хол', self._result(data, f'score_{self.s1.id}')['message'])

    def test_behavior_range_1_5_enforced(self):
        resp = self._post([self._item(self.s1, 'behavior_score', '9')])
        data = resp.json()
        self.assertTrue(data['success'])
        # Out-of-range conduct normalizes to None; an all-empty row is
        # deleted rather than stored — identical to the per-cell path.
        self.assertFalse(Grade.objects.filter(student=self.s1, date=D).exists())

    def test_empty_score_deletes_empty_row(self):
        make_grade(self.s1, score=7)
        resp = self._post([self._item(self.s1, 'score', '')])
        self.assertTrue(resp.json()['success'])
        self.assertFalse(
            Grade.objects.filter(student=self.s1, date=D).exists())

    def test_empty_score_keeps_row_with_other_fields(self):
        make_grade(self.s1, score=7, attendance='-')
        resp = self._post([self._item(self.s1, 'score', '')])
        self.assertTrue(resp.json()['success'])
        g = Grade.objects.get(student=self.s1, date=D)
        self.assertIsNone(g.score)
        self.assertEqual(g.attendance, '-')

    # -- permissions / scoping -------------------------------------------------

    def test_anonymous_redirected(self):
        self.client.logout()
        resp = self._post([self._item(self.s1, 'score', '8')])
        self.assertEqual(resp.status_code, 302)

    def test_cross_school_teacher_rejected(self):
        self.client.force_login(self.teacher_b)
        resp = self._post([self._item(self.s1, 'score', '8')])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 0)
        self.assertFalse(data['results'][0]['ok'])
        self.assertIn('Дастрасӣ', data['results'][0]['message'])
        self.assertFalse(Grade.objects.filter(student=self.s1).exists())

    def test_student_from_other_school_rejected(self):
        # teacher_a is allocated to School A 7-А; s3 lives in School B.
        resp = self._post([self._item(self.s3, 'score', '8')])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertIn('Дастрасӣ', data['results'][0]['message'])

    def test_unassigned_subject_rejected(self):
        resp = self._post([self._item(self.s1, 'score', '8', subject=TAJIK)])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertIn('Дастрасӣ', data['results'][0]['message'])

    def test_other_class_student_rejected(self):
        other = make_student(self.school_a, '8-А', 'Тест Тестов')
        resp = self._post([self._item(other, 'score', '8')])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertFalse(Grade.objects.filter(student=other).exists())

    def test_unknown_student_item_rejected(self):
        item = self._item(self.s1, 'score', '8', key='score_ghost')
        item['student_id'] = 'no-such-student'
        resp = self._post([item])
        data = resp.json()
        self.assertFalse(data['results'][0]['ok'])
        self.assertIn('ёфт нашуд', data['results'][0]['message'])

    # -- lesson scope (D1/D2/D3 + lesson-less) --------------------------------

    def test_lesson_scoped_items_write_separate_rows(self):
        l1 = make_lesson(self.cs_a, D, 1)
        l2 = make_lesson(self.cs_a, D, 2)
        resp = self._post([
            self._item(self.s1, 'score', '8', lesson=l1.id),
            self._item(self.s1, 'score', '10', lesson=l2.id),
        ])
        self.assertEqual(resp.json()['saved'], 2)
        self.assertEqual(
            Grade.objects.get(student=self.s1, lesson=l1).score, 8)
        self.assertEqual(
            Grade.objects.get(student=self.s1, lesson=l2).score, 10)

    def test_lessonless_slot_stays_distinct(self):
        l1 = make_lesson(self.cs_a, D, 1)
        resp = self._post([
            self._item(self.s1, 'score', '9'),
            self._item(self.s1, 'score', '6', lesson=l1.id),
        ])
        self.assertEqual(resp.json()['saved'], 2)
        legacy = Grade.objects.get(student=self.s1, lesson__isnull=True)
        self.assertEqual(legacy.score, 9)
        self.assertIsNone(legacy.lesson_id)
        self.assertIsNone(legacy.assessment_id)

    def test_wrong_lesson_for_date_rejected(self):
        l_other_day = make_lesson(self.cs_a, D_Q1_B, 1)
        resp = self._post([
            self._item(self.s1, 'score', '8', lesson=l_other_day.id)])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertFalse(
            Grade.objects.filter(student=self.s1, lesson=l_other_day).exists())

    def test_control_lesson_blocks_daily_write(self):
        l1 = make_lesson(self.cs_a, D, 1)
        make_control(self.cs_a, date=D, lesson=l1)
        resp = self._post([self._item(self.s1, 'score', '8', lesson=l1.id)])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertIn('санҷишӣ', data['results'][0]['message'])

    def test_control_date_blocks_lessonless_write(self):
        make_control(self.cs_a, date=D)
        resp = self._post([self._item(self.s1, 'score', '8')])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertFalse(Grade.objects.filter(student=self.s1, date=D).exists())

    # -- quarter lock ----------------------------------------------------------

    def test_locked_quarter_rejected_per_item(self):
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        resp = self._post([
            self._item(self.s1, 'score', '8'),
            self._item(self.s2, 'score', '9', date=D_Q1_B),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertIn('баста', data['results'][0]['message'])

    # -- partial failure / per-item results ------------------------------------

    def test_partial_failure_reports_each_item(self):
        resp = self._post([
            self._item(self.s1, 'score', '99'),
            self._item(self.s2, 'score', '7'),
        ])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 1)
        bad = self._result(data, f'score_{self.s1.id}')
        good = self._result(data, f'score_{self.s2.id}')
        self.assertFalse(bad['ok'])
        self.assertTrue(bad['message'])
        self.assertTrue(good['ok'])
        self.assertNotIn('message', good)
        self.assertEqual(
            Grade.objects.get(student=self.s2, date=D).score, 7)

    def test_duplicate_item_in_batch_no_duplicate_grade(self):
        resp = self._post([
            self._item(self.s1, 'score', '8'),
            self._item(self.s1, 'score', '8'),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 2)
        self.assertEqual(
            Grade.objects.filter(student=self.s1, date=D).count(), 1)

    def test_extra_keys_ignored(self):
        # Old-queue compatibility: params-shaped items keep working.
        resp = self._post([self._item(self.s1, 'score', '8')
                           | {'quarter': '3', 'assessment_id': '5'}])
        data = resp.json()
        self.assertEqual(data['saved'], 1)
        g = Grade.objects.get(student=self.s1, date=D)
        self.assertIsNone(g.assessment_id)   # assessment key never binds

    # -- payload robustness ------------------------------------------------------

    def test_malformed_payload(self):
        resp = self.client.post(DAILY_SAVE_URL, {'payload': 'not-json{'})
        self.assertFalse(resp.json()['success'])

    def test_non_list_payload(self):
        resp = self.client.post(DAILY_SAVE_URL, {'payload': '{"a": 1}'})
        self.assertFalse(resp.json()['success'])

    def test_non_dict_item_reported(self):
        resp = self._post([123, self._item(self.s1, 'score', '8')])
        data = resp.json()
        self.assertFalse(data['results'][0]['ok'])
        self.assertTrue(data['results'][1]['ok'])
        self.assertEqual(data['saved'], 1)

    def test_empty_payload_ok(self):
        resp = self._post([])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], 0)

    def test_get_rejected(self):
        resp = self.client.get(DAILY_SAVE_URL)
        self.assertEqual(resp.status_code, 405)

    # -- atomicity ---------------------------------------------------------------

    def test_unexpected_error_rolls_back_whole_batch(self):
        orig = views._save_journal_cell
        calls = [0]

        def flaky(user, item, ctx=None):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError('boom')
            return orig(user, item, ctx)

        with mock.patch.object(
            views, '_save_journal_cell', side_effect=flaky
        ):
            resp = self._post([
                self._item(self.s1, 'score', '8'),
                self._item(self.s2, 'score', '9'),
            ])
        data = resp.json()
        self.assertFalse(data['success'])
        # Nothing committed — the client keeps every item pending.
        self.assertFalse(Grade.objects.filter(
            student__in=[self.s1, self.s2], subject=MATH).exists())


# ---------------------------------------------------------------------------
# grade_entry page wiring — batched-save UI must be present in the template
# ---------------------------------------------------------------------------

class DailyBatchWiringTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        self.url = reverse('grade_entry', kwargs={
            'school_id': self.school_a.id, 'class_name': '7-А',
            'subject': MATH,
        })

    def test_page_renders(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'portal/grade_entry.html')

    def test_batch_endpoint_url_present(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn(reverse('daily_save'), html)

    def test_save_bar_and_leave_modal_markup(self):
        html = self.client.get(self.url).content.decode()
        for marker in (
            'id="save-bar"', 'id="save-count"', 'id="leave-modal"',
            'id="lv-save"', 'id="lv-stay"', 'id="lv-leave"',
        ):
            self.assertIn(marker, html)

    def test_tajik_unsaved_warning_strings(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Шумо баҳоҳои сабтнашуда доред', html)
        self.assertIn('Сабт ва гузаштан', html)
        self.assertIn('Дар саҳифа мондан', html)
        self.assertIn('Бе сабт баромадан', html)

    def test_navigation_guard_hooks_present(self):
        html = self.client.get(self.url).content.decode()
        for marker in ('beforeunload', 'requestNav', 'hasPending',
                       'flushDailyBatch', 'savePendingNow'):
            self.assertIn(marker, html)

    def test_legacy_single_save_endpoint_still_wired(self):
        # Assessment-linked and quarterly cells keep the per-cell endpoint.
        html = self.client.get(self.url).content.decode()
        self.assertIn(reverse('save_grade_ajax'), html)

    def test_writes_use_immutable_page_date(self):
        # Cancelled date navigation must never desync writes: saveDaily's
        # params and the localStorage queue key bind the rendered page date,
        # not the mutable date-picker input.
        html = self.client.get(self.url).content.decode()
        self.assertIn('const pageDate = "', html)
        self.assertIn('date: pageDate', html)
        self.assertIn('_${pageDate}', html)
        self.assertNotIn("getElementById('daily-date').value", html)

    def test_stay_button_restores_page_date(self):
        # "Дар саҳифа мондан" resets the picker to the rendered page date.
        html = self.client.get(self.url).content.decode()
        self.assertIn('dd.value = pageDate', html)

    def test_save_pending_now_releases_inflight_on_error(self):
        # A rejection inside syncPending must not wedge the save UI.
        html = self.client.get(self.url).content.decode()
        self.assertIn('.catch(', html)
        self.assertIn('.finally(', html)
        self.assertIn('flushInFlight = false', html)

    def test_offline_edit_gets_pending_visual_state(self):
        # The pending field styling is applied before the connectivity
        # check, so offline edits show the amber state immediately.
        html = self.client.get(self.url).content.decode()
        branch = html.index('if (!assessmentId) {')
        pending = html.index("input.classList.add('field-pending')", branch)
        offline = html.index(
            'if (!navigator.onLine) showOfflineBanner();', branch
        )
        self.assertLess(pending, offline)

    # -- keyboard navigation wiring -------------------------------------------

    def test_score_enter_uses_vertical_mover(self):
        # Enter on a "Хол" cell moves down the score column, not across
        # the row: .daily-input is bound to a column-scoped mover.
        html = self.client.get(self.url).content.decode()
        self.assertIn('function onScoreKeydown', html)
        self.assertIn('function moveFocusVertical', html)
        self.assertIn("querySelectorAll('.daily-input')", html)
        self.assertIn("'keydown', onScoreKeydown", html)

    def test_shift_enter_direction_and_prevent_default(self):
        # Shift+Enter steps up (-1) vs Enter down (+1), and Enter still
        # suppresses the implicit grade-form submission.
        html = self.client.get(self.url).content.decode()
        fn = html.index('function onScoreKeydown')
        body = html.index('moveFocusVertical(this', fn)
        self.assertIn('e.preventDefault()', html[fn:body])
        self.assertIn('e.shiftKey ? -1 : 1', html[fn:body])

    def test_arrows_and_tab_not_intercepted(self):
        # ArrowUp/ArrowDown keep their native number-input stepping;
        # Tab is never intercepted anywhere in this page.
        html = self.client.get(self.url).content.decode()
        script = html[html.index('<script>'):html.rindex('</script>')]
        self.assertNotIn('ArrowUp', script)
        self.assertNotIn('ArrowDown', script)
        self.assertNotIn("'Tab'", script)
        self.assertNotIn('"Tab"', script)

    def test_other_fields_keep_horizontal_enter(self):
        # Attendance/behavior/quarterly keep the existing flat mover.
        html = self.client.get(self.url).content.decode()
        self.assertIn('function onGradeKeydown', html)
        self.assertIn('function moveFocus(', html)
        self.assertIn(
            "querySelectorAll('.daily-att, .daily-behavior, "
            ".q-input, .att-input')",
            html,
        )

    def test_assessment_cells_keep_flat_mover(self):
        # ascore_* cells reuse .daily-input; the score handler must fall
        # back to moveFocus for them.
        html = self.client.get(self.url).content.decode()
        fn = html.index('function onScoreKeydown')
        body = html.index('moveFocusVertical(this', fn)
        self.assertIn('this.dataset.assessmentId', html[fn:body])
        self.assertIn('moveFocus(this, e.shiftKey ? -1 : 1)',
                      html[fn:body])


# ---------------------------------------------------------------------------
# Per-batch memoization — invariant lookups are resolved once per request.
# These tests lock the boundary: faster, never different.
# ---------------------------------------------------------------------------

class DailyBatchMemoizationTests(GoldenBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.teacher_a)
        # Warm the request pipeline once: the first authenticated POST of
        # the day performs one-off session/DailyActivity writes that would
        # skew a query-count comparison.
        self.client.post(DAILY_SAVE_URL, {'payload': '[]'})

    def _post(self, items):
        return self.client.post(
            DAILY_SAVE_URL, {'payload': json.dumps(items)}
        )

    def _item(self, student, field='score', value='8', subject=MATH,
              date=D, lesson=None):
        item = {
            'key': f'{field}_{student.id}',
            'student_id': student.id,
            'subject': subject,
            'date': date.isoformat(),
            'lesson_id': str(lesson) if lesson else '',
        }
        item[field] = value
        return item

    def test_invariant_lookups_do_not_scale_with_items(self):
        # Six cells of one column share (school, class, subject, date):
        # permission, ClassSubject, scope and lock checks must be resolved
        # once, while per-item work (student fetch, grade read/write) keeps
        # running for every item.
        students = [self.s1, self.s2] + [
            make_student(self.school_a, '7-А', f'Хонандаи {i}')
            for i in range(4)
        ]
        with CaptureQueriesContext(connection) as one:
            resp = self._post([self._item(students[0])])
        self.assertTrue(resp.json()['success'])
        with CaptureQueriesContext(connection) as many:
            resp = self._post([self._item(s) for s in students])
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['saved'], len(students))
        # Sub-linear total: one-item request does ~the same per-cell work.
        self.assertLess(len(many), len(one) * len(students) // 2)
        hits = lambda table: sum(
            1 for q in many if table in q['sql'].lower())
        # Each invariant lookup runs once per batch, not once per item —
        # permission CS filter + CS lookup, and the lesson-less Assessment
        # scope check JOINs classsubject (all resolved once):
        self.assertLessEqual(hits('portal_classsubject'), 3)
        self.assertLessEqual(hits('portal_quarterlock'), 1)
        self.assertLessEqual(hits('portal_assessment'), 1)
        # Per-item work still happens per item: 6 student fetches.
        self.assertGreaterEqual(
            hits('portal_student'), len(students))

    def test_memoization_does_not_leak_permission_across_subjects(self):
        # An allowed item must not make a denied subject's lookup succeed:
        # the memo is keyed per (school, class, subject).
        resp = self._post([
            self._item(self.s1, subject=MATH),
            self._item(self.s1, subject=TAJIK),   # teacher_a not allocated
            self._item(self.s2, subject=MATH),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 2)
        denied = next(r for r in data['results'] if not r['ok'])
        self.assertIn('Дастрасӣ', denied['message'])
        self.assertFalse(
            Grade.objects.filter(student=self.s1, subject=TAJIK).exists())

    def test_memoization_enforces_group_membership_per_student(self):
        # Grouped CS: teacher's own group contains only s1 — the per-CS
        # writable set must still reject s2 inside the same batch.
        profile = make_teacher_profile(self.teacher_a, self.school_a)
        group = TeachingGroup.objects.create(
            class_subject=self.cs_a, label='Гурӯҳи 1', teacher=profile)
        SubjectGroupMembership.objects.create(
            class_subject=self.cs_a, group=group, student=self.s1)
        resp = self._post([
            self._item(self.s1, 'score', '8'),
            self._item(self.s2, 'score', '9'),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 1)
        self.assertTrue(
            Grade.objects.filter(student=self.s1, date=D).exists())
        self.assertFalse(
            Grade.objects.filter(student=self.s2, date=D).exists())

    def test_scope_error_memoized_with_identical_message(self):
        # Same forged lesson for two students: one resolved (rejected)
        # outcome, identical per-item message — no cross-item leakage.
        lesson = make_lesson(self.cs_a, D_Q1_B, 1)  # wrong date for D
        resp = self._post([
            self._item(self.s1, lesson=lesson.id),
            self._item(self.s2, lesson=lesson.id),
        ])
        data = resp.json()
        self.assertEqual(data['saved'], 0)
        self.assertFalse(data['results'][0]['ok'])
        self.assertFalse(data['results'][1]['ok'])
        self.assertEqual(
            data['results'][0]['message'], data['results'][1]['message'])

    def test_monthly_save_unchanged_uses_fresh_per_item_context(self):
        # monthly_save intentionally does not share a memo across items.
        from portal.tests.helpers import make_grade as _mk  # noqa
        resp = self.client.post(
            reverse('monthly_save'),
            {'payload': json.dumps([
                {'key': 'a', 'student_id': self.s1.id, 'subject': MATH,
                 'date': D.isoformat(), 'lesson_id': '', 'score': '8'},
            ])},
        )
        self.assertTrue(resp.json()['success'])
        self.assertEqual(
            Grade.objects.get(student=self.s1, date=D).score, 8)
