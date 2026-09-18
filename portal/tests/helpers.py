"""Shared fixture factories for the golden-master regression suite.

Test data is intentionally small and deterministic:

    School A ("Мактаб №1")
        class 7-А — graded level, subject МАТЕМАТИКА, teacher_1 assigned
        class 1-А — non-graded level (sticker journal territory)
    School B ("Мактаб №2")
        class 7-Б — graded level, teacher_2 assigned

Users mirror production conventions:
    admin        — is_superuser
    director_1   — role='director', no school (district-wide access)
    principal_1  — role='principal', School A
    zavuch_1     — role='teacher' but username prefix 'zavuch_' (like zavuch_20
                   in the real DB), School A
    teacher_1    — role='teacher', School A, assigned to 7-А МАТЕМАТИКА
    teacher_2    — role='teacher', School B, assigned to 7-Б МАТЕМАТИКА
"""
import datetime

from django.contrib.auth.models import User
from django.test import TestCase

from portal.models import (
    ClassSubject,
    Grade,
    QuarterGrade,
    QuarterLock,
    School,
    Student,
    SubjectDeactivationRequest,
    TeacherProfile,
    UserProfile,
)

# Canonical normalized values used across the suite.
MATH = 'МАТЕМАТИКА'
TAJIK = 'ЗАБОНИ ТОҶИКӢ'
CUSTOM_SUBJECT = 'ФАНИ ФАЪОЛИЯТ'  # not in TJC_SUBJECTS — used for seeding tests
PERIOD = 'Холҳои ҷорӣ (Онлайн)'

# Dates inside each academic quarter (get_date_quarter: Sep-Nov=1, Dec-Feb=2,
# Mar-May=3, Jun-Aug=4).
D_Q1 = datetime.date(2025, 10, 15)
D_Q1_B = datetime.date(2025, 11, 20)
D_Q2 = datetime.date(2026, 1, 15)
D_Q3 = datetime.date(2026, 4, 10)
D_Q4 = datetime.date(2026, 6, 5)


def make_school(name, type='Мактаб', language='Тоҷикӣ'):
    return School.objects.create(name=name, type=type, language=language)


def make_user(username, role=None, school=None, superuser=False, staff=False):
    user = User.objects.create_user(username=username, password='pw_test_123')
    user.is_superuser = superuser
    user.is_staff = staff or superuser
    user.save()
    if role is not None:
        UserProfile.objects.create(user=user, role=role, school=school)
    return user


def make_teacher_profile(user, school, full_name='Омӯзгор Санъатбар'):
    return TeacherProfile.objects.create(
        user=user, school=school, full_name=full_name,
    )


def make_student(school, class_name, full_name, gender=None):
    """Create a Student; save() computes the string PK and auto-detects
    gender when left unset, and seeds default ClassSubjects for the class."""
    kwargs = {'school': school, 'class_name': class_name, 'full_name': full_name}
    if gender is not None:
        kwargs['gender'] = gender
    return Student.objects.create(**kwargs)


def make_class_subject(school, class_name, subject, teacher=None,
                       allocated=None, is_active=True, is_default=False):
    """Attach/update a ClassSubject row (students may have already seeded it)."""
    obj, _ = ClassSubject.objects.update_or_create(
        school=school, class_name=class_name, subject=subject,
        defaults={
            'teacher': teacher,
            'allocated_teacher': allocated,
            'is_active': is_active,
            'is_default': is_default,
        },
    )
    return obj


def make_grade(student, subject=MATH, score=None, date=D_Q1, attendance=None,
               behavior=None, sticker=None, period=PERIOD):
    return Grade.objects.create(
        student=student, subject=subject, score=score, period=period,
        date=date, attendance=attendance, behavior_score=behavior,
        sticker=sticker,
    )


def make_quarter_grade(student, class_name='7-А', subject=MATH, quarter=1,
                       grade=None, att=None):
    return QuarterGrade.objects.create(
        student=student, class_name=class_name, subject=subject,
        quarter=quarter, grade=grade, att_grade=att,
    )


def lock_quarter(school, class_name='7-А', subject=MATH, quarter=1, locked=True):
    return QuarterLock.objects.create(
        school=school, class_name=class_name, subject=subject,
        quarter=quarter, locked=locked,
    )


def make_deactivation_request(class_subject, user, status='approved'):
    return SubjectDeactivationRequest.objects.create(
        class_subject=class_subject, requested_by=user, status=status,
    )


class GoldenBase(TestCase):
    """Deterministic two-school fixture shared by the whole suite."""

    @classmethod
    def setUpTestData(cls):
        cls.school_a = make_school('Мактаб №1')
        cls.school_b = make_school('Мактаб №2')

        cls.admin = make_user('admin_test', superuser=True)
        cls.director = make_user('director_1', role='director')
        cls.principal_a = make_user('principal_1', role='principal',
                                  school=cls.school_a)
        # Production zavuch accounts carry role='teacher' + 'zavuch_' username.
        cls.zavuch_a = make_user('zavuch_1', role='teacher',
                                 school=cls.school_a)
        cls.teacher_a = make_user('teacher_1', role='teacher',
                                  school=cls.school_a)
        cls.teacher_b = make_user('teacher_2', role='teacher',
                                  school=cls.school_b)

        cls.s1 = make_student(cls.school_a, '7-А', 'Фирӯз Раҳимов')
        cls.s2 = make_student(cls.school_a, '7-А', 'Мадина Каримова')
        cls.s3 = make_student(cls.school_b, '7-Б', 'Ҷаҳонгир Назаров')

        # Regular-teacher assignments in "Тақсимоти дарсҳо".
        cls.cs_a = make_class_subject(cls.school_a, '7-А', MATH,
                                      teacher=cls.teacher_a)
        cls.cs_b = make_class_subject(cls.school_b, '7-Б', MATH,
                                      teacher=cls.teacher_b)
