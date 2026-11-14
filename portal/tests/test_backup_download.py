"""Superuser database-backup download.

Access control and the snapshot mechanics: the view must let only a
superuser through, produce a valid SQLite copy via the online backup
API, stream it (FileResponse under DEBUG / X-Accel-Redirect in prod),
and never let the snapshot directory grow unbounded.
"""
import os
import sqlite3
import tempfile

from django.core.cache import cache
from django.test import TransactionTestCase
from django.urls import reverse

from portal.tests.helpers import make_user


# TransactionTestCase, not TestCase: the view snapshots the DB with
# sqlite's backup API, which blocks forever while the wrapping test
# transaction holds the shared in-memory database open.
class BackupDownloadTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._override = self.settings(
            BACKUP_SNAPSHOT_DIR=self._tmpdir.name)
        self._override.enable()
        self.addCleanup(self._override.disable)
        self.url = reverse('backup_download')
        self.admin = make_user('admin_bk', superuser=True)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/login/'))

    def test_regular_user_forbidden(self):
        plain = make_user('plain_bk')
        self.client.force_login(plain)
        response = self.client.get(self.url)
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_superuser_gets_valid_sqlite_snapshot(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # Production path (DEBUG=False, as in tests): nginx serves the
        # snapshot via X-Accel-Redirect from the internal location.
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('maorif_backup_', response['Content-Disposition'])
        self.assertTrue(response['X-Accel-Redirect'].startswith(
            '/protected-backup/maorif_snapshot_'))
        # The snapshot on disk is a real, queryable database.
        snaps = [
            f for f in os.listdir(self._tmpdir.name)
            if f.startswith('maorif_snapshot_')]
        self.assertEqual(len(snaps), 1)
        snap = os.path.join(self._tmpdir.name, snaps[0])
        conn = sqlite3.connect(snap)
        try:
            self.assertEqual(
                conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            conn.close()

    def test_only_two_snapshots_kept(self):
        self.client.force_login(self.admin)
        for _ in range(3):
            self.client.get(self.url)
        snaps = [
            f for f in os.listdir(self._tmpdir.name)
            if f.startswith('maorif_snapshot_')]
        self.assertLessEqual(len(snaps), 2)

    def test_busy_lock_blocks_second_snapshot(self):
        cache.set('backup-snapshot-busy', 1, 300)
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertRedirects(response, '/', fetch_redirect_response=False)
