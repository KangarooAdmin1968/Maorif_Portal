"""Server-error file logging (settings.LOGGING).

Production had no on-disk record of 500s: the only handler receiving
django.request errors under Django's default LOGGING is AdminEmailHandler,
which calls send_mail(fail_silently=True), so a dead/unconfigured SMTP
silently swallowed every traceback. These tests pin the wiring of the added
django_error_file RotatingFileHandler and prove a real request exception
lands in it with a traceback — while normal requests write nothing and the
request body never reaches the log.
"""
import logging
import logging.handlers
import os
import shutil
import tempfile

from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import path
from django.utils.log import AdminEmailHandler

PROBE_MESSAGE = 'LOGGING_TEST_PROBE_7f3b'
PROBE_SECRET = 's3cr3t-body-probe-9x2'
PROBE_COOKIE = 'sessionid-probe-4k8'


def _boom_view(request):
    raise ValueError(PROBE_MESSAGE)


def _ok_view(request):
    return HttpResponse('ok')


urlpatterns = [
    path('__boom__/', _boom_view),
    path('__ok__/', _ok_view),
]


@override_settings(ROOT_URLCONF=__name__)
class ErrorFileLoggingTests(TestCase):
    def setUp(self):
        logger = logging.getLogger('django.request')
        self.handler = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        # Redirect the real configured handler at a temp file so tests never
        # create logs/ artifacts inside the repository.
        self._tmp = tempfile.mkdtemp(prefix='maorif_errlog_')
        self._orig_filename = self.handler.baseFilename
        self.handler.close()
        self.handler.baseFilename = os.path.join(self._tmp, 'django_errors.log')

    def tearDown(self):
        self.handler.close()
        self.handler.baseFilename = self._orig_filename
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _log_text(self):
        if not os.path.exists(self.handler.baseFilename):
            return ''
        with open(self.handler.baseFilename, encoding='utf-8') as f:
            return f.read()

    # -- configuration --------------------------------------------------------

    def test_error_file_handler_attached_to_django_request(self):
        self.assertEqual(self.handler.level, logging.ERROR)
        self.assertGreater(self.handler.maxBytes, 0)
        self.assertGreater(self.handler.backupCount, 0)
        self.assertTrue(self._orig_filename.endswith('django_errors.log'))
        self.assertTrue(os.path.isabs(self._orig_filename))

    def test_default_handlers_and_propagation_retained(self):
        # AdminEmailHandler (mail_admins) must still receive request errors
        # via propagation to the 'django' logger.
        django_handlers = logging.getLogger('django').handlers
        self.assertTrue(any(
            isinstance(h, AdminEmailHandler)
            for h in django_handlers
        ))
        self.assertTrue(logging.getLogger('django.request').propagate)

    # -- behavior --------------------------------------------------------------

    def test_request_exception_writes_traceback_to_file(self):
        self.client.cookies.load({'sessionid': PROBE_COOKIE})
        with self.assertRaises(ValueError):
            self.client.post(
                f'/__boom__/?api_token={PROBE_SECRET}',
                {'password': PROBE_SECRET},
            )
        text = self._log_text()
        self.assertIn(PROBE_MESSAGE, text)
        self.assertIn('Traceback', text)
        self.assertIn('ValueError', text)
        self.assertIn('Internal Server Error: /__boom__/', text)

    def test_request_body_query_and_cookies_not_logged(self):
        self.client.cookies.load({'sessionid': PROBE_COOKIE})
        with self.assertRaises(ValueError):
            self.client.post(
                f'/__boom__/?api_token={PROBE_SECRET}',
                {'password': PROBE_SECRET},
            )
        text = self._log_text()
        self.assertNotIn(PROBE_SECRET, text)
        self.assertNotIn(PROBE_COOKIE, text)

    def test_successful_request_writes_nothing(self):
        resp = self.client.get('/__ok__/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(self.handler.baseFilename))
