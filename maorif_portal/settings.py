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
    '1': ['Забони тоҷикӣ', 'Математика', 'Табиатшиносӣ', 'Тарбияи ҷисмонӣ', 'Расм', 'Мусиқӣ', 'Дасткорӣ'],
    '2': ['Забони тоҷикӣ', 'Математика', 'Табиатшиносӣ', 'Забони русӣ', 'Тарбияи ҷисмонӣ', 'Расм', 'Мусиқӣ', 'Дасткорӣ'],
    '3': ['Забони тоҷикӣ', 'Математика', 'Табиатшиносӣ', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Расм', 'Мусиқӣ', 'Дасткорӣ'],
    '4': ['Забони тоҷикӣ', 'Математика', 'Табиатшиносӣ', 'Таърих', 'География', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Расм', 'Мусиқӣ', 'Дасткорӣ'],
    '5': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Математика', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'География', 'Биология', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Технология'],
    '6': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Математика', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'География', 'Биология', 'Физика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Технология'],
    '7': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Алгебра', 'Геометрия', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'География', 'Биология', 'Физика', 'Химия', 'Информатика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ'],
    '8': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Алгебра', 'Геометрия', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'География', 'Биология', 'Физика', 'Химия', 'Информатика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ'],
    '9': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Алгебра', 'Геометрия', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'География', 'Биология', 'Физика', 'Химия', 'Информатика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ'],
    '10': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Алгебра', 'Геометрия', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'Физика', 'Химия', 'Биология', 'Информатика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Маърифати оила'],
    '11': ['Забони тоҷикӣ', 'Адабиёти тоҷик', 'Алгебра', 'Геометрия', 'Таърихи халқи тоҷик', 'Таърихи умумӣ', 'Физика', 'Химия', 'Биология', 'Информатика', 'Забони русӣ', 'Забони англисӣ', 'Тарбияи ҷисмонӣ', 'Астрономия', 'ОИХ'],
}

# Non-graded levels (no numeric grades)
NON_GRADED_CLASSES = ['0', '1']  # Kindergarten / 1-sinf uses qualitative markers
