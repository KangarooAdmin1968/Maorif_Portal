"""📁 Солҳои таҳсил — quarter closeout, archive, and reopen access.

GoldenBase gives School A (7-А graded, 1-А sticker class territory via
students only) and School B (7-Б). The zavuch closes quarters for their
own school; маориф/админ reopen.
"""
import datetime
import io
import tempfile

from django.test import override_settings
from django.urls import reverse
from openpyxl import load_workbook

from portal.models import QuarterLock
from portal.quarter_close import (archive_path, auto_close_due,
                                  collect_quarter_close,
                                  save_quarter_archive)

from .helpers import (GoldenBase, MATH, TAJIK, lock_quarter,
                      make_class_subject, make_grade, make_quarter_grade,
                      make_user)


_TMP_MEDIA = tempfile.mkdtemp(prefix='qc_archive_')


@override_settings(MEDIA_ROOT=_TMP_MEDIA)
class QuarterCloseTests(GoldenBase):

    def setUp(self):
        super().setUp()
        # School A gets a second graded subject; the auto-seeded
        # curriculum subjects stay active too.
        make_class_subject(self.school_a, '7-А', TAJIK,
                           teacher=self.teacher_a)
        from portal.views import academic_year_start
        self.ay = academic_year_start()

    # ---------------------------------------------------------- status

    def test_status_counts_locks_and_missing(self):
        qs = collect_quarter_close(self.school_a, 1, self.ay)
        self.assertFalse(qs['closed'])
        self.assertGreater(qs['total'], 0)
        self.assertEqual(qs['locked_count'], 0)
        # No QuarterGrade rows yet -> every student counts as missing.
        self.assertGreater(qs['missing_total'], 0)

        make_quarter_grade(self.s1, quarter=1, grade=9)
        make_quarter_grade(self.s2, quarter=1, grade=8)
        qs = collect_quarter_close(self.school_a, 1, self.ay)
        # МАТЕМАТИКА now fully graded — its missing drops to 0, other
        # subjects keep their gaps.
        math_row = next(r for r in qs['rows']
                        if r['subject'] == MATH)
        self.assertEqual(math_row['missing'], 0)

    # ---------------------------------------------------------- close

    def test_close_requires_tasdiq_word(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.post(reverse('quarter_close'), {
            'school_id': self.school_a.id, 'quarter': 1, 'year': self.ay,
            'confirm': 'ҳа'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a).exists())

        resp = self.client.post(reverse('quarter_close'), {
            'school_id': self.school_a.id, 'quarter': 1, 'year': self.ay,
            'confirm': 'ТАСДИҚ'})
        self.assertEqual(resp.status_code, 302)
        qs = collect_quarter_close(self.school_a, 1, self.ay)
        self.assertTrue(qs['closed'])
        self.assertEqual(qs['locked_count'], qs['total'])
        # Other quarters and school B stay untouched.
        self.assertFalse(
            collect_quarter_close(self.school_a, 2, self.ay)['closed'])
        self.assertFalse(
            collect_quarter_close(self.school_b, 1, self.ay)['closed'])

    def test_close_locks_grades(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('quarter_close'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay, 'confirm': 'ТАСДИҚ'})
        self.client.force_login(self.teacher_a)
        resp = self.client.post(reverse('save_grade_ajax'), {
            'student_id': self.s1.id, 'subject': MATH,
            'grade_type': 'daily', 'score': '8',
            'date': f'{self.ay}-10-15',
        })
        self.assertIn('баста', resp.json().get('message', '').lower())

    def test_zavuch_cannot_close_other_school(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('quarter_close'), {
            'school_id': self.school_b.id, 'quarter': 1,
            'year': self.ay, 'confirm': 'ТАСДИҚ'})
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_b).exists())

    def test_teacher_cannot_close(self):
        self.client.force_login(self.teacher_a)
        self.client.post(reverse('quarter_close'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay, 'confirm': 'ТАСДИҚ'})
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a).exists())

    # ---------------------------------------------------------- reopen

    def _close_q1(self):
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('quarter_close'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay, 'confirm': 'ТАСДИҚ'})

    def test_reopen_only_maorif(self):
        self._close_q1()
        # Zavuch cannot reopen their own closed quarter.
        resp = self.client.post(reverse('quarter_reopen'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay})
        self.assertTrue(collect_quarter_close(
            self.school_a, 1, self.ay)['closed'])

        self.client.force_login(self.director)
        self.client.post(reverse('quarter_reopen'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay})
        qs = collect_quarter_close(self.school_a, 1, self.ay)
        self.assertFalse(qs['closed'])
        self.assertEqual(qs['locked_count'], 0)

    def test_reopen_button_on_partial_lock(self):
        """A partially-locked quarter must still offer Кушодан to
        маориф — the button used to appear only on fully-closed
        quarters, leaving stray locks unopenable."""
        lock_quarter(self.school_a, '7-А', MATH, quarter=1)
        self.client.force_login(self.director)
        resp = self.client.get(reverse('academic_years'), {
            'school_id': self.school_a.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Қисман баста')
        self.assertContains(resp, reverse('quarter_reopen') + '"')

    # ---------------------------------------------------- reopen all

    def test_reopen_all_unlocks_every_school(self):
        self._close_q1()                       # school A: full lock
        lock_quarter(self.school_b, '7-Б', MATH, quarter=1)  # school B
        self.client.force_login(self.director)
        until = (datetime.date.today()
                 + datetime.timedelta(days=10)).isoformat()
        resp = self.client.post(reverse('quarter_reopen_all'), {
            'quarter': 1, 'year': self.ay, 'until': until})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(QuarterLock.objects.filter(locked=True).exists())
        self.assertTrue(QuarterLock.objects.filter(
            reopened_until__isnull=False).exists())

    def test_reopen_all_denied_for_zavuch(self):
        self._close_q1()
        self.client.force_login(self.zavuch_a)
        self.client.post(reverse('quarter_reopen_all'), {
            'quarter': 1, 'year': self.ay})
        self.assertTrue(QuarterLock.objects.filter(locked=True).exists())

    def test_reopen_all_touches_only_given_quarter(self):
        self._close_q1()
        lock_quarter(self.school_a, '7-А', MATH, quarter=2)
        self.client.force_login(self.director)
        self.client.post(reverse('quarter_reopen_all'), {
            'quarter': 1, 'year': self.ay})
        self.assertFalse(QuarterLock.objects.filter(
            quarter=1, locked=True).exists())
        self.assertTrue(QuarterLock.objects.filter(
            quarter=2, locked=True).exists())

    # ---------------------------------------------------------- page

    def test_years_page_shows_quarters(self):
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(reverse('academic_years'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Соли таҳсили')
        self.assertContains(resp, 'Чоряки')

    # ---------------------------------------------------------- archive

    def test_archive_workbook_contents(self):
        make_quarter_grade(self.s1, subject=MATH, quarter=1, grade=9)
        make_quarter_grade(self.s2, subject=TAJIK, quarter=1, grade=7)
        make_grade(self.s1, attendance='-', date=datetime.date(
            self.ay, 10, 10))
        buf = save_quarter_archive(self.school_a, 1, self.ay)
        self.assertTrue(buf.exists())
        wb = load_workbook(io.BytesIO(buf.read_bytes()))
        self.assertIn('7-А', wb.sheetnames)
        ws = wb['7-А']
        self.assertEqual(ws.cell(2, 1).value, '№')
        header = [c.value for c in ws[2]]
        self.assertIn('Хонанда', header)
        self.assertIn(MATH, header)
        # Grade written to the right subject cell.
        name_col = header.index('Хонанда') + 1
        math_col = header.index(MATH) + 1
        row = None
        for r in range(3, ws.max_row + 1):
            if ws.cell(r, name_col).value == 'Фирӯз Раҳимов':
                row = r
        self.assertIsNotNone(row)
        self.assertEqual(ws.cell(row, math_col).value, 9)
        # Attendance sheet has the unexcused mark.
        self.assertIn('Давомат', wb.sheetnames)
        att = wb['Давомат']
        found = any(att.cell(r, 2).value == 'Фирӯз Раҳимов'
                    and att.cell(r, 4).value == 1
                    for r in range(3, att.max_row + 1))
        self.assertTrue(found)

    def test_archive_download(self):
        self._close_q1()
        resp = self.client.get(reverse('quarter_archive_download'), {
            'quarter': 1, 'year': self.ay})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertTrue(archive_path(
            self.school_a, 1, self.ay).exists())

    # ------------------------------------------------------- auto close

    def _q2_start(self):
        """First day of Q2 — Q1's grading window ended the day before."""
        from portal.academic_calendar import quarter_windows
        return quarter_windows(self.ay)[2][0]

    def test_auto_close_closes_due_quarter(self):
        closed = auto_close_due(self.school_a, today=self._q2_start())
        self.assertEqual(closed, [(1, self.ay)])
        self.assertTrue(collect_quarter_close(
            self.school_a, 1, self.ay)['closed'])
        self.assertTrue(archive_path(self.school_a, 1, self.ay).exists())
        # Q2 itself is not due yet.
        self.assertFalse(collect_quarter_close(
            self.school_a, 2, self.ay)['closed'])

    def test_auto_close_skips_open_window(self):
        from portal.academic_calendar import quarter_windows
        inside_q1 = quarter_windows(self.ay)[1][0]
        self.assertEqual(
            auto_close_due(self.school_a, today=inside_q1), [])
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a).exists())

    def test_auto_close_never_relocks_reopened(self):
        """A маориф reopen leaves locked=False rows — automation must
        leave that quarter open for the zavuch to close by hand."""
        self._close_q1()
        QuarterLock.objects.filter(
            school=self.school_a, quarter=1,
            academic_year=self.ay).update(locked=False)
        closed = auto_close_due(self.school_a, today=self._q2_start())
        self.assertEqual(closed, [])
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a, quarter=1, locked=True).exists())

    def test_auto_close_relocks_expired_reopen(self):
        """Маориф opened with «то <date>»: once that date passes the
        cron locks the quarter again and refreezes the archive."""
        self._close_q1()
        # Маориф reopens for a 10-day window via the view.
        self.client.force_login(self.director)
        until = self._q2_start() + datetime.timedelta(days=9)
        self.client.post(reverse('quarter_reopen'), {
            'school_id': self.school_a.id, 'quarter': 1,
            'year': self.ay, 'until': until.isoformat()})
        qs = collect_quarter_close(self.school_a, 1, self.ay)
        self.assertFalse(qs['closed'])
        self.assertEqual(qs['reopened_until'], until)

        # Inside the window: automation leaves it open.
        closed = auto_close_due(self.school_a, today=until)
        self.assertEqual(closed, [])
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a, quarter=1, locked=True).exists())

        # The day after «то»: cron re-locks and refreezes the archive.
        closed = auto_close_due(
            self.school_a, today=until + datetime.timedelta(days=1))
        self.assertEqual(closed, [(1, self.ay)])
        locks = QuarterLock.objects.filter(
            school=self.school_a, quarter=1,
            academic_year=self.ay, locked=True)
        self.assertTrue(locks.exists())
        self.assertTrue(locks.filter(reopened_until__isnull=True)
                        .exists())
        self.assertTrue(archive_path(self.school_a, 1, self.ay)
                        .exists())

    def test_close_due_quarters_command(self):
        import io as _io
        from django.core.management import call_command
        out = _io.StringIO()
        # Dry-run writes nothing even when the window has passed —
        # force it by running with --apply only.
        call_command('close_due_quarters', stdout=out)
        self.assertIn('DRY-RUN', out.getvalue())
        self.assertFalse(QuarterLock.objects.filter(
            school=self.school_a).exists())
