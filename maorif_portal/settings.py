"""
Django settings for maorif_portal project.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-change-me-before-production'

DEBUG = True

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
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
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
    '1': ["Забони модарӣ", "Алифбо", "Математика", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Қоидаҳои ҳаракат дар роҳ"],
    '2': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Алифбои мусиқӣ", "Суруд ва мусиқӣ", "Санъат ва меҳнат", "Табиатшиносӣ", "Қоидаҳои ҳаракат дар роҳ"],
    '3': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Забони англисӣ", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Тарзи ҳаёти солим", "Забони арабӣ"],
    '4': ["Забони модарӣ", "Математика", "Русская речь (Нутқи русӣ)", "Забони англисӣ", "Табиатшиносӣ", "Санъат ва меҳнат", "Суруд ва мусиқӣ"],
    '5': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Математика", "Ботаника", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Технологияи иттилоотӣ", "Технология", "Санъати тасвирӣ", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ"],
    '6': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Математика", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Ботаника", "Технологияи иттилоотӣ", "Технология", "Санъати тасвирӣ", "Суруд ва мусиқӣ", "Тарбияи ҷисмонӣ", "Асосҳои бехатарии ҳаёт", "Забони арабӣ", "Забони немисӣ", "Забони франсавӣ"],
    '7': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Забони немисӣ", "Алгебра", "Геометрия", "География", "Физика", "Зоология", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Технологияи иттилоотӣ", "Алифбо ва матни ниёгон", "Санъати тасвирӣ", "Тарбияи ҷисмонӣ", "Асосҳои бехатарии ҳаёт"],
    '8': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Забони немисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Зоология", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Алифбо ва матни ниёгон", "Технологияи иттилоотӣ", "Технология", "Асосҳои давлат ва ҳуқуқи Ҷумҳурии Тоҷикистон", "Одоби муошират ва рӯзгордорӣ", "Асосҳои касбу ҳунар"],
    '9': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биология", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Таърихи дин", "Асосҳои давлат ва ҳуқуқи Ҷумҳурии Тоҷикистон", "Технологияи иттилоотӣ", "Технология", "Нақшакашӣ", "Асосҳои касбу ҳунар"],
    '10': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биологияи умумӣ", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Ҳуқуқи инсон", "Технологияи иттилоотӣ", "Технология", "Тарбияи ҷисмонӣ", "Омодагии ибтидоии ҳарбӣ", "Асосҳои иқтисодиёт", "Асосҳои интихоби касб"],
    '11': ["Забони тоҷикӣ", "Адабиёти тоҷик", "Забони русӣ", "Забони англисӣ", "Забони немисӣ", "Забони франсавӣ", "Забони арабӣ", "Алгебра", "Геометрия", "Физика", "Химия", "Биологияи умумӣ", "География", "Таърихи халқи тоҷик", "Таърихи умумӣ", "Нуҷум (астрономия)", "Технологияи иттилоотӣ", "Ҳуқуқи инсон", "Асосҳои иқтисодиёт", "Омодагии ибтидоии ҳарбӣ", "Асосҳои интихоби касб", "Тарбияи ҷисмонӣ"],
}

# Non-graded levels (no numeric grades)
NON_GRADED_CLASSES = ['0', '1']  # Kindergarten / 1-sinf uses qualitative markers
