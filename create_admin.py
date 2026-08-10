import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'maorif_portal.settings')
django.setup()

from django.contrib.auth.models import User

# Remove any existing admin user to ensure a clean record
User.objects.filter(username='admin').delete()

# Create a fresh superuser
User.objects.create_superuser('admin', '', 'admin123')
print("SUPERUSER CREATED SUCCESSFULLY: admin / admin123")
