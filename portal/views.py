from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Avg, Count, Sum, Q, Max
import calendar
import io
import json
import re
import urllib.parse
import datetime
from collections import defaultdict
import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, Alignment, Protection, PatternFill, Border, Side
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.conf import settings
from django.contrib import messages
from django.views.decorators.http import require_POST

from .models import School, Teacher, Student, Grade, QuarterGrade, QuarterLock, ClassSubject, UserProfile, TeacherProfile, SubjectDeactivationRequest, Lesson, Assessment, TeachingGroup, SubjectGroupMembership, CLASS_LETTERS
from .forms import LoginForm, SchoolForm, TeacherForm, StudentForm, GradeForm, ClassSubjectForm
from .utils import (
    normalize_class_name, normalize_subject, is_litsey, class_numeric_part,
    default_subjects_for_class, ensure_class_subjects, is_non_graded,
    get_school_number, is_academic_school, academic_schools,
    academic_school_ids, official_subjects,
    official_subjects_for
)
from .curriculum_hours import (
    MIN_GRADES_BANDS, MAX_WEEKLY_HOURS, UNGRADED_SUBJECTS,
    weekly_hours, quarter_min_norm,
)
from .assessment_catalog import (
    ASSESSMENT_PURPOSES, SUMMATIVE_SCOPES,
    SEMANTIC_SINGLE, SEMANTIC_PAIRED, SEMANTIC_CONVERTED,
    get_work_type, work_type_choices, work_type_label,
    result_semantics_for, assessment_purpose, assessment_scope,
    test_percentage, convert_test_points,
)
from .grouping import (
    GROUP_LABELS, gender_split, balanced_split, validate_assignment,
)


QUALITATIVE_CHOICES = [
    ('', '---'),
    ('1', 'Ғайб'),
    ('2', 'Ҳозир'),
    ('3', 'Қонеъкунанда'),
    ('4', 'Қаноатбахш'),
    ('5', 'Аъло'),
]


def std_round(value):
    """Round positive numbers using standard half-up rounding."""
    if value is None:
        return None
    return int(value + 0.5)


def calc_quarterly(qmap, live_flags=None):
    """Calculate semi-annual, annual, and final grades from a quarter map.

    When only part of a period is filled, fall back to a live projection
    from the quarters that already exist; projected values are flagged with
    *_proj = True so the UI can label them as "Ҷорӣ" (in-progress) estimates.
    """
    live_flags = live_flags or set()
    calc = {
        'semi_annual_1': None, 'semi_annual_2': None, 'annual': None, 'final': None,
        'semi_annual_1_proj': False, 'semi_annual_2_proj': False,
        'annual_proj': False, 'final_proj': False,
    }
    q1, q2 = qmap.get(1), qmap.get(2)
    has_q1, has_q2 = q1 is not None, q2 is not None
    if has_q1 and has_q2:
        calc['semi_annual_1'] = std_round((q1 + q2) / 2)
    elif has_q1 or has_q2:
        calc['semi_annual_1'] = q1 if has_q1 else q2
    if has_q1 != has_q2 or (1 in live_flags or 2 in live_flags):
        if calc['semi_annual_1'] is not None:
            calc['semi_annual_1_proj'] = True
    q3, q4 = qmap.get(3), qmap.get(4)
    has_q3, has_q4 = q3 is not None, q4 is not None
    if has_q3 and has_q4:
        calc['semi_annual_2'] = std_round((q3 + q4) / 2)
    elif has_q3 or has_q4:
        calc['semi_annual_2'] = q3 if has_q3 else q4
    if has_q3 != has_q4 or (3 in live_flags or 4 in live_flags):
        if calc['semi_annual_2'] is not None:
            calc['semi_annual_2_proj'] = True
    if calc['semi_annual_1'] is not None and calc['semi_annual_2'] is not None:
        calc['annual'] = std_round((calc['semi_annual_1'] + calc['semi_annual_2']) / 2)
        calc['annual_proj'] = calc['semi_annual_1_proj'] or calc['semi_annual_2_proj']
    elif calc['semi_annual_1'] is not None or calc['semi_annual_2'] is not None:
        calc['annual'] = calc['semi_annual_1'] if calc['semi_annual_1'] is not None else calc['semi_annual_2']
        calc['annual_proj'] = True
    if calc['annual'] is not None:
        if qmap.get('att') is not None:
            calc['final'] = std_round((calc['annual'] + qmap['att']) / 2)
        else:
            calc['final'] = calc['annual']
        calc['final_proj'] = calc['annual_proj']
    return calc


def get_date_quarter(date):
    """Map a date to its academic quarter."""
    m = date.month
    if m in (9, 10, 11):
        return 1
    elif m in (12, 1, 2):
        return 2
    elif m in (3, 4, 5):
        return 3
    elif m in (6, 7, 8):
        return 4
    return None


def grade_component_result(grade):
    """Return the student's single result for a Grade row.

    A row carrying component scores (e.g. [8, 9] for one 8/9 assessment)
    contributes their arithmetic mean — exactly once. Empty or malformed
    component data falls back to Grade.score, which always remains the
    authoritative combined score.
    """
    comps = grade.components
    if isinstance(comps, (list, tuple)) and comps:
        values = []
        for c in comps:
            # Structured components store {'aspect': ..., 'value': n};
            # legacy rows store plain numbers. Both mean the same for the
            # contribution aggregation.
            if isinstance(c, dict):
                c = c.get('value')
            if isinstance(c, bool) or not isinstance(c, (int, float)):
                values = []
                break
            values.append(float(c))
        if values:
            return sum(values) / len(values)
    return grade.score


def _split_quarter_grades(grades, quarter):
    """Split one student's Grade rows for a subject into quarter pools.

    Returns (current_scores, ajm_results). Legacy rows (assessment NULL) and
    rows linked to a non-АҶМ assessment count as current grades; a row's
    quarter is its Assessment.quarter when linked, otherwise the existing
    get_date_quarter of grade.date — identical for all legacy data. Rows
    linked to an АҶМ assessment are grouped by assessment_id so each
    assessment contributes exactly one result (the mean of its row results;
    normally a single row).
    """
    current_scores = []
    ajm_groups = {}
    for g in grades:
        a = g.assessment
        gq = a.quarter if a is not None else get_date_quarter(g.date)
        if gq != quarter:
            continue
        result = grade_component_result(g)
        if result is None:
            continue
        if a is not None and a.is_ajm:
            ajm_groups.setdefault(a.id, []).append(result)
        else:
            current_scores.append(result)
    ajm_results = [sum(rows) / len(rows) for rows in ajm_groups.values()]
    return current_scores, ajm_results


def collect_quarter_inputs(student, subject, quarter):
    """Return (current_scores, ajm_results) for one student+subject+quarter.

    Uses the same scored-row pool as the existing daily calculations
    (period='Холҳои ҷорӣ (Онлайн)').
    """
    grades = Grade.objects.filter(
        student=student,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        score__isnull=False,
    ).select_related('assessment')
    return _split_quarter_grades(grades, quarter)


def calc_quarter_value(current_scores, ajm_results):
    """АҶЧ for one quarter, as a precise float (callers apply std_round).

    - No АҶМ  -> АҶЧ = АТ (never halved; identical to legacy behavior).
    - АТ+АҶМ  -> АҶЧ = (АТ + АҶМ) / 2, where АҶМ is the mean of the
      per-assessment results (each assessment counted once).
    - АҶМ only (АТ missing) -> АҶЧ = АҶМ. Missing АТ is never treated as
      zero and never halved; this fallback is deliberately symmetric with
      the no-АҶМ rule and lives only here.
    - Neither -> None (nothing to display, as before).
    """
    at = sum(current_scores) / len(current_scores) if current_scores else None
    ajm = sum(ajm_results) / len(ajm_results) if ajm_results else None
    if at is not None and ajm is not None:
        return (at + ajm) / 2
    if at is not None:
        return at
    return ajm


def _live_quarter_payload(student, subject, class_name):
    """Live quarter projection for one student: (qmap, live_flags).

    Mirrors the grade_entry live bridge — an official QuarterGrade always
    wins; otherwise the quarter is projected from the current Grade rows.
    Used by AJAX responses so the page can sync quarter cells immediately
    after a save or assessment deletion without reloading.
    """
    official = {}
    for qg in QuarterGrade.objects.filter(student=student, class_name=class_name, subject=subject):
        if qg.quarter == 0 and qg.att_grade is not None:
            official['att'] = qg.att_grade
        elif qg.grade is not None:
            official[qg.quarter] = qg.grade
    qmap = {}
    live_flags = set()
    for q in (1, 2, 3, 4):
        if q in official:
            qmap[q] = official[q]
        else:
            current_scores, ajm_results = collect_quarter_inputs(student, subject, q)
            value = calc_quarter_value(current_scores, ajm_results)
            if value is not None:
                qmap[q] = std_round(value)
                live_flags.add(q)
    qmap['att'] = official.get('att')
    return qmap, live_flags


def parse_result_input(raw, work_type=None):
    """Parse an assessment-linked result input under the work type's
    component semantics.

    Returns (score, components):
    - empty input      -> (None, None) meaning "clear the value"
    - single score     -> (numeric score, None)
    - slash components -> semantics-dependent:
        'single'          -> rejected (dictation takes ONE journal grade)
        'paired_grades'   -> exactly 2 parts -> (mean scalar, structured
                           [{'aspect','value'}] list). The pair is two
                           journal grades — Grade.score keeps the mean only
                           as the period-contribution aggregation; the
                           journal display shows the pair itself.
        'mean' / legacy   -> 2-3 parts -> (precise float mean, [ints])
        'converted_points'-> score field accepts only a direct 1-10 result;
                           raw points travel via points_achieved/
                           points_possible params, not this field.

    Raises ValueError for malformed input. Grade.score must never be a
    string, so multi-value input always resolves to a numeric scalar here.
    """
    if raw is None:
        return None, None
    text = str(raw).strip().replace(',', '.')
    if text == '':
        return None, None
    if '/' not in text:
        try:
            score = float(text)
        except ValueError:
            raise ValueError('Хол бояд рақам бошад.')
        score = int(score)
        if not (1 <= score <= 10):
            raise ValueError('Хол бояд аз 1 то 10 бошад.')
        return score, None
    cfg = get_work_type(work_type) if work_type else None
    semantics = cfg['semantics'] if cfg else 'mean'
    max_parts = (cfg or {}).get('max_components') or 3

    if cfg is not None and not cfg.get('slash_allowed'):
        raise ValueError('Ин навъи кор танҳо як баҳо мегирад.')
    parts = [p.strip() for p in text.split('/')]
    if semantics == SEMANTIC_PAIRED:
        if len(parts) != 2:
            raise ValueError('Ин навъи кор ду баҳо мегирад (масалан 8/9).')
    elif not (2 <= len(parts) <= max_parts):
        raise ValueError('Ҷузъҳо бояд 2 ё 3 адад бошанд (масалан 8/9).')
    values = []
    for part in parts:
        if not part.isdigit():
            raise ValueError('Ҷузъҳо бояд адади бутун бошанд.')
        v = int(part)
        if not (1 <= v <= 10):
            raise ValueError('Ҷузъҳо бояд аз 1 то 10 бошанд.')
        values.append(v)
    if semantics == SEMANTIC_PAIRED:
        aspects = cfg.get('aspects') or ()
        components = [
            {'aspect': aspects[i] if i < len(aspects) else f'part{i + 1}', 'value': v}
            for i, v in enumerate(values)
        ]
    else:
        components = values
    return sum(values) / len(values), components


def result_display(grade, semantics_cfg=None):
    """Journal display string for a Grade row under its work-type semantics.

    paired_grades -> '8/9' (the two journal grades, never the mean);
    everything else -> the combined score ('8.5' for legacy mean rows).
    """
    if grade is None or (grade.score is None and not grade.components):
        return ''
    cfg = semantics_cfg or result_semantics_for(getattr(grade, 'assessment', None))
    if cfg.get('semantics') == SEMANTIC_PAIRED and grade.components:
        parts = []
        for c in grade.components:
            v = c.get('value') if isinstance(c, dict) else c
            if isinstance(v, (int, float)) and float(v).is_integer():
                v = int(v)
            parts.append(str(v))
        return '/'.join(parts)
    if grade.score is None:
        return ''
    return str(int(grade.score)) if float(grade.score).is_integer() else str(grade.score)


def _resolve_assessment_for_grade(student, subject, assessment_id, lesson_id=None, user=None):
    """Resolve and scope-check an Assessment (+ optional Lesson) for a write.

    Raises ValueError with a safe message when the objects do not exist or
    belong to a different school/class/subject — foreign IDs are rejected
    without leaking details. When `user` is a regular teacher on a grouped
    ClassSubject, an assessment attached to another group's lesson is also
    rejected. Returns (assessment, lesson) where lesson may be inherited
    from assessment.lesson when not explicitly supplied.
    """
    try:
        assessment = Assessment.objects.select_related('class_subject__school', 'lesson').get(pk=assessment_id)
    except (Assessment.DoesNotExist, ValueError, TypeError):
        raise ValueError('Санҷиш ёфт нашуд.')
    cs = assessment.class_subject
    if (
        cs.school_id != student.school_id
        or normalize_class_name(cs.class_name) != normalize_class_name(student.class_name)
        or normalize_subject(cs.subject) != normalize_subject(subject)
    ):
        raise ValueError('Санҷиш ба ин синф ё фан тааллуқ надорад.')
    if not cs.is_active:
        raise ValueError('Санҷиш фаъол нест.')
    lesson = None
    if lesson_id not in (None, ''):
        try:
            lesson = Lesson.objects.get(pk=lesson_id)
        except (Lesson.DoesNotExist, ValueError, TypeError):
            raise ValueError('Дарс ёфт нашуд.')
        if lesson.class_subject_id != cs.id:
            raise ValueError('Дарс ба ин санҷиш тааллуқ надорад.')
        if assessment.lesson_id and assessment.lesson_id != lesson.id:
            raise ValueError('Дарс ба ин санҷиш тааллуқ надорад.')
    if lesson is None:
        lesson = assessment.lesson
    if lesson is not None and lesson.date != assessment.date:
        # A Grade row's date comes from the Assessment; it must never
        # disagree with the date of the Lesson it is filed under.
        raise ValueError('Дарс ба ин сана тааллуқ надорад.')
    if user is not None and lesson is not None and not _teacher_can_use_lesson(user, lesson):
        raise ValueError('Дарс ба гурӯҳи шумо тааллуқ надорад.')
    return assessment, lesson


def _parse_score(raw):
    """Shared 1-10 integer score validation for regular daily writes."""
    if not raw:
        return None
    try:
        score = float(raw)
    except (ValueError, TypeError):
        raise ValueError('Хол бояд рақам бошад.')
    score = int(score)
    if not (1 <= score <= 10):
        raise ValueError('Хол бояд аз 1 то 10 бошад.')
    return score


def _resolve_daily_scope(student, subject, date, lesson_id, user=None):
    """Validate the target slot of a regular (non-assessment) daily write.

    Returns the resolved Lesson or None for the legacy lesson-less slot.
    Raises ValueError with a teacher-safe message on forged/mismatched IDs,
    when a control assessment already owns the slot, or when a regular
    teacher tries to use another group's lesson.
    """
    class_name = normalize_class_name(student.class_name)
    if lesson_id:
        try:
            lesson = Lesson.objects.select_related('class_subject__school').get(pk=lesson_id)
        except (Lesson.DoesNotExist, ValueError, TypeError):
            raise ValueError('Дарс ёфт нашуд.')
        lcs = lesson.class_subject
        if (
            lcs.school_id != student.school_id
            or normalize_class_name(lcs.class_name) != class_name
            or normalize_subject(lcs.subject) != subject
        ):
            raise ValueError('Дарс ба ин синф ё фан тааллуқ надорад.')
        if not lcs.is_active:
            raise ValueError('Дарс фаъол нест.')
        if lesson.date != date:
            raise ValueError('Дарс ба ин сана тааллуқ надорад.')
        # Mode exclusivity is lesson-scoped: only a control assessment
        # attached to THIS lesson blocks Ҷорӣ here.
        if Assessment.objects.filter(
            class_subject=lcs,
            category='control',
            lesson=lesson,
        ).exists():
            raise ValueError('Ин дарс барои кори санҷишӣ таъйин шудааст — холи ҷорӣ дохил карда намешавад.')
        if user is not None and not _teacher_can_use_lesson(user, lesson):
            raise ValueError('Дарс ба гурӯҳи шумо тааллуқ надорад.')
        return lesson
    # Lesson-less slot: only legacy lesson-less control assessments lock it.
    if Assessment.objects.filter(
        class_subject__school=student.school,
        class_subject__class_name=class_name,
        class_subject__subject=subject,
        class_subject__is_active=True,
        category='control',
        date=date,
        lesson__isnull=True,
    ).exists():
        raise ValueError('Ин сана барои кори санҷишӣ таъйин шудааст — холи ҷорӣ дохил карда намешавад.')
    return None


def _apply_grade_fields(grade, values):
    """Apply cell values to a Grade row; a key's presence means 'write it'.

    Keys: score, attendance, behavior_score, sticker. Returns True when
    the row was saved, False when it became empty and was deleted.
    """
    if 'score' in values:
        raw = str(values['score']).strip().replace(',', '.')
        grade.score = _parse_score(raw if raw else None)
    if 'attendance' in values:
        att = str(values['attendance']).strip()
        grade.attendance = att if att in ('+', '-') else None
    if 'behavior_score' in values:
        beh_raw = str(values['behavior_score']).strip()
        if beh_raw:
            try:
                b = int(beh_raw)
                grade.behavior_score = b if 1 <= b <= 5 else None
            except (ValueError, TypeError):
                grade.behavior_score = None
        else:
            grade.behavior_score = None
    if 'sticker' in values:
        st = str(values['sticker']).strip()
        grade.sticker = st if st in ('⭐', '☀️', '🌸', '📖') else None
    if (grade.score is None and not grade.attendance
            and grade.behavior_score is None and not grade.sticker):
        grade.delete()
        return False
    grade.save()
    return True


def _assessment_label(assessment):
    """Teacher-facing Tajik label for a control assessment."""
    label = f"Назоратӣ №{assessment.number} — {assessment.date.strftime('%d.%m')}" if assessment.number \
        else f"Назоратӣ — {assessment.date.strftime('%d.%m')}"
    if assessment.title:
        label += f" {assessment.title}"
    if assessment.is_ajm:
        label += ' — АҶМ'
    return label


def is_quarter_locked(school, class_name, subject, quarter):
    """Return True if the given quarter is administratively locked."""
    if quarter is None:
        return False
    return QuarterLock.objects.filter(
        school=school,
        class_name=normalize_class_name(class_name),
        subject=normalize_subject(subject),
        quarter=quarter,
        locked=True
    ).exists()


def get_or_create_unique(model, defaults=None, **lookup):
    """get_or_create that tolerates duplicate rows left by concurrent writes."""
    qs = model.objects.filter(**lookup).order_by('pk')
    rows = list(qs)
    if rows:
        return rows[0], False
    params = dict(lookup)
    params.update(defaults or {})
    return model.objects.create(**params), True


def get_or_create_grade(defaults=None, **lookup):
    """Fetch-or-create a daily Grade row; merges and removes duplicate rows."""
    qs = Grade.objects.filter(**lookup).order_by('pk')
    rows = list(qs)
    if rows:
        grade = rows[0]
        if len(rows) > 1:
            changed = False
            for extra in rows[1:]:
                for field in ('score', 'attendance', 'behavior_score', 'sticker'):
                    if getattr(grade, field) in (None, '') and getattr(extra, field) not in (None, ''):
                        setattr(grade, field, getattr(extra, field))
                        changed = True
            Grade.objects.filter(pk__in=[r.pk for r in rows[1:]]).delete()
            if changed:
                grade.save()
        return grade, False
    params = dict(lookup)
    params.update(defaults or {})
    return Grade.objects.create(**params), True


def get_user_school(user):
    if not user or user.is_anonymous:
        return None
    try:
        return user.userprofile.school
    except UserProfile.DoesNotExist:
        try:
            return user.teacherprofile.school
        except TeacherProfile.DoesNotExist:
            return None


def get_user_role(user):
    if not user or user.is_anonymous:
        return settings.ROLE_TEACHER
    try:
        return user.userprofile.role
    except UserProfile.DoesNotExist:
        return settings.ROLE_TEACHER


def _is_zavuch(user, role=None):
    if not user or user.is_anonymous:
        return False
    if role is None:
        role = get_user_role(user)
    return (role and role.lower() == 'zavuch') or user.username.lower().startswith('zavuch_')


def _is_regular_teacher(user, role=None):
    """Return True for an authenticated teacher without superuser/zavuch privileges."""
    if not user or not user.is_authenticated or user.is_superuser:
        return False
    if role is None:
        role = get_user_role(user)
    return role == settings.ROLE_TEACHER and not _is_zavuch(user, role)


