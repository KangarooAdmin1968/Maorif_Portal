"""
Django settings for maorif_portal project.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-change-me-before-production'

# Local development keeps DEBUG=True. Production sets DJANGO_DEBUG=0 in the
# Gunicorn launch environment (deploy/gunicorn.service, deploy_to_server.py,
# deploy_hotfix.py, .github/workflows/deploy.yml).
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'portal.apps.PortalConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'portal.middleware.DailyActivityMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'maorif_portal.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'portal' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'portal.context_processors.user_role',
                'portal.context_processors.parent_portal',
            ],
        },
    },
]

WSGI_APPLICATION = 'maorif_portal.wsgi.application'
ASGI_APPLICATION = 'maorif_portal.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
    # Production example (PostgreSQL):
    # 'default': {
    #     'ENGINE': 'django.db.backends.postgresql',
    #     'NAME': 'maorif_portal',
    #     'USER': 'maorif_user',
    #     'PASSWORD': 'secure_password',
    #     'HOST': 'localhost',
    #     'PORT': '5432',
    # }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 6,
        }
    },
]

LANGUAGE_CODE = 'tg'
TIME_ZONE = 'Asia/Dushanbe'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# Dev-only: extra CSRF-trusted origins for local preview proxies (e.g. browser
# preview on a random localhost port). Inactive in production (DEBUG=False).
if DEBUG:
    CSRF_TRUSTED_ORIGINS = [
        o.strip()
        for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
        if o.strip()
    ]

# Server-error visibility: Django's default LOGGING sends 5xx request errors
# only to AdminEmailHandler (mail_admins), which calls send_mail with
# fail_silently=True. With no reachable SMTP the traceback is swallowed and
# nothing reaches the gunicorn error log. Keep those defaults and
# additionally write ERROR-level django.request records (message + full
# traceback only — never POST bodies or headers) to a small rotating file.
# Override the location with DJANGO_ERROR_LOG (e.g. to place it under
# /var/log/gunicorn/, which deploy/gunicorn-logrotate already rotates).
DJANGO_ERROR_LOG = os.environ.get(
    'DJANGO_ERROR_LOG', str(BASE_DIR / 'logs' / 'django_errors.log'))
try:
    # RotatingFileHandler does not create missing parent directories.
    Path(DJANGO_ERROR_LOG).parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # logging will report the problem on first write instead

LOGGING = {
    'version': 1,
    # Merge into Django's DEFAULT_LOGGING (console handler in dev, mail_admins
    # in prod) rather than replacing it.
    'disable_existing_loggers': False,
    'formatters': {
        'django_error': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'handlers': {
        'django_error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': DJANGO_ERROR_LOG,
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'encoding': 'utf-8',
            'formatter': 'django_error',
            'delay': True,  # create the file only when an error occurs
        },
    },
    'loggers': {
        # Handler level=ERROR keeps 4xx warnings out of the file;
        # propagate=True keeps records flowing to 'django' (console +
        # mail_admins) exactly as before.
        'django.request': {
            'handlers': ['django_error_file'],
            'propagate': True,
        },
    },
}

# Role constants
ROLE_DIRECTOR = 'director'
ROLE_PRINCIPAL = 'principal'
ROLE_TEACHER = 'teacher'

ROLE_CHOICES = [
    (ROLE_DIRECTOR, 'Раиси маориф'),
    (ROLE_PRINCIPAL, 'Директори муассиса'),
    (ROLE_TEACHER, 'Муаллим'),
]

# Tajikistan national curriculum (default subjects per grade)
TJC_SUBJECTS = {
    '1': ["Забони модарӣ", "Алифбо", "Математика", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Қоидаҳои ҳаракат дар роҳ", "Соати тарбиявӣ"],
    '2': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Суруд ва мусиқӣ", "Санъат ва меҳнат", "Табиатшиносӣ", "Тарбияи ҷисмонӣ", "Қоидаҳои ҳаракат дар роҳ", "Соати тарбиявӣ"],
    '3': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Забони англисӣ", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
    '4': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Забони англисӣ", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
    '5': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Математика", "Ботаника", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Технологияи иттилоотӣ", "Технология", "Санъати тасвирӣ", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
    '6': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Математика", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Ботаника", "Технологияи иттилоотӣ", "Технология", "Санъати тасвирӣ", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
    '7': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "География", "Физика", "Зоология", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Технологияи иттилоотӣ", "Алифбо ва матни ниёгон", "Санъати тасвирӣ", "Тарбияи ҷисмонӣ", "Технология", "Соати тарбиявӣ"],
    '8': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Зоология", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Алифбо ва матни ниёгон", "Технологияи иттилоотӣ", "Технология", "Асосҳои давлат ва ҳуқуқи Ҷумҳурии Тоҷикистон", "Нақшакашӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
    '9': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биология", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Таърихи дин", "Асосҳои давлат ва ҳуқуқи Ҷумҳурии Тоҷикистон", "Технологияи иттилоотӣ", "Технология", "Нақшакашӣ", "Тарбияи ҷисмонӣ", "Экология", "Соати тарбиявӣ"],
    '10': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биологияи умумӣ", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Ҳуқуқи инсон", "Маърифати оиладорӣ", "Технологияи иттилоотӣ", "Технология", "Тарбияи ҷисмонӣ", "Омодагии ибтидоии ҳарбӣ", "Адабиёти ҷаҳон", "Нуҷум (Астрономия)", "Соати тарбиявӣ"],
    '11': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биологияи умумӣ", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Нуҷум (астрономия)", "Технологияи иттилоотӣ", "Ҳуқуқи инсон", "Асосҳои иқтисодиёт", "Омодагии ибтидоии ҳарбӣ", "Тарбияи ҷисмонӣ", "Соати тарбиявӣ"],
}

# Non-graded levels (no numeric grades)
NON_GRADED_CLASSES = ['0', '1']

# Weekly lesson hours per subject (used by Ҳуҷҷатнигорӣ workload documents).
TJC_SUBJECT_HOURS = {
    'ЗАБОНИ МОДАРӢ': 5, 'АЛИФБО': 4, 'ЗАБОНИ ТОҶИКӢ': 4, 'АДАБИЁТИ ТОҶИК': 2,
    'МАТЕМАТИКА': 5, 'АЛГЕБРА': 4, 'ГЕОМЕТРИЯ': 2,
    'ЗАБОНИ РУСӢ': 3, 'РУССКАЯ РЕЧЬ (НУТҚИ РУСӢ)': 3, 'ЗАБОНИ АНГЛИСӢ': 3,
    'ФИЗИКА': 2, 'ХИМИЯ': 2, 'БИОЛОГИЯ': 2, 'БИОЛОГИЯИ УМУМӢ': 2,
    'ГЕОГРАФИЯ': 2, 'ТАЪРИХИ ХАЛҚИ ТОҶИК': 2, 'ТАЪРИХИ УМУМӢ': 2,
    'ТАБИАТШИНОСӢ': 2, 'ТЕХНОЛОГИЯИ ИТТИЛООТӢ': 1, 'ТЕХНОЛОГИЯ': 2,
    'САНЪАТ ВА МЕҲНАТ': 2, 'САНЪАТИ ТАСВИРӢ': 1, 'СУРУД ВА МУСИҚӢ': 1,
    'ТАРБИЯИ ҶИСМОНӢ': 2, 'СОАТИ ТАРБИЯВӢ': 1, 'НАҚШАКАШӢ': 1,
    'ОМОДАГИИ ИБТИДОИИ ҲАРБӢ': 2, 'ҲУҚУҚИ ИНСОН': 1, 'НУҶУМ (АСТРОНОМИЯ)': 1,
    'АСОСҲОИ ИҚТИСОДИЁТ': 1, 'АСОСҲОИ ДАВЛАТ ВА ҲУҚУҚИ ҶУМҲУРИИ ТОҶИКИСТОН': 1,
    'ҚОИДАҲОИ ҲАРАКАТ ДАР РОҲ': 1, 'ТАЪРИХИ ДИН': 1,
}
TJC_SUBJECT_HOURS_DEFAULT = 2  # weekly hours for any subject not listed above  # Kindergarten / 1-sinf uses qualitative markers
