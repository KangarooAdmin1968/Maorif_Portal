"""
WSGI config for maorif_portal project.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')

application = get_wsgi_application()