def _teacher_assignment_filter(user):
    """Q filter matching the ClassSubjects a teacher is assigned to (either field)."""
    return Q(teacher=user) | Q(allocated_teacher__user=user)


CLASS_NAME_RE = re.compile(rf'^(?:[1-9]|1[0-1])-[{CLASS_LETTERS}]$')


def _can_manage_school(user, school):
    """Return True if the user is a superuser or a zavuch of the given school."""
    if not user or user.is_anonymous:
        return False
    if user.is_superuser:
        return True
    return _is_zavuch(user) and get_user_school(user) == school


def _validate_class_name(raw):
    """Normalize and validate a class name, returning the normalized form or None."""
    if not raw:
        return None
    norm = normalize_class_name(raw)
    if not CLASS_NAME_RE.match(norm):
        return None
    return norm


# Official Tajik Cyrillic letter order used for class merge direction checks
_CLASS_LETTER_ORDER = {c: i for i, c in enumerate(CLASS_LETTERS)}
_CLASS_LETTER_RE = re.compile(rf'^(\d+)-([{CLASS_LETTERS}])$')


def _is_valid_class_merge(source, target):
    """Return True only if source is a higher-alphabet letter than target in the same grade."""
    source = normalize_class_name(source)
    target = normalize_class_name(target)
    m = _CLASS_LETTER_RE.match(source)
    n = _CLASS_LETTER_RE.match(target)
    if not m or not n:
        return False
    if int(m.group(1)) != int(n.group(1)):
        return False
    s_i = _CLASS_LETTER_ORDER.get(m.group(2))
    t_i = _CLASS_LETTER_ORDER.get(n.group(2))
    if s_i is None or t_i is None:
        return False
    return s_i > t_i


def _deactivate_empty_class(school, class_name):
    """Deactivate all ClassSubjects for a class that no longer has students."""
    class_name = normalize_class_name(class_name)
    if not Student.objects.filter(school=school, class_name=class_name).exists():
        ClassSubject.objects.filter(school=school, class_name=class_name).update(is_active=False)


def _move_student(school, student, new_class_name, new_full_name=None, gender=None):
    """Move a student to a new class and/or name, preserving grade history.

    Because the Student primary key is derived from school + class + full name,
    a new Student row is created and all Grade / QuarterGrade records are
    re-linked to it. The old row is then removed.
    """
    new_full_name = (new_full_name or student.full_name).strip()
    new_class_name = normalize_class_name(new_class_name)
    new_id = f"{school.name}__{new_class_name}__{new_full_name}"
    old_id = student.id
    if old_id == new_id:
        if gender in ('M', 'F'):
            student.gender = gender
            student.save()
        return student

    if Student.objects.filter(id=new_id).exclude(id=old_id).exists():
        raise ValueError('Хонанда бо ин ном дар синфи ҳадаф аллакай вуҷуд дорад.')

    with transaction.atomic():
        new_student = Student.objects.create(
            id=new_id,
            full_name=new_full_name,
            class_name=new_class_name,
            school=school,
            gender=gender or None,
        )
        Grade.objects.filter(student_id=old_id).update(student=new_student)
        QuarterGrade.objects.filter(student_id=old_id).update(
            student=new_student, class_name=new_class_name
        )
        Student.objects.filter(id=old_id).delete()

    return new_student


def has_school_access(user, school):
    if user.is_superuser:
        return True
    role = get_user_role(user)
    if role == settings.ROLE_DIRECTOR:
        return True
    user_school = get_user_school(user)
    return user_school == school


def get_user_profile(user):
    try:
        return user.userprofile
    except UserProfile.DoesNotExist:
        return None


def can_edit_grade_journal(user, school, class_name, subject):
    """Return True if the user may save grades for this school, class and subject."""
    if school is None or not is_academic_school(school):
        return False
    if user.is_superuser:
        return True
    if not has_school_access(user, school):
        return False
    profile = get_user_profile(user)
    if not profile or profile.school != school:
        return False
    role = get_user_role(user)
    # District/department-level director has master access to any school they can reach
    if role == settings.ROLE_DIRECTOR:
        return True
    # School director (principal) can edit anything in their own school
    if role == settings.ROLE_PRINCIPAL:
        return True
    # Zavuchs have full write/edit access to their school's classes
    if role == getattr(settings, 'ROLE_ZAVUCH', 'zavuch') or user.username.startswith('zavuch_'):
        return True
    if role == settings.ROLE_TEACHER:
        # Regular teachers must be explicitly allocated to this ClassSubject in "Тақсимоти дарсҳо"
        qs = ClassSubject.objects.filter(
            school=school,
            class_name=normalize_class_name(class_name),
            is_active=True,
        )
        # Non-graded classes (e.g. Grade 1 sticker journal) are class-wide:
        # any active subject assignment inside this classroom grants full access
        if is_non_graded(class_name):
            return qs.filter(_teacher_assignment_filter(user)).exists()
        qs = qs.filter(subject=normalize_subject(subject)).prefetch_related(
            'groups', 'allocated_teacher'
        )
        return any(_teacher_can_access_cs(user, cs) for cs in qs)
    return False


def can_view_grade_journal(user, school, class_name, subject):
    """Return True if the user may view this grade journal."""
    if school is None or not is_academic_school(school):
        return False
    if user.is_superuser:
        return True
    if not has_school_access(user, school):
        return False
    role = get_user_role(user)
    if role in (settings.ROLE_DIRECTOR, settings.ROLE_PRINCIPAL):
        return True
    return can_edit_grade_journal(user, school, class_name, subject)


# ---------------------------------------------------------------------------
# TeachingGroup scoping (Phase 3)
#
# Active TeachingGroups are authoritative for regular teachers: on a grouped
# ClassSubject the CS-level teacher fields no longer widen journal access.
# Privileged roles (superuser/director/principal/zavuch) keep full access.
# ---------------------------------------------------------------------------

def _teacher_can_access_cs(user, cs):
    """True if a regular teacher may open this ClassSubject's journal.

    Grouped CS -> only its active group teachers; ungrouped CS -> the legacy
    teacher/allocated_teacher assignment. Not for privileged roles.
    """
    if cs.groups.filter(is_active=True).exists():
        return cs.groups.filter(is_active=True, teacher__user=user).exists()
    return cs.teacher_id == user.id or (
        cs.allocated_teacher_id is not None
        and cs.allocated_teacher.user_id == user.id
    )


def teacher_group_for(user, cs):
    """The regular teacher's active TeachingGroup on this CS, else None."""
    if not _is_regular_teacher(user):
        return None
    return cs.groups.filter(is_active=True, teacher__user=user).first()


def _class_subject_for(school, class_name, subject):
    """The active ClassSubject row for a school/class/subject triple."""
    return ClassSubject.objects.filter(
        school=school,
        class_name=normalize_class_name(class_name),
        subject=normalize_subject(subject),
        is_active=True,
    ).order_by('pk').first()


def visible_students_for(user, cs):
    """Students visible to `user` for this ClassSubject (ordered by name).

    No active groups -> the whole class (legacy). Grouped -> all students
    for privileged roles; only own-group members for regular teachers.
    Students without membership in an active group stay invisible to
    teachers until the Admin/Zavuch assigns them in the group UI.
    """
    students = Student.objects.filter(
        school=cs.school, class_name=cs.class_name
    ).order_by('full_name')
    if not _is_regular_teacher(user):
        return students
    group = teacher_group_for(user, cs)
    if group is None:
        # Grouped CS without an own group -> nothing; ungrouped -> legacy.
        if cs.groups.filter(is_active=True).exists():
            return students.none()
        return students
    return students.filter(
        subject_group_memberships__class_subject=cs,
        subject_group_memberships__group=group,
    )


def _teacher_can_write_student(user, student, cs):
    """Server-side membership guard for single-student writes.

    Ungrouped CS -> True (legacy). Grouped CS -> regular teachers may only
    write students whose membership sits in their own active group.
    """
    if cs is None or not _is_regular_teacher(user):
        return True
    if not cs.groups.filter(is_active=True).exists():
        return True
    return SubjectGroupMembership.objects.filter(
        class_subject=cs,
        student=student,
        group__is_active=True,
        group__teacher__user=user,
    ).exists()


def _teacher_can_use_lesson(user, lesson):
    """Group check on a Lesson for grade/journal use by regular teachers.

    Ungrouped CS -> True; grouped CS -> own-group lessons plus legacy
    group=NULL lessons (shared class history, readable by all).
    """
    cs = lesson.class_subject
    if not _is_regular_teacher(user):
        return True
    if not cs.groups.filter(is_active=True).exists():
        return True
    if lesson.group_id is None:
        return True
    return cs.groups.filter(
        is_active=True, teacher__user=user, pk=lesson.group_id
    ).exists()


def _teacher_can_edit_lesson(user, lesson):
    """Stricter variant: teachers may edit/delete only own-group lessons;
    legacy group=NULL lessons stay read-only for them (Admin/Zavuch manage)."""
    cs = lesson.class_subject
    if not _is_regular_teacher(user):
        return True
    if not cs.groups.filter(is_active=True).exists():
        return True
    return lesson.group_id is not None and cs.groups.filter(
        is_active=True, teacher__user=user, pk=lesson.group_id
    ).exists()


def _visible_lessons_q(user, cs):
    """Q for lessons a regular teacher may select on a grouped CS.

    Own-group lessons plus legacy group=NULL rows. (On an ungrouped CS
    every lesson has group=NULL, so this filter is a no-op there.)
    """
    group = teacher_group_for(user, cs)
    if group is None:
        return Q(group__isnull=True)
    return Q(group=group) | Q(group__isnull=True)


def _visible_assessments_q(user, cs):
    """Q for control assessments a regular teacher may use on a grouped CS:
    own-group lesson assessments plus lesson-less (admin-managed) ones."""
    group = teacher_group_for(user, cs)
    if group is None:
        return Q(lesson__isnull=True)
    return Q(lesson__isnull=True) | Q(lesson__group=group)


def _add_score(totals, key, total, count):
    if total is None or count is None:
        return
    prev = totals.setdefault(key, [0.0, 0])
    prev[0] += float(total)
    prev[1] += int(count)


def calculate_school_rankings():
    """Return all schools ranked by average GPA from daily and quarterly grades (excluding non-graded classes)."""
    totals = {}

    for g in Grade.objects.filter(score__isnull=False).select_related('student'):
        if is_non_graded(g.student.class_name):
            continue
        _add_score(totals, g.student.school_id, g.score, 1)

    for q in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False).select_related('student'):
        if is_non_graded(q.class_name):
            continue
        _add_score(totals, q.student.school_id, q.grade, 1)

    for q in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False).select_related('student'):
        if is_non_graded(q.class_name):
            continue
        _add_score(totals, q.student.school_id, q.att_grade, 1)

    schools = [s for s in School.objects.all() if is_academic_school(s)]
    data = []
    for school in schools:
        total, count = totals.get(school.id, (0.0, 0))
        gpa = round(total / count, 2) if count else 0.0
        data.append({'school': school, 'gpa': gpa})
    data.sort(key=lambda x: x['gpa'], reverse=True)
    rank = 0
    prev_gpa = None
    for item in data:
        if item['gpa'] != prev_gpa:
            rank += 1
            prev_gpa = item['gpa']
        item['rank'] = rank
    return data


def calculate_class_rankings(school_filter=None):
    """Return class rankings with district and school ranks from all grade records."""
    entries = {}
    academic_school_ids = {s.id for s in School.objects.all() if is_academic_school(s)}

    # Seed with every school/class combination that has students (exclude non-graded classes)
    for row in Student.objects.values('school_id', 'school__name', 'class_name').distinct():
        sid = row['school_id']
        if sid not in academic_school_ids:
            continue
        cname = normalize_class_name(row['class_name'])
        if is_non_graded(cname):
            continue
        sname = row['school__name'] or ''
        key = (sid, cname)
        entries[key] = {'school_id': sid, 'school_name': sname, 'class_name': cname, 'total': 0.0, 'count': 0}

    # Daily grades
    for row in Grade.objects.filter(score__isnull=False).values('student__school', 'student__class_name').annotate(total=Sum('score'), count=Count('score')):
        sid = row['student__school']
        if sid not in academic_school_ids:
            continue
        cname = normalize_class_name(row['student__class_name'])
        if is_non_graded(cname):
            continue
        key = (sid, cname)
        if key not in entries:
            entries[key] = {'school_id': row['student__school'], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(row['total'] or 0)
        entries[key]['count'] += int(row['count'])

    # Quarterly grades
    for row in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False).values('student__school', 'class_name').annotate(total=Sum('grade'), count=Count('grade')):
        sid = row['student__school']
        if sid not in academic_school_ids:
            continue
        cname = normalize_class_name(row['class_name'])
        if is_non_graded(cname):
            continue
        key = (sid, cname)
        if key not in entries:
            entries[key] = {'school_id': row['student__school'], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(row['total'] or 0)
        entries[key]['count'] += int(row['count'])

    # Attestation grades
    for row in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False).values('student__school', 'class_name').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        sid = row['student__school']
        if sid not in academic_school_ids:
            continue
        cname = normalize_class_name(row['class_name'])
        if is_non_graded(cname):
            continue
        key = (sid, cname)
        if key not in entries:
            entries[key] = {'school_id': row['student__school'], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(row['total'] or 0)
        entries[key]['count'] += int(row['count'])

    # Resolve any missing school names
    missing_ids = {e['school_id'] for e in entries.values() if not e['school_name']}
    if missing_ids:
        name_map = {s.id: s.name for s in School.objects.filter(id__in=missing_ids)}
        for e in entries.values():
            if not e['school_name']:
                e['school_name'] = name_map.get(e['school_id'], '')

    data = [
        {
            'school_id': e['school_id'],
            'school_name': e['school_name'],
            'class_name': e['class_name'],
            'gpa': round(e['total'] / e['count'], 2) if e['count'] else 0.0,
        }
        for e in entries.values()
    ]
    data.sort(key=lambda x: x['gpa'], reverse=True)

    rank = 0
    prev_gpa = None
    for item in data:
        if item['gpa'] != prev_gpa:
            rank += 1
            prev_gpa = item['gpa']
        item['district_rank'] = rank

    school_state = {}
    for item in data:
        school = item['school_name']
        gpa = item['gpa']
        if school not in school_state:
            school_state[school] = {'rank': 0, 'prev_gpa': None}
        if gpa != school_state[school]['prev_gpa']:
            school_state[school]['rank'] += 1
            school_state[school]['prev_gpa'] = gpa
        item['school_rank'] = school_state[school]['rank']

    if school_filter:
        try:
            sid = int(school_filter)
            data = [item for item in data if item['school_id'] == sid]
        except (ValueError, TypeError):
            data = [item for item in data if school_filter.lower() in item['school_name'].lower()]

    return data


def calculate_top_students(school_filter=None, limit=100):
    """Return top-performing graded students (2-11) with dense school/district ranks."""
    academic_school_ids = {s.id for s in School.objects.all() if is_academic_school(s)}
    totals = defaultdict(lambda: [0.0, 0])

    non_graded_filter = (
        ~Q(student__class_name__startswith='0-') &
        ~Q(student__class_name__startswith='1-')
    )
    student_filter = Q(student__school_id__in=academic_school_ids) & non_graded_filter

    grade_qs = Grade.objects.filter(
        student_filter, score__isnull=False
    ).values('student').annotate(total=Sum('score'), count=Count('score'))
    for row in grade_qs:
        sid = row['student']
        totals[sid][0] += float(row['total'] or 0)
        totals[sid][1] += int(row['count'])

    quarter_qs = QuarterGrade.objects.filter(
        student_filter, quarter__in=(1, 2, 3, 4), grade__isnull=False
    ).values('student').annotate(total=Sum('grade'), count=Count('grade'))
    for row in quarter_qs:
        sid = row['student']
        totals[sid][0] += float(row['total'] or 0)
        totals[sid][1] += int(row['count'])

    att_qs = QuarterGrade.objects.filter(
        student_filter, quarter=0, att_grade__isnull=False
    ).values('student').annotate(total=Sum('att_grade'), count=Count('att_grade'))
    for row in att_qs:
        sid = row['student']
        totals[sid][0] += float(row['total'] or 0)
        totals[sid][1] += int(row['count'])

    student_map = {
        s.id: s
        for s in Student.objects.filter(id__in=list(totals.keys())).select_related('school')
    }

    data = []
    for sid, (total, count) in totals.items():
        student = student_map.get(sid)
        if not student:
            continue
        if not is_academic_school(student.school):
            continue
        if is_non_graded(student.class_name):
            continue
        gpa = round(total / count, 2) if count else 0.0
        data.append({
            'student_id': sid,
            'full_name': student.full_name,
            'class_name': student.class_name,
            'school_id': student.school.id,
            'school_name': student.school.name,
            'gpa': gpa,
        })

    data.sort(key=lambda x: x['gpa'], reverse=True)

    rank = 0
    prev_gpa = None
    for item in data:
        if item['gpa'] != prev_gpa:
            rank += 1
            prev_gpa = item['gpa']
        item['district_rank'] = rank

    school_state = {}
    for item in data:
        sid = item['school_id']
        state = school_state.setdefault(sid, {'rank': 0, 'prev_gpa': None})
        if item['gpa'] != state['prev_gpa']:
            state['rank'] += 1
            state['prev_gpa'] = item['gpa']
        item['school_rank'] = state['rank']

    if school_filter:
        try:
            sid = int(school_filter)
            data = [item for item in data if item['school_id'] == sid]
        except (ValueError, TypeError):
            data = [item for item in data if school_filter.lower() in item['school_name'].lower()]

    if len(data) > limit:
        cutoff_gpa = data[limit - 1]['gpa']
        data = [item for item in data if item['gpa'] >= cutoff_gpa]

    return data


def calculate_subject_rankings():
    """Return subject rankings including all default subjects with 0.0 GPA if no grades exist."""
    totals = {}
    for row in Grade.objects.filter(score__isnull=False).values('subject').annotate(total=Sum('score'), count=Count('score')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    for row in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False).values('subject').annotate(total=Sum('grade'), count=Count('grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    for row in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False).values('subject').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])

    # Build a complete set of default subjects from ClassSubject and TJC defaults
    all_subjects = set()
    for subj in ClassSubject.objects.values_list('subject', flat=True).distinct():
        all_subjects.add(normalize_subject(subj))
    for subject_list in settings.TJC_SUBJECTS.values():
        for subj in subject_list:
            all_subjects.add(normalize_subject(subj))

    official = official_subjects()
    data = []
    for subj in all_subjects:
        if subj not in official:
            continue
        total, count = totals.get(subj, (0.0, 0))
        gpa = round(total / count, 2) if count else 0.0
        data.append({'subject': subj, 'gpa': gpa})

    data.sort(key=lambda x: x['gpa'], reverse=True)
    rank = 0
    prev_gpa = None
    for item in data:
        if item['gpa'] != prev_gpa:
            rank += 1
            prev_gpa = item['gpa']
        item['rank'] = rank
    return data


def dashboard(request):
    role = get_user_role(request.user)
    user_school = get_user_school(request.user)

    # Live rankings from Grade and QuarterGrade records
    all_school_ranking = calculate_school_rankings()
    all_subject_ranking = calculate_subject_rankings()

    academic_ids = academic_school_ids()
    if not request.user.is_authenticated or request.user.is_superuser or role == settings.ROLE_DIRECTOR:
        schools = academic_schools()
    else:
        schools = [user_school] if user_school and is_academic_school(user_school) else []

    # Full district-wide rankings are visible to all logged-in users
    school_ranking = all_school_ranking
    subject_ranking = all_subject_ranking

    schools_dropdown = [s for s in School.objects.all().order_by('name') if is_academic_school(s)]

    # Optional school filter for class ranking dropdown
    selected_school = request.GET.get('school')
    if selected_school is None:
        # Default to the user's own school on first load
        selected_school = str(user_school.id) if user_school else '0'
    else:
        selected_school = selected_school.strip() or '0'

    class_ranking = calculate_class_rankings()
    if selected_school != '0':
        try:
            sid = int(selected_school)
            class_ranking = [c for c in class_ranking if c['school_id'] == sid]
        except (ValueError, TypeError):
            class_ranking = []

    top_students = calculate_top_students()

    # Grade 1 (non-graded) classes grouped by school for the homepage widget
    grade1_map = {}
    for row in Student.objects.values('school_id', 'school__name', 'class_name').distinct():
        if class_numeric_part(row['class_name']) == 1:
            sid = row['school_id']
            if sid not in academic_ids:
                continue
            if sid not in grade1_map:
                grade1_map[sid] = {'school_id': sid, 'school_name': row['school__name'], 'classes': set()}
            grade1_map[sid]['classes'].add(row['class_name'])
    grade1_schools = sorted(
        [
            {'school_id': sid, 'school_name': data['school_name'], 'classes': sorted(data['classes'])}
            for sid, data in grade1_map.items()
        ],
        key=lambda x: x['school_name']
    )

    total_schools = len(academic_ids)
    total_students = Student.objects.filter(school_id__in=academic_ids).count()
    male_students = Student.objects.filter(school_id__in=academic_ids, gender='M').count()
    female_students = Student.objects.filter(school_id__in=academic_ids, gender='F').count()
    male_pct = round(male_students / total_students * 100, 1) if total_students else 0.0
    female_pct = round(female_students / total_students * 100, 1) if total_students else 0.0
    total_teachers = Teacher.objects.filter(school_id__in=academic_ids).count()
    try:
        ratio = round(total_students / total_teachers, 1) if total_teachers else 0.0
    except ZeroDivisionError:
        ratio = 0.0

    my_requests = list(SubjectDeactivationRequest.objects.filter(
        requested_by=request.user
    ).select_related('class_subject').order_by('-requested_at')) if request.user.is_authenticated else []

    context = {
        'role': role,
        'schools': schools,
        'schools_dropdown': schools_dropdown,
        'school_ranking': school_ranking,
        'class_ranking': class_ranking,
        'top_students': top_students,
        'subject_ranking': subject_ranking,
        'grade1_schools': grade1_schools,
        'total_schools': total_schools,
        'total_students': total_students,
        'male_students': male_students,
        'female_students': female_students,
        'male_pct': male_pct,
        'female_pct': female_pct,
        'total_teachers': total_teachers,
        'ratio': ratio,
        'user_school': user_school,
        'selected_school': selected_school,
        'my_requests': my_requests,
    }
    return render(request, 'portal/dashboard.html', context)


def _clean_school_param(school):
    """Normalize the school filter query parameter; '0'/'all' means no filter."""
    if not school:
        return None
    school = school.strip()
    if school in ('0', 'all'):
        return None
    return school


def top_students_ajax(request):
    """Return top students for the honor roll, optionally filtered by school."""
    school = _clean_school_param(request.GET.get('school'))
    data = calculate_top_students(school)
    return JsonResponse(data, safe=False)


def class_rankings_ajax(request):
    """Return class rankings, optionally filtered by school."""
    school = _clean_school_param(request.GET.get('school'))
    data = calculate_class_rankings(school)
    return JsonResponse(data, safe=False)


def _safe_login_next(request, next_url):
    """Normalize the login `next` target for the post-login redirect.

    The value arrives query-decoded once, i.e. as an already URI-escaped
    path like /school/24/class/10-%D0%90/... (some proxies add yet another
    escape layer). Unquote until stable, then allow only local paths so
    Cyrillic class/subject URLs survive intact without double-encoding.
    """
    if not next_url:
        return None
    candidate = str(next_url)
    for _ in range(3):
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    if url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return None


def login_view(request):
    next_url = request.POST.get('next') or request.GET.get('next', '')
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            user = authenticate(request, username=username, password=password)
            if user:
                login(request, user)
                target = _safe_login_next(request, next_url)
                if target:
                    return redirect(target)
                return redirect('dashboard')
            else:
                form.add_error(None, 'Номи корбар ёки рамз нодуруст')
    else:
        form = LoginForm()
    return render(request, 'portal/login.html', {'form': form, 'next': next_url})


def logout_view(request):
    logout(request)
    return redirect('dashboard')


def school_list(request):
    role = get_user_role(request.user)
    if not request.user.is_authenticated or request.user.is_superuser or role == settings.ROLE_DIRECTOR:
        schools = [s for s in School.objects.all() if is_academic_school(s)]
    else:
        user_school = get_user_school(request.user)
        schools = [user_school] if user_school and is_academic_school(user_school) else []

    def school_sort_key(school):
        name_lower = school.name.lower()
        type_lower = school.type.lower()

        if 'идор' in type_lower:
            group_priority = 4
        elif 'томактаб' in type_lower:
            group_priority = 3
        elif 'лит' in type_lower or 'лиц' in type_lower:
            group_priority = 2
        elif 'гимн' in name_lower:
            group_priority = 1
            return (group_priority, 0, school.name)
        else:
            group_priority = 1

        nums = re.findall(r'\d+', school.name)
        num = int(nums[0]) if nums else 999999

        return (group_priority, num, school.name)

    schools_list = list(schools)
    schools_list.sort(key=school_sort_key)

    return render(request, 'portal/school_list.html', {'schools': schools_list, 'role': role})


@login_required
def school_add(request):
    if not request.user.is_superuser:
        return redirect('school_list')
    if request.method == 'POST':
        form = SchoolForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('school_list')
    else:
        form = SchoolForm()
    return render(request, 'portal/school_form.html', {'form': form})


def class_list(request, school_id=None):
    user_school = get_user_school(request.user)
    role = get_user_role(request.user)
    if school_id:
        school = get_object_or_404(School, id=school_id)
    else:
        school = user_school
    if school is not None and not is_academic_school(school):
        return redirect('school_list')
    if request.user.is_authenticated:
        is_zavuch = _is_zavuch(request.user, role)
        if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER and school != user_school:
            messages.warning(request, 'Ин муассиса ба шумо тааллуқ надорад!')
            if user_school:
                return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
            return redirect('dashboard')
        if not has_school_access(request.user, school):
            return redirect('dashboard')

    student_classes = {c for c in Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()}
    subject_classes = {c for c in ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()}
    class_names = sorted(student_classes | subject_classes, key=class_numeric_part)

    # Regular teachers only see the classes they are assigned to in "Тақсимоти дарсҳо"
    # or teach a group in (grouped subjects count as assignments).
    if _is_regular_teacher(request.user, role):
        assigned_class_names = {
            cs.class_name
            for cs in ClassSubject.objects.filter(school=school, is_active=True)
            .prefetch_related('groups', 'allocated_teacher')
            if _teacher_can_access_cs(request.user, cs)
        }
        class_names = [c for c in class_names if c in assigned_class_names]

    student_counts = dict(
        Student.objects.filter(school=school).values('class_name').annotate(count=Count('id')).values_list('class_name', 'count')
    )

    graded_stats = []
    non_graded_stats = []
    for cname in class_names:
        if is_non_graded(cname):
            label = 'Стикерҳо' if str(class_numeric_part(cname)) == '1' else 'Ғайрибаҳо'
            non_graded_stats.append({
                'class_name': cname,
                'school_rank': '-',
                'gpa': label,
                'student_count': student_counts.get(cname, 0),
            })
            continue

        total = 0.0
        count = 0
        for g in Grade.objects.filter(student__school=school, student__class_name=cname, score__isnull=False):
            total += float(g.score)
            count += 1
        for q in QuarterGrade.objects.filter(student__school=school, class_name=cname, quarter__in=(1, 2, 3, 4), grade__isnull=False):
            total += float(q.grade)
            count += 1
        for q in QuarterGrade.objects.filter(student__school=school, class_name=cname, quarter=0, att_grade__isnull=False):
            total += float(q.att_grade)
            count += 1

        gpa = round(total / count, 2) if count else 0.0
        graded_stats.append({
            'class_name': cname,
            'school_rank': None,
            'gpa': gpa,
            'student_count': student_counts.get(cname, 0),
        })

    # Assign school rank by GPA (descending) using dense ranking
    graded_stats.sort(key=lambda x: x['gpa'], reverse=True)
    rank = 0
    prev_gpa = None
    for s in graded_stats:
        if s['gpa'] != prev_gpa:
            rank += 1
            prev_gpa = s['gpa']
        s['school_rank'] = rank

    # Combine in class_name order
    rank_map = {s['class_name']: s['school_rank'] for s in graded_stats}
    gpa_map = {s['class_name']: s['gpa'] for s in graded_stats}
    count_map = {s['class_name']: s['student_count'] for s in graded_stats + non_graded_stats}
    class_stats = []
    for cname in class_names:
        if cname in rank_map:
            class_stats.append({'class_name': cname, 'school_rank': rank_map[cname], 'gpa': gpa_map[cname], 'student_count': count_map[cname]})
        else:
            for s in non_graded_stats:
                if s['class_name'] == cname:
                    class_stats.append(s)
                    break

    return render(request, 'portal/class_list.html', {
        'school': school,
        'class_stats': class_stats,
        'role': role,
        'grade_numbers': list(range(1, 12)),
        'class_letters': list(CLASS_LETTERS),
    })


@login_required
@require_POST
def add_class(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')

    grade = request.POST.get('grade', '').strip()
    letter = request.POST.get('letter', '').strip().upper()
    if not grade.isdigit() or not (1 <= int(grade) <= 11):
        messages.error(request, 'Дараҷаи синф бояд аз 1 то 11 бошад.')
        return redirect('class_list', school_id=school.id)
    if letter not in CLASS_LETTERS:
        messages.error(request, 'Ҳарфи синф нодуруст аст.')
        return redirect('class_list', school_id=school.id)

    class_name = f"{int(grade)}-{letter}"
    if (
        ClassSubject.objects.filter(school=school, class_name=class_name, is_active=True).exists()
        or Student.objects.filter(school=school, class_name=class_name).exists()
    ):
        messages.warning(request, f'Синфи «{class_name}» аллакай дар муассиса вуҷуд дорад!')
        return redirect('class_list', school_id=school.id)

    ensure_class_subjects(school, class_name)
    messages.success(request, f'Синфи «{class_name}» бомуваффақият сохта шуд.')
    return redirect('class_list', school_id=school.id)


@login_required
@require_POST
def delete_class(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)

    student_count = Student.objects.filter(school=school, class_name=class_name).count()
    if student_count > 0:
        messages.error(request, '❌ Ин синфро хориҷ кардан мумкин нест, чунки дар он хонандагон ҳастанд! Аввал хонандагонро кӯчонед ё хориҷ намоед.')
        return redirect('class_list', school_id=school.id)

    ClassSubject.objects.filter(school=school, class_name=class_name).delete()
    messages.success(request, f'Синфи {class_name} бомуваффақият хориҷ карда шуд.')
    return redirect('class_list', school_id=school.id)


def class_detail(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    class_name = normalize_class_name(class_name)
    if request.user.is_authenticated:
        user_school = get_user_school(request.user)
        role = get_user_role(request.user)
        is_zavuch = _is_zavuch(request.user, role)
        if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
            if school != user_school:
                messages.warning(request, 'Ин муассиса ба шумо тааллуқ надорад!')
                if user_school:
                    return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
                return redirect('dashboard')
            if not any(
                _teacher_can_access_cs(request.user, cs)
                for cs in ClassSubject.objects.filter(
                    school=school, class_name=class_name, is_active=True
                ).prefetch_related('groups', 'allocated_teacher')
            ):
                messages.warning(request, 'Ин синф ё фан ба шумо вобаста карда нашудааст!')
                return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
        if not has_school_access(request.user, school):
            return redirect('dashboard')
    if not is_academic_school(school):
        return redirect('school_list')
    ensure_class_subjects(school, class_name)
    students = Student.objects.filter(school=school, class_name=class_name).order_by('full_name')
    total_count = students.count()
    male_count = students.filter(gender='M').count()
    female_count = students.filter(gender='F').count()
    subjects = ClassSubject.objects.filter(
        school=school, class_name=class_name, is_active=True,
        subject__in=official_subjects_for(school, class_name)
    ).order_by('subject')
    # Regular teachers only see their own assigned subjects in the journal
    # picker — for grouped subjects that means their TeachingGroup.
    if _is_regular_teacher(request.user):
        subjects = [
            cs for cs in subjects.prefetch_related('groups', 'allocated_teacher')
            if _teacher_can_access_cs(request.user, cs)
        ]
        for cs in subjects:
            own_group = teacher_group_for(request.user, cs)
            cs.group_label = own_group.label if own_group else None
    non_graded = is_non_graded(class_name)
    all_classes = sorted({
        c for c in Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    } | {
        c for c in ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    }, key=class_numeric_part)
    return render(request, 'portal/class_detail.html', {
        'school': school,
        'class_name': class_name,
        'students': students,
        'total_count': total_count,
        'male_count': male_count,
        'female_count': female_count,
        'subjects': subjects,
        'non_graded': non_graded,
        'all_classes': all_classes,
    })


@login_required
def sticker_entry(request, school_id, class_name):
    """Interactive sticker, attendance and behavior journal for non-graded classes."""
    school = get_object_or_404(School, id=school_id)
    class_name = normalize_class_name(class_name)
    user_school = get_user_school(request.user)
    role = get_user_role(request.user)
    is_zavuch = _is_zavuch(request.user, role)
    if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
        if school != user_school:
            messages.warning(request, 'Ин муассиса ба шумо тааллуқ надорад!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
    elif not has_school_access(request.user, school):
        return redirect('dashboard')

    if not is_non_graded(class_name):
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    subject = normalize_subject('Стикерҳо')
    # Ensure a ClassSubject record exists for permission control
    cs_sticker, _ = get_or_create_unique(
        ClassSubject,
        school=school,
        class_name=class_name,
        subject=subject,
        defaults={'is_default': False, 'is_active': True}
    )
    # The pseudo-subject is not part of the official curriculum, so the
    # ensure_class_subjects sweep (run on every class_detail visit) may have
    # deactivated it. Reactivate — unless an approved deactivation request
    # exists, matching the semantics ensure_class_subjects itself honors.
    if not cs_sticker.is_active and not SubjectDeactivationRequest.objects.filter(
        class_subject=cs_sticker, status='approved'
    ).exists():
        cs_sticker.is_active = True
        cs_sticker.save(update_fields=['is_active'])

    if not can_view_grade_journal(request.user, school, class_name, subject):
        if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
            messages.warning(request, 'Ин синф ё фан ба шумо вобаста карда нашудааст!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
        return HttpResponse('Дастрасӣ манъ аст.', status=403)

    students = Student.objects.filter(school=school, class_name=class_name).order_by('full_name')

    date_str = request.GET.get('date', '')
    try:
        selected_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        selected_date = datetime.date.today()

    sticker_choices = {'⭐': 'Ситора', '☀️': 'Офтобак', '🌸': 'Гул', '📖': 'Китоб'}

    daily_grades = {}
    daily_attendance = {}
    daily_behavior = {}
    daily_stickers = {}
    for g in Grade.objects.filter(
        student__school=school,
        student__class_name=class_name,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        date=selected_date
    ):
        if g.attendance:
            daily_attendance[g.student_id] = g.attendance
        if g.behavior_score is not None:
            daily_behavior[g.student_id] = g.behavior_score
        if g.sticker:
            daily_stickers[g.student_id] = g.sticker

    # Most recent prior behavior score per student (carry-over default)
    prior_behavior = {}
    for g in Grade.objects.filter(
        student__in=students,
        behavior_score__isnull=False,
        date__lt=selected_date
    ).order_by('student_id', '-date'):
        if g.student_id not in prior_behavior:
            prior_behavior[g.student_id] = g.behavior_score

    behavior_default = {}
    for s in students:
        if s.id in daily_behavior:
            behavior_default[s.id] = daily_behavior[s.id]
        elif s.id in prior_behavior:
            behavior_default[s.id] = prior_behavior[s.id]
        else:
            behavior_default[s.id] = 5

    daily_quarter_locked = is_quarter_locked(school, class_name, subject, get_date_quarter(selected_date))

    return render(request, 'portal/sticker_entry.html', {
        'school': school,
        'class_name': class_name,
        'subject': subject,
        'students': students,
        'date': selected_date,
        'daily_attendance': daily_attendance,
        'daily_stickers': daily_stickers,
        'behavior_default': behavior_default,
        'sticker_choices': sticker_choices,
        'daily_quarter_locked': daily_quarter_locked,
    })


@login_required
@require_POST
def add_student(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)
    full_name = request.POST.get('full_name', '').strip()
    if not full_name:
        messages.error(request, 'Номи хонанда ворид карда шавад.')
        return redirect('class_detail', school_id=school.id, class_name=class_name)
    student_id = f"{school.name}__{class_name}__{full_name}"
    if Student.objects.filter(id=student_id).exists():
        messages.warning(request, 'Хонанда бо ин ном аллакай вуҷуд дорад.')
    else:
        Student.objects.create(
            id=student_id,
            full_name=full_name,
            class_name=class_name,
            school=school
        )
        messages.success(request, 'Хонанда илова шуд.')
    return redirect('class_detail', school_id=school.id, class_name=class_name)


@login_required
@require_POST
def remove_student(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)
    student_id = request.POST.get('student_id', '').strip()
    if student_id:
        try:
            student = Student.objects.get(id=student_id, school=school, class_name=class_name)
            student.delete()
            messages.success(request, 'Хонанда хориҷ карда шуд.')
        except Student.DoesNotExist:
            messages.error(request, 'Хонанда ёфт нашуд.')
    return redirect('class_detail', school_id=school.id, class_name=class_name)


@login_required
@require_POST
def edit_student(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)
    student_id = request.POST.get('student_id', '').strip()
    full_name = request.POST.get('full_name', '').strip()
    gender = request.POST.get('gender', '').strip()
    gender = gender if gender in ('M', 'F') else None
    class_option = request.POST.get('class_name', '').strip()
    new_class_input = request.POST.get('new_class_name', '').strip()

    if not student_id or not full_name:
        messages.error(request, 'Иттилооти нокифоя барои таҳрири хонанда.')
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    try:
        student = Student.objects.get(id=student_id, school=school, class_name=class_name)
    except Student.DoesNotExist:
        messages.error(request, 'Хонанда ёфт нашуд.')
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    target_class = new_class_input if class_option == '__new__' else class_option
    if not target_class:
        messages.error(request, 'Синф интихоб карда шавад.')
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    target_class = _validate_class_name(target_class)
    if not target_class:
        messages.error(request, 'Лутфан, номи синфро дуруст ворид намоед (масалан: 1-В ёки 10-А).')
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    try:
        student = _move_student(school, student, target_class, full_name, gender=gender)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('class_detail', school_id=school.id, class_name=class_name)

    ensure_class_subjects(school, target_class)
    _deactivate_empty_class(school, class_name)
    messages.success(request, f'Хонанда {full_name} таҳрир шуд.')
    # Stay on the source class so the zavuch can transfer the next student
    # without navigating back; the #student-ID fragment focuses the row.
    return redirect(reverse('class_detail', args=[school.id, class_name]) + f'#student-{student.id}')


@login_required
@require_POST
def transfer_class(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')
    source_class = normalize_class_name(class_name)
    target_option = request.POST.get('target_class', '').strip()
    new_class_input = request.POST.get('new_class_name', '').strip()

    target_class = new_class_input if target_option == '__new__' else target_option
    if not target_class:
        messages.error(request, 'Синфи ҳадаф интихоб карда шавад.')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    target_class = _validate_class_name(target_class)
    if not target_class:
        messages.error(request, 'Лутфан, номи синфро дуруст ворид намоед (масалан: 1-В ёки 10-А).')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    if target_class == source_class:
        messages.warning(request, 'Синфи манба ва ҳадаф якхеланд.')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    if not _is_valid_class_merge(source_class, target_class):
        if source_class.endswith('-А'):
            messages.error(request, 'Синфи асосии «А» наметавонад ба дигар синфҳо кӯчонида шавад! Танҳо синфҳои «Б, В, Г» метавонанд ба синфи «А» муттаҳид карда шаванд.')
        else:
            messages.error(request, 'Кӯчонидан танҳо аз ҳарфи ҷуфттар ба ҳарфи пештар иҷозат дорад (масалан: 10-Б -> 10-А).')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    students = list(Student.objects.filter(school=school, class_name=source_class).order_by('full_name'))
    if not students:
        messages.warning(request, 'Синфи манба холӣ аст.')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    try:
        with transaction.atomic():
            for student in students:
                _move_student(school, student, target_class, student.full_name)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    ensure_class_subjects(school, target_class)
    ClassSubject.objects.filter(school=school, class_name=source_class).update(is_active=False)
    messages.success(request, 'Хонандагон бомуваффақият кӯчонида шуданд.')
    return redirect('class_detail', school_id=school.id, class_name=target_class)


@login_required
def grade_entry(request, school_id, class_name, subject):
    school = get_object_or_404(School, id=school_id)
    class_name = normalize_class_name(class_name)
    subject = normalize_subject(subject)
    user_school = get_user_school(request.user)
    role = get_user_role(request.user)
    is_zavuch = _is_zavuch(request.user, role)
    if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
        if school != user_school:
            messages.warning(request, 'Ин муассиса ба шумо тааллуқ надорад!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
        if not can_view_grade_journal(request.user, school, class_name, subject):
            messages.warning(request, 'Ин синф ё фан ба шумо вобаста карда нашудааст!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
    elif not has_school_access(request.user, school):
        return redirect('dashboard')
    elif not can_view_grade_journal(request.user, school, class_name, subject):
        return HttpResponse('Дастрасӣ манъ аст.', status=403)

    class_subject = _class_subject_for(school, class_name, subject)
    if class_subject is not None:
        students = visible_students_for(request.user, class_subject)
    else:
        students = Student.objects.filter(
            school=school, class_name=class_name
        ).order_by('full_name')

    if request.method == 'POST':
        if not can_edit_grade_journal(request.user, school, class_name, subject):
            return HttpResponse('Дастрасӣ барои тағйир додан манъ аст.', status=403)

        date_str = request.POST.get('date', '')
        try:
            date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
        except ValueError:
            date = datetime.date.today()

        # Batch POST may carry a lesson context — same scope validation as
        # the AJAX path: the Lesson must belong to this school/class/subject
        # and live on the posted date.
        post_lesson_id = request.POST.get('lesson_id', '').strip()
        post_lesson = None
        if post_lesson_id:
            try:
                post_lesson = Lesson.objects.select_related('class_subject').get(pk=post_lesson_id)
            except (Lesson.DoesNotExist, ValueError, TypeError):
                return HttpResponse('Дарс ёфт нашуд.', status=400)
            pcs = post_lesson.class_subject
            if (
                pcs.school_id != school.id
                or normalize_class_name(pcs.class_name) != class_name
                or normalize_subject(pcs.subject) != subject
                or not pcs.is_active
                or post_lesson.date != date
            ):
                return HttpResponse('Дарс ба ин синф, фан ё сана тааллуқ надорад.', status=400)
            if not _teacher_can_use_lesson(request.user, post_lesson):
                return HttpResponse('Дарс ба гурӯҳи шумо тааллуқ надорад.', status=400)

        daily_q = get_date_quarter(date)
        daily_locked = is_quarter_locked(school, class_name, subject, daily_q)
        # Mode exclusivity: a control assessment on this date makes it
        # Назорат-only — batch daily writes are skipped like a locked day.
        # Scoped per lesson: an assessment attached to a specific Lesson
        # locks only that lesson's slot; the lesson-less slot keeps the
        # legacy date-wide check (lesson__isnull=True).
        control_q = Assessment.objects.filter(
            class_subject__school=school,
            class_subject__class_name=class_name,
            class_subject__subject=subject,
            class_subject__is_active=True,
            category='control',
            date=date,
        )
        if post_lesson is not None:
            daily_is_control = control_q.filter(lesson=post_lesson).exists()
        else:
            daily_is_control = control_q.filter(lesson__isnull=True).exists()

        for student in students:
            # daily grade fallback (skip if quarter is locked)
            if not daily_locked and not daily_is_control:
                # Explicit lesson=None/assessment=None: the legacy daily
                # write must never match a lesson-linked or assessment-linked
                # row for the same student/subject/date.
                grade, _ = get_or_create_grade(
                    student=student,
                    subject=subject,
                    period='Холҳои ҷорӣ (Онлайн)',
                    date=date,
                    assessment=None,
                    lesson=post_lesson,
                    defaults={'score': None, 'attendance': None, 'behavior_score': None}
                )

                key = f"score_{student.id}"
                val = request.POST.get(key, '').strip()
                if val:
                    try:
                        score = float(val)
                        if not score.is_integer():
                            score = None
                        else:
                            score = int(score)
                            if not (1 <= score <= 10):
                                score = None
                    except ValueError:
                        score = None
                else:
                    score = None
                grade.score = score

                att = request.POST.get(f"attendance_{student.id}", '').strip()
                grade.attendance = att if att in ('+', '-') else None

                beh = request.POST.get(f"behavior_{student.id}", '').strip()
                if beh:
                    try:
                        b = int(beh)
                        grade.behavior_score = b if 1 <= b <= 5 else None
                    except ValueError:
                        grade.behavior_score = None
                else:
                    grade.behavior_score = None

                if grade.score is None and not grade.attendance and grade.behavior_score is None:
                    grade.delete()
                else:
                    grade.save()

            # quarterly grade fallback (skip locked quarters)
            for q in (1, 2, 3, 4, 0):
                if is_quarter_locked(school, class_name, subject, q):
                    continue
                if q == 0:
                    qkey = f"att_{student.id}"
                else:
                    qkey = f"q_{q}_{student.id}"
                qval = request.POST.get(qkey, '').strip()
                if qval:
                    try:
                        qscore = float(qval)
                        if 1 <= qscore <= 10:
                            if q == 0:
                                QuarterGrade.objects.update_or_create(
                                    student=student,
                                    class_name=class_name,
                                    subject=subject,
                                    quarter=0,
                                    defaults={'att_grade': int(qscore)}
                                )
                            else:
                                QuarterGrade.objects.update_or_create(
                                    student=student,
                                    class_name=class_name,
                                    subject=subject,
                                    quarter=q,
                                    defaults={'grade': int(qscore)}
                                )
                    except ValueError:
                        pass
                else:
                    if q == 0:
                        QuarterGrade.objects.filter(
                            student=student, class_name=class_name, subject=subject, quarter=0
                        ).delete()
                    else:
                        QuarterGrade.objects.filter(
                            student=student, class_name=class_name, subject=subject, quarter=q
                        ).delete()
        back = f"?date={date.isoformat()}"
        if post_lesson is not None:
            back += f"&lesson={post_lesson.id}"
        return redirect(
            reverse('grade_entry', kwargs={
                'school_id': school.id, 'class_name': class_name, 'subject': subject
            }) + back
        )

    date_str = request.GET.get('date', '')
    try:
        selected_date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
    except ValueError:
        selected_date = datetime.date.today()

    # Lessons for this ClassSubject on the selected date. 'lesson' carries a
    # lesson id; 'ln' carries a lesson number (date navigation keeps the
    # same position across days when it exists). On grouped subjects a
    # regular teacher sees only their own group's lessons plus legacy
    # group=NULL rows.
    day_lessons = []
    selected_lesson = None
    if class_subject is not None:
        lesson_qs = Lesson.objects.filter(
            class_subject=class_subject, date=selected_date
        ).select_related('group')
        if _is_regular_teacher(request.user):
            lesson_qs = lesson_qs.filter(_visible_lessons_q(request.user, class_subject))
        day_lessons = list(lesson_qs.order_by('lesson_number'))
        lesson_param = request.GET.get('lesson', '').strip()
        ln_param = request.GET.get('ln', '').strip()
        if lesson_param:
            selected_lesson = next(
                (l for l in day_lessons if str(l.id) == lesson_param), None
            )
        elif ln_param:
            try:
                ln = int(ln_param)
                selected_lesson = next(
                    (l for l in day_lessons if l.lesson_number == ln), None
                )
            except ValueError:
                selected_lesson = None

    daily_grades = {}
    daily_attendance = {}
    daily_behavior = {}
    for g in Grade.objects.filter(
        student__school=school,
        student__class_name=class_name,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        date=selected_date,
        # Assessment-linked rows belong to Назорат mode only — they must
        # never leak into the Ҷорӣ daily cells for the same date.
        assessment__isnull=True,
        lesson=selected_lesson,
    ):
        if g.score is not None:
            daily_grades[g.student_id] = int(g.score) if g.score.is_integer() else g.score
        if g.attendance:
            daily_attendance[g.student_id] = g.attendance
        if g.behavior_score is not None:
            daily_behavior[g.student_id] = g.behavior_score

    # Most recent prior behavior score per student (carry-over default)
    prior_behavior = {}
    for g in Grade.objects.filter(
        student__in=students,
        behavior_score__isnull=False,
        date__lt=selected_date,
        assessment__isnull=True,
    ).order_by('student_id', '-date'):
        if g.student_id not in prior_behavior:
            prior_behavior[g.student_id] = g.behavior_score

    behavior_default = {}
    for s in students:
        if s.id in daily_behavior:
            behavior_default[s.id] = daily_behavior[s.id]
        elif s.id in prior_behavior:
            behavior_default[s.id] = prior_behavior[s.id]
        else:
            behavior_default[s.id] = 5

    q_grades = QuarterGrade.objects.filter(
        student__school=school,
        class_name=class_name,
        subject=subject
    ).select_related('student')
    qmap_by_student = {}
    for g in q_grades:
        sid = g.student_id
        if sid not in qmap_by_student:
            qmap_by_student[sid] = {}
        if g.quarter == 0 and g.att_grade is not None:
            qmap_by_student[sid]['att'] = g.att_grade
        elif g.grade is not None:
            qmap_by_student[sid][g.quarter] = g.grade

    # Collect scored daily rows per student for live quarter bridging.
    # Rows are split into current (legacy + non-АҶМ) and АҶМ pools inside
    # _split_quarter_grades; each АҶМ assessment contributes exactly once.
    grades_by_student = defaultdict(list)
    for g in Grade.objects.filter(
        student__in=students,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        score__isnull=False
    ).select_related('assessment'):
        grades_by_student[g.student_id].append(g)

    quarter_grades = {}
    display_quarter_grades = {}
    live_quarter_flags = {}
    calculated = {}
    for student in students:
        sid = student.id
        official = qmap_by_student.get(sid, {})
        qmap = {}
        live_flags = set()
        display = {}
        for q in (1, 2, 3, 4):
            official_grade = official.get(q)
            if official_grade is not None:
                qmap[q] = official_grade
                display[q] = official_grade
            else:
                current_scores, ajm_results = _split_quarter_grades(
                    grades_by_student.get(sid, ()), q
                )
                value = calc_quarter_value(current_scores, ajm_results)
                if value is not None:
                    avg = std_round(value)
                    qmap[q] = avg
                    display[q] = avg
                    live_flags.add(q)
                else:
                    display[q] = None
        display['att'] = official.get('att')
        quarter_grades[sid] = display
        live_quarter_flags[sid] = live_flags
        qmap['att'] = official.get('att')
        calculated[sid] = calc_quarterly(qmap, live_flags)

    non_graded = is_non_graded(class_name)

    locked_quarters = set(
        QuarterLock.objects.filter(
            school=school,
            class_name=class_name,
            subject=subject,
            locked=True
        ).values_list('quarter', flat=True)
    )
    daily_quarter_locked = is_quarter_locked(school, class_name, subject, get_date_quarter(selected_date))

    # Control (Назорат) assessments for this ClassSubject, for the picker.
    control_assessments = []
    date_is_control = False
    if class_subject is not None:
        assessments_qs = Assessment.objects.filter(
            class_subject=class_subject, category='control'
        )
        if _is_regular_teacher(request.user) and class_subject.groups.filter(is_active=True).exists():
            assessments_qs = assessments_qs.filter(
                _visible_assessments_q(request.user, class_subject)
            )
        for a in assessments_qs.order_by('quarter', 'number', 'date', 'id'):
            control_assessments.append({
                'id': a.id,
                'label': _assessment_label(a),
                'number': a.number,
                'title': a.title,
                'is_ajm': a.is_ajm,
                'date': a.date.isoformat(),
                'lesson_id': a.lesson_id,
                'purpose': a.purpose,
                'work_type': a.work_type,
                'work_type_label': work_type_label(a.work_type),
                'summative_scope': a.summative_scope,
                'semantics': result_semantics_for(a).get('semantics'),
                'locked': is_quarter_locked(school, class_name, subject, a.quarter),
            })
            # Mode lock is slot-scoped: with a lesson selected only that
            # lesson's assessments lock it; otherwise only the legacy
            # lesson-less slot counts — a lesson-scoped assessment never
            # flips the whole date.
            if a.date == selected_date and a.lesson_id == (
                selected_lesson.id if selected_lesson else None
            ):
                date_is_control = True

    # Lesson picker payload: each lesson carries its mode so the UI can mark
    # Ҷорӣ/Назорат per lesson without a second query.
    lessons_payload = []
    for l in day_lessons:
        has_control = any(
            a['lesson_id'] == l.id and a['date'] == selected_date.isoformat()
            for a in control_assessments
        )
        has_data = (
            has_control
            or Grade.objects.filter(lesson=l).exists()
            or Assessment.objects.filter(lesson=l).exists()
        )
        lessons_payload.append({
            'id': l.id,
            'lesson_number': l.lesson_number,
            'topic': l.topic,
            'group_label': l.group.label if l.group_id else None,
            'mode': 'control' if has_control else 'current',
            'has_data': has_data,
        })

    # Saved per-student results for each assessment: {assessment_id: {student_id: display}}
    # Display follows the work type's semantics: paired-grade works show the
    # journal pair ('8/9'), legacy/untyped rows show the combined score.
    assessment_grade_map = {}
    if control_assessments:
        sem_by_id = {}
        for a in Assessment.objects.filter(id__in=[a['id'] for a in control_assessments]):
            sem_by_id[a.id] = result_semantics_for(a)
        for g in Grade.objects.filter(
            student__in=students,
            subject=subject,
            assessment_id__in=[a['id'] for a in control_assessments],
        ).select_related('assessment'):
            assessment_grade_map.setdefault(g.assessment_id, {})[g.student_id] = result_display(
                g, sem_by_id.get(g.assessment_id)
            )

    return render(request, 'portal/grade_entry.html', {
        'school': school,
        'class_name': class_name,
        'subject': subject,
        'students': students,
        'date': selected_date,
        'daily_grades': daily_grades,
        'daily_attendance': daily_attendance,
        'daily_behavior': daily_behavior,
        'behavior_default': behavior_default,
        'quarter_grades': quarter_grades,
        'live_quarter_flags': live_quarter_flags,
        'calculated': calculated,
        'non_graded': non_graded,
        'qualitative_choices': QUALITATIVE_CHOICES,
        'quarter_numbers': [1, 2, 3, 4],
        'locked_quarters': locked_quarters,
        'daily_quarter_locked': daily_quarter_locked,
        'class_subject_id': class_subject.id if class_subject else None,
        'teacher_group': (
            teacher_group_for(request.user, class_subject)
            if class_subject else None
        ),
        'lessons': lessons_payload,
        'selected_lesson': selected_lesson,
        'selected_lesson_has_data': next(
            (l['has_data'] for l in lessons_payload
             if selected_lesson and l['id'] == selected_lesson.id), False
        ),
        'control_assessments': control_assessments,
        'assessment_grade_map': assessment_grade_map,
        'date_is_control': date_is_control,
        'work_type_choices': work_type_choices(),
    })


@login_required
def monthly_journal(request, school_id, class_name, subject):
    """Month-overview journal: students × (date, lesson-slot) grid.

    Read-only review companion to the daily journal. Every Lesson of the
    same subject/day gets its own column so same-day lessons are never
    collapsed; the legacy lesson-less slot appears only when it holds data.
    """
    school = get_object_or_404(School, id=school_id)
    class_name = normalize_class_name(class_name)
    subject = normalize_subject(subject)
    user_school = get_user_school(request.user)
    role = get_user_role(request.user)
    is_zavuch = _is_zavuch(request.user, role)
    if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
        if school != user_school:
            messages.warning(request, 'Ин муассиса ба шумо тааллуқ надорад!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
        if not can_view_grade_journal(request.user, school, class_name, subject):
            messages.warning(request, 'Ин синф ё фан ба шумо вобаста карда нашудааст!')
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
    elif not has_school_access(request.user, school):
        return redirect('dashboard')
    elif not can_view_grade_journal(request.user, school, class_name, subject):
        return HttpResponse('Дастрасӣ манъ аст.', status=403)

    # Month resolution: ?month=YYYY-MM, default = current month.
    month_str = request.GET.get('month', '').strip()
    month_start = None
    if month_str:
        try:
            year, mon = (int(p) for p in month_str.split('-', 1))
            month_start = datetime.date(year, mon, 1)
        except (ValueError, TypeError):
            month_start = None
    if month_start is None:
        month_start = timezone.localdate().replace(day=1)
    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = month_start.replace(day=last_day)
    prev_month = (month_start - datetime.timedelta(days=1)).replace(day=1)
    next_month = (month_end + datetime.timedelta(days=1)).replace(day=1)

    class_subject = _class_subject_for(school, class_name, subject)
    if class_subject is not None:
        students = list(visible_students_for(request.user, class_subject))
    else:
        students = list(Student.objects.filter(
            school=school, class_name=class_name
        ).order_by('full_name'))

    can_edit = can_edit_grade_journal(request.user, school, class_name, subject)
    locked_quarters = set(
        QuarterLock.objects.filter(
            school=school, class_name=class_name, subject=subject, locked=True
        ).values_list('quarter', flat=True)
    )

    # On grouped subjects a regular teacher sees only their own group's
    # lesson columns plus legacy group=NULL slots.
    grouped = (
        class_subject is not None
        and _is_regular_teacher(request.user)
        and class_subject.groups.filter(is_active=True).exists()
    )

    lessons_by_date = defaultdict(list)
    if class_subject is not None:
        lesson_qs = Lesson.objects.filter(
            class_subject=class_subject,
            date__range=(month_start, month_end),
        ).select_related('group')
        if grouped:
            lesson_qs = lesson_qs.filter(_visible_lessons_q(request.user, class_subject))
        for l in lesson_qs.order_by('date', 'lesson_number'):
            lessons_by_date[l.date].append(l)

    # Control assessments in the month, keyed by (date, lesson_id) so each
    # column can show its own Назорат marker.
    control_slots = set()
    if class_subject is not None:
        assessment_qs = Assessment.objects.filter(
            class_subject=class_subject,
            category='control',
            date__range=(month_start, month_end),
        )
        if grouped:
            assessment_qs = assessment_qs.filter(
                _visible_assessments_q(request.user, class_subject)
            )
        for a in assessment_qs:
            control_slots.add((a.date, a.lesson_id))

    # Grade cells: (date, lesson_id) -> student_id -> [cell, ...]
    cells = defaultdict(dict)
    if students:
        sem_cache = {}
        month_grades = Grade.objects.filter(
            student__in=students,
            subject=subject,
            period='Холҳои ҷорӣ (Онлайн)',
            date__range=(month_start, month_end),
        ).select_related('assessment')
        for g in month_grades:
            if g.assessment_id and g.assessment_id not in sem_cache:
                sem_cache[g.assessment_id] = result_semantics_for(g.assessment)
            text = result_display(g, sem_cache.get(g.assessment_id))
            cell = {
                'text': text,
                'control': bool(g.assessment_id),
                'absent': g.attendance == '-',
                'att': g.attendance or '',
                'beh': g.behavior_score if g.behavior_score is not None else '',
            }
            if not text and g.attendance == '-':
                cell['text'] = 'н'
            if cell['text'] or cell['absent']:
                cells[(g.date, g.lesson_id)].setdefault(g.student_id, []).append(cell)

    # Flat column list: for each day, one column per Lesson; the lesson-less
    # legacy slot only appears when it actually holds data for that date.
    columns = []
    header_groups = []
    day_rows = []
    total_students = len(students)
    dates_with_data = {k[0] for k in cells} | {k[0] for k in control_slots}
    for i in range(last_day):
        d = month_start + datetime.timedelta(days=i)
        if d.weekday() == 6 and d not in dates_with_data and not lessons_by_date.get(d):
            continue
        day_lessons = lessons_by_date.get(d, [])
        day_columns = []
        if day_lessons:
            if cells.get((d, None)):
                day_columns.append({
                    'date': d, 'lesson_id': None, 'lesson_number': None,
                    'control': (d, None) in control_slots,
                })
            for l in day_lessons:
                day_columns.append({
                    'date': d, 'lesson_id': l.id,
                    'lesson_number': l.lesson_number,
                    'control': (d, l.id) in control_slots,
                })
        else:
            day_columns.append({
                'date': d, 'lesson_id': None, 'lesson_number': None,
                'control': (d, None) in control_slots,
            })
        day_quarter = get_date_quarter(d)
        for col in day_columns:
            # A cell is writable only for a plain (non-control) slot in an
            # unlocked quarter; assessment cells stay display-only here so
            # work-type semantics are never bypassed from the month grid.
            col['editable'] = (
                can_edit
                and not col['control']
                and day_quarter not in locked_quarters
            )
        columns.extend(day_columns)

        has_control = any(col['control'] for col in day_columns)
        filled_students = set()
        for col in day_columns:
            for sid, cell_list in cells.get((d, col['lesson_id']), {}).items():
                if cell_list:
                    filled_students.add(sid)
                    if any(c['control'] for c in cell_list):
                        has_control = True
        if not filled_students:
            status = 'empty'
        elif len(filled_students) < total_students:
            status = 'partial'
        else:
            status = 'complete'
        header_groups.append({'date': d, 'span': len(day_columns), 'status': status})
        # Compact per-day grid for the mobile view: same cells, narrow
        # column set — one column per lesson slot, never merged.
        mini = [
            {
                'sid': s.id,
                'name': s.full_name,
                'cells': [
                    {
                        'lesson_id': col['lesson_id'],
                        'lesson_number': col['lesson_number'],
                        'editable': col['editable'],
                        'items': cells.get((d, col['lesson_id']), {}).get(s.id, []),
                    }
                    for col in day_columns
                ],
            }
            for s in students
        ]
        day_rows.append({
            'date': d,
            'status': status,
            'has_control': has_control,
            'multi_lesson': len(day_lessons) > 1,
            'lessons': day_lessons,
            'has_legacy_data': bool(cells.get((d, None))),
            'columns': day_columns,
            'mini': mini,
        })

    table_rows = []
    for s in students:
        row_cells = [
            {
                'date': col['date'],
                'lesson_id': col['lesson_id'],
                'editable': col['editable'],
                'items': cells.get((col['date'], col['lesson_id']), {}).get(s.id, []),
            }
            for col in columns
        ]
        table_rows.append({'student': s, 'cells': row_cells})

    return render(request, 'portal/monthly_journal.html', {
        'school': school,
        'class_name': class_name,
        'subject': subject,
        'students': students,
        'columns': columns,
        'header_groups': header_groups,
        'table_rows': table_rows,
        'day_rows': day_rows,
        'month_start': month_start,
        'prev_month': prev_month,
        'next_month': next_month,
        'month_label': month_start.strftime('%m.%Y'),
        'can_edit': can_edit,
    })


@login_required
def export_journal_excel(request, school_id, class_name, subject):
    """Lesson-aware flat export of the journal for one month.

    Reads the same authoritative Grade/Assessment/Lesson rows the monthly
    journal renders — one row per grade record, with the lesson number
    preserved explicitly so same-day lessons are never merged. Legacy
    lesson-less rows export with an empty lesson column.
    """
    school = get_object_or_404(School, id=school_id)
    class_name = normalize_class_name(class_name)
    subject = normalize_subject(subject)
    user_school = get_user_school(request.user)
    role = get_user_role(request.user)
    is_zavuch = _is_zavuch(request.user, role)
    if not (request.user.is_superuser or is_zavuch) and role == settings.ROLE_TEACHER:
        if school != user_school:
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
        if not can_view_grade_journal(request.user, school, class_name, subject):
            return redirect('class_list', school_id=user_school.id) if user_school else redirect('dashboard')
    elif not has_school_access(request.user, school):
        return redirect('dashboard')
    elif not can_view_grade_journal(request.user, school, class_name, subject):
        return HttpResponse('Дастрасӣ манъ аст.', status=403)

    month_str = request.GET.get('month', '').strip()
    month_start = None
    if month_str:
        try:
            year, mon = (int(p) for p in month_str.split('-', 1))
            month_start = datetime.date(year, mon, 1)
        except (ValueError, TypeError):
            month_start = None
    if month_start is None:
        month_start = timezone.localdate().replace(day=1)
    month_end = month_start.replace(
        day=calendar.monthrange(month_start.year, month_start.month)[1]
    )

    class_subject = _class_subject_for(school, class_name, subject)
    students = (
        visible_students_for(request.user, class_subject)
        if class_subject is not None
        else Student.objects.filter(school=school, class_name=class_name)
    )
    grades = Grade.objects.filter(
        student__in=students,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        date__range=(month_start, month_end),
    ).select_related('student', 'assessment', 'lesson').order_by(
        'student__full_name', 'date', 'lesson__lesson_number', 'id'
    )

    wb = Workbook()
    ws = wb.active
    ws.title = f'{class_name} {month_start.strftime("%m.%Y")}'

    headers = [
        '№', 'Хонанда', 'Сана', 'Дарс', 'Навъ', 'Санҷиш',
        'Хол', 'Давомат',
    ]
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')

    sem_cache = {}
    for idx, g in enumerate(grades, start=2):
        if g.assessment_id and g.assessment_id not in sem_cache:
            sem_cache[g.assessment_id] = result_semantics_for(g.assessment)
        ws.cell(row=idx, column=1, value=idx - 1)
        ws.cell(row=idx, column=2, value=g.student.full_name)
        ws.cell(row=idx, column=3, value=g.date.strftime('%d.%m.%Y'))
        ws.cell(
            row=idx, column=4,
            value=g.lesson.lesson_number if g.lesson_id else ''
        )
        ws.cell(row=idx, column=5, value='Назорат' if g.assessment_id else 'Ҷорӣ')
        label = ''
        if g.assessment_id:
            label = _assessment_label(g.assessment)
            if g.assessment.work_type:
                label += f' [{work_type_label(g.assessment.work_type)}]'
        ws.cell(row=idx, column=6, value=label)
        ws.cell(
            row=idx, column=7,
            value=result_display(g, sem_cache.get(g.assessment_id))
        )
        ws.cell(row=idx, column=8, value='н' if g.attendance == '-' else '')

    for i, width in enumerate([5, 35, 12, 8, 10, 40, 10, 10], start=1):
        ws.column_dimensions[chr(64 + i)].width = width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f'Журнал_{class_name}_{subject}_{month_start.strftime("%Y-%m")}.xlsx'
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f"attachment; filename*=utf-8''{urllib.parse.quote(filename)}"
    )
    return response


@login_required
@require_POST
def monthly_save(request):
    """Batch write endpoint for the editable monthly journal.

    Accepts a JSON `payload` list of cell edits:
        [{student_id, subject, date, lesson_id?, score?,
          attendance?, behavior_score?}, ...]
    Every item goes through the same permission, lesson-scope,
    control-mode and quarter-lock checks as the daily AJAX path — this
    endpoint adds no second write path. Assessment-linked cells are not
    writable here; they keep their semantics via the daily journal.
    """
    try:
        payload = json.loads(request.POST.get('payload', '[]'))
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'success': False, 'message': 'Формати нодуруст.'}, status=200)
    if not isinstance(payload, list):
        return JsonResponse({'success': False, 'message': 'Формати нодуруст.'}, status=200)

    results = []
    saved = 0
    for item in payload:
        key = item.get('key') if isinstance(item, dict) else None
        try:
            student = Student.objects.get(pk=item.get('student_id'))
        except (Student.DoesNotExist, AttributeError, ValueError, TypeError):
            results.append({'key': key, 'ok': False, 'message': 'Хонанда ёфт нашуд.'})
            continue
        subject = normalize_subject(str(item.get('subject', '')))
        class_name = normalize_class_name(student.class_name)
        if not can_edit_grade_journal(request.user, student.school, class_name, subject):
            results.append({'key': key, 'ok': False, 'message': 'Дастрасӣ манъ аст.'})
            continue
        cs = _class_subject_for(student.school, class_name, subject)
        if not _teacher_can_write_student(request.user, student, cs):
            results.append({'key': key, 'ok': False, 'message': 'Дастрасӣ манъ аст.'})
            continue
        try:
            date = datetime.date.fromisoformat(str(item.get('date', '')))
            lesson = _resolve_daily_scope(
                student, subject, date, item.get('lesson_id') or '',
                user=request.user,
            )
            if is_quarter_locked(
                student.school, class_name, subject, get_date_quarter(date)
            ):
                raise ValueError('Ин чоряк баста шудааст.')
            grade, _ = get_or_create_grade(
                student=student,
                subject=subject,
                period='Холҳои ҷорӣ (Онлайн)',
                date=date,
                assessment=None,
                lesson=lesson,
                defaults={'score': None, 'attendance': None,
                          'behavior_score': None, 'sticker': None},
            )
            values = {
                k: item[k] for k in ('score', 'attendance', 'behavior_score')
                if k in item
            }
            _apply_grade_fields(grade, values)
        except (ValueError, TypeError) as e:
            results.append({'key': key, 'ok': False, 'message': str(e)})
            continue
        saved += 1
        results.append({'key': key, 'ok': True})
    return JsonResponse(
        {'success': True, 'saved': saved, 'results': results}, status=200
    )


@login_required
def add_remove_subject(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)

    if request.method == 'POST':
        action = request.POST.get('action')
        subject = request.POST.get('subject', '').strip()
        subject = normalize_subject(subject)
        if action == 'add' and subject:
            get_or_create_unique(
                ClassSubject,
                school=school,
                class_name=class_name,
                subject=subject,
                defaults={'is_active': True, 'is_default': False}
            )
        elif action == 'remove' and subject:
            ClassSubject.objects.filter(
                school=school,
                class_name=class_name,
                subject=subject
            ).update(is_active=False)
    return redirect('class_detail', school_id=school.id, class_name=class_name)


@login_required
def download_template(request, class_name=None, school_id=None):
    if class_name:
        class_name = normalize_class_name(class_name)

    if school_id:
        school = get_object_or_404(School, id=school_id)
    else:
        school = get_user_school(request.user)
    school_number = get_school_number(school) if school else 'unknown'

    wb = Workbook()
    ws = wb.active
    ws.title = 'Хонандагон'

    headers = ['№ мактаб', '№ синф', 'Ному насаб', 'Синф']
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')
    header_align = Alignment(horizontal='center', vertical='center')
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align

    for i in range(2, 1001):
        ws.cell(row=i, column=1, value=f'=IF(C{i}<>"", COUNTA($C$2:C{i}), "")')
        ws.cell(row=i, column=2, value=f'=IF(C{i}<>"", COUNTIF($D$2:D{i}, D{i}), "")')

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 12
    ws.column_dimensions['C'].width = 35
    ws.column_dimensions['D'].width = 15

    # Unlock C (Ному насаб) and D (Синф) for data entry; A and B stay locked
    for row in ws.iter_rows(min_row=2, max_row=1000, min_col=1, max_col=4):
        for cell in row:
            if cell.column in (3, 4):
                cell.alignment = Alignment(horizontal='left', vertical='center')
                cell.protection = Protection(locked=False)
                cell.number_format = '@'
            else:
                cell.alignment = Alignment(horizontal='center', vertical='center')

    ws.protection.sheet = True
    ws.protection.set_password('maorif_zafarobod')

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"Шаблони_Синфҳо_Мактаби_{school_number}.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response


@login_required
def import_excel(request, class_name=None, school_id=None):
    if school_id is not None:
        school = get_object_or_404(School, id=school_id)
    else:
        user_school = get_user_school(request.user)
        if request.user.is_superuser and not user_school:
            class_name = normalize_class_name(class_name or '')
            sample_student = Student.objects.filter(class_name=class_name).first()
            if sample_student:
                school = sample_student.school
            else:
                acad = academic_schools()
                school = acad[0] if acad else None
        else:
            school = user_school

    if not school or not is_academic_school(school):
        return redirect('dashboard')

    if request.method != 'POST' or 'excel' not in request.FILES:
        return redirect('class_list', school_id=school.id)

    target_class = normalize_class_name(class_name) if class_name else None
    df = pd.read_excel(request.FILES['excel'])
    df.columns = [str(c).strip() for c in df.columns]

    name_col = 'Ному насаб' if 'Ному насаб' in df.columns else None
    class_col = 'Синф' if 'Синф' in df.columns else None
    if name_col is None:
        messages.error(request, 'Сутуни "Ному насаб" ёфт нашуд.')
        return redirect('class_list', school_id=school.id)

    fixed_cols = {name_col, class_col} if class_col else {name_col}

    imported = 0
    for _, row in df.iterrows():
        full_name = str(row.get(name_col, '')).strip()
        if not full_name or full_name.lower() in ('nan', 'none'):
            continue

        if class_col:
            c_name = str(row.get(class_col, '')).strip()
            if not c_name or c_name.lower() in ('nan', 'none'):
                c_name = target_class
            if not c_name:
                continue
            c_name = normalize_class_name(c_name)
        else:
            if not target_class:
                continue
            c_name = target_class

        student_id = f"{school.name}__{c_name}__{full_name}"
        student, _ = Student.objects.update_or_create(
            id=student_id,
            defaults={'full_name': full_name, 'class_name': c_name, 'school': school}
        )

        for col in df.columns:
            if col in fixed_cols:
                continue
            low = col.lower()
            if 'unnamed' in low or 'жами' in low or 'рейтинг' in low or '№' in col or col.strip() == '':
                continue
            subj = normalize_subject(col)
            val = row.get(col)
            if val is None or str(val).lower() in ('nan', 'none', ''):
                continue
            try:
                score = float(val)
                if 1 <= score <= 10:
                    Grade.objects.update_or_create(
                        student=student,
                        subject=subj,
                        period='Холҳои ҷорӣ (Онлайн)',
                        assessment=None,
                        lesson=None,
                        defaults={'score': score}
                    )
                    imported += 1
            except (ValueError, TypeError):
                continue

    messages.success(request, f'{imported} хол(ҳо) ворид карда шуд.')
    if target_class:
        return redirect('class_detail', school_id=school.id, class_name=target_class)
    return redirect('class_list', school_id=school.id)


@login_required
@require_POST
def save_grade_ajax(request):
    try:
        student_id = request.POST.get('student_id', '').strip()
        subject = normalize_subject(request.POST.get('subject', ''))
        date_str = request.POST.get('date', '').strip()
        score_raw = request.POST.get('score', '')
        if score_raw is not None:
            score_raw = score_raw.strip().replace(',', '.')
            if score_raw == '':
                score_raw = None
        grade_type = request.POST.get('type', 'daily')
        quarter_str = request.POST.get('quarter', '').strip()

        if not student_id or not subject:
            return JsonResponse({'success': False, 'message': 'Далелҳои нокифоя.'}, status=200)

        try:
            student = Student.objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Хонанда ёфт нашуд.'}, status=200)

        class_name = normalize_class_name(student.class_name)

        if not can_edit_grade_journal(request.user, student.school, class_name, subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)

        # Grouped ClassSubject: a regular teacher may only write students
        # who belong to their own active TeachingGroup (forged student_ids
        # of the other group are rejected here, not by hidden UI).
        cs = _class_subject_for(student.school, class_name, subject)
        if not _teacher_can_write_student(request.user, student, cs):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)

        if grade_type == 'daily':
            assessment_id = request.POST.get('assessment_id', '').strip()
            lesson_id = request.POST.get('lesson_id', '').strip()
            assessment = None
            lesson = None
            if assessment_id:
                try:
                    assessment, lesson = _resolve_assessment_for_grade(
                        student, subject, assessment_id, lesson_id,
                        user=request.user,
                    )
                except ValueError as e:
                    return JsonResponse({'success': False, 'message': str(e)}, status=200)
                # Assessment-linked grades are keyed to the server-side
                # assessment date/quarter, never to a client-supplied date.
                date = assessment.date
                lock_quarter = assessment.quarter
            else:
                try:
                    date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
                except ValueError:
                    return JsonResponse({'success': False, 'message': 'Санаи нодуруст.'}, status=200)
                lock_quarter = get_date_quarter(date)
                # Lesson-aware Ҷорӣ write (Phase 4): resolve the Lesson and
                # verify school/class/subject/date scope; mode exclusivity
                # is lesson-scoped for new records and date-wide for the
                # legacy lesson-less slot. Forged IDs are rejected.
                try:
                    lesson = _resolve_daily_scope(
                        student, subject, date, lesson_id, user=request.user
                    )
                except ValueError as e:
                    return JsonResponse({'success': False, 'message': str(e)}, status=200)

            if is_quarter_locked(student.school, class_name, subject, lock_quarter):
                return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

            # Validate the score before creating the linked row so rejected
            # input cannot leave an empty assessment Grade behind.
            parsed_score = parsed_components = None
            points_a = points_p = None
            if assessment is not None:
                wt = assessment.work_type or ''
                wt_cfg = get_work_type(wt)
                # Raw test points ride dedicated params — never the score
                # field — so '12/20' cannot be mistaken for paired grades.
                pa_raw = request.POST.get('points_achieved', '').strip()
                pp_raw = request.POST.get('points_possible', '').strip()
                if pa_raw or pp_raw:
                    if wt_cfg is None or not wt_cfg.get('points_allowed'):
                        return JsonResponse({'success': False, 'message': 'Ин навъи кор холҳои хом қабул намекунад.'}, status=200)
                    try:
                        points_a = float(pa_raw)
                        points_p = float(pp_raw)
                    except ValueError:
                        return JsonResponse({'success': False, 'message': 'Холҳо бояд рақам бошанд.'}, status=200)
                    if points_p <= 0 or not (0 <= points_a <= points_p):
                        return JsonResponse({'success': False, 'message': 'Холҳои нодуруст.'}, status=200)
                if 'score' in request.POST:
                    try:
                        parsed_score, parsed_components = parse_result_input(
                            request.POST.get('score', '').strip(),
                            work_type=wt or None,
                        )
                    except ValueError as e:
                        return JsonResponse({'success': False, 'message': str(e)}, status=200)
                if points_p is not None and wt_cfg['semantics'] == SEMANTIC_CONVERTED:
                    # Points are the authoritative raw input for tests; the
                    # 10-point result is derived via the (provisional,
                    # configurable) conversion table — a manually typed
                    # score must not override it.
                    parsed_score = convert_test_points(points_a, points_p)
                    parsed_components = None

            grade, _ = get_or_create_grade(
                student=student,
                subject=subject,
                period='Холҳои ҷорӣ (Онлайн)',
                date=date,
                assessment=assessment,
                lesson=lesson,
                defaults={'score': None, 'attendance': None, 'behavior_score': None, 'sticker': None}
            )

            if assessment is not None and ('score' in request.POST or points_p is not None):
                grade.score = parsed_score
                grade.components = parsed_components
                grade.points_achieved = points_a
                grade.points_possible = points_p

            values = {}
            if assessment is None and 'score' in request.POST:
                values['score'] = request.POST.get('score', '')
            for k in ('attendance', 'behavior_score', 'sticker'):
                if k in request.POST:
                    values[k] = request.POST.get(k, '')
            try:
                deleted = not _apply_grade_fields(grade, values)
            except ValueError as e:
                return JsonResponse({'success': False, 'message': str(e)}, status=200)

            resp = {'success': True, 'saved': True}
            if lesson is not None:
                resp['lesson_id'] = lesson.id
                resp['lesson_number'] = lesson.lesson_number
            if assessment is not None:
                # Authoritative echo for immediate UI sync: the combined
                # result, its display form, and the recalculated quarter
                # projection (official QuarterGrade still wins over live).
                if deleted or (grade.score is None and not grade.components):
                    resp['score'] = None
                    resp['display'] = ''
                else:
                    resp['score'] = grade.score
                    if grade.components:
                        # Echo the typed fraction form ('8/9'). For
                        # paired-grades works this IS the journal display;
                        # for legacy mean rows it preserves the Phase 3
                        # response contract (score carries the mean).
                        resp['display'] = '/'.join(
                            str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() else str(v)
                            for v in (c.get('value') if isinstance(c, dict) else c
                                      for c in grade.components)
                        )
                    else:
                        resp['display'] = str(int(grade.score)) if float(grade.score).is_integer() else str(grade.score)
                    if grade.points_possible:
                        resp['points_achieved'] = grade.points_achieved
                        resp['points_possible'] = grade.points_possible
                        resp['percentage'] = round(test_percentage(
                            grade.points_achieved, grade.points_possible
                        ), 1)
                qmap, live_flags = _live_quarter_payload(student, subject, class_name)
                resp.update(calc_quarterly(qmap, live_flags))
                resp['quarter'] = assessment.quarter
                resp['quarter_value'] = qmap.get(assessment.quarter)
                resp['quarter_live'] = assessment.quarter in live_flags
            return JsonResponse(resp, status=200)

        elif grade_type == 'quarterly':
            try:
                quarter = int(quarter_str)
            except ValueError:
                return JsonResponse({'success': False, 'message': 'Чораки нодуруст.'}, status=200)

            if is_quarter_locked(student.school, class_name, subject, quarter):
                return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

            try:
                score = _parse_score(score_raw)
            except ValueError as e:
                return JsonResponse({'success': False, 'message': str(e)}, status=200)

            if score is not None and 1 <= score <= 10:
                if quarter == 0:
                    QuarterGrade.objects.update_or_create(
                        student=student,
                        class_name=class_name,
                        subject=subject,
                        quarter=0,
                        defaults={'att_grade': score}
                    )
                else:
                    QuarterGrade.objects.update_or_create(
                        student=student,
                        class_name=class_name,
                        subject=subject,
                        quarter=quarter,
                        defaults={'grade': score}
                    )
            else:
                QuarterGrade.objects.filter(
                    student=student,
                    class_name=class_name,
                    subject=subject,
                    quarter=quarter
                ).delete()

            qmap = {}
            for g in QuarterGrade.objects.filter(student=student, class_name=class_name, subject=subject):
                if g.quarter == 0 and g.att_grade is not None:
                    qmap['att'] = g.att_grade
                elif g.grade is not None:
                    qmap[g.quarter] = g.grade
            calc = calc_quarterly(qmap)
            return JsonResponse({'success': True, 'saved': True, **calc}, status=200)

        return JsonResponse({'success': False, 'message': 'Навъи нодуруст.'}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


@login_required
@require_POST
def calc_quarter_from_daily(request, school_id, class_name, subject):
    school = get_object_or_404(School, id=school_id)
    if not has_school_access(request.user, school):
        return HttpResponse('Дастрасӣ манъ аст.', status=403)
    class_name = normalize_class_name(class_name)
    subject = normalize_subject(subject)

    if not can_edit_grade_journal(request.user, school, class_name, subject):
        return HttpResponse('Дастрасӣ барои тағйир додан манъ аст.', status=403)

    class_subject = _class_subject_for(school, class_name, subject)
    students = (
        visible_students_for(request.user, class_subject)
        if class_subject is not None
        else Student.objects.filter(school=school, class_name=class_name)
    )
    for student in students:
        grades = Grade.objects.filter(
            student=student,
            subject=subject,
            period='Холҳои ҷорӣ (Онлайн)',
            score__isnull=False
        ).select_related('assessment')
        for q in (1, 2, 3, 4):
            if is_quarter_locked(school, class_name, subject, q):
                continue
            current_scores, ajm_results = _split_quarter_grades(grades, q)
            value = calc_quarter_value(current_scores, ajm_results)
            if value is None:
                continue
            QuarterGrade.objects.update_or_create(
                student=student,
                class_name=class_name,
                subject=subject,
                quarter=q,
                defaults={'grade': std_round(value)}
            )
    return redirect('grade_entry', school_id=school.id, class_name=class_name, subject=subject)


@login_required
@require_POST
def assessment_save(request):
    """Create or edit a Назорат (control) Assessment for a ClassSubject.

    New assessments are always category='control' with the quarter derived
    server-side from the date. Edits touch only title/number/is_ajm/
    date/lesson — linked Grade rows are never modified; the Phase 2
    calculation derives pool membership from Assessment.is_ajm at read time.
    """
    try:
        assessment_id = request.POST.get('assessment_id', '').strip()
        class_subject_id = request.POST.get('class_subject_id', '').strip()
        date_str = request.POST.get('date', '').strip()
        title = request.POST.get('title', '').strip()[:255]
        number_str = request.POST.get('number', '').strip()
        is_ajm = request.POST.get('is_ajm') in ('1', 'true', 'on')
        lesson_id = request.POST.get('lesson_id', '').strip()
        purpose_in = request.POST.get('purpose', '').strip()
        work_type_in = request.POST.get('work_type', '').strip()
        scope_in = request.POST.get('summative_scope', '').strip()
        if purpose_in and purpose_in not in ASSESSMENT_PURPOSES:
            return JsonResponse({'success': False, 'message': 'Мақсади нодуруст.'}, status=200)
        if work_type_in and get_work_type(work_type_in) is None:
            return JsonResponse({'success': False, 'message': 'Навъи кори нодуруст.'}, status=200)
        if scope_in and scope_in not in SUMMATIVE_SCOPES:
            return JsonResponse({'success': False, 'message': 'Доираи нодуруст.'}, status=200)

        assessment = None
        if assessment_id:
            try:
                assessment = Assessment.objects.select_related('class_subject__school').get(pk=assessment_id)
            except (Assessment.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Санҷиш ёфт нашуд.'}, status=200)
            cs = assessment.class_subject
        else:
            try:
                cs = ClassSubject.objects.select_related('school').get(pk=class_subject_id)
            except (ClassSubject.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Фан ёфт нашуд.'}, status=200)
            if not cs.is_active:
                return JsonResponse({'success': False, 'message': 'Фан фаъол нест.'}, status=200)

        if not can_edit_grade_journal(request.user, cs.school, cs.class_name, cs.subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)

        is_regular = _is_regular_teacher(request.user)
        cs_grouped = cs.groups.filter(is_active=True).exists()
        own_group = teacher_group_for(request.user, cs) if is_regular else None

        try:
            date = datetime.date.fromisoformat(date_str) if date_str else (
                assessment.date if assessment is not None else datetime.date.today()
            )
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Санаи нодуруст.'}, status=200)
        quarter = get_date_quarter(date)

        # Lock check covers both the quarter the assessment currently sits
        # in and the quarter it is being moved to (edits change results).
        quarters_to_check = {quarter}
        if assessment is not None:
            quarters_to_check.add(assessment.quarter)
        for q in quarters_to_check:
            if is_quarter_locked(cs.school, cs.class_name, cs.subject, q):
                return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

        number = None
        if number_str:
            try:
                number = int(number_str)
            except ValueError:
                return JsonResponse({'success': False, 'message': 'Рақами нодуруст.'}, status=200)
            if number < 1:
                return JsonResponse({'success': False, 'message': 'Рақами нодуруст.'}, status=200)

        lesson = None
        if lesson_id:
            try:
                lesson = Lesson.objects.get(pk=lesson_id)
            except (Lesson.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Дарс ёфт нашуд.'}, status=200)
            if lesson.class_subject_id != cs.id:
                return JsonResponse({'success': False, 'message': 'Дарс ба ин санҷиш тааллуқ надорад.'}, status=200)
            # The lesson must live on the assessment's own date — a
            # lesson from another day cannot host this assessment.
            expected_date = assessment.date if assessment is not None else date
            if lesson.date != expected_date:
                return JsonResponse({'success': False, 'message': 'Дарс ба ин сана тааллуқ надорад.'}, status=200)

        if cs_grouped and is_regular:
            # A group teacher may only file/edit controls that live on their
            # own group's lesson; lesson-less assessments on grouped
            # subjects stay Admin/Zavuch-managed.
            if assessment is not None and (
                assessment.lesson_id is None
                or assessment.lesson.group_id != (own_group.id if own_group else None)
            ):
                return JsonResponse({'success': False, 'message': 'Санҷиш ба гурӯҳи шумо тааллуқ надорад.'}, status=200)
            target_lesson = lesson if lesson is not None else (
                assessment.lesson if assessment is not None else None
            )
            if own_group is None or target_lesson is None or target_lesson.group_id != own_group.id:
                return JsonResponse({'success': False, 'message': 'Санҷиш бояд ба дарси гурӯҳи шумо вобаста бошад.'}, status=200)

        with transaction.atomic():
            if assessment is None:
                if number is None:
                    max_number = Assessment.objects.filter(
                        class_subject=cs, quarter=quarter, category='control'
                    ).aggregate(m=Max('number'))['m'] or 0
                    number = max_number + 1
                assessment, created = get_or_create_unique(
                    Assessment,
                    class_subject=cs,
                    date=date,
                    category='control',
                    number=number,
                    defaults={
                        'quarter': quarter,
                        'title': title,
                        'is_ajm': is_ajm,
                        'lesson': lesson,
                        # Control-category events are summative by default;
                        # the posted purpose wins when explicitly supplied.
                        'purpose': purpose_in or 'summative',
                        'work_type': work_type_in,
                        'summative_scope': scope_in or 'none',
                    }
                )
                if not created:
                    # Same natural key already exists (double submit): make
                    # the call idempotent instead of duplicating the row.
                    # Grouped CS: a regular teacher must never hijack a row
                    # owned by another group (or a lesson-less admin row).
                    if cs_grouped and is_regular and (
                        assessment.lesson_id is None
                        or assessment.lesson.group_id != own_group.id
                    ):
                        return JsonResponse({'success': False, 'message': 'Санҷиш ба гурӯҳи шумо тааллуқ надорад.'}, status=200)
                    assessment.title = title or assessment.title
                    assessment.is_ajm = is_ajm
                    if lesson is not None:
                        assessment.lesson = lesson
                    if purpose_in:
                        assessment.purpose = purpose_in
                    if work_type_in:
                        assessment.work_type = work_type_in
                    if scope_in:
                        assessment.summative_scope = scope_in
                    assessment.save()
            else:
                # Editable fields only: title/number/is_ajm/lesson/methodology.
                # The Assessment's date and quarter are preserved — the journal
                # date the teacher happens to be viewing must not silently
                # move an existing assessment (and its linked Grade rows)
                # into another date or quarter.
                assessment.title = title
                assessment.number = number
                assessment.is_ajm = is_ajm
                if lesson is not None or lesson_id:
                    assessment.lesson = lesson
                if purpose_in:
                    assessment.purpose = purpose_in
                if work_type_in:
                    assessment.work_type = work_type_in
                if scope_in:
                    assessment.summative_scope = scope_in
                assessment.save()

        return JsonResponse({'success': True, 'assessment': {
            'id': assessment.id,
            'label': _assessment_label(assessment),
            'number': assessment.number,
            'title': assessment.title,
            'is_ajm': assessment.is_ajm,
            'date': assessment.date.isoformat(),
            'purpose': assessment.purpose,
            'work_type': assessment.work_type,
            'work_type_label': work_type_label(assessment.work_type),
            'summative_scope': assessment.summative_scope,
            'lesson_id': assessment.lesson_id,
        }}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


@login_required
@require_POST
def assessment_delete(request):
    """Delete a control Assessment together with its linked Grade rows.

    Deliberate teacher action (the UI confirms first). Linked Grade rows
    are removed rather than detached — Grade.assessment is SET_NULL, and
    letting it detach would silently turn control results into ordinary
    Ҷорӣ daily grades on that date. Once the assessment is gone the date
    becomes eligible for Ҷорӣ entry again.
    """
    try:
        assessment_id = request.POST.get('assessment_id', '').strip()
        try:
            assessment = Assessment.objects.select_related('class_subject__school').get(pk=assessment_id)
        except (Assessment.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Санҷиш ёфт нашуд.'}, status=200)
        cs = assessment.class_subject
        if not can_edit_grade_journal(request.user, cs.school, cs.class_name, cs.subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)
        if _is_regular_teacher(request.user) and cs.groups.filter(is_active=True).exists():
            own_group = teacher_group_for(request.user, cs)
            if (own_group is None or assessment.lesson_id is None
                    or assessment.lesson.group_id != own_group.id):
                return JsonResponse({'success': False, 'message': 'Санҷиш ба гурӯҳи шумо тааллуқ надорад.'}, status=200)
        if is_quarter_locked(cs.school, cs.class_name, cs.subject, assessment.quarter):
            return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)
        deleted_quarter = assessment.quarter
        affected_ids = list(
            Grade.objects.filter(assessment=assessment).values_list('student_id', flat=True).distinct()
        )
        with transaction.atomic():
            # Deliberate, narrowly scoped exception to the "grades are never
            # silently deleted" rule: ONLY rows linked to this Assessment are
            # removed, and they must be deleted rather than detached — letting
            # Grade.assessment SET_NULL fire would silently convert control
            # results into ordinary Ҷорӣ daily grades on that date.
            Grade.objects.filter(assessment=assessment).delete()
            assessment.delete()
        # Return the recalculated quarter for every affected student so the
        # page can refresh quarter cells immediately — the deleted grades no
        # longer contribute to the projection.
        students_payload = {}
        for sid in affected_ids:
            st = Student.objects.get(pk=sid)
            qmap, live_flags = _live_quarter_payload(st, cs.subject, cs.class_name)
            entry = calc_quarterly(qmap, live_flags)
            entry['quarter'] = deleted_quarter
            entry['quarter_value'] = qmap.get(deleted_quarter)
            entry['quarter_live'] = deleted_quarter in live_flags
            students_payload[str(sid)] = entry
        return JsonResponse({'success': True, 'students': students_payload}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


@login_required
@require_POST
def lesson_save(request):
    """Create a Lesson for a ClassSubject+date (Phase 4 backend).

    Idempotent on (class_subject, date, lesson_number): a repeated identical
    request returns the existing row instead of duplicating it. When
    lesson_number is omitted the next free number for that date is assigned.
    """
    try:
        class_subject_id = request.POST.get('class_subject_id', '').strip()
        date_str = request.POST.get('date', '').strip()
        number_str = request.POST.get('lesson_number', '').strip()
        topic = request.POST.get('topic', '').strip()[:255]

        try:
            cs = ClassSubject.objects.select_related('school').get(pk=class_subject_id)
        except (ClassSubject.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Фан ёфт нашуд.'}, status=200)
        if not cs.is_active:
            return JsonResponse({'success': False, 'message': 'Фан фаъол нест.'}, status=200)
        if not can_edit_grade_journal(request.user, cs.school, cs.class_name, cs.subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)

        # Group ownership (Phase 3): on a grouped ClassSubject a regular
        # teacher's new lessons always belong to their own active group —
        # a client-supplied group_id is never trusted. Admin/Zavuch may
        # explicitly target a group, or leave the lesson shared (NULL).
        group = None
        if cs.groups.filter(is_active=True).exists():
            if _is_regular_teacher(request.user):
                group = teacher_group_for(request.user, cs)
                if group is None:
                    return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)
            else:
                gid = request.POST.get('group_id', '').strip()
                if gid:
                    try:
                        group = cs.groups.get(pk=int(gid), is_active=True)
                    except (TeachingGroup.DoesNotExist, ValueError, TypeError):
                        return JsonResponse({'success': False, 'message': 'Гурӯҳ ёфт нашуд.'}, status=200)

        try:
            date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Санаи нодуруст.'}, status=200)

        if is_quarter_locked(cs.school, cs.class_name, cs.subject, get_date_quarter(date)):
            return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

        number = None
        if number_str:
            try:
                number = int(number_str)
            except ValueError:
                return JsonResponse({'success': False, 'message': 'Рақами нодуруст.'}, status=200)
            if number < 1:
                return JsonResponse({'success': False, 'message': 'Рақами нодуруст.'}, status=200)

        with transaction.atomic():
            if number is None:
                number = (Lesson.objects.filter(
                    class_subject=cs, date=date
                ).aggregate(m=Max('lesson_number'))['m'] or 0) + 1
            lesson, created = get_or_create_unique(
                Lesson,
                class_subject=cs,
                date=date,
                lesson_number=number,
                defaults={'topic': topic, 'group': group},
            )
            if not created:
                # The slot already exists: a regular teacher may only touch
                # their own group's lesson — never edit or re-label a row
                # owned by another group or left shared (group=NULL).
                if _is_regular_teacher(request.user) and not _teacher_can_edit_lesson(request.user, lesson):
                    return JsonResponse({'success': False, 'message': 'Дарс ба гурӯҳи шумо тааллуқ надорад.'}, status=403)
                if topic:
                    lesson.topic = topic
                    lesson.save()

        return JsonResponse({'success': True, 'lesson': {
            'id': lesson.id,
            'lesson_number': lesson.lesson_number,
            'date': lesson.date.isoformat(),
            'topic': lesson.topic,
        }}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


@login_required
@require_POST
def lesson_delete(request):
    """Delete an empty Lesson. Block-when-data-exists by design.

    Grade.lesson and Assessment.lesson are SET_NULL — deleting a lesson
    with linked rows would silently orphan them into the day's lesson-less
    pool, so deletion is refused until the teacher clears the data first.
    """
    try:
        lesson_id = request.POST.get('lesson_id', '').strip()
        try:
            lesson = Lesson.objects.select_related('class_subject__school').get(pk=lesson_id)
        except (Lesson.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Дарс ёфт нашуд.'}, status=200)
        cs = lesson.class_subject
        if not can_edit_grade_journal(request.user, cs.school, cs.class_name, cs.subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)
        if not _teacher_can_edit_lesson(request.user, lesson):
            return JsonResponse({'success': False, 'message': 'Дарс ба гурӯҳи шумо тааллуқ надорад.'}, status=403)
        if is_quarter_locked(cs.school, cs.class_name, cs.subject, get_date_quarter(lesson.date)):
            return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)
        if Grade.objects.filter(lesson=lesson).exists() or Assessment.objects.filter(lesson=lesson).exists():
            return JsonResponse({
                'success': False,
                'message': 'Ин дарс холҳо ё санҷишҳо дорад — аввал маълумоти дарсро тоза кунед.'
            }, status=200)
        with transaction.atomic():
            lesson.delete()
        return JsonResponse({'success': True}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


def _student_subject_scores(student):
    """Return a dict of {normalized_subject: average_score} for a student."""
    totals = {}
    for row in Grade.objects.filter(student=student, score__isnull=False).values('subject').annotate(total=Sum('score'), count=Count('score')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    for row in QuarterGrade.objects.filter(student=student, quarter__in=(1, 2, 3, 4), grade__isnull=False).values('subject').annotate(total=Sum('grade'), count=Count('grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    for row in QuarterGrade.objects.filter(student=student, quarter=0, att_grade__isnull=False).values('subject').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    return {subj: round(total / count, 2) if count else 0.0 for subj, (total, count) in totals.items()}


def _all_student_gpas():
    """Return {student_id: overall_gpa} for every student using Grade and QuarterGrade."""
    totals = {}
    for row in Grade.objects.filter(score__isnull=False).values('student_id').annotate(total=Sum('score'), count=Count('score')):
        _add_score(totals, row['student_id'], row['total'], row['count'])
    for row in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False).values('student_id').annotate(total=Sum('grade'), count=Count('grade')):
        _add_score(totals, row['student_id'], row['total'], row['count'])
    for row in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False).values('student_id').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        _add_score(totals, row['student_id'], row['total'], row['count'])

    gpas = {}
    for student in Student.objects.all():
        total, count = totals.get(student.id, (0.0, 0))
        gpas[student.id] = round(total / count, 2) if count else 0.0
    return gpas


def student_detail(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    if request.user.is_authenticated and not has_school_access(request.user, student.school):
        return redirect('dashboard')

    subject_scores = _student_subject_scores(student)
    subjects = sorted(subject_scores.keys())
    scores = [subject_scores[s] for s in subjects]

    non_graded = is_non_graded(student.class_name)

    if non_graded:
        student_gpa = None
        class_rank = None
        school_rank = None
        district_rank = None
    else:
        all_gpas = _all_student_gpas()
        student_gpa = all_gpas.get(student.id, 0.0)

        class_gpas = []
        school_gpas = []
        district_gpas = []
        # Rank only against graded students of academic schools (same population as dashboard rankings)
        academic_ids = {s.id for s in School.objects.all() if is_academic_school(s)}
        for s in Student.objects.all():
            if s.school_id not in academic_ids or is_non_graded(s.class_name):
                continue
            g = all_gpas.get(s.id, 0.0)
            district_gpas.append(g)
            if s.school_id == student.school_id:
                school_gpas.append(g)
            if s.school_id == student.school_id and s.class_name == student.class_name:
                class_gpas.append(g)

        class_rank = len({g for g in class_gpas if g > student_gpa}) + 1
        school_rank = len({g for g in school_gpas if g > student_gpa}) + 1
        district_rank = len({g for g in district_gpas if g > student_gpa}) + 1

    if non_graded:
        recent_stickers = Grade.objects.filter(student=student, sticker__isnull=False).order_by('-date')
    else:
        recent_stickers = Grade.objects.filter(student=student, sticker__isnull=False).order_by('-date')[:15]

    today = datetime.date.today()
    today_grades = [
        {'subject': g.subject, 'score': int(g.score) if g.score == int(g.score) else g.score}
        for g in Grade.objects.filter(student=student, date=today, score__isnull=False)
    ]

    today_attendance = Grade.objects.filter(student=student, date=today, attendance__in=['+', '-'])
    today_excused = today_attendance.filter(attendance='+').count()
    today_unexcused = today_attendance.filter(attendance='-').count()

    today_behavior_qs = Grade.objects.filter(student=student, date=today, behavior_score__isnull=False)
    today_behavior_breakdown = {}
    today_total_infractions = 0
    for g in today_behavior_qs:
        infractions = 5 - g.behavior_score
        today_behavior_breakdown[g.subject] = {'score': g.behavior_score, 'infractions': infractions}
        today_total_infractions += infractions
    today_behavior_status = 'Намунавӣ' if today_total_infractions == 0 else 'Нигаронкунанда'

    today_avg_qs = today_behavior_qs.aggregate(avg=Avg('behavior_score'))
    today_avg = today_avg_qs['avg'] if today_avg_qs['avg'] is not None else None

    previous_day = Grade.objects.filter(
        student=student, behavior_score__isnull=False, date__lt=today
    ).order_by('-date').values_list('date', flat=True).first()
    yesterday_avg = None
    if previous_day:
        yesterday_avg_qs = Grade.objects.filter(
            student=student, date=previous_day, behavior_score__isnull=False
        ).aggregate(avg=Avg('behavior_score'))
        yesterday_avg = yesterday_avg_qs['avg'] if yesterday_avg_qs['avg'] is not None else None

    behavior_trend = None
    behavior_trend_class = ''
    if today_avg is not None and yesterday_avg is not None:
        if today_avg == 5 and yesterday_avg == 5:
            behavior_trend = '⭐ Рафтори намунавӣ ва устувор!'
            behavior_trend_class = 'trend-stable'
        elif today_avg > yesterday_avg:
            behavior_trend = '📈 Рафтор беҳтар шуд! Баракалла, ҳамин тавр давом диҳед!'
            behavior_trend_class = 'trend-improved'
        elif today_avg < yesterday_avg:
            behavior_trend = '📉 Рафтор паст шуд. Лутфан, бештар диққат диҳед!'
            behavior_trend_class = 'trend-declined'

    context = {
        'student': student,
        'gpa': student_gpa,
        'non_graded': non_graded,
        'class_rank': class_rank,
        'school_rank': school_rank,
        'district_rank': district_rank,
        'subjects': subjects,
        'scores': scores,
        'recent_stickers': recent_stickers,
        'sticker_labels': {'⭐': 'Ситора', '☀️': 'Офтобак', '🌸': 'Гул', '📖': 'Китоб'},
        'today': today,
        'today_grades': today_grades,
        'today_excused': today_excused,
        'today_unexcused': today_unexcused,
        'today_behavior_breakdown': today_behavior_breakdown,
        'today_total_infractions': today_total_infractions,
        'today_behavior_status': today_behavior_status,
        'behavior_trend': behavior_trend,
        'behavior_trend_class': behavior_trend_class,
    }
    return render(request, 'portal/student_detail.html', context)


def _next_teacher_counter(school_num):
    """Return the next sequential teacher username counter for a school."""
    prefix = f'teacher_{school_num}_'
    pattern = re.compile(rf'^{re.escape(prefix)}(\d+)$')
    max_counter = 0
    for username in User.objects.filter(username__startswith=prefix).values_list('username', flat=True):
        m = pattern.match(username)
        if m:
            max_counter = max(max_counter, int(m.group(1)))
    return max_counter + 1


@login_required
def teacher_list(request, school_id=None):
    role = get_user_role(request.user)
    user_school = get_user_school(request.user)
    if school_id:
        school = get_object_or_404(School, id=school_id)
    else:
        school = user_school
    if school is not None and not is_academic_school(school):
        return redirect('school_list')
    if not has_school_access(request.user, school):
        return redirect('dashboard')
    teachers = Teacher.objects.filter(school=school).order_by('name')
    for teacher in teachers:
        tp = TeacherProfile.objects.filter(school=school, full_name=teacher.name).select_related('user').first()
        if tp and tp.user:
            teacher.username = tp.user.username
            teacher.is_password_private = tp.user.last_login is not None
            if teacher.is_password_private:
                teacher.password_display = 'Рамзи шахсӣ 🔒'
            else:
                teacher.password_display = f'{tp.user.username.capitalize()}@2026'
        else:
            teacher.username = ''
            teacher.is_password_private = False
            teacher.password_display = '—'
    return render(request, 'portal/teacher_list.html', {'school': school, 'teachers': teachers, 'role': role})


@login_required
@require_POST
def add_teacher(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')

    full_name = request.POST.get('full_name', '').strip()
    phone = request.POST.get('phone', '').strip()
    subject = request.POST.get('subject', '').strip()

    if not full_name:
        messages.error(request, 'Ному насаби омӯзгор бояд ворид карда шавад.')
        return redirect('teacher_list', school_id=school.id)

    school_num = get_school_number(school)
    counter = _next_teacher_counter(school_num)
    username = f'teacher_{school_num}_{counter}'
    password = f'Teacher_{school_num}_{counter}@2026'

    try:
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=full_name,
        )
    except Exception:
        messages.error(request, 'Эҷоди ҳисоби корбар муваффақият амалӣ нагашт.')
        return redirect('teacher_list', school_id=school.id)

    TeacherProfile.objects.create(
        user=user,
        school=school,
        full_name=full_name,
        phone=phone,
        specialty=subject,
    )
    UserProfile.objects.update_or_create(
        user=user,
        defaults={
            'role': settings.ROLE_TEACHER,
            'school': school,
        },
    )
    Teacher.objects.create(
        school=school,
        name=full_name,
        subject=subject,
        phone=phone,
    )

    messages.success(request, f'Омӯзгор {full_name} бо муваффақият илова шуд. Логин: {username}')
    return redirect('teacher_list', school_id=school.id)


@login_required
@require_POST
def remove_teacher(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')

    teacher_id = request.POST.get('teacher_id', '').strip()
    if not teacher_id:
        messages.error(request, 'ID-и омӯзгор муайян карда нашуд.')
        return redirect('teacher_list', school_id=school.id)

    try:
        teacher = Teacher.objects.get(id=teacher_id, school=school)
    except Teacher.DoesNotExist:
        messages.error(request, 'Омӯзгор ёфт нашуд.')
        return redirect('teacher_list', school_id=school.id)

    full_name = teacher.name
    teacher_profile = TeacherProfile.objects.filter(school=school, full_name=full_name).first()
    if teacher_profile:
        teacher_profile.user.delete()
    teacher.delete()

    messages.success(request, f'Омӯзгор {full_name} хориҷ карда шуд.')
    return redirect('teacher_list', school_id=school.id)


@login_required
@require_POST
def edit_teacher(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')

    teacher_id = request.POST.get('teacher_id', '').strip()
    full_name = request.POST.get('full_name', '').strip()
    phone = request.POST.get('phone', '').strip()
    subject = request.POST.get('subject', '').strip()
    new_password = request.POST.get('new_password', '').strip()

    if not teacher_id or not full_name:
        messages.error(request, 'Иттилооти нокифоя барои таҳрири омӯзгор.')
        return redirect('teacher_list', school_id=school.id)

    try:
        teacher = Teacher.objects.get(id=teacher_id, school=school)
    except Teacher.DoesNotExist:
        messages.error(request, 'Омӯзгор ёфт нашуд.')
        return redirect('teacher_list', school_id=school.id)

    old_name = teacher.name
    teacher.name = full_name
    teacher.phone = phone
    teacher.subject = subject
    teacher.save()

    teacher_profile = TeacherProfile.objects.filter(school=school, full_name=old_name).first()
    if teacher_profile:
        teacher_profile.full_name = full_name
        teacher_profile.phone = phone
        teacher_profile.specialty = subject
        teacher_profile.save()
        if teacher_profile.user:
            teacher_profile.user.first_name = full_name
            if new_password:
                teacher_profile.user.set_password(new_password)
            teacher_profile.user.save()

    messages.success(request, f'Омӯзгор {full_name} таҳрир шуд.')
    return redirect('teacher_list', school_id=school.id)


@login_required
def download_teacher_template(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not has_school_access(request.user, school):
        messages.error(request, 'Дастрасӣ ба ин муассиса манъ аст.')
        return redirect('dashboard')
    school_number = get_school_number(school)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Омӯзгорон'

    headers = ['№', 'Ному насаби омӯзгор', 'Рақами телефон', 'Маълумот', 'Ихтисос']
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')
    header_align = Alignment(horizontal='center', vertical='center')

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align

    # Sample data row (B-E unlocked for editing; A is formula-driven and locked)
    ws.cell(row=2, column=2, value='Намуна: Раҷабов А.')
    ws.cell(row=2, column=3, value='+992901234567')
    ws.cell(row=2, column=4, value='Олии педагогӣ')
    ws.cell(row=2, column=5, value='Математика')

    for i in range(2, 1001):
        ws.cell(row=i, column=1, value=f'=IF(B{i}<>"", COUNTA($B$2:B{i}), "")')

    ws.column_dimensions['A'].width = 5
    ws.column_dimensions['B'].width = 35
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 20
    ws.column_dimensions['E'].width = 25

    # Leave A locked with formulas; unlock B, C, D, E for data entry
    for row in ws.iter_rows(min_row=2, max_row=1000, min_col=2, max_col=5):
        for cell in row:
            cell.protection = Protection(locked=False)
            cell.number_format = '@'

    ws.protection.sheet = True
    ws.protection.set_password('maorif_zafarobod')

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"Шаблони_Омӯзгорон_Мактаби_{school_number}.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response


@login_required
def import_teachers(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not has_school_access(request.user, school):
        messages.error(request, 'Дастрасӣ ба ин муассиса манъ аст.')
        return redirect('dashboard')

    if request.method != 'POST' or 'excel' not in request.FILES:
        return redirect('teacher_list', school_id=school.id)

    wb = load_workbook(request.FILES['excel'], data_only=True)
    ws = wb.active

    created_teachers = []
    updated_teachers = []
    school_num = get_school_number(school)
    counter = _next_teacher_counter(school_num)

    for row in ws.iter_rows(min_row=2, values_only=True):
        full_name = str(row[1] or '').strip() if len(row) > 1 else ''
        if not full_name or full_name.lower() in ('nan', 'none', ''):
            continue

        phone = str(row[2] or '').strip() if len(row) > 2 else ''
        education = str(row[3] or '').strip() if len(row) > 3 else ''
        specialty = str(row[4] or '').strip() if len(row) > 4 else ''

        # Avoid creating duplicates: update existing teacher details if found.
        existing_tp = TeacherProfile.objects.filter(school=school, full_name=full_name).first()
        existing_teacher = Teacher.objects.filter(school=school, name=full_name).first()
        if existing_tp or existing_teacher:
            if existing_tp:
                existing_tp.phone = phone
                existing_tp.education = education
                existing_tp.specialty = specialty
                existing_tp.save()
                if existing_tp.user:
                    existing_tp.user.first_name = full_name
                    existing_tp.user.save()
            if existing_teacher:
                existing_teacher.phone = phone
                existing_teacher.subject = specialty
                existing_teacher.save()
            updated_teachers.append({'full_name': full_name})
            continue

        # Find the next available username for this school.
        username = f'teacher_{school_num}_{counter}'
        password = f'Teacher_{school_num}_{counter}@2026'
        while User.objects.filter(username=username).exists():
            counter += 1
            username = f'teacher_{school_num}_{counter}'
            password = f'Teacher_{school_num}_{counter}@2026'

        try:
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=full_name,
            )
        except Exception:
            continue

        TeacherProfile.objects.create(
            user=user,
            school=school,
            full_name=full_name,
            phone=phone,
            education=education,
            specialty=specialty,
        )

        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                'role': settings.ROLE_TEACHER,
                'school': school,
            },
        )

        Teacher.objects.create(
            school=school,
            name=full_name,
            subject=specialty,
            phone=phone,
        )

        created_teachers.append({
            'full_name': full_name,
            'username': username,
            'password': password,
        })
        counter += 1

    return render(request, 'portal/imported_teachers_list.html', {
        'school': school,
        'created_teachers': created_teachers,
        'updated_teachers': updated_teachers,
    })


@login_required
def lesson_allocation(request):
    if not (request.user.is_superuser or _is_zavuch(request.user, get_user_role(request.user))):
        messages.error(request, 'Дастрасӣ маҳдуд аст. Ин бахш танҳо барои муовини директор (завуч) дастрас аст.')
        return redirect('dashboard')

    all_schools = None
    if request.user.is_superuser:
        # Superadmin may inspect/allocate any district school via ?school_id=
        all_schools = [s for s in School.objects.all().order_by('id') if is_academic_school(s)]
        school = None
        school_id = request.GET.get('school_id') or request.GET.get('school')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
                if not is_academic_school(school):
                    school = None
            except (ValueError, School.DoesNotExist):
                school = None
        if school is None:
            school = get_user_school(request.user) or (all_schools[0] if all_schools else None)
    else:
        # Zavuchs are strictly locked to their own school
        school = get_user_school(request.user)

    if school and not is_academic_school(school):
        messages.warning(request, 'Ин муассиса мактаби таҳсилоти умумӣ нест.')
        return redirect('school_list')
    if not school:
        messages.error(request, 'Муассисаи шумо муайян карда нашуд.')
        return redirect('dashboard')
    if not has_school_access(request.user, school):
        return redirect('dashboard')

    class_subjects = ClassSubject.objects.filter(
        school=school, is_active=True
    ).select_related('allocated_teacher', 'teacher').prefetch_related('groups').order_by('class_name', 'subject')
    teachers = TeacherProfile.objects.filter(school=school).select_related('user').order_by('full_name')

    user_to_profile = {tp.user_id: tp.id for tp in teachers}
    for cs in class_subjects:
        cs.selected_profile_id = cs.allocated_teacher_id or user_to_profile.get(cs.teacher_id)
        # Explicitly saved weekly hours win; otherwise show the national
        # curriculum recommendation as a suggested (muted) value.
        cs.hours_display = weekly_hours(cs.class_name, cs.subject, cs)
        cs.group_count = sum(1 for g in cs.groups.all() if g.is_active)

    my_requests = list(SubjectDeactivationRequest.objects.filter(
        requested_by=request.user
    ).select_related('class_subject').order_by('-requested_at'))

    pending_request_ids = {r.class_subject_id for r in my_requests if r.status == 'pending'}

    return render(request, 'school/lesson_allocation.html', {
        'school': school,
        'selected_school': school,
        'all_schools': all_schools,
        'class_subjects': class_subjects,
        'teachers': teachers,
        'is_superuser': request.user.is_superuser,
        'is_zavuch': _is_zavuch(request.user, get_user_role(request.user)),
        'my_requests': my_requests,
        'pending_request_ids': pending_request_ids,
        'max_weekly_hours': MAX_WEEKLY_HOURS,
    })


@login_required
@require_POST
def save_lesson_allocation(request):
    if not (request.user.is_superuser or _is_zavuch(request.user, get_user_role(request.user))):
        messages.error(request, 'Дастрасӣ маҳдуд аст. Ин бахш танҳо барои муовини директор (завуч) дастрас аст.')
        return redirect('dashboard')
    school = get_user_school(request.user)
    if request.user.is_superuser:
        school_id = request.POST.get('school_id')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
            except (ValueError, School.DoesNotExist):
                school = None
    if not school or not is_academic_school(school) or not has_school_access(request.user, school):
        return redirect('dashboard')

    cs_keys = [k for k in request.POST if k.startswith('alloc_')]
    hours_keys = [k for k in request.POST if k.startswith('hours_')]
    cs_ids = []
    for k in cs_keys + hours_keys:
        try:
            cs_ids.append(int(k.split('_', 1)[1]))
        except (ValueError, IndexError):
            continue

    class_subject_map = {
        cs.id: cs for cs in ClassSubject.objects.filter(id__in=cs_ids, school=school, is_active=True)
    }
    teacher_map = {
        tp.id: tp for tp in TeacherProfile.objects.filter(school=school)
    }

    updated = []
    for key in cs_keys:
        try:
            cs_id = int(key.split('_', 1)[1])
        except (ValueError, IndexError):
            continue
        cs = class_subject_map.get(cs_id)
        if not cs:
            continue

        val = request.POST.get(key, '').strip()
        if not val:
            cs.allocated_teacher_id = None
            cs.teacher_id = None
        else:
            try:
                profile_id = int(val)
            except ValueError:
                continue
            profile = teacher_map.get(profile_id)
            if not profile:
                continue
            cs.allocated_teacher_id = profile.id
            cs.teacher_id = profile.user_id
        updated.append(cs)

    if updated:
        ClassSubject.objects.bulk_update(updated, ['allocated_teacher', 'teacher'], batch_size=100)

    # Weekly lesson hours ("Ҳафтада соат"). Empty input clears the explicit
    # override (the curriculum recommendation applies again); non-numeric or
    # out-of-range values are rejected without touching the row.
    hours_updated = []
    invalid_hours = []
    for key in hours_keys:
        try:
            cs_id = int(key.split('_', 1)[1])
        except (ValueError, IndexError):
            continue
        cs = class_subject_map.get(cs_id)
        if not cs:
            continue
        raw = request.POST.get(key, '').strip()
        if raw == '':
            new_hours = None
        else:
            try:
                new_hours = int(raw)
            except ValueError:
                invalid_hours.append(cs)
                continue
            if not 1 <= new_hours <= MAX_WEEKLY_HOURS:
                invalid_hours.append(cs)
                continue
        if cs.hours_per_week != new_hours:
            cs.hours_per_week = new_hours
            hours_updated.append(cs)

    if hours_updated:
        ClassSubject.objects.bulk_update(hours_updated, ['hours_per_week'], batch_size=100)

    if invalid_hours:
        messages.warning(
            request,
            'Соати нодуруст рад шуд (аз 1 то %d): %s' % (
                MAX_WEEKLY_HOURS,
                ', '.join(f'{cs.class_name} {cs.subject}' for cs in invalid_hours),
            )
        )
    messages.success(request, 'Тақсимоти дарсҳо ба омӯзгорон бо муваффақият сабт шуд.')
    if request.user.is_superuser and school:
        return redirect(f'{reverse("lesson_allocation")}?school_id={school.id}')
    return redirect('lesson_allocation')


# ---------------------------------------------------------------------------
# TeachingGroup management (Phase 2)
#
# One ClassSubject stays one curriculum row — groups only split its students
# between teachers. Admin (superuser) manages any school; a zavuch manages
# only their own school via the existing _can_manage_school helper.
# ---------------------------------------------------------------------------

_GROUP_SAVE_ERRORS = {
    'duplicate_within': 'Як хонанда ду бор дар ҳамон гурӯҳ интихоб шудааст.',
    'duplicate_membership': 'Хонанда ҳамзамон дар ҳарду гурӯҳ буда наметавонад.',
    'unknown_students': 'Хонандаи номаълум ё аз синфи дигар интихоб шудааст.',
    'unassigned': 'Ҳама хонандагон бояд дақиқан ба яке аз ду гурӯҳ ворид шаванд.',
}


def _get_manageable_class_subject(request, cs_id):
    """Return the active ClassSubject if the user may manage its groups."""
    cs = get_object_or_404(
        ClassSubject.objects.select_related('school'), pk=cs_id)
    if not cs.is_active or not is_academic_school(cs.school):
        return None
    if not _can_manage_school(request.user, cs.school):
        return None
    return cs


def _allocation_redirect(request, cs):
    base = reverse('lesson_allocation')
    class_param = urllib.parse.quote(cs.class_name)
    if request.user.is_superuser:
        return redirect(f'{base}?school_id={cs.school_id}&class={class_param}')
    return redirect(f'{base}?class={class_param}')


@login_required
def class_groups(request, cs_id):
    """Group-management page for one ClassSubject (Admin/Zavuch only)."""
    cs = _get_manageable_class_subject(request, cs_id)
    if cs is None:
        messages.error(request, 'Дастрасӣ маҳдуд аст. Гурӯҳҳоро танҳо маъмурият ё завучи ҳамон муассиса идора мекунад.')
        return redirect('dashboard')

    school = cs.school
    students = list(
        Student.objects.filter(school=school, class_name=cs.class_name)
        .order_by('full_name')
    )
    students_json = [
        {'id': s.id, 'name': s.full_name, 'gender': s.gender or ''}
        for s in students
    ]
    teachers = TeacherProfile.objects.filter(school=school).order_by('full_name')

    groups = sorted(
        (g for g in cs.groups.filter(is_active=True).prefetch_related('memberships')),
        key=lambda g: GROUP_LABELS.index(g.label) if g.label in GROUP_LABELS else len(GROUP_LABELS),
    )
    existing_json = {
        g.label: {
            'teacher_id': g.teacher_id,
            'members': [m.student_id for m in g.memberships.all()],
        }
        for g in groups if g.label in GROUP_LABELS
    }

    g1_gender, g2_gender = gender_split(students_json)
    g1_balanced, g2_balanced = balanced_split(students_json)

    # Students with no membership in an active group are invisible to group
    # teachers — surface the count so the Admin/Zavuch can fix it here.
    member_ids = {
        sid
        for entry in existing_json.values()
        for sid in entry['members']
    }
    unassigned_count = sum(1 for s in students if s.id not in member_ids)

    return render(request, 'school/class_groups.html', {
        'cs': cs,
        'school': school,
        'teachers': teachers,
        'students_json': students_json,
        'existing_json': existing_json,
        'proposals_json': {
            'gender': {'g1': g1_gender, 'g2': g2_gender},
            'balanced': {'g1': g1_balanced, 'g2': g2_balanced},
        },
        'has_groups': bool(groups),
        'unassigned_count': unassigned_count,
        'group_labels': GROUP_LABELS,
    })


@login_required
@require_POST
def save_class_groups(request, cs_id):
    """Persist the two TeachingGroups + memberships for a ClassSubject.

    Everything is validated before the transaction and the write itself is
    atomic, so a failure can never leave a partially saved grouping.
    """
    cs = _get_manageable_class_subject(request, cs_id)
    if cs is None:
        messages.error(request, 'Дастрасӣ маҳдуд аст.')
        return redirect('dashboard')
    school = cs.school

    if request.POST.get('confirm') != '1':
        messages.error(request, 'Сабт бидуни тасдиқи корбар иҷро намешавад.')
        return redirect('class_groups', cs_id=cs.id)

    teacher_ids = [
        request.POST.get('teacher_g1', '').strip(),
        request.POST.get('teacher_g2', '').strip(),
    ]
    member_lists = [
        request.POST.getlist('members_g1'),
        request.POST.getlist('members_g2'),
    ]

    teacher_map = {
        str(tp.id): tp
        for tp in TeacherProfile.objects.filter(school=school)
    }
    teachers = []
    for i, tid in enumerate(teacher_ids):
        tp = teacher_map.get(tid)
        if tp is None:
            messages.error(
                request,
                f'Омӯзгори {GROUP_LABELS[i]} интихоб нашудааст ё ба ин муассиса тааллуқ надорад.'
            )
            return redirect('class_groups', cs_id=cs.id)
        teachers.append(tp)

    student_ids = set(Student.objects.filter(
        school=school, class_name=cs.class_name
    ).values_list('id', flat=True))
    errors = validate_assignment(student_ids, member_lists[0], member_lists[1])
    if errors:
        messages.error(
            request,
            'Тақсимоти хонандагон нодуруст аст: ' +
            ' '.join(_GROUP_SAVE_ERRORS[e] for e in errors)
        )
        return redirect('class_groups', cs_id=cs.id)

    # This UI manages exactly the two canonical labels. If unexpected groups
    # exist (created outside this flow), refuse rather than orphaning their
    # memberships silently.
    if cs.groups.exclude(label__in=GROUP_LABELS).exists():
        messages.error(
            request,
            'Ин фан гурӯҳҳои дигар дорад, ки аз ин саҳифа идора намешаванд. Сабт қатъ шуд.'
        )
        return redirect('class_groups', cs_id=cs.id)

    with transaction.atomic():
        group_objs = []
        for i, label in enumerate(GROUP_LABELS):
            group, _ = TeachingGroup.objects.update_or_create(
                class_subject=cs,
                label=label,
                defaults={'teacher': teachers[i], 'is_active': True},
            )
            group_objs.append(group)
        SubjectGroupMembership.objects.filter(class_subject=cs).delete()
        SubjectGroupMembership.objects.bulk_create([
            SubjectGroupMembership(
                class_subject=cs, group=group_objs[i], student_id=sid)
            for i in (0, 1) for sid in member_lists[i]
        ])

    messages.success(
        request,
        f'Гурӯҳҳои «{cs.subject}» ({cs.class_name}) бо муваффақият сабт шуданд.'
    )
    return _allocation_redirect(request, cs)


@login_required
@require_POST
def delete_class_groups(request, cs_id):
    """Delete a ClassSubject's groups — only when no recorded activity
    (lessons, and through them grades/assessments) references them."""
    cs = _get_manageable_class_subject(request, cs_id)
    if cs is None:
        messages.error(request, 'Дастрасӣ маҳдуд аст.')
        return redirect('dashboard')

    groups = list(cs.groups.all())
    if not groups:
        messages.info(request, 'Барои ин фан гурӯҳе вуҷуд надорад.')
        return _allocation_redirect(request, cs)

    if Lesson.objects.filter(group__in=groups).exists():
        messages.error(
            request,
            'Нест кардан мумкин нест: ба ин гурӯҳҳо дарсҳо/фаъолияти сабтшуда вобастаанд. '
            'Несткунӣ таърихи журналро вайрон мекунад.'
        )
        return redirect('class_groups', cs_id=cs.id)

    with transaction.atomic():
        TeachingGroup.objects.filter(pk__in=[g.pk for g in groups]).delete()

    messages.success(
        request,
        f'Гурӯҳҳои «{cs.subject}» ({cs.class_name}) нест карда шуданд.'
    )
    return _allocation_redirect(request, cs)


@login_required
@require_POST
def deactivate_subject(request):
    if not request.user.is_superuser:
        messages.error(request, 'Танҳо superuser метавонад фанро хориҷ кунад.')
        return redirect('lesson_allocation')
    cs_id = request.POST.get('cs_id')
    if not cs_id:
        return redirect('lesson_allocation')
    try:
        cs = ClassSubject.objects.get(pk=int(cs_id))
    except (ValueError, ClassSubject.DoesNotExist):
        messages.error(request, 'Фан ёфт нашуд.')
        return redirect('lesson_allocation')
    if not is_academic_school(cs.school):
        messages.error(request, 'Фан танҳо дар муассисаҳои таҳсилоти умумӣ идора мешавад.')
        return redirect('lesson_allocation')
    cs.is_active = False
    cs.save()
    messages.success(request, f'Фани «{cs.subject}» барои {cs.class_name} хориҷ шуд.')
    return redirect(f'{reverse("lesson_allocation")}?school_id={cs.school_id}')


@login_required
@require_POST
def request_deactivation(request):
    if not _is_zavuch(request.user, get_user_role(request.user)):
        messages.error(request, 'Танҳо завучҳо метавонанд дархости хориҷкунӣ ирсол кунанд.')
        return redirect('lesson_allocation')
    cs_id = request.POST.get('cs_id')
    if not cs_id:
        return redirect('lesson_allocation')
    try:
        cs = ClassSubject.objects.get(pk=int(cs_id))
    except (ValueError, ClassSubject.DoesNotExist):
        messages.error(request, 'Фан ёфт нашуд.')
        return redirect('lesson_allocation')
    school = get_user_school(request.user)
    if not school or not is_academic_school(school) or not has_school_access(request.user, school) or cs.school != school:
        messages.error(request, 'Шумо метавонед танҳо барои муассисаи худ дархост диҳед.')
        return redirect('lesson_allocation')
    if SubjectDeactivationRequest.objects.filter(class_subject=cs, status__in=['pending', 'approved']).exists():
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.headers.get('Accept', '').startswith('application/json'):
            return JsonResponse({'status': 'exists', 'message': 'Дархости мутаносиб аллакай вуҷуд дорад'})
        messages.warning(request, 'Дархости мутаносиб аллакай вуҷуд дорад.')
        return redirect(f'{reverse("lesson_allocation")}?class={cs.class_name}')
    SubjectDeactivationRequest.objects.get_or_create(
        class_subject=cs,
        requested_by=request.user,
        status='pending',
    )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.headers.get('Accept', '').startswith('application/json'):
        return JsonResponse({'status': 'ok', 'message': 'Дархост ирсол шуд'})
    messages.success(request, f'Дархости хориҷкунии «{cs.subject}» барои {cs.class_name} ирсол шуд.')
    return redirect(f'{reverse("lesson_allocation")}?class={cs.class_name}')


@user_passes_test(lambda u: u.is_superuser)
def deactivation_requests(request):
    pending = SubjectDeactivationRequest.objects.filter(
        status='pending'
    ).select_related('class_subject', 'class_subject__school', 'requested_by').order_by('-requested_at')
    return render(request, 'portal/deactivation_requests.html', {
        'pending_requests': pending,
    })


@user_passes_test(lambda u: u.is_superuser)
@require_POST
def review_deactivation_request(request):
    req_id = request.POST.get('request_id')
    action = request.POST.get('action')
    if not req_id or action not in ('approve', 'reject'):
        return redirect('deactivation_requests')
    try:
        req = SubjectDeactivationRequest.objects.get(pk=int(req_id))
    except (ValueError, SubjectDeactivationRequest.DoesNotExist):
        return redirect('deactivation_requests')
    now = timezone.now()
    cs = req.class_subject
    if action == 'approve':
        cs.is_active = False
        cs.teacher = None
        cs.allocated_teacher = None
        cs.save()
        # Approve all other pending duplicates for the same subject/class cleanly.
        SubjectDeactivationRequest.objects.filter(class_subject=cs, status='pending').update(
            status='approved', reviewed_by=request.user, reviewed_at=now
        )
        req.refresh_from_db()
        messages.success(
            request,
            f'Фани «{cs.subject}» барои {cs.class_name} дар {cs.school.name} тасдиқ ва хориҷ шуд.'
        )
    else:
        cs.is_active = True
        cs.save()
        req.status = 'rejected'
        messages.success(request, 'Дархост рад карда шуд ва фан фаъол монд.')
    req.reviewed_by = request.user
    req.reviewed_at = now
    req.save()
    return redirect('deactivation_requests')


def _school_sort_key(school):
    """Standard sort order used for the monitoring dashboard."""
    name_lower = (school.name or '').lower()
    type_lower = (school.type or '').lower()

    if 'идор' in type_lower:
        group_priority = 4
    elif 'томактаб' in type_lower:
        group_priority = 3
    elif 'лит' in type_lower or 'лиц' in type_lower:
        group_priority = 2
    elif 'гимн' in name_lower:
        group_priority = 1
        return (group_priority, 0, school.name)
    else:
        group_priority = 1

    nums = re.findall(r'\d+', school.name)
    num = int(nums[0]) if nums else 999999
    return (group_priority, num, school.name)


def _get_monitoring_stats():
    """Return the same statistics list used by the dashboard and the Excel export."""
    schools = sorted(
        [s for s in School.objects.all() if is_academic_school(s)],
        key=_school_sort_key
    )
    stats = []
    for school in schools:
        school_num = get_school_number(school)
        zavuch_username = f'zavuch_{school_num}'
        zavuch_user = User.objects.filter(username=zavuch_username).first()
        logged_in = bool(zavuch_user and zavuch_user.last_login)
        stats.append({
            'school': school,
            'zavuch_username': zavuch_username,
            'zavuch_user': zavuch_user,
            'logged_in': logged_in,
            'class_count': Student.objects.filter(school=school).values('class_name').distinct().count(),
            'teacher_count': Teacher.objects.filter(school=school).count(),
            'student_count': Student.objects.filter(school=school).count(),
        })
    return stats


@login_required
def monitoring_dashboard(request):
    """Monitoring dashboard for superusers, staff, and school Zavuchs."""
    role = get_user_role(request.user)
    is_zavuch = (role and role.lower() == 'zavuch') or request.user.username.lower().startswith('zavuch_')
    if not (request.user.is_superuser or request.user.is_staff or is_zavuch):
        return redirect('dashboard')
    stats = _get_monitoring_stats()
    stats = [
        s for s in stats
        if s['school'].type != 'Идораи маориф'
        and 'идораи маориф' not in (s['school'].name or '').lower()
        and s['zavuch_username'] != 'zavuch_0'
    ]
    return render(request, 'portal/monitoring_dashboard.html', {'stats': stats})


@login_required
def school_readiness_rating(request):
    """Unified district leaderboard for all academic schools (2 tabs).

    Tab 1 — lesson-allocation readiness:
        percentage = assigned active ClassSubjects / total active ClassSubjects.
    Tab 2 — grading activity: total Grade rows and grades-per-student.

    Visible to superusers, staff (education officials) and school Zavuchs.
    """
    role = get_user_role(request.user)
    is_zavuch = (role and role.lower() == 'zavuch') or request.user.username.lower().startswith('zavuch_')
    if not (request.user.is_superuser or request.user.is_staff or is_zavuch):
        return redirect('dashboard')

    schools = [s for s in School.objects.all() if is_academic_school(s)]
    school_ids = [s.id for s in schools]

    totals = dict(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .values_list('school_id').annotate(n=Count('id'))
    )
    assigned = dict(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .filter(Q(teacher__isnull=False) | Q(allocated_teacher__isnull=False))
        .values_list('school_id').annotate(n=Count('id'))
    )

    data = []
    for school in schools:
        total = totals.get(school.id, 0)
        done = assigned.get(school.id, 0)
        pct = round(done / total * 100, 1) if total else 0.0
        zavuch_user = User.objects.filter(username=f'zavuch_{get_school_number(school)}').first()
        data.append({
            'school': school,
            'total': total,
            'assigned': done,
            'percentage': pct,
            'zavuch_username': f'zavuch_{get_school_number(school)}',
            'zavuch_last_login': zavuch_user.last_login if zavuch_user else None,
        })
    data.sort(key=lambda x: x['percentage'], reverse=True)

    # Dense ranking so tied percentages share the same rank
    rank = 0
    prev_pct = None
    for item in data:
        if item['percentage'] != prev_pct:
            rank += 1
            prev_pct = item['percentage']
        item['rank'] = rank

    # Tab 2: live district grading-activity leaderboard.
    # "Active students" and norm expectations exclude non-graded class levels ('0' and '1').
    NON_GRADED_RE = r'^(0|1)(-|$|[^0-9])'
    user_school = get_user_school(request.user)
    student_counts = dict(
        Student.objects.filter(school_id__in=school_ids)
        .exclude(class_name__regex=NON_GRADED_RE)
        .values_list('school_id').annotate(n=Count('id'))
    )
    grade_counts = dict(
        Grade.objects.filter(student__school_id__in=school_ids)
        .values_list('student__school_id').annotate(n=Count('id'))
    )
    class_student_counts = {
        (r['school_id'], r['class_name']): r['n']
        for r in Student.objects.filter(school_id__in=school_ids)
        .exclude(class_name__regex=NON_GRADED_RE)
        .values('school_id', 'class_name').annotate(n=Count('id'))
    }
    pair_grade_counts = {
        (r['student__school_id'], r['student__class_name'], r['subject']): r['n']
        for r in Grade.objects.filter(student__school_id__in=school_ids)
        .values('student__school_id', 'student__class_name', 'subject')
        .annotate(n=Count('id'))
    }
    # Use the exact same school-GPA formula as the academic leaderboard
    # (calculate_school_rankings) so all leaderboards show identical values.
    gpa_map = {
        item['school'].id: item['gpa']
        for item in calculate_school_rankings()
    }
    class_subjects = list(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .exclude(class_name__regex=NON_GRADED_RE)
        .select_related('allocated_teacher', 'teacher')
    )
    school_cs = defaultdict(list)
    for cs in class_subjects:
        school_cs[cs.school_id].append(cs)
    profiles_by_school = defaultdict(dict)
    for tp in TeacherProfile.objects.filter(school_id__in=school_ids):
        profiles_by_school[tp.school_id][tp.user_id] = tp

    def class_sort(cn):
        return (class_numeric_part(cn) or 0, cn)

    activity = []
    for school in schools:
        students_n = student_counts.get(school.id, 0)
        grades_n = grade_counts.get(school.id, 0)
        user_to_profile = profiles_by_school[school.id]

        expected_min = 0
        norm_grades_done = 0
        teacher_pairs = defaultdict(list)
        for cs in school_cs.get(school.id, []):
            n_students = class_student_counts.get((school.id, cs.class_name), 0)
            # Un-graded subjects (e.g. СОАТИ ТАРБИЯВӢ) must not inflate the norm.
            min_norm = quarter_min_norm(cs.class_name, cs.subject, cs)
            if min_norm:
                expected_min += min_norm * n_students
                norm_grades_done += pair_grade_counts.get((school.id, cs.class_name, cs.subject), 0)
            profile = cs.allocated_teacher or user_to_profile.get(cs.teacher_id)
            if profile:
                teacher_pairs[profile].append(cs)

        teachers = []
        for profile, pairs in teacher_pairs.items():
            subj_classes = defaultdict(list)
            hours = 0
            min_grades = 0
            grades_done = 0
            norm_done = 0
            for cs in pairs:
                subj_classes[cs.subject].append(cs.class_name)
                hours += weekly_hours(cs.class_name, cs.subject, cs)
                entered = pair_grade_counts.get((school.id, cs.class_name, cs.subject), 0)
                grades_done += entered
                # Exclude un-graded subjects (norm == 0) from required minimum.
                min_norm = quarter_min_norm(cs.class_name, cs.subject, cs)
                if min_norm:
                    min_grades += min_norm * class_student_counts.get((school.id, cs.class_name), 0)
                    norm_done += entered
            fulfillment = round(norm_done / min_grades * 100, 1) if min_grades else 0.0
            teachers.append({
                'name': profile.full_name,
                'subjects': [
                    f"{subj}: {', '.join(sorted(set(cls), key=class_sort))}"
                    for subj, cls in sorted(subj_classes.items())
                ],
                'hours': hours,
                'min_grades': min_grades,
                'grades_done': grades_done,
                'fulfillment': fulfillment,
            })
        teachers.sort(key=lambda t: (-t['fulfillment'], -t['grades_done'], t['name']))

        activity.append({
            'school': school,
            'students': students_n,
            'total_grades': grades_n,
            'fulfillment': round(norm_grades_done / expected_min * 100, 1) if expected_min else 0.0,
            'gpa': gpa_map.get(school.id, 0.0),
            'teachers': teachers,
            'is_mine': bool(user_school and user_school.id == school.id),
        })
    activity.sort(key=lambda x: (-x['fulfillment'], -x['total_grades']))

    # Dense ranking so tied fulfillment percentages share the same rank
    rank = 0
    prev_fulfillment = None
    for item in activity:
        if item['fulfillment'] != prev_fulfillment:
            rank += 1
            prev_fulfillment = item['fulfillment']
        item['rank'] = rank

    return render(request, 'portal/school_readiness_rating.html', {
        'stats': data,
        'activity': activity,
        'min_norm_bands': MIN_GRADES_BANDS,
    })


@login_required
def school_documents(request):
    """Ҳуҷҷатнигорӣ — documents hub for superusers, staff and school Zavuchs.

    First document: weekly lesson-hours distribution per teacher, built from
    active ClassSubject rows (allocated_teacher preferred, teacher as fallback).
    Per-allocation ClassSubject.hours_per_week values saved in "Тақсимоти
    дарсҳо" win; rows without an explicit value fall back to
    settings.TJC_SUBJECT_HOURS / TJC_SUBJECT_HOURS_DEFAULT.
    """
    role = get_user_role(request.user)
    is_zavuch = (role and role.lower() == 'zavuch') or request.user.username.lower().startswith('zavuch_')
    if not (request.user.is_superuser or request.user.is_staff or is_zavuch):
        return redirect('dashboard')

    school = get_user_school(request.user)
    workload = []
    if school:
        class_subjects = ClassSubject.objects.filter(
            school=school, is_active=True
        ).select_related('allocated_teacher', 'teacher').order_by('class_name', 'subject')
        user_to_profile = {
            tp.user_id: tp for tp in TeacherProfile.objects.filter(school=school)
        }

        hours_map = getattr(settings, 'TJC_SUBJECT_HOURS', {})
        default_hours = getattr(settings, 'TJC_SUBJECT_HOURS_DEFAULT', 2)
        # Explicit weekly hours confirmed in "Тақсимоти дарсҳо" are the
        # authoritative per school + class + subject value.
        explicit_hours = {
            (cs.subject, cs.class_name): cs.hours_per_week
            for cs in class_subjects
            if cs.hours_per_week is not None
        }

        # teacher -> {subject: [class_names]}
        grouped = {}
        for cs in class_subjects:
            profile = cs.allocated_teacher or user_to_profile.get(cs.teacher_id)
            if not profile:
                continue
            subj_map = grouped.setdefault(profile, {})
            subj_map.setdefault(cs.subject, []).append(cs.class_name)

        def class_sort(cn):
            return (class_numeric_part(cn) or 0, cn)

        for profile, subj_map in grouped.items():
            items = []
            academic_hours = 0
            homeroom_hours = 0
            for subject, classes in sorted(subj_map.items(), key=lambda kv: kv[0]):
                classes_sorted = sorted(set(classes), key=class_sort)
                items.append({'subject': subject, 'classes': classes_sorted})
                norm = normalize_subject(subject)
                overrides = [explicit_hours.get((subject, cn)) for cn in classes_sorted]
                if any(h is not None for h in overrides):
                    # Per-allocation resolution: saved hours where set, the
                    # existing map/default recommendation otherwise.
                    rate = hours_map.get(norm, default_hours)
                    hours = sum(h if h is not None else rate for h in overrides)
                elif norm in hours_map:
                    hours = hours_map[norm] * len(classes_sorted)
                else:
                    hours = default_hours
                if norm in UNGRADED_SUBJECTS:
                    homeroom_hours += hours
                else:
                    academic_hours += hours
            workload.append({
                'teacher': profile,
                'items': items,
                'total_hours': academic_hours,
                'homeroom_hours': homeroom_hours,
            })
        workload.sort(key=lambda w: w['teacher'].full_name)

    return render(request, 'portal/documents_landing.html', {
        'school': school,
        'workload': workload,
    })


@login_required
def export_monitoring_excel(request):
    """Export the monitoring dashboard data as a styled Excel workbook."""
    if not request.user.is_superuser:
        return redirect('dashboard')

    stats = _get_monitoring_stats()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Назорати муассисаҳо'

    headers = [
        '№',
        'Муассиса',
        'Номи корбар',
        'Воридшавӣ',
        'Миқдори синфҳо',
        'Миқдори омӯзгорон',
        'Миқдори хонандагон',
    ]

    thin_side = Side(style='thin', color='000000')
    header_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_fill = PatternFill(start_color='B4C7E7', end_color='B4C7E7', fill_type='solid')
    header_font = Font(bold=True, color='000000')
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = header_border

    thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')

    for idx, row in enumerate(stats, start=2):
        ws.cell(row=idx, column=1, value=idx - 1).alignment = center_align
        ws.cell(row=idx, column=2, value=row['school'].name).alignment = left_align
        ws.cell(row=idx, column=3, value=row['zavuch_username']).alignment = left_align

        login_status = 'Фаъол' if row['logged_in'] else 'Ғайрифаъол'
        ws.cell(row=idx, column=4, value=login_status).alignment = center_align

        ws.cell(row=idx, column=5, value=row['class_count']).alignment = center_align
        ws.cell(row=idx, column=6, value=row['teacher_count']).alignment = center_align
        ws.cell(row=idx, column=7, value=row['student_count']).alignment = center_align

        for col in range(1, 8):
            ws.cell(row=idx, column=col).border = thin_border

    column_widths = [6, 45, 18, 16, 20, 22, 25]
    for i, width in enumerate(column_widths, 1):
        ws.column_dimensions[chr(64 + i)].width = width

    ws.row_dimensions[1].height = 30

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    today = datetime.date.today().strftime('%Y-%m-%d')
    filename = f"Гузориши_Назорати_Муассисаҳо_{today}.xlsx"
    encoded_filename = urllib.parse.quote(filename)

    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response


def google_verification(request):
    """Return the Google Search Console verification file content."""
    return HttpResponse(
        'google-site-verification: google3c6e6431fb434e83.html',
        content_type='text/html'
    )


def service_worker(request):
    """Serve the PWA service worker at root scope.

    The asset lives in portal/static/portal/sw.js, but a service worker can
    only control pages under its own URL path — serving it at /sw.js with
    the Service-Worker-Allowed header gives it the '/' scope the manifest's
    start_url needs, in both DEBUG and production.
    """
    from django.contrib.staticfiles.finders import find
    path = find('portal/sw.js')
    if not path:
        return HttpResponseNotFound('service worker not found')
    with open(path, 'rb') as f:
        response = HttpResponse(f.read(), content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response
