from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.models import Avg, Count, Sum, Q, Max, ProtectedError
from django.core.cache import cache
import calendar
import contextlib
import contextvars
import io
import json
import os
import re
import threading
import time
import urllib.parse
import datetime
from collections import defaultdict
from decimal import Decimal, InvalidOperation
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

from .models import School, Teacher, Student, Grade, QuarterGrade, QuarterLock, ClassSubject, UserProfile, TeacherProfile, TeacherTabelMonth, SubjectDeactivationRequest, Lesson, Assessment, TeachingGroup, SubjectGroupMembership, Subject, SubjectAvailability, StudentMovement, StudentExemption, CLASS_LETTERS
from .forms import LoginForm, SchoolForm, TeacherForm, StudentForm, GradeForm, ClassSubjectForm
from .utils import (
    normalize_class_name, normalize_subject, is_litsey, class_numeric_part,
    default_subjects_for_class, ensure_class_subjects, is_non_graded,
    get_school_number, is_academic_school, academic_schools,
    academic_school_ids, official_subjects,
    official_subjects_for, canonical_subject_for,
    registry_subjects_for, reactivate_class_subjects,
    reverse_approved_deactivations,
    canonical_grade_rows, canonical_grade_values,
)
from .curriculum_hours import (
    MIN_GRADES_BANDS, MAX_WEEKLY_HOURS, UNGRADED_SUBJECTS,
    weekly_hours, quarter_min_norm, disp_hours,
    group_weekly_hours, group_min_norm, is_graded_subject,
)
from .tabel import (
    BAND_KEYS, MONTH_NAMES_TJ,
    MONTHLY_BAND_FIELDS, MONTHLY_ALLOWANCE_FIELDS,
    MAX_MONTHLY_HOURS, MAX_MONEY_VALUE,
    _user_to_profile_map,
    build_tabel_workbook, merge_workload_and_monthly,
    parse_decimal, parse_month_param, teacher_workload,
    workload_snapshot,
)
from .class_report import ClassReportError, build_class_report_workbook
from .academic_calendar import is_school_day
from .exemptions import active_exemption_map, home_student_ids
from .norm_pace import ATTENDANCE_MARKS, absence_counts
from .maorif_report import (
    build_maorif_grades_district_workbook, build_maorif_grades_workbook,
    build_maorif_report_workbook, build_maorif_total_workbook)
from .zavuch_report import (
    build_zavuch_grades_workbook, build_zavuch_report_workbook)
from .tariff_rates import suggested_rate_map, tariff_data_available
from .monitoring import (
    can_access_monitoring, get_activity_summary, scope_monitoring_stats,
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
from .tajik_sort import name_key, sort_students

import logging

logger = logging.getLogger(__name__)


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
    """Map a date to its academic quarter — official calendar windows.

    Delegates to portal.academic_calendar: exact windows per academic
    year, month buckets for unlisted years, vacation gaps attributed to
    the quarter that just ended.
    """
    from .academic_calendar import get_date_quarter as _gdq
    return _gdq(date)


def _current_quarter_bounds(today):
    """Return (start, end) calendar dates of the quarter containing today.

    Uses the official quarter windows; a today that falls inside a
    vacation gap resolves to the quarter that just ended.
    """
    from .academic_calendar import quarter_windows, academic_year_for_date
    windows = quarter_windows(academic_year_for_date(today))
    prev = None
    for start, end in sorted(windows.values()):
        if start <= today <= end:
            return start, end
        if today > end:
            prev = (start, end)
        else:
            break
    return prev or windows[1]


def academic_year_start(today=None):
    """September-start year of the academic year containing `today`.

    The portal's academic calendar runs Sep..Aug: dates in Sep..Dec belong
    to the academic year that starts that September, dates in Jan..Aug to
    the one that started the previous September. Derived from the date —
    never hard-coded, so future years need no code change.
    """
    today = today or datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


def academic_year_quarter_windows(year):
    """Inclusive (start, end) dates per quarter of the Sep..Aug year.

    `year` is the September-start year from academic_year_start()
    (e.g. 2026 -> 2026-09-01..2027-08-31). Exact official windows live
    in portal.academic_calendar; unlisted years get month buckets.
    """
    from .academic_calendar import quarter_windows
    return quarter_windows(year)


def academic_year_bounds(year=None):
    """Inclusive (start, end) dates of one Sep..Aug academic year.

    Defaults to the academic year containing today.
    """
    if year is None:
        year = academic_year_start()
    windows = academic_year_quarter_windows(year)
    return windows[1][0], windows[4][1]


def _quarter_progress(today):
    """Elapsed fraction of today's academic quarter, in (0, 1].

    The first day of a quarter counts as elapsed (1/total), so the
    fraction is never zero and the fulfillment calculation never
    divides by zero.
    """
    start, end = _current_quarter_bounds(today)
    total = (end - start).days + 1
    elapsed = min(max((today - start).days + 1, 1), total)
    return elapsed / total


def _norm_fulfillment(done, expected, progress):
    """Time-aware norm fulfillment percentage.

    ``expected`` is the full-quarter target (quarter norm x students);
    only its elapsed share is required by now — ``done`` reaching
    ``expected * progress`` means the teacher is on pace -> 100%. The
    cap is a monitoring display limit only; it never restricts grade
    entry.
    """
    if not expected:
        return 0.0
    return min(100.0, round(done / (expected * progress) * 100, 1))


def _journal_status(done, met, total):
    """Quarter-level journal completion status — NOT time-aware.

    ``done`` is the count of current-quarter Grade rows, ``met`` the number
    of visible students who individually reached the full quarterly norm,
    ``total`` the number of visible/relevant students. Unlike
    _norm_fulfillment this never scales by elapsed time, and unlike a raw
    total it cannot be satisfied by one over-entered student: Пурра means
    EVERY relevant student is at norm. Reaching 'complete' early never
    restricts further grade entry.
    """
    if done <= 0:
        return 'empty'
    if met >= total:
        return 'complete'
    return 'partial'


def _absence_credit(slots):
    """Missed-lesson credit for one student from pre-grouped slot sets.

    Same rule as norm_pace.absence_counts(): score-less '+/-' attendance
    marks count once per (date, lesson_id); NULL-lesson marks count once
    per date not already covered by an explicit slot or a score. Returns
    0 for no slots.
    """
    if not slots:
        return 0
    open_slots = slots['e'] - slots['s']
    covered = {d for d, _ in open_slots} | {d for d, _ in slots['s']}
    return len(open_slots) + len(slots['n'] - covered)


def _journal_norm_status(class_name, subject, class_subject, students, today,
                         exempt_ids=None, group=None):
    """Journal status payload for one School + Class + Subject context.

    Counts only ACADEMIC rows (score__isnull=False — the same pool every
    academic consumer uses) for the real-grade tallies. Attendance
    '+'/'-' rows ("Иштирок накардан дар дарс") are pulled by the same
    single query into a second-degree credit pool: they count toward
    the norm check only, never become grades. A row that carries a real
    score alongside an attendance mark still counts once — as presence.
    Completion is scoped to the students visible in this journal (a regular
    teacher's own group on grouped subjects, else the class): Пурра only
    when EVERY student reaches the norm this quarter. One grouped
    aggregate query — no per-student work.

    ``exempt_ids`` removes students from the norm denominator only: an
    exempt student (дар давлати дигар таҳсил мекунад) keeps their marks
    counted in ``done`` but is never required to reach the norm.

    ``group`` (the regular teacher's TeachingGroup on this CS) shifts the
    norm to that group's own weekly hours — used by таҳсил дар хона
    journals whose explicit «Соат дар як ҳафта» differs from the class's.
    """
    q_start, q_end = _current_quarter_bounds(today)
    norm = (
        group_min_norm(group, class_subject)
        if group is not None and class_subject is not None
        else quarter_min_norm(class_name, subject, class_subject)
    )
    student_ids = [s.id for s in students]
    exempt = set(exempt_ids or ()) & set(student_ids)
    # One quarter-scoped query feeds both pools: scored rows count real
    # grades; attendance rows feed the missed-lesson credit map.
    quarter_rows = list(Grade.objects.filter(
        student_id__in=student_ids,
        subject=subject,
        date__range=(q_start, q_end),
    ).filter(Q(score__isnull=False) | Q(attendance__in=ATTENDANCE_MARKS)))
    per_student = defaultdict(int)
    for g in canonical_grade_rows(g for g in quarter_rows
                                  if g.score is not None):
        per_student[g.student_id] += 1
    absences = absence_counts(quarter_rows)
    done = sum(per_student.values())
    met = sum(1 for sid in student_ids
              if sid not in exempt and per_student.get(sid, 0) > 0
              and per_student[sid] + absences.get(sid, 0) >= norm)
    graded = len(student_ids) - len(exempt)
    required = norm * graded
    # Aggregate credit: each student's missed lessons top their real
    # count up to the norm — never beyond it and never for grade-less
    # students, so every student enters the total exactly once and the
    # fulfillment percentage cannot be inflated. The student total is
    # additionally capped AT the norm: marks beyond it can never cover
    # a classmate who has none (Иҷрои меъёр counts ungraded students).
    done_eff = sum(
        min(actual + min(absences.get(sid, 0), max(0, norm - actual)),
            norm)
        for sid, actual in per_student.items() if actual > 0)
    return {
        'done': done,
        'required': required,
        'norm': norm,
        'students': len(student_ids),
        'students_met': met,
        'students_exempt': len(exempt),
        'status': _journal_status(done, met, graded),
        'fulfillment': _norm_fulfillment(done_eff, required, _quarter_progress(today)),
        # The same aggregated counts the pace monitor (norm_pace.py)
        # reuses — exposing them here keeps the indicator math free of
        # any extra SQL.
        'per_student': dict(per_student),
        'absences': absences,
    }


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


def _split_quarter_grades(grades, quarter, year=None):
    """Split one student's Grade rows for a subject into quarter pools.

    Returns (current_scores, ajm_results). Legacy rows (assessment NULL) and
    rows linked to a non-АҶМ assessment count as current grades; a row's
    quarter is its Assessment.quarter when linked, otherwise the existing
    get_date_quarter of grade.date — identical for all legacy data. Rows
    linked to an АҶМ assessment are grouped by assessment_id so each
    assessment contributes exactly one result (the mean of its row results;
    normally a single row).

    `year` (a September-start academic year from academic_year_start())
    restricts the pool to rows dated inside that Sep..Aug window: a
    prior-year row can never leak into this year's quarter just because
    its month buckets the same way. The Grade row's own date decides —
    the same rule the year-scoped audit applies — while
    Assessment.quarter stays authoritative for the quarter bucket.
    """
    current_scores = []
    ajm_groups = {}
    for g in canonical_grade_rows(grades):
        g_year, gq = _grade_row_year_quarter(g)
        if gq != quarter:
            continue
        if year is not None and g_year != year:
            continue
        result = grade_component_result(g)
        if result is None:
            continue
        a = g.assessment
        if a is not None and a.is_ajm:
            ajm_groups.setdefault(a.id, []).append(result)
        else:
            current_scores.append(result)
    ajm_results = [sum(rows) / len(rows) for rows in ajm_groups.values()]
    return current_scores, ajm_results


def _grade_row_year_quarter(grade):
    """(academic_year, quarter) a source Grade row belongs to.

    Assessment-linked rows follow the assessment's own date and declared
    quarter (both authoritative); unlinked rows follow the Grade row's
    date. Pure attribution — no score evaluation — so failure paths where
    grade_component_result itself raised can still use it.
    """
    a = grade.assessment
    if a is not None:
        return academic_year_start(a.date), a.quarter
    return academic_year_start(grade.date), get_date_quarter(grade.date)


def _quarter_contributing_filter(quarter, year):
    """Q() keeping only Grade rows _split_quarter_grades can attribute to
    (quarter, year) — the SQL form of _grade_row_year_quarter:

    - assessment-linked rows follow the assessment: its declared quarter
      plus the Sep..Aug year of the assessment's own date;
    - unlinked rows follow the grade's date: the span
      [q_start, next_q_start) covers the official window AND the
      vacation gap get_date_quarter attributes to `quarter` (late
      write-ups during a break belong to the quarter that just ended).

    Returns None when quarter/year cannot be windowed — callers then run
    the unfiltered scan, identical to the previous behavior. The scan it
    narrows used to pull EVERY scored Ҷорӣ row for the student/subject
    set and bucket in Python; the filter cuts that to one quarter's
    window — the quarter pools are what the per-save recalc and the
    reports iterate over.
    """
    if year is None:
        return None
    windows = academic_year_quarter_windows(year)
    if quarter not in windows:
        return None
    q_start = windows[quarter][0]
    q_next = (windows[quarter + 1][0] if quarter + 1 in windows
              else datetime.date(year + 1, 9, 1))
    ay_start = datetime.date(year, 9, 1)
    ay_end = datetime.date(year + 1, 8, 31)
    return (
        Q(assessment__isnull=True, date__gte=q_start, date__lt=q_next)
        | Q(assessment__quarter=quarter,
            assessment__date__gte=ay_start, assessment__date__lte=ay_end)
    )


def _year_contributing_filter(year):
    """Q() keeping only Grade rows _split_quarter_grades can attribute to
    ANY quarter of academic `year` — the union of the per-quarter
    filters for live projections that derive all four quarters at once:

    - unlinked rows: grade.date inside the Sep..Aug bounds of `year`;
    - linked rows: assessment quarter 1-4 AND the assessment's date
      inside those same bounds (the year of a linked row is the
      academic year of its assessment's date).

    Rows outside can never contribute to quarters 1-4 of `year`, so the
    scan skips prior-year history entirely.
    """
    if year is None:
        return None
    ay_start = datetime.date(year, 9, 1)
    ay_end = datetime.date(year + 1, 8, 31)
    return (
        Q(assessment__isnull=True, date__gte=ay_start, date__lte=ay_end)
        | Q(assessment__quarter__in=(1, 2, 3, 4),
            assessment__date__gte=ay_start, assessment__date__lte=ay_end)
    )


def collect_quarter_inputs(student, subject, quarter, year=None):
    """Return (current_scores, ajm_results) for one student+subject+quarter.

    Uses the same scored-row pool as the existing daily calculations
    (period='Холҳои ҷорӣ (Онлайн)'), restricted to the given academic year
    (Sep..Aug; default: the year containing today) — a row of a different
    year never feeds this quarter just because its month buckets the same
    way.
    """
    eff_year = year if year is not None else academic_year_start()
    grades = Grade.objects.filter(
        student=student,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        score__isnull=False,
    ).select_related('assessment')
    contributing = _quarter_contributing_filter(quarter, eff_year)
    if contributing is not None:
        grades = grades.filter(contributing)
    return _split_quarter_grades(grades, quarter, eff_year)


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


def recalculate_quarter_grade(student, subject, quarter, year=None):
    """Persist the automatically-derived QuarterGrade for one year+quarter.

    The single writer for quarter grades 1-4: every source Grade
    add/edit/delete funnels here via signals (and the manual Ҳисобкунӣ
    button). `year` is the September-start academic year of the cell being
    refreshed (default: the year containing today); the stored row is
    keyed on it, so a prior-year result can never be overwritten or read
    back as this year's value. A lock is only honored within its own
    year — locking Q1 of last year does not freeze Q1 of this year.
    With no derivation for the year the year-keyed row is removed so the
    live projection takes over; legacy NULL-year and other-year rows are
    out of key range and stay untouched.
    """
    if quarter not in (1, 2, 3, 4):
        return
    if year is None:
        year = academic_year_start()
    if is_quarter_locked(
            student.school, student.class_name, subject, quarter, year):
        return
    grades = Grade.objects.filter(
        student=student,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        score__isnull=False,
    ).select_related('assessment')
    contributing = _quarter_contributing_filter(quarter, year)
    if contributing is not None:
        grades = grades.filter(contributing)
    grades = list(grades)
    value = calc_quarter_value(*_split_quarter_grades(grades, quarter, year))
    if value is None:
        QuarterGrade.objects.filter(
            student=student, class_name=student.class_name,
            subject=subject, quarter=quarter, academic_year=year,
        ).delete()
        return
    new_grade = std_round(value)
    # get_or_create + conditional save instead of update_or_create: most
    # source edits (attendance, behavior, stickers, scores that do not move
    # the rounded average) leave the stored value unchanged, and skipping
    # the redundant UPDATE removes a write-lock round-trip per save — the
    # biggest avoidable write in the per-cell signal path. get_or_create
    # keeps the same concurrent-insert retry as update_or_create.
    obj, _ = QuarterGrade.objects.get_or_create(
        student=student,
        class_name=student.class_name,
        subject=subject,
        quarter=quarter,
        academic_year=year,
        defaults={'grade': new_grade},
    )
    if obj.grade != new_grade:
        obj.grade = new_grade
        obj.save(update_fields=['grade'])


def recalculate_quarter_grades_bulk(rows):
    """Bulk counterpart of recalculate_quarter_grade.

    `rows` is an iterable of (student_id, subject, quarter[, year]) keys —
    typically the set collected by defer_quarter_recalc() while a batch
    endpoint wrote source grades. `year` is the September-start academic
    year of the cell (default: the year containing today); every stored
    row is keyed on it so cells of different years can never collide.
    Lock checks, the source pool, and the existing QuarterGrade rows are
    resolved with a handful of shared queries instead of a full pass per
    student, and the writes go through bulk operations.
    """
    current_year = academic_year_start()
    keys = set()
    for row in rows:
        sid, subj, q = row[0], row[1], row[2]
        year = row[3] if len(row) > 3 and row[3] is not None else current_year
        if sid and q in (1, 2, 3, 4):
            keys.add((sid, subj, q, year))
    if not keys:
        return
    students = {
        s.pk: s for s in Student.objects.filter(
            pk__in={k[0] for k in keys}).select_related('school')
    }
    if not students:
        return
    subjects = {k[1] for k in keys}
    # One query per distinct subject — a journal batch edits a single
    # subject, so this is normally exactly one fetch. Rows are narrowed
    # to the (quarter, year) windows the keys actually need instead of
    # pulling every scored row of the year (membership is still decided
    # in Python below — the filter only skips rows no key can use).
    pools = {}
    for subj in subjects:
        qs = Grade.objects.filter(
            student_id__in=students.keys(), subject=subj,
            period='Холҳои ҷорӣ (Онлайн)',
            score__isnull=False).select_related('assessment')
        contributing = None
        for q, year in {(k[2], k[3]) for k in keys if k[1] == subj}:
            f = _quarter_contributing_filter(q, year)
            if f is None:
                contributing = None
                break
            contributing = f if contributing is None else contributing | f
        if contributing is not None:
            qs = qs.filter(contributing)
        for g in qs:
            pools.setdefault(
                (g.student_id, normalize_subject(subj)), []).append(g)
    # Every distinct lock key resolved in one query, then matched exactly.
    lock_keys = {
        (st.school_id, normalize_class_name(st.class_name),
         normalize_subject(subj), q, year)
        for sid, subj, q, year in keys for st in [students.get(sid)]
        if st is not None
    }
    lock_years = {k[4] for k in lock_keys}
    locked = {
        (row[0], normalize_class_name(row[1]),
         normalize_subject(row[2]), row[3],
         row[4] if row[4] is not None
         else (academic_year_start(timezone.localdate(row[5]))
               if row[5] else None)): True
        for row in QuarterLock.objects.filter(
            school_id__in={k[0] for k in lock_keys},
            class_name__in={k[1] for k in lock_keys},
            subject__in={k[2] for k in lock_keys},
            quarter__in={k[3] for k in lock_keys},
            locked=True,
        ).filter(_lock_year_q(lock_years)).values_list(
            'school_id', 'class_name', 'subject', 'quarter',
            'academic_year', 'locked_at')
    } if lock_keys else set()
    existing = {
        (qg.student_id, normalize_class_name(qg.class_name),
         normalize_subject(qg.subject), qg.quarter,
         qg.academic_year): qg
        for qg in QuarterGrade.objects.filter(
            student_id__in=students, subject__in=subjects,
            quarter__in={k[2] for k in keys},
            academic_year__in={k[3] for k in keys})
    }
    to_create, to_update, to_delete = [], [], []
    for sid, subj, q, year in keys:
        student = students.get(sid)
        if student is None:
            continue
        lock_key = (student.school_id, normalize_class_name(student.class_name),
                    normalize_subject(subj), q, year)
        if lock_key in locked:
            continue
        pool = pools.get((sid, normalize_subject(subj)), [])
        value = calc_quarter_value(*_split_quarter_grades(pool, q, year))
        qkey = (sid, normalize_class_name(student.class_name),
                normalize_subject(subj), q, year)
        qg = existing.get(qkey)
        if value is None:
            # No derivation for THIS year — the year-keyed row (if any) is
            # stale cache; other-year and legacy NULL rows stay untouched.
            if qg is not None:
                to_delete.append(qg.pk)
            continue
        new_grade = std_round(value)
        if qg is None:
            to_create.append(QuarterGrade(
                student=student, class_name=student.class_name,
                subject=subj, quarter=q, academic_year=year,
                grade=new_grade))
        elif qg.grade != new_grade:
            qg.grade = new_grade
            to_update.append(qg)
    if to_delete:
        QuarterGrade.objects.filter(pk__in=to_delete).delete()
    if to_create:
        try:
            QuarterGrade.objects.bulk_create(to_create)
        except IntegrityError:
            # A concurrent write won the (student, class, subject, quarter,
            # year) key — fall back to row-wise upserts for the conflicts.
            for row in to_create:
                QuarterGrade.objects.update_or_create(
                    student_id=row.student_id, class_name=row.class_name,
                    subject=row.subject, quarter=row.quarter,
                    academic_year=row.academic_year,
                    defaults={'grade': row.grade})
    if to_update:
        QuarterGrade.objects.bulk_update(to_update, ['grade'])


def drop_stale_quarter_grade(student, subject, quarter, year=None):
    """Best-effort fallback when automatic recalculation fails.

    Every display prefers a stored QuarterGrade over the live projection,
    so a row whose refresh failed would keep showing the old value —
    silently misleading. Removing it makes the established live bridge
    project the quarter from the source Grade rows instead (and flag it
    as a live value). Never called on the happy path.

    The row is keyed by academic year, so only the exact
    (student, class, subject, quarter, year) cell whose recalc failed is
    dropped — prior-year and legacy NULL-year rows are out of key range
    and can never be destroyed by a failed current-year refresh. A lock
    only protects its own year: a locked year's frozen row is kept (and
    if the lock check itself fails, the caller's exception guard leaves
    the row rather than risk deleting a frozen value).
    """
    if quarter not in (1, 2, 3, 4):
        return
    if year is None:
        year = academic_year_start()
    if is_quarter_locked(
            student.school, student.class_name, subject, quarter, year):
        return
    QuarterGrade.objects.filter(
        student=student,
        class_name=normalize_class_name(student.class_name),
        subject=normalize_subject(subject),
        quarter=quarter,
        academic_year=year,
    ).delete()


def drop_stale_quarter_grades(keys):
    """Bulk variant of drop_stale_quarter_grade for batch failure paths.

    `keys` is an iterable of (student_id, subject, quarter[, year]) —
    typically the set collected by defer_quarter_recalc() when its bulk
    recalc failed. Only the exact year-keyed unlocked rows are dropped:
    locks protect their own year only, and prior-year/legacy rows are out
    of key range.
    """
    current_year = academic_year_start()
    normed = set()
    for row in keys:
        sid, subj, q = row[0], row[1], row[2]
        year = row[3] if len(row) > 3 and row[3] is not None else current_year
        if sid and q in (1, 2, 3, 4):
            normed.add((sid, normalize_subject(subj), q, year))
    keys = normed
    if not keys:
        return
    students = {
        s.pk: s for s in Student.objects.filter(
            pk__in={k[0] for k in keys}).select_related('school')
    }
    if not students:
        return
    key_years = {k[3] for k in keys}
    locked = {
        (row[0], normalize_class_name(row[1]),
         normalize_subject(row[2]), row[3],
         row[4] if row[4] is not None
         else (academic_year_start(timezone.localdate(row[5]))
               if row[5] else None))
        for row in QuarterLock.objects.filter(
            school_id__in={st.school_id for st in students.values()},
            locked=True,
        ).filter(_lock_year_q(key_years)).values_list(
            'school_id', 'class_name', 'subject', 'quarter',
            'academic_year', 'locked_at')
    }
    drop_q = None
    for sid, subj, q, year in keys:
        st = students.get(sid)
        if st is None or (
            st.school_id, normalize_class_name(st.class_name), subj, q, year
        ) in locked:
            continue
        clause = Q(
            student_id=sid,
            class_name=normalize_class_name(st.class_name),
            subject=subj, quarter=q, academic_year=year)
        drop_q = clause if drop_q is None else drop_q | clause
    if drop_q is not None:
        QuarterGrade.objects.filter(drop_q).delete()


def _drain_deferred_quarter_recalc(deferred):
    """Bulk recalc drain for batch writes that commit without atomicity.

    daily_save and assessment_delete call recalculate_quarter_grades_bulk
    inside transaction.atomic() — a failure there rolls the whole write
    back and surfaces an error to the client, so no stale state persists.
    The non-atomic batch paths (grade_entry POST, monthly_save, the Excel
    import) have already committed their source rows when this runs; an
    unhandled failure would 500 while leaving stale stored quarters that
    every display prefers over the live projection. Log it and drop the
    affected unlocked rows instead — the live bridge then projects each
    cell from its source grades.
    """
    try:
        recalculate_quarter_grades_bulk(deferred)
    except Exception:
        logger.exception('Bulk quarter recalculation failed')
        try:
            drop_stale_quarter_grades(deferred)
        except Exception:
            logger.exception('Failed to drop stale quarter grades')


def _live_quarter_payload(student, subject, class_name):
    """Live quarter projection for one student: (qmap, live_flags).

    Mirrors the grade_entry live bridge — an official QuarterGrade always
    wins; otherwise the quarter is projected from the current Grade rows.
    Used by AJAX responses so the page can sync quarter cells immediately
    after a save or assessment deletion without reloading.
    """
    year = academic_year_start()
    official = {}
    for qg in QuarterGrade.objects.filter(
            student=student, class_name=class_name, subject=subject,
            academic_year=year):
        if qg.quarter == 0 and qg.att_grade is not None:
            official['att'] = qg.att_grade
        elif qg.grade is not None:
            official[qg.quarter] = qg.grade
    grades = list(Grade.objects.filter(
        student=student, subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        score__isnull=False).select_related('assessment')
        .filter(_year_contributing_filter(year)))
    qmap = {}
    live_flags = set()
    for q in (1, 2, 3, 4):
        if q in official:
            qmap[q] = official[q]
        else:
            current_scores, ajm_results = _split_quarter_grades(
                grades, q, year)
            value = calc_quarter_value(current_scores, ajm_results)
            if value is not None:
                qmap[q] = std_round(value)
                live_flags.add(q)
    qmap['att'] = official.get('att')
    return qmap, live_flags


def _live_quarter_payloads(infos):
    """Shared-query version of _live_quarter_payload for batch responses.

    `infos` is an iterable of {'student': Student, 'subject': str} dicts.
    Returns {student_pk: (qmap, live_flags)} — identical content to calling
    _live_quarter_payload per student, but the QuarterGrade and Grade pools
    are fetched once for the whole batch.
    """
    out = {}
    subjects = {i['subject'] for i in infos}
    students = [i['student'] for i in infos]
    if not subjects or not students:
        return out
    grade_pool = {}
    year = academic_year_start()
    for g in Grade.objects.filter(
            student__in=students, subject__in=subjects,
            period='Холҳои ҷорӣ (Онлайн)',
            score__isnull=False).select_related('assessment').filter(
            _year_contributing_filter(year)):
        grade_pool.setdefault(
            (g.student_id, normalize_subject(g.subject)), []).append(g)
    qg_rows = {}
    for qg in QuarterGrade.objects.filter(
            student__in=students, subject__in=subjects,
            academic_year=year):
        qg_rows.setdefault(
            (qg.student_id, normalize_class_name(qg.class_name),
             normalize_subject(qg.subject)), {})[qg.quarter] = qg
    for info in infos:
        st, subj = info['student'], info['subject']
        rows = qg_rows.get(
            (st.pk, normalize_class_name(st.class_name),
             normalize_subject(subj)), {})
        official = {}
        for quarter, qg in rows.items():
            if quarter == 0 and qg.att_grade is not None:
                official['att'] = qg.att_grade
            elif qg.grade is not None:
                official[quarter] = qg.grade
        pool = grade_pool.get((st.pk, normalize_subject(subj)), [])
        qmap, live_flags = {}, set()
        for q in (1, 2, 3, 4):
            if q in official:
                qmap[q] = official[q]
            else:
                current_scores, ajm_results = _split_quarter_grades(
                    pool, q, year)
                value = calc_quarter_value(current_scores, ajm_results)
                if value is not None:
                    qmap[q] = std_round(value)
                    live_flags.add(q)
        qmap['att'] = official.get('att')
        out[st.pk] = (qmap, live_flags)
    return out


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


def _journal_write_today():
    """'Today' for the future-date write cutoff.

    One seam for every journal write guard: production always returns
    the local date; tests that must exercise quarter fixtures dated
    ahead of the real clock patch this function.
    """
    return timezone.localdate()


def _resolve_daily_scope(student, subject, date, lesson_id, user=None,
                         allow_control=False):
    """Validate the target slot of a regular (non-assessment) daily write.

    Returns the resolved Lesson or None for the legacy lesson-less slot.
    Raises ValueError with a teacher-safe message on forged/mismatched IDs,
    when a control assessment already owns the slot, or when a regular
    teacher tries to use another group's lesson. With allow_control=True
    the control-slot check is skipped — the caller (the batched journal
    pipeline) resolves and writes to the slot's assessment itself.

    Future-dated writes are always refused — a lesson that has not
    happened yet cannot carry a real score/attendance mark; phantom rows
    otherwise pollute the quarter pools.
    """
    if date > _journal_write_today():
        raise ValueError('Санаи ояндада хол ё давомат гузошта намешавад.')
    # Official vacation gaps (and Sundays) hold no lessons: journal writes
    # onto them are refused at the scope level, so every caller of this
    # helper (AJAX, monthly/daily batch) shares the same rule. Late writes
    # on past SCHOOL days of the same quarter stay allowed.
    if not is_school_day(date):
        raise ValueError('Дар рӯзи таътил хол ё давомат гузошта намешавад.')
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
        if not allow_control and Assessment.objects.filter(
            class_subject=lcs,
            category='control',
            lesson=lesson,
        ).exists():
            raise ValueError('Ин дарс барои кори санҷишӣ таъйин шудааст — холи ҷорӣ дохил карда намешавад.')
        if user is not None and not _teacher_can_use_lesson(user, lesson):
            raise ValueError('Дарс ба гурӯҳи шумо тааллуқ надорад.')
        return lesson
    # Lesson-less slot: only legacy lesson-less control assessments lock it.
    if not allow_control and Assessment.objects.filter(
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


def _conduct_advice(conduct_map):
    """Short Tajik recommendation built from current per-subject conduct.

    Deterministic static text — no external calls. Names only subjects
    whose current value is at or below the existing satisfactory
    boundary (<=3); everything else stays positive.
    """
    if not conduct_map:
        return ''
    if all(v == 5 for v in conduct_map.values()):
        return ('Рафтори шумо намунавӣ аст. Ҳамин тавр идома диҳед — '
                'интизом ва эҳтиром асоси муваффақият мебошанд.')
    low = sorted(s for s, v in conduct_map.items() if v <= 3)
    if low:
        return ('Дар фанҳои ' + ', '.join(low) +
                ' ба интизом ва иштироки фаъол дар дарс диққати бештар '
                'диҳед. Кӯшиши шумо натиҷаро ҳатман беҳтар мекунад.')
    return ('Натиҷаи тарбияи шумо хуб аст. '
            'Кӯшиш кунед, ки ин сатҳро ҳамеша нигоҳ доред.')


def _grade_slot_label(grade):
    """Student-facing label for a Grade row: subject plus its slot context.

    A plain legacy row shows just the subject ('АЛГЕБРА'). Rows bound to a
    lesson or a control assessment keep that distinction visible
    ('АЛГЕБРА (дарси 2)', 'АЛГЕБРА (Назоратӣ №1)') so legitimate same-day
    slots are never collapsed into an apparent duplicate. Callers must
    select_related('lesson', 'assessment').
    """
    parts = []
    if grade.assessment_id:
        a = grade.assessment
        parts.append(
            f"Назоратӣ №{a.number}" if a is not None and a.number else 'Назоратӣ'
        )
    if grade.lesson_id:
        parts.append(f"дарси {grade.lesson.lesson_number}")
    return grade.subject + (f" ({', '.join(parts)})" if parts else '')


def _lock_year_q(years):
    """Q matching locks effective for any of the given academic years.

    A year-stamped lock matches only its own year; a legacy NULL-year lock
    is pulled too and attributed by its locked_at date in Python — a
    potentially valid lock is never silently ignored, but never applied
    to an unrelated year either.
    """
    return Q(academic_year__in=set(years)) | Q(academic_year__isnull=True)


def _locked_quarters_for_year(school, class_name, subject, year):
    """Set of quarter numbers locked for `year` in one query.

    Year-stamped locks match their own year; legacy NULL-year rows are
    attributed by their locked_at date.
    """
    return {
        q for q, ay, lat in QuarterLock.objects.filter(
            school=school,
            class_name=normalize_class_name(class_name),
            subject=normalize_subject(subject),
            locked=True,
        ).filter(_lock_year_q({year})).values_list(
            'quarter', 'academic_year', 'locked_at')
        if ay == year or (
            ay is None and lat is not None
            and academic_year_start(timezone.localdate(lat)) == year)
    }


def is_quarter_locked(school, class_name, subject, quarter, year=None):
    """Return True if the quarter is locked for that academic year.

    `year` is the September-start academic year of the quarter in
    question (default: the year containing today). A lock stamped for a
    different year does not apply. Legacy NULL-year rows match only when
    their own locked_at date falls inside the year being checked — the
    lock freezes the year it was created in, not every future year.
    """
    if quarter is None:
        return False
    if year is None:
        year = academic_year_start()
    for row in QuarterLock.objects.filter(
            school=school,
            class_name=normalize_class_name(class_name),
            subject=normalize_subject(subject),
            quarter=quarter,
            locked=True,
    ).filter(_lock_year_q({year})).values_list(
            'academic_year', 'locked_at'):
        if row[0] == year or (
                row[0] is None and row[1] is not None
                and academic_year_start(
                    timezone.localdate(row[1])) == year):
            return True
    return False


def get_or_create_unique(model, defaults=None, **lookup):
    """get_or_create that tolerates duplicate rows left by concurrent writes."""
    qs = model.objects.filter(**lookup).order_by('pk')
    rows = list(qs)
    if rows:
        return rows[0], False
    params = dict(lookup)
    params.update(defaults or {})
    return model.objects.create(**params), True


# Serializes the check-then-create sequence in get_or_create_grade within
# this process. Without it, a double-submitted save (the journal fires both
# 'change' and 'blur', and offline-queue replay can overlap a live save) can
# race: both requests see zero rows and both INSERT identical Grade rows.
# A DB unique constraint cannot express the logical key — (assessment,
# lesson) are nullable FKs, so SQLite treats NULL rows as distinct and a
# plain unique_together would still allow duplicates.
# NOTE: this lock is per-process. On a multi-worker deployment a cross-worker
# race remains possible; it is tolerated by design — the next write with the
# same key merges and removes the extra rows, and read paths dedupe
# identical rows for display.
_DAILY_GRADE_LOCK = threading.Lock()


def get_or_create_grade(defaults=None, **lookup):
    """Fetch-or-create a daily Grade row; merges and removes duplicate rows."""
    with _DAILY_GRADE_LOCK:
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


def _can_manage_subjects(user):
    """Return True if the user may open the canonical subject workflow.

    Mirrors the subject_registry permission model: superuser, district
    director / school principal, or a zavuch (deputy director). Regular
    teachers are never allowed.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or _is_zavuch(user):
        return True
    return get_user_role(user) in (settings.ROLE_DIRECTOR, settings.ROLE_PRINCIPAL)


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


def _class_exists(school, class_name):
    """A class exists when it has ClassSubject rows or students at the school."""
    return (
        ClassSubject.objects.filter(school=school, class_name=class_name).exists()
        or Student.objects.filter(school=school, class_name=class_name).exists()
    )


def _deactivate_empty_class(school, class_name):
    """Deactivate all ClassSubjects for a class that no longer has students."""
    class_name = normalize_class_name(class_name)
    if not Student.objects.filter(school=school, class_name=class_name).exists():
        ClassSubject.objects.filter(school=school, class_name=class_name).update(is_active=False)


def _move_student(school, student, new_class_name, new_full_name=None,
                  gender=None, relabel_quarter_grades=True):
    """Move a student to a new class and/or name, preserving grade history.

    Because the Student primary key is derived from school + class + full name,
    a new Student row is created and all Grade / QuarterGrade records are
    re-linked to it. The old row is then removed.

    relabel_quarter_grades controls the QuarterGrade.class_name field: same-
    grade merges relabel rows so the merged class journal shows them, while
    cross-grade promotion keeps the original class label so grade-5 quarter
    history cannot be overwritten by grade-6 update_or_create lookups.
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

    if Student.all_objects.filter(id=new_id).exclude(id=old_id).exists():
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
        quarter_update = {'student': new_student}
        if relabel_quarter_grades:
            quarter_update['class_name'] = new_class_name
            # Relabeling lands every stored row on new_class_name, so two
            # rows that differed only by their old class label (a student
            # who moved classes and kept rows from both) would collide on
            # the unique_together key and abort the whole move. Survivor
            # choice: a row already labeled with the target class wins —
            # for a locked cell it is the value frozen under the lock now
            # guarding that cell — otherwise the lowest pk survives (the
            # canonical rule). The bulk recalc below re-derives unlocked
            # cells either way. NULL-year legacy rows can never violate
            # the constraint and stay untouched.
            target_norm = normalize_class_name(new_class_name)
            seen = {}
            drop_pks = []
            for stored in QuarterGrade.objects.filter(
                    student_id=old_id).order_by('pk'):
                if stored.academic_year is None:
                    continue
                key = (stored.subject, stored.quarter,
                       stored.academic_year)
                prev = seen.get(key)
                if prev is None:
                    seen[key] = stored
                    continue
                keep_stored = (
                    normalize_class_name(stored.class_name) == target_norm
                    and normalize_class_name(prev.class_name)
                    != target_norm)
                if keep_stored:
                    drop_pks.append(prev.pk)
                    seen[key] = stored
                else:
                    drop_pks.append(stored.pk)
            if drop_pks:
                QuarterGrade.objects.filter(pk__in=drop_pks).delete()
        QuarterGrade.objects.filter(student_id=old_id).update(**quarter_update)
        StudentExemption.objects.filter(student_id=old_id).update(
            student=new_student)
        Student.all_objects.filter(id=old_id).delete()
        # QuerySet.update() bypasses the Grade signals, so the moved rows'
        # quarters are re-derived here through the same bulk pass journal
        # batches use. Each source row re-derives the (quarter, year) cell
        # it actually belongs to — a prior-year row refreshes the
        # prior-year stored record, not this year's. Relabeled stored rows
        # are refreshed or dropped in place; relabel_quarter_grades=False
        # history rows keep their old class label AND year stamp and stay
        # untouched (the new class gets its own derived row). Locked
        # quarters keep their frozen value within their own year.
        affected = set()
        for subj, date, a_quarter, a_date in Grade.objects.filter(
                student=new_student).values_list(
                    'subject', 'date', 'assessment__quarter',
                    'assessment__date'):
            if a_quarter in (1, 2, 3, 4) and a_date is not None:
                q, y = a_quarter, academic_year_start(a_date)
            else:
                q, y = get_date_quarter(date), academic_year_start(date)
            if q in (1, 2, 3, 4):
                affected.add((new_student.pk, subj, q, y))
        for subj, q, y in QuarterGrade.objects.filter(
                student=new_student).values_list(
                    'subject', 'quarter', 'academic_year'):
            # NULL-year legacy rows stay legacy; their current-year cell
            # still gets re-derived under its own key.
            affected.add((new_student.pk, subj, q,
                          y if y is not None else academic_year_start()))
        recalculate_quarter_grades_bulk(affected)

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

# Home-education (таҳсил дар хона) groups sit beside the class subject
# without narrowing it: the canonical two-group UI (Гурӯҳи 1/2) switches a
# ClassSubject into group-scoped mode, a home group does NOT — the regular
# class teacher keeps full access minus the home students.
HOME_GROUP_LABEL = 'Таҳсил дар хона'


def _canonical_group_q():
    """Active TeachingGroups that drive group-scoped mode (excludes home)."""
    return Q(is_active=True, is_home=False)


def _teacher_can_access_cs(user, cs):
    """True if a regular teacher may open this ClassSubject's journal.

    Grouped CS -> only its active group teachers; ungrouped CS -> the legacy
    teacher/allocated_teacher assignment. A home-education group grants its
    own teacher access either way. Not for privileged roles.

    Uses the prefetched ``cs.groups`` cache when callers provide it (the
    permission sweep in class_detail / can_edit_grade_journal) — a Python
    scan over a handful of groups instead of two EXISTS queries per CS.
    """
    groups = [g for g in cs.groups.all() if g.is_active]
    if not groups:
        # Ungrouped CS: no profile lookup needed at all.
        return cs.teacher_id == user.id or (
            cs.allocated_teacher_id is not None
            and cs.allocated_teacher.user_id == user.id
        )
    try:
        profile_id = user.teacherprofile.id
    except TeacherProfile.DoesNotExist:
        profile_id = None
    if profile_id is not None and any(
            g.is_home and g.teacher_id == profile_id for g in groups):
        return True
    canonical = [g for g in groups if not g.is_home]
    if canonical:
        return profile_id is not None and any(
            g.teacher_id == profile_id for g in canonical)
    return cs.teacher_id == user.id or (
        cs.allocated_teacher_id is not None
        and cs.allocated_teacher.user_id == user.id
    )


def teacher_group_for(user, cs):
    """The regular teacher's active TeachingGroup on this CS, else None.

    A teacher owning both a canonical and a таҳсил дар хона group on the
    same CS resolves to the canonical one (is_home=False sorts first) —
    deterministic, and the class journal keeps its primary meaning.
    """
    if not _is_regular_teacher(user):
        return None
    return cs.groups.filter(
        is_active=True, teacher__user=user
    ).order_by('is_home', 'pk').first()


def _class_subject_for(school, class_name, subject):
    """The active ClassSubject row for a school/class/subject triple."""
    return ClassSubject.objects.filter(
        school=school,
        class_name=normalize_class_name(class_name),
        subject=normalize_subject(subject),
        is_active=True,
    ).order_by('pk').first()


# Sentinel: caller did not precompute the group, resolve it inside.
_GROUP_UNSET = object()


def _exclude_home_students(students):
    """Drop students with an active «Дар хона таҳсил мегирад» exemption —
    they appear only in the home-education journal, never in the class
    journal roster."""
    return students.exclude(
        pk__in=StudentExemption.objects.filter(
            reason='home', cancelled_at__isnull=True,
            start_date__lte=timezone.localdate(),
        ).exclude(end_date__lt=timezone.localdate()).values('student_id'))


def visible_students_for(user, cs, group=_GROUP_UNSET, has_groups=None):
    """Students visible to `user` for this ClassSubject, as a list in
    Tajik-alphabet order (sort_students honors the rollback flag).

    No active groups -> the whole class (legacy). Grouped -> all students
    for privileged roles; only own-group members for regular teachers.
    Students without membership in an active group stay invisible to
    teachers until the Admin/Zavuch assigns them in the group UI.
    Home-education students (истиснои «Дар хона таҳсил мегирад») are
    excluded from every whole-class branch — they are reached through
    their own is_home TeachingGroup only.

    `group` (a teacher_group_for result) and `has_groups` (the canonical
    active-group existence flag) may be passed by a caller that already
    resolved them for the same (user, cs) pair, avoiding duplicate queries
    in a request.
    """
    students = Student.objects.filter(
        school=cs.school, class_name=cs.class_name
    ).order_by('full_name')
    if not _is_regular_teacher(user):
        return sort_students(_exclude_home_students(students))
    if group is _GROUP_UNSET:
        group = teacher_group_for(user, cs)
    if group is None:
        # Canonical-grouped CS without an own group -> nothing; ungrouped
        # -> legacy class roster minus home-education students.
        if has_groups is None:
            has_groups = cs.groups.filter(_canonical_group_q()).exists()
        if has_groups:
            return []
        return sort_students(_exclude_home_students(students))
    return sort_students(students.filter(
        subject_group_memberships__class_subject=cs,
        subject_group_memberships__group=group,
    ))


def _student_is_home(student):
    """True when the student has an active «Дар хона таҳсил мегирад» status."""
    today = timezone.localdate()
    return student.exemptions.filter(
        reason='home', cancelled_at__isnull=True,
        start_date__lte=today).exclude(end_date__lt=today).exists()


def _teacher_can_write_student(user, student, cs):
    """Server-side membership guard for single-student writes.

    Ungrouped CS -> True (legacy), except a home-education student, whom
    only the owning home-group teacher may write. Grouped CS -> regular
    teachers may only write students whose membership sits in their own
    active group.
    """
    if cs is None or not _is_regular_teacher(user):
        return True
    if not cs.groups.filter(_canonical_group_q()).exists():
        if not _student_is_home(student):
            return True
        return SubjectGroupMembership.objects.filter(
            class_subject=cs,
            student=student,
            group__is_active=True,
            group__is_home=True,
            group__teacher__user=user,
        ).exists()
    return SubjectGroupMembership.objects.filter(
        class_subject=cs,
        student=student,
        group__is_active=True,
        group__teacher__user=user,
    ).exists()


def _teacher_can_use_lesson(user, lesson):
    """Group check on a Lesson for grade/journal use by regular teachers.

    group=NULL lessons are shared class history — usable by any teacher
    who reached this journal, EXCEPT a teacher whose only group is
    таҳсил дар хона (shared class lessons are not theirs). A grouped
    lesson (canonical or home) is usable only by that group's teacher.
    """
    if not _is_regular_teacher(user):
        return True
    if lesson.group_id is None:
        own = teacher_group_for(user, lesson.class_subject)
        return own is None or not own.is_home
    return lesson.class_subject.groups.filter(
        is_active=True, teacher__user=user, pk=lesson.group_id
    ).exists()


def _teacher_can_edit_lesson(user, lesson):
    """Stricter variant: teachers may edit/delete only own-group lessons;
    legacy group=NULL lessons stay read-only for them (Admin/Zavuch manage).

    On a CS carrying only a home-education group the class teacher keeps
    full legacy edit rights, while a teacher owning ONLY the home group
    may not touch shared class lessons.
    """
    cs = lesson.class_subject
    if not _is_regular_teacher(user):
        return True
    if lesson.group_id is not None:
        return cs.groups.filter(
            is_active=True, teacher__user=user, pk=lesson.group_id
        ).exists()
    if cs.groups.filter(_canonical_group_q()).exists():
        return False
    if cs.groups.filter(is_active=True, teacher__user=user).exists() and not (
            cs.teacher_id == user.id
            or (cs.allocated_teacher_id is not None
                and cs.allocated_teacher.user_id == user.id)):
        return False
    return True


def _visible_lessons_q(user, cs, group=_GROUP_UNSET):
    """Q for lessons a regular teacher may select on a grouped CS.

    Own-group lessons plus legacy group=NULL rows. (On an ungrouped CS
    every lesson has group=NULL, so this filter is a no-op there.)
    """
    if group is _GROUP_UNSET:
        group = teacher_group_for(user, cs)
    if group is None:
        return Q(group__isnull=True)
    if group.is_home:
        # The home journal sees only its own lessons — shared class
        # lessons never belong to home-education teaching.
        return Q(group=group)
    return Q(group=group) | Q(group__isnull=True)


def _visible_assessments_q(user, cs, group=_GROUP_UNSET):
    """Q for control assessments a regular teacher may use on a grouped CS:
    own-group lesson assessments plus lesson-less (admin-managed) ones."""
    if group is _GROUP_UNSET:
        group = teacher_group_for(user, cs)
    if group is None:
        return Q(lesson__isnull=True)
    if group.is_home:
        return Q(lesson__group=group)
    return Q(lesson__isnull=True) | Q(lesson__group=group)


def _add_score(totals, key, total, count):
    if total is None or count is None:
        return
    prev = totals.setdefault(key, [0.0, 0])
    prev[0] += float(total)
    prev[1] += int(count)


# Short-lived cache for the global (user-independent) ranking results.
# TTL stays even with signal invalidation: out-of-band scripts and
# QuerySet.update()/raw SQL can bypass model signals.
RANKING_CACHE_TTL = 60
RANKING_GPAS_CACHE_KEY = 'rank:v1:gpas'
RANKING_CACHE_KEYS = (
    'rank:v1:school',
    'rank:v1:class:unfiltered',
    'rank:v1:subject',
    'rank:v1:top:prelimit',
    RANKING_GPAS_CACHE_KEY,  # all_student_gpas() — the student_detail GPA/rank map
)
# Bounded refresh (Oct 2026 504 mitigation): invalidation still deletes
# the main keys on every ranking-input save, but readers recompute at
# most once per RANKING_REFRESH_INTERVAL per key and otherwise serve the
# shadow copy kept under key+':stale'. During a burst of grade saves the
# full aggregation set therefore runs once per interval per worker
# instead of once per read after each save. Every signal-visible change
# is reflected within RANKING_REFRESH_INTERVAL; the TTL above remains
# the backstop for writes that never fire signals.
RANKING_REFRESH_INTERVAL = 30
RANKING_STALE_TTL = 600
RANKING_STALE_SUFFIX = ':stale'
# Single-flight: one refresh per key at a time across every gunicorn
# worker and thread, enforced by an OS file lock (see _ranking_flock_try).
# The key+':computing' cache entry is only the post-failure backoff
# marker: while it exists nobody starts a refresh and readers keep the
# shadow.
RANKING_LOCK_SUFFIX = ':computing'
# Site-wide refresh gate (504 fix): the per-key flock alone let misses on
# DIFFERENT keys refresh in parallel — up to one full-table canonical
# Grade scan per worker at once — saturating CPU/memory and pinning
# request threads past nginx's 60s proxy timeout. The gate is a second
# OS file lock taken before the per-key lock, so at most one ranking
# refresh runs at a time anywhere; contenders keep the existing
# shadow/bounded-wait behavior and never duplicate the work.
RANKING_GATE_LOCK_NAME = 'rank:v1:gate'
# Cold start (no shadow yet) while another worker holds the lock: wait
# for its result up to RANKING_LOCK_WAIT seconds, taking the lock over
# if the holder releases it or dies; kept below the 60s nginx/gunicorn
# timeout. compute() never runs without the OS locks — a waiter that
# times out raises RankingRefreshBusy instead of duplicating the work.
RANKING_LOCK_WAIT = 45
RANKING_LOCK_POLL = 0.05
# Timestamp of the latest invalidation. Entries are stamped at refresh
# START, so a save that commits while a refresh is mid-flight still
# invalidates its result — only entries post-dating this stamp are fresh.
RANKING_DIRTY_KEY = 'rank:v1:dirty'
RANKING_DIRTY_TTL = RANKING_STALE_TTL
# District-wide school_readiness_rating payload. Kept out of
# RANKING_CACHE_KEYS so the four leaderboards' warmup/invalidation
# bookkeeping is untouched; the dirty stamp still bounds its freshness
# and signals delete it alongside the others.
READINESS_CACHE_KEY = 'rank:v1:readiness'


def readiness_quarter_cache_key(quarter, academic_year):
    """Per-quarter readiness key — the Ҳисоботҳо period selector can pin
    the teacher/journal status scan to a past quarter. The shared
    READINESS_CACHE_KEY always holds the current quarter; past-quarter
    keys are warmed by refresh_rankings on demand (the view marks the
    quarters it asks for)."""
    return f'{READINESS_CACHE_KEY}:q{quarter}:{academic_year}'


# Every cache key the ranking cache can touch (used to fully reset it).
RANKING_ALL_CACHE_KEYS = (
    RANKING_CACHE_KEYS
    + tuple(k + RANKING_STALE_SUFFIX for k in RANKING_CACHE_KEYS)
    + tuple(k + RANKING_LOCK_SUFFIX for k in RANKING_CACHE_KEYS)
    + (RANKING_DIRTY_KEY,)
    + (READINESS_CACHE_KEY,
       READINESS_CACHE_KEY + RANKING_STALE_SUFFIX,
       READINESS_CACHE_KEY + RANKING_LOCK_SUFFIX)
)


def _ranking_cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _ranking_cache_set(key, value, timeout=RANKING_CACHE_TTL):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception:
        pass


def _ranking_cache_delete(key):
    try:
        cache.delete(key)
    except Exception:
        pass


try:
    import fcntl
except ImportError:  # Windows development machines
    fcntl = None
    import msvcrt

class RankingRefreshBusy(RuntimeError):
    """Cold cache and the single-flight lock could not be obtained within
    RANKING_LOCK_WAIT; raised instead of computing without the lock."""


def _ranking_lock_path(key):
    base = settings.CACHES['default'].get('LOCATION') or ''
    return os.path.join(base, 'ranking-locks',
                        re.sub(r'[^A-Za-z0-9_.-]', '_', key) + '.lock')


def _ranking_flock_try(key):
    """Non-blocking exclusive OS lock on the key's lock file.

    FileBasedCache.add() is a has_key()+set() pair, so it cannot arbitrate
    between gunicorn workers. flock/msvcrt locks are atomic across
    processes and threads (each call opens its own descriptor), survive a
    refresh longer than any TTL, and are released by the kernel if the
    holding worker dies. Returns the locked fd, or None when another
    holder has it or the lock file cannot be opened — never a pseudo-lock
    that would let compute() run unguarded.
    """
    path = _ranking_lock_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning('Ranking lock file unavailable for %s: %s', key, exc)
        return None
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    return fd


def _ranking_flock_release(handle):
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(handle)


def _ranking_lock_acquire(key):
    """(key lock, refresh gate) pair when this caller may compute `key`,
    or None when any refresh is in flight or a failed one is backing off.

    Two OS file locks, always taken in the same order (gate first, then
    per-key) so concurrent acquirers cannot deadlock. The gate admits at
    most one refresh at a time across all keys and workers; the per-key
    lock keeps a same-key duplicate impossible. Either piece being
    unavailable — another holder or an unopenable lock file — yields
    None and the caller keeps the shadow / bounded-wait behavior. A dead
    holder's locks are released by the kernel.
    """
    if _ranking_cache_get(key + RANKING_LOCK_SUFFIX) is not None:
        return None
    gate = _ranking_flock_try(RANKING_GATE_LOCK_NAME)
    if gate is None:
        return None
    handle = _ranking_flock_try(key)
    if handle is None:
        _ranking_flock_release(gate)
        return None
    return (handle, gate)


def _ranking_lookup(key):
    """(hit, data, stale): fresh main entry or rate-limited shadow."""
    now = time.time()
    entry = _ranking_cache_get(key)
    dirty = _ranking_cache_get(RANKING_DIRTY_KEY) or 0.0
    if (isinstance(entry, tuple) and entry[0] > dirty
            and now - entry[0] < RANKING_CACHE_TTL):
        return True, entry[1], None
    stale = _ranking_cache_get(key + RANKING_STALE_SUFFIX)
    if (isinstance(stale, tuple)
            and now - stale[0] < RANKING_REFRESH_INTERVAL):
        return True, stale[1], stale
    return False, None, stale


def _ranking_refresh(key, compute, locks, stale):
    """Recompute `key` while holding `locks` — the (per-key lock,
    refresh gate) pair from _ranking_lock_acquire; both are released on
    every exit path, including a failed compute."""
    try:
        # A sibling may have finished the same refresh between our lookup
        # and the lock acquisition — reuse its result.
        hit, data, latest_stale = _ranking_lookup(key)
        if hit:
            return data
        if isinstance(latest_stale, tuple):
            stale = latest_stale
        try:
            started = time.time()
            data = compute()
            # A compute that reused a request-shared Grade scan read its
            # rows at the scan time, not at `started`: stamp the entry no
            # later than that so the dirty check still sees saves that
            # landed after the scan.
            scan_at = _shared_ranking_scan_time()
            if scan_at is not None:
                started = min(started, scan_at)
        except Exception:
            if isinstance(stale, tuple):
                # Back off: the marker delays the next retry, and
                # readers keep the last good result.
                _ranking_cache_set(key + RANKING_LOCK_SUFFIX, 1)
                return stale[1]
            raise
        _ranking_cache_set(key, (started, data))
        _ranking_cache_set(
            key + RANKING_STALE_SUFFIX, (started, data),
            timeout=RANKING_STALE_TTL)
        _ranking_cache_delete(key + RANKING_LOCK_SUFFIX)
        return data
    finally:
        for handle in locks:
            _ranking_flock_release(handle)


def _ranking_cached(key, compute):
    """Return ``compute()`` output through the bounded-refresh cache.

    Entries are ``(computed_at, data)`` tuples stamped at refresh start,
    so a save landing mid-refresh still invalidates the result via
    RANKING_DIRTY_KEY. Resolution order: fresh main entry; shadow entry
    inside the refresh interval (the rate limit); recompute under the
    cross-process single-flight lock pair (site-wide gate + per-key
    lock — at most one refresh runs anywhere); shadow entry while another
    worker refreshes; on a cold start, wait for that worker's result,
    taking over the locks once released (holder finished, failed or
    died). compute() only ever runs while holding both OS locks: a
    cold-start waiter that cannot get them within RANKING_LOCK_WAIT
    raises RankingRefreshBusy.
    """
    hit, data, stale = _ranking_lookup(key)
    if hit:
        return data
    if _ranking_read_only.get():
        # Read-only scope (requests): never take the refresh locks or
        # wait — the shadow entry covers up to RANKING_STALE_TTL, then
        # the caller's busy fallback renders an empty block.
        if isinstance(stale, tuple):
            return stale[1]
        raise RankingRefreshBusy(
            f'Ranking refresh of {key} deferred to background job')
    locks = _ranking_lock_acquire(key)
    if locks is not None:
        return _ranking_refresh(key, compute, locks, stale)
    if isinstance(stale, tuple):
        return stale[1]
    for _ in range(int(RANKING_LOCK_WAIT / RANKING_LOCK_POLL)):
        time.sleep(RANKING_LOCK_POLL)
        entry = _ranking_cache_get(key)
        if isinstance(entry, tuple):
            return entry[1]
        locks = _ranking_lock_acquire(key)
        if locks is not None:
            return _ranking_refresh(key, compute, locks, stale)
    logger.error('Ranking refresh of %s: lock not obtained within %ss; '
                 'not computing without the lock', key, RANKING_LOCK_WAIT)
    raise RankingRefreshBusy(
        f'Ranking refresh of {key} still in progress elsewhere')


# Request-scoped share of the district-wide canonical Grade scan (504
# follow-up). The dashboard refreshes up to four ranking keys in one
# request and each used to repeat the same full current-year Grade scan
# plus canonical dedup. Inside sharing_ranking_grade_scan() the first
# compute that needs the rows scans once and later computes in the same
# request reuse them. Each key still refreshes under its own OS lock;
# only the data source is shared. Outside the scope nothing changes.
_shared_ranking_scan = contextvars.ContextVar(
    'shared_ranking_scan', default=None)


# Request paths never compute rankings: refreshes are memory-heavy (the
# full current-year Grade scan is materialised for canonical dedup), so
# they belong to the refresh_rankings command run from cron. Inside this
# scope _ranking_cached serves fresh/shadow entries and raises
# RankingRefreshBusy on a cold cache — the same fallback requests already
# had for a slow refresh, minus the memory spike and lock pile-up that
# OOM-killed workers (504 storm fix).
_ranking_read_only = contextvars.ContextVar(
    'ranking_read_only', default=False)


@contextlib.contextmanager
def rankings_read_only():
    token = _ranking_read_only.set(True)
    try:
        yield
    finally:
        _ranking_read_only.reset(token)


@contextlib.contextmanager
def sharing_ranking_grade_scan():
    token = _shared_ranking_scan.set({})
    try:
        yield
    finally:
        _shared_ranking_scan.reset(token)


def _shared_ranking_scan_time():
    memo = _shared_ranking_scan.get()
    return memo.get('at') if memo else None


def _shared_ranking_grade_rows():
    """Canonical current-year scored Grade rows for the active sharing
    scope, or None outside one. Rows carry the cell-key fields, score and
    the student's school_id/class_name. Reused only while younger than
    RANKING_REFRESH_INTERVAL and for the same academic year, so a reused
    scan is never older than a shadow entry may be."""
    memo = _shared_ranking_scan.get()
    if memo is None:
        return None
    bounds = academic_year_bounds()
    now = time.time()
    if (memo.get('bounds') == bounds
            and now - memo['at'] < RANKING_REFRESH_INTERVAL):
        return memo['rows']
    rows = [r for r in canonical_grade_values(Grade.objects.filter(
        score__isnull=False, student__is_archived=False,
        date__range=bounds).order_by().values(
        'pk', 'student_id', 'subject', 'period', 'date',
        'assessment_id', 'lesson_id', 'score',
        'student__school_id', 'student__class_name').iterator())
        if is_graded_subject(r['subject'])]
    memo.update(at=now, bounds=bounds, rows=rows)
    return rows


def calculate_school_rankings():
    """Return all schools ranked by average GPA from daily and quarterly grades (excluding non-graded classes)."""
    return _ranking_cached(
        RANKING_CACHE_KEYS[0], _calculate_school_rankings_uncached)


def _calculate_school_rankings_uncached():
    totals = {}
    # Current academic year only: Grade rows by source date; stored quarter
    # and attestation rows by their academic_year key — legacy NULL-year
    # rows never count as this year's result.
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()

    rows = _shared_ranking_grade_rows()
    if rows is None:
        rows = canonical_grade_values(Grade.objects.filter(
            score__isnull=False,
            date__range=(ay_start, ay_end)).order_by().values(
            'pk', 'student_id', 'subject', 'period', 'date',
            'assessment_id', 'lesson_id', 'score',
            'student__school_id', 'student__class_name').iterator())
    for r in rows:
        if is_non_graded(r['student__class_name']):
            continue
        if not is_graded_subject(r['subject']):
            continue
        _add_score(totals, r['student__school_id'], r['score'], 1)

    # One scan for both stored pools: the quarter predicates are
    # disjoint, so each row feeds exactly one aggregate. Sum/Count skip
    # NULLs the same way the former __isnull=False filters did; a group
    # where both aggregates are NULL is dropped by _add_score.
    for row in QuarterGrade.objects.filter(
            academic_year=ay, student__is_archived=False).values(
            'student__school_id', 'class_name', 'subject').annotate(
            total=Sum('grade', filter=Q(quarter__in=(1, 2, 3, 4))),
            count=Count('grade', filter=Q(quarter__in=(1, 2, 3, 4))),
            att_total=Sum('att_grade', filter=Q(quarter=0)),
            att_count=Count('att_grade', filter=Q(quarter=0))):
        if is_non_graded(row['class_name']):
            continue
        if not is_graded_subject(row['subject']):
            continue
        _add_score(totals, row['student__school_id'], row['total'], row['count'])
        _add_score(totals, row['student__school_id'], row['att_total'], row['att_count'])

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
    data = _ranking_cached(
        RANKING_CACHE_KEYS[1], _calculate_class_rankings_uncached)

    if school_filter:
        try:
            sid = int(school_filter)
            data = [item for item in data if item['school_id'] == sid]
        except (ValueError, TypeError):
            data = [item for item in data if school_filter.lower() in item['school_name'].lower()]

    return data


def _calculate_class_rankings_uncached():
    entries = {}
    schools_by_id = {s.id: s for s in School.objects.all()}
    academic_school_ids = {sid for sid, s in schools_by_id.items() if is_academic_school(s)}

    # student_id -> (school_id, raw class_name): lets the grade aggregations
    # group by the local student FK instead of joining Student per grade row.
    students = {
        st_id: (st_school, st_cls)
        for st_id, st_school, st_cls in Student.objects.values_list('id', 'school_id', 'class_name')
    }

    # Seed with every school/class combination that has students (exclude non-graded classes)
    for sid, raw_cls in students.values():
        if sid not in academic_school_ids:
            continue
        cname = normalize_class_name(raw_cls)
        if is_non_graded(cname):
            continue
        key = (sid, cname)
        entries[key] = {'school_id': sid, 'school_name': schools_by_id[sid].name, 'class_name': cname, 'total': 0.0, 'count': 0}

    # Current academic year only: Grade rows by source date; stored quarter
    # and attestation rows by their academic_year key. A stored row counts
    # toward the class it was earned in (its own class_name); legacy
    # NULL-year rows never count.
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()

    # Daily grades
    rows = _shared_ranking_grade_rows()
    if rows is None:
        rows = canonical_grade_values(Grade.objects.filter(
            score__isnull=False,
            date__range=(ay_start, ay_end)).order_by().values(
            'pk', 'student_id', 'subject', 'period', 'date',
            'assessment_id', 'lesson_id', 'score').iterator())
    for r in rows:
        info = students.get(r['student_id'])
        if info is None or info[0] not in academic_school_ids:
            continue
        cname = normalize_class_name(info[1])
        if is_non_graded(cname):
            continue
        if not is_graded_subject(r['subject']):
            continue
        key = (info[0], cname)
        if key not in entries:
            entries[key] = {'school_id': info[0], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(r['score'])
        entries[key]['count'] += 1

    # Quarterly grades
    for row in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False, academic_year=ay).values('student', 'class_name', 'subject').annotate(total=Sum('grade'), count=Count('grade')):
        info = students.get(row['student'])
        if info is None or info[0] not in academic_school_ids:
            continue
        cname = normalize_class_name(row['class_name'])
        if is_non_graded(cname):
            continue
        if not is_graded_subject(row['subject']):
            continue
        key = (info[0], cname)
        if key not in entries:
            entries[key] = {'school_id': info[0], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(row['total'] or 0)
        entries[key]['count'] += int(row['count'])

    # Attestation grades
    for row in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False, academic_year=ay).values('student', 'class_name', 'subject').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        info = students.get(row['student'])
        if info is None or info[0] not in academic_school_ids:
            continue
        cname = normalize_class_name(row['class_name'])
        if is_non_graded(cname):
            continue
        if not is_graded_subject(row['subject']):
            continue
        key = (info[0], cname)
        if key not in entries:
            entries[key] = {'school_id': info[0], 'school_name': '', 'class_name': cname, 'total': 0.0, 'count': 0}
        entries[key]['total'] += float(row['total'] or 0)
        entries[key]['count'] += int(row['count'])

    # Resolve any missing school names
    missing_ids = {e['school_id'] for e in entries.values() if not e['school_name']}
    if missing_ids:
        for e in entries.values():
            if not e['school_name']:
                school = schools_by_id.get(e['school_id'])
                e['school_name'] = school.name if school else ''

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

    return data


def calculate_top_students(school_filter=None, limit=100):
    """Return top-performing graded students (2-11) with dense school/district ranks."""
    data = _ranking_cached(
        RANKING_CACHE_KEYS[3], _calculate_top_students_uncached)

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


def _calculate_top_students_uncached():
    academic_school_ids = {s.id for s in School.objects.all() if is_academic_school(s)}
    totals = defaultdict(lambda: [0.0, 0])

    non_graded_filter = (
        ~Q(student__class_name__startswith='0-') &
        ~Q(student__class_name__startswith='1-')
    )
    student_filter = (Q(student__school_id__in=academic_school_ids)
                      & Q(student__is_archived=False)
                      & non_graded_filter)

    # Current academic year only: Grade rows by source date; stored quarter
    # and attestation rows by their academic_year key (legacy NULL-year
    # rows never count).
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()

    shared = _shared_ranking_grade_rows()
    if shared is None:
        grade_rows = canonical_grade_values(Grade.objects.filter(
            student_filter, score__isnull=False,
            date__range=(ay_start, ay_end)
        ).order_by().values('pk', 'student_id', 'subject', 'period', 'date',
                 'assessment_id', 'lesson_id', 'score').iterator())
    else:
        # Python form of student_filter. The cell key includes
        # student_id, so filtering canonical rows by student equals
        # canonicalizing the filtered queryset.
        grade_rows = (
            r for r in shared
            if r['student__school_id'] in academic_school_ids
            and not r['student__class_name'].startswith(('0-', '1-')))
    for r in grade_rows:
        if not is_graded_subject(r['subject']):
            continue
        totals[r['student_id']][0] += float(r['score'] or 0)
        totals[r['student_id']][1] += 1

    quarter_qs = QuarterGrade.objects.filter(
        student_filter, quarter__in=(1, 2, 3, 4), grade__isnull=False,
        academic_year=ay
    ).values('student', 'subject').annotate(
        total=Sum('grade'), count=Count('grade'))
    for row in quarter_qs:
        if not is_graded_subject(row['subject']):
            continue
        sid = row['student']
        totals[sid][0] += float(row['total'] or 0)
        totals[sid][1] += int(row['count'])

    att_qs = QuarterGrade.objects.filter(
        student_filter, quarter=0, att_grade__isnull=False,
        academic_year=ay
    ).values('student', 'subject').annotate(
        total=Sum('att_grade'), count=Count('att_grade'))
    for row in att_qs:
        if not is_graded_subject(row['subject']):
            continue
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

    return data


def calculate_subject_rankings():
    """Return subject rankings including all default subjects with 0.0 GPA if no grades exist."""
    return _ranking_cached(
        RANKING_CACHE_KEYS[2], _calculate_subject_rankings_uncached)


def _calculate_subject_rankings_uncached():
    totals = {}
    # Current academic year only: Grade rows by source date; stored quarter
    # and attestation rows by their academic_year key (legacy NULL-year
    # rows never count).
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()
    rows = _shared_ranking_grade_rows()
    if rows is None:
        rows = canonical_grade_values(Grade.objects.filter(
            score__isnull=False,
            date__range=(ay_start, ay_end)).order_by().values(
            'pk', 'student_id', 'subject', 'period', 'date',
            'assessment_id', 'lesson_id', 'score').iterator())
    for r in rows:
        subj = normalize_subject(r['subject'])
        _add_score(totals, subj, r['score'], 1)
    for row in QuarterGrade.objects.filter(quarter__in=(1, 2, 3, 4), grade__isnull=False, academic_year=ay).values('subject').annotate(total=Sum('grade'), count=Count('grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])
    for row in QuarterGrade.objects.filter(quarter=0, att_grade__isnull=False, academic_year=ay).values('subject').annotate(total=Sum('att_grade'), count=Count('att_grade')):
        subj = normalize_subject(row['subject'])
        _add_score(totals, subj, row['total'], row['count'])

    # Build a complete set of default subjects from ClassSubject and TJC defaults
    all_subjects = set()
    for subj in ClassSubject.objects.values_list('subject', flat=True).distinct():
        all_subjects.add(normalize_subject(subj))
    for subject_list in settings.TJC_SUBJECTS.values():
        for subj in subject_list:
            all_subjects.add(normalize_subject(subj))

    # Un-graded curriculum subjects (e.g. СОАТИ ТАРБИЯВӢ homeroom hour)
    # are official but are never ranked.
    official = official_subjects() - UNGRADED_SUBJECTS
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


def _ranking_or_empty(calculate):
    """Dashboard fallback for a cold ranking still being refreshed by
    another worker past RANKING_LOCK_WAIT: render that block empty (the
    template's existing {% empty %} row) instead of failing the page.
    Never computes — RankingRefreshBusy is raised only after the lock
    could not be obtained."""
    try:
        return calculate()
    except RankingRefreshBusy:
        return []


def dashboard(request):
    role = get_user_role(request.user)
    user_school = get_user_school(request.user)

    # Live rankings from Grade and QuarterGrade records. Requests are
    # read-only here — refreshes run in the refresh_rankings cron job.
    with rankings_read_only():
        all_school_ranking = _ranking_or_empty(calculate_school_rankings)
        all_subject_ranking = _ranking_or_empty(calculate_subject_rankings)
        class_ranking = _ranking_or_empty(calculate_class_rankings)
        top_students = _ranking_or_empty(calculate_top_students)

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

    if selected_school != '0':
        try:
            sid = int(selected_school)
            class_ranking = [c for c in class_ranking if c['school_id'] == sid]
        except (ValueError, TypeError):
            class_ranking = []

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

    from .news import slider_slides, slider_videos, video_watch_seconds
    news_slides = slider_slides()

    context = {
        'role': role,
        'schools': schools,
        'schools_dropdown': schools_dropdown,
        'school_ranking': school_ranking,
        'class_ranking': class_ranking,
        'top_students': top_students,
        'subject_ranking': subject_ranking,
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
        'news_slides': news_slides,
        'news_videos': slider_videos(),
        'news_video_secs': video_watch_seconds(),
        'news_can_manage': _news_allowed(request),
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
    with rankings_read_only():
        data = _ranking_or_empty(lambda: calculate_top_students(school))
    return JsonResponse(data, safe=False)


def class_rankings_ajax(request):
    """Return class rankings, optionally filtered by school."""
    school = _clean_school_param(request.GET.get('school'))
    with rankings_read_only():
        data = _ranking_or_empty(lambda: calculate_class_rankings(school))
    return JsonResponse(data, safe=False)


# ---------------------------------------------------------------------------
# Student search (/search/)
#
# One bounded, role-aware GET view. No query runs unless a non-empty `q` is
# submitted, and every submitted school_id is validated against the caller's
# permitted scope. Cyrillic-insensitive matching goes through iregex because
# SQLite's LIKE is not Unicode case-folding; phone-keyboard substitutions
# (Qodirov -> Қодиров) are handled by expanding each typed character into a
# small equivalence class instead of rewriting stored names.
# ---------------------------------------------------------------------------

SEARCH_RESULT_LIMIT = 30
SEARCH_Q_MAX = 100

# Symmetric equivalence groups: a typed character belonging to a group may
# match any member of that group in stored names. Covers the six Tajik
# letters missing from common phone keyboards (қ ғ ҳ ҷ ӯ ӣ), their Latin /
# Russian-keyboard substitutes, and visually interchangeable Latin/Cyrillic
# pairs. Case is handled by the case-insensitive regex itself.
_NAME_EQUIV_GROUPS = (
    'aа', 'bб', 'vвw', 'gгғ', 'dд', 'eеэ', 'zз',
    'iиӣ', 'jжҷ', 'kкqқ', 'lл', 'mм', 'nн', 'oо',
    'pп', 'rр', 'sс', 'tт', 'uуӯ', 'yуӯй', 'fф',
    'hхҳx', 'cк', "'ъ`’",
)

# A character may sit in several groups (e.g. 'к' in 'kкqқ' and 'cк');
# its expansion is the union of every group containing it.
_NAME_CHAR_GROUP = {}
for _group in _NAME_EQUIV_GROUPS:
    for _ch in _group:
        _NAME_CHAR_GROUP[_ch] = _NAME_CHAR_GROUP.get(_ch, '') + _group

# Two Latin characters that commonly stand for a single Cyrillic letter.
# The generated regex accepts either the digraph target(s) or the literal
# two-character sequence, so "Ishoq" matches both "Ишоқ" and "Исҳоқ".
_NAME_DIGRAPHS = {
    'sh': 'ш', 'ch': 'ч', 'zh': 'жҷ', 'kh': 'хҳ', 'gh': 'ғ',
    'yo': 'ё', 'yu': 'ю', 'ya': 'я',
}

# The Tajik apostrophe (ъ) is frequently omitted on phone keyboards, so an
# optional apostrophe-class slot is allowed between pattern elements:
# "Sadullo" still matches "Саъдулло".
_NAME_OPTIONAL_CHAR_CLASS = ('['
    + ''.join(re.escape(c) for c in "'ъ`’")
    + ']?')


def _char_class(chars):
    return '[' + ''.join(re.escape(c) for c in chars) + ']'


def _search_name_pattern(q):
    """Compile `q` into a single bounded case-insensitive regex pattern.

    Every element is either a fixed character class of equivalence-group
    members or a re.escape'd literal, so user input can never inject regex
    metacharacters. The result is one pattern (no exponential variant
    explosion) evaluated as a substring match by SQLite's REGEXP/Python re.
    """
    ql = q.lower()
    parts = []
    i = 0
    while i < len(ql):
        pair = ql[i:i + 2]
        if pair in _NAME_DIGRAPHS:
            a = _NAME_CHAR_GROUP.get(ql[i], ql[i])
            b = _NAME_CHAR_GROUP.get(ql[i + 1], ql[i + 1])
            parts.append(
                '(?:' + _char_class(_NAME_DIGRAPHS[pair])
                + '|' + _char_class(a) + _char_class(b) + ')'
            )
            i += 2
            continue
        group = _NAME_CHAR_GROUP.get(ql[i])
        parts.append(_char_class(group) if group else re.escape(ql[i]))
        i += 1
    return _NAME_OPTIONAL_CHAR_CLASS.join(parts)


def _teacher_permitted_class_names(user, school):
    """Class names a regular teacher may access at `school`.

    Mirrors the class_list filter exactly — a class is permitted when the
    teacher is reachable through at least one active ClassSubject (group
    teachers on grouped subjects, teacher/allocated_teacher on ungrouped
    ones) — but resolves it with two bounded queries instead of a
    per-ClassSubject permission loop.
    """
    cs_rows = list(
        ClassSubject.objects
        .filter(school=school, is_active=True)
        .select_related('allocated_teacher')
    )
    group_teachers = defaultdict(set)
    for cs_id, teacher_user_id in (
        TeachingGroup.objects
        .filter(class_subject__in=cs_rows, is_active=True)
        .values_list('class_subject_id', 'teacher__user_id')
    ):
        group_teachers[cs_id].add(teacher_user_id)

    permitted = set()
    for cs in cs_rows:
        if cs.id in group_teachers:
            if user.id in group_teachers[cs.id]:
                permitted.add(cs.class_name)
        elif cs.teacher_id == user.id or (
            cs.allocated_teacher is not None
            and cs.allocated_teacher.user_id == user.id
        ):
            permitted.add(cs.class_name)
    return permitted


def search(request):
    """Role-aware student search.

    Scope rules: anonymous visitors must choose a school explicitly;
    superuser/staff/district director may search all academic schools;
    zavuch, principal and regular teachers are clamped to their own school,
    and regular teachers are further limited to classes they are assigned
    to in "Тақсимоти дарсҳо".
    """
    user = request.user
    q = (request.GET.get('q') or '').strip()[:SEARCH_Q_MAX]
    school_raw = (request.GET.get('school') or '').strip()
    class_raw = (request.GET.get('class_name') or '').strip()

    role = get_user_role(user) if user.is_authenticated else ''
    academic = sorted(academic_schools(), key=_school_sort_key)

    privileged = user.is_authenticated and (
        user.is_superuser
        or user.is_staff
        or role == settings.ROLE_DIRECTOR
    )
    school_fixed = user.is_authenticated and not privileged
    if school_fixed:
        own = get_user_school(user)
        permitted_schools = (
            [own] if own is not None and is_academic_school(own) else []
        )
    else:
        permitted_schools = academic

    # School scope: scoped users are clamped to their own school; anything
    # they submit is ignored. Others must submit a valid permitted id.
    school_error = False
    if school_fixed:
        selected_school = permitted_schools[0] if permitted_schools else None
    elif school_raw:
        selected_school = None
        try:
            sid = int(school_raw)
        except (TypeError, ValueError):
            sid = None
        for s in permitted_schools:
            if s.id == sid:
                selected_school = s
                break
        school_error = selected_school is None
    else:
        selected_school = None

    # Regular teachers only reach classes they are assigned to.
    permitted_classes = None
    if (
        selected_school is not None
        and _is_regular_teacher(user, role)
    ):
        permitted_classes = _teacher_permitted_class_names(user, selected_school)

    # Class filter: normalized and validated; an invalid or unauthorized
    # class simply yields no results instead of widening the scope.
    class_filter = None
    class_blocked = False
    if class_raw:
        class_filter = _validate_class_name(class_raw)
        if class_filter is None or (
            permitted_classes is not None
            and class_filter not in permitted_classes
        ):
            class_filter = None
            class_blocked = True

    # Class dropdown options come from ClassSubject rows only — every class
    # that has students also has ClassSubject rows (auto-seeded on Student
    # save), so no Student query is needed to render the form.
    if selected_school is not None:
        cs_scope = ClassSubject.objects.filter(school=selected_school)
    elif privileged:
        cs_scope = ClassSubject.objects.filter(
            school_id__in=[s.id for s in permitted_schools])
    else:
        cs_scope = ClassSubject.objects.none()
    class_choices = sorted(
        {c for c in cs_scope.values_list('class_name', flat=True).distinct()},
        key=lambda c: (class_numeric_part(c), c),
    )
    if permitted_classes is not None:
        class_choices = [c for c in class_choices if c in permitted_classes]

    # Anonymous visitors may not run a district-wide name search; a specific
    # school is mandatory. Scoped users with no school cannot search at all.
    need_school = (
        selected_school is None
        and (not user.is_authenticated or school_fixed)
    )

    results = []
    searched = False
    if q and not (school_error or class_blocked or need_school):
        searched = True
        qs = Student.objects.filter(
            full_name__iregex=_search_name_pattern(q))
        if selected_school is not None:
            qs = qs.filter(school_id=selected_school.id)
        else:
            qs = qs.filter(school_id__in=[s.id for s in permitted_schools])
        if permitted_classes is not None:
            qs = qs.filter(class_name__in=permitted_classes)
        if class_filter:
            qs = qs.filter(class_name=class_filter)
        results = list(
            qs.values('id', 'full_name', 'class_name',
                      'school_id', 'school__name')
            .order_by('school_id', 'class_name', 'full_name')
            [:SEARCH_RESULT_LIMIT]
        )
        results.sort(key=lambda r: (
            r['school_id'], r['class_name'], name_key(r['full_name'])))

    return render(request, 'portal/search.html', {
        'q': q,
        'permitted_schools': permitted_schools,
        'school_fixed': school_fixed,
        'school_required': not user.is_authenticated,
        'selected_school': selected_school,
        'class_choices': class_choices,
        'class_selected': class_filter or '',
        'results': results,
        'searched': searched,
        'school_error': school_error,
        'class_blocked': class_blocked,
        'need_school': need_school,
        'result_limit': SEARCH_RESULT_LIMIT,
    })


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
    # Current academic year only: Grade rows by source date; stored quarter
    # and attestation rows by their academic_year key — a grade earned in
    # this class this year counts toward this class; legacy NULL-year and
    # prior-year rows never count (same rule as the ranking engines).
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()
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
        for g in canonical_grade_rows(Grade.objects.filter(
                student__school=school, student__class_name=cname,
                student__is_archived=False,
                score__isnull=False,
                date__range=(ay_start, ay_end))):
            total += float(g.score)
            count += 1
        for q in QuarterGrade.objects.filter(
                student__school=school, class_name=cname,
                student__is_archived=False,
                academic_year=ay,
                quarter__in=(1, 2, 3, 4), grade__isnull=False):
            total += float(q.grade)
            count += 1
        for q in QuarterGrade.objects.filter(
                student__school=school, class_name=cname,
                student__is_archived=False,
                academic_year=ay,
                quarter=0, att_grade__isnull=False):
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
    # A class re-created on top of previously deactivated rows revives its
    # stored configuration (intentional opt-outs stay untouched).
    reactivate_class_subjects(school, class_name, require_students=False)
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
    students = sort_students(Student.objects.filter(
        school=school, class_name=class_name))
    total_count = len(students)
    male_count = sum(1 for s in students if s.gender == 'M')
    female_count = sum(1 for s in students if s.gender == 'F')
    # Exempt-from-grading map («Дар давлати дигар таҳсил мекунад») — one
    # grouped query, attached per student for the edit dialog + badge.
    exemption_map = active_exemption_map(s.id for s in students)
    for s in students:
        s.exemption = exemption_map.get(s.id)
    # Archived (рафт) students of this class — collapsed section with a
    # restore path; history is preserved, so re-enrollment never merges
    # or loses anything.
    archived_students = sort_students(
        Student.all_objects
        .filter(school=school, class_name=class_name, is_archived=True))
    subjects = ClassSubject.objects.filter(
        school=school, class_name=class_name, is_active=True,
        subject__in=official_subjects_for(school, class_name)
    ).order_by('subject')
    # Regular teachers only see their own assigned subjects in the journal
    # picker — for grouped subjects that means their TeachingGroup.
    teacher_hours = []
    if _is_regular_teacher(request.user):
        subjects = [
            cs for cs in subjects.prefetch_related('groups', 'allocated_teacher')
            if _teacher_can_access_cs(request.user, cs)
        ]
        for cs in subjects:
            own_group = teacher_group_for(request.user, cs)
            cs.group_label = own_group.label if own_group else None
            # «Соатҳои ҳафтаинаи шумо» — the hours the teacher is actually
            # paid for. For a home-education group that is its own explicit
            # value; otherwise the class-subject weekly hours (confirmed in
            # «Тақсимоти дарсҳо» or the national curriculum).
            if own_group is not None and own_group.is_home:
                hours_val = group_weekly_hours(own_group, cs)
            else:
                hours_val = weekly_hours(class_name, cs.subject, cs)
            teacher_hours.append({
                'subject': cs.subject,
                'label': own_group.label if own_group else None,
                'is_home': bool(own_group is not None and own_group.is_home),
                'hours': disp_hours(hours_val),
            })
    non_graded = is_non_graded(class_name)
    student_classes = {
        c for c in Student.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    }
    all_classes = sorted(student_classes | {
        c for c in ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct()
    }, key=class_numeric_part)
    # Full-class transfer targets: every EXISTING class in this school that
    # currently has zero students — any grade, any letter, any prior usage.
    # Emptiness is decided by the student-class set, so a previously-used
    # but now-empty class stays eligible.
    empty_targets = [
        c for c in all_classes
        if c != class_name
        and c not in student_classes
        and _validate_class_name(c)
    ]
    # ⏰ Ғоибият entry: school/district document viewers plus the роҳбари
    # синф (the teacher allocated to this class's СОАТИ ТАРБИЯВӢ).
    can_view_absences = _tabel_access(request.user)
    if not can_view_absences and request.user.is_authenticated:
        from .absences import led_class_names
        can_view_absences = class_name in led_class_names(
            request.user, school)
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
        'empty_targets': empty_targets,
        'archived_students': archived_students,
        'teacher_hours': teacher_hours,
        'can_view_absences': can_view_absences,
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

    students = sort_students(_exclude_home_students(
        Student.objects.filter(school=school, class_name=class_name)))

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
        student__is_archived=False,
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

    # Most recent prior behavior score per student for THIS subject only —
    # conduct is subject-specific and must never borrow another subject's
    # value as the carry-over default.
    prior_behavior = {}
    for g in Grade.objects.filter(
        student__in=students,
        subject=subject,
        behavior_score__isnull=False,
        date__lt=selected_date
    ).order_by('student_id', '-date', '-pk'):
        if g.student_id not in prior_behavior:
            prior_behavior[g.student_id] = g.behavior_score

    # Display chain per student: today's slot value -> latest stored value
    # FOR THIS SUBJECT -> neutral default 5. The 5 is display-only: untouched
    # behavior selects are excluded from batch submits and saveAll, and the
    # server writes behavior_score only when the field is actually posted.
    behavior_default = {}
    for s in students:
        if s.id in daily_behavior:
            behavior_default[s.id] = daily_behavior[s.id]
        elif s.id in prior_behavior:
            behavior_default[s.id] = prior_behavior[s.id]
        else:
            behavior_default[s.id] = 5

    daily_quarter_locked = is_quarter_locked(
        school, class_name, subject, get_date_quarter(selected_date),
        academic_year_start(selected_date))

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
        'holiday_date': not is_school_day(selected_date),
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
    today = datetime.date.today()
    existing = Student.all_objects.filter(id=student_id).first()
    if existing and not existing.is_archived:
        messages.warning(request, 'Хонанда бо ин ном аллакай вуҷуд дорад.')
    elif existing:
        # Same id re-enrollment — reactivate the archived row so every
        # stored Grade/QuarterGrade comes back with it (no duplicate).
        existing.is_archived = False
        existing.archived_at = None
        existing.save(update_fields=['is_archived', 'archived_at'])
        StudentMovement.objects.create(
            school=school, student=existing,
            student_name=existing.full_name, class_name=class_name,
            gender=existing.gender or '', movement_type='arrival',
            date=today, created_by=request.user)
        reactivate_class_subjects(school, class_name)
        messages.success(request, 'Хонанда аз бойгонӣ барқарор шуд — таърихи ӯ ҳифз шудааст.')
    else:
        # Cross-class rejoin: an archived student with the same name in a
        # different class of this school is the same person — move the
        # archived row (re-links all history) instead of a fresh identity.
        archived_same = (Student.all_objects
                         .filter(school=school, full_name=full_name,
                                 is_archived=True)
                         .exclude(class_name=class_name)
                         .order_by('-archived_at').first())
        if archived_same is not None:
            try:
                student = _move_student(
                    school, archived_same, class_name, full_name)
            except ValueError:
                student = None
            if student is not None:
                student.is_archived = False
                student.archived_at = None
                student.save(update_fields=['is_archived', 'archived_at'])
                StudentMovement.objects.create(
                    school=school, student=student,
                    student_name=student.full_name, class_name=class_name,
                    gender=student.gender or '', movement_type='arrival',
                    date=today, created_by=request.user)
                reactivate_class_subjects(school, class_name)
                messages.success(
                    request,
                    'Хонанда аз бойгонӣ барқарор шуд — таърихи ӯ ҳифз шудааст.')
                return redirect(
                    'class_detail', school_id=school.id,
                    class_name=class_name)
        student = Student.objects.create(
            id=student_id,
            full_name=full_name,
            class_name=class_name,
            school=school
        )
        StudentMovement.objects.create(
            school=school, student=student,
            student_name=student.full_name, class_name=class_name,
            gender=student.gender or '', movement_type='arrival',
            date=today, created_by=request.user)
        reactivate_class_subjects(school, class_name)
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
            StudentMovement.objects.create(
                school=school, student=student,
                student_name=student.full_name, class_name=class_name,
                gender=student.gender or '', movement_type='departure',
                date=datetime.date.today(), created_by=request.user)
            # Soft-delete: the row and every grade stay in the database —
            # roster queries hide archived students automatically.
            student.is_archived = True
            student.archived_at = timezone.now()
            student.save(update_fields=['is_archived', 'archived_at'])
            messages.success(request, 'Хонанда хориҷ ва ба бойгонӣ гузаронида шуд.')
        except Student.DoesNotExist:
            messages.error(request, 'Хонанда ёфт нашуд.')
    return redirect('class_detail', school_id=school.id, class_name=class_name)


@login_required
@require_POST
def restore_student(request, school_id, class_name):
    """Reactivate an archived student back into the same class roster."""
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)
    student_id = request.POST.get('student_id', '').strip()
    if student_id:
        student = (Student.all_objects
                   .filter(id=student_id, school=school,
                           class_name=class_name, is_archived=True)
                   .first())
        if student is None:
            messages.error(request, 'Хонандаи бойгонӣ ёфт нашуд.')
        else:
            student.is_archived = False
            student.archived_at = None
            student.save(update_fields=['is_archived', 'archived_at'])
            StudentMovement.objects.create(
                school=school, student=student,
                student_name=student.full_name, class_name=class_name,
                gender=student.gender or '', movement_type='arrival',
                date=datetime.date.today(), created_by=request.user)
            reactivate_class_subjects(school, class_name)
            messages.success(
                request,
                'Хонанда аз бойгонӣ барқарор шуд — таърихи ӯ ҳифз шудааст.')
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
    reactivate_class_subjects(school, target_class)
    _deactivate_empty_class(school, class_name)

    # «Дар давлати дигар таҳсил мекунад» exemption — set directly by
    # завуч/директор in this same edit dialog (no approval workflow).
    exempt_on = request.POST.get('exempt_abroad') == 'on'
    exemption = active_exemption_map([student.id]).get(student.id)
    # This dialog manages the 'abroad' status only — a «Дар хона таҳсил
    # мегирад» exemption lives under the таҳсил дар хона tab and must
    # never be toggled or cancelled here.
    if exemption is not None and exemption.reason != 'abroad':
        exemption = None
    if exempt_on:
        document = request.POST.get('exempt_document', '').strip()[:255]
        end_raw = request.POST.get('exempt_end_date', '').strip()
        end_date = None
        if end_raw:
            try:
                end_date = datetime.date.fromisoformat(end_raw)
            except ValueError:
                end_date = None
        if exemption is None:
            StudentExemption.objects.create(
                student=student, reason='abroad', document=document,
                start_date=datetime.date.today(), end_date=end_date,
                created_by=request.user)
        else:
            changed = False
            if document != exemption.document:
                exemption.document = document
                changed = True
            if end_raw and end_date != exemption.end_date:
                exemption.end_date = end_date
                changed = True
            if changed:
                exemption.save()
    elif exemption is not None:
        exemption.cancelled_at = timezone.now()
        exemption.cancelled_by = request.user
        exemption.save(update_fields=['cancelled_at', 'cancelled_by'])

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

    # Routing is decided by target emptiness FIRST:
    #   - empty target  -> safe full-class transfer path (any grade/letter);
    #   - populated target -> existing same-grade merge rules only.
    target_populated = Student.objects.filter(
        school=school, class_name=target_class).exists()
    merge_ok = _is_valid_class_merge(source_class, target_class)

    if target_populated and not merge_ok:
        if source_class.endswith('-А'):
            messages.error(request, 'Синфи асосии «А» наметавонад ба дигар синфҳо кӯчонида шавад! Танҳо синфҳои «Б, В, Г» метавонанд ба синфи «А» муттаҳид карда шаванд.')
        else:
            messages.error(request, 'Кӯчонидан танҳо аз ҳарфи ҷуфттар ба ҳарфи пештар иҷозат дорад (масалан: 10-Б -> 10-А).')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    students = list(Student.objects.filter(school=school, class_name=source_class).order_by('full_name'))
    if not students:
        messages.warning(request, 'Синфи манба холӣ аст.')
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    if not target_populated:
        # Full-class transfer to an existing empty class — any grade, any
        # letter (e.g. 5-Д -> 6-Д, 5-Д -> 7-Д, 5-Д -> 5-Е, 5-Д -> 5-А).
        # The target is never created here and never merged — it must
        # already exist with zero students.
        if not _class_exists(school, target_class):
            messages.error(request, f'Синфи ҳадаф «{target_class}» вуҷуд надорад. Аввал онро дар рӯйхати синфҳо эҷод кунед.')
            return redirect('class_detail', school_id=school.id, class_name=source_class)
        try:
            with transaction.atomic():
                # Re-checked inside the transaction: the target may have
                # gained students after the dialog was opened.
                if Student.objects.filter(school=school, class_name=target_class).exists():
                    raise ValueError(f'Синфи ҳадаф «{target_class}» холӣ нест. Гузарондан танҳо ба синфи холӣ иҷозат дода мешавад.')
                for student in Student.objects.filter(
                        school=school, class_name=source_class).order_by('full_name'):
                    _move_student(
                        school, student, target_class, student.full_name,
                        gender=student.gender, relabel_quarter_grades=False,
                    )
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('class_detail', school_id=school.id, class_name=source_class)
        reactivate_class_subjects(school, target_class)
        _deactivate_empty_class(school, source_class)
        messages.success(request, f'Ҳамаи хонандагони синфи {source_class} ба синфи {target_class} бомуваффақият гузаронида шуданд.')
        return redirect('class_detail', school_id=school.id, class_name=target_class)

    try:
        with transaction.atomic():
            for student in students:
                _move_student(school, student, target_class, student.full_name)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('class_detail', school_id=school.id, class_name=source_class)

    ensure_class_subjects(school, target_class)
    reactivate_class_subjects(school, target_class)
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
        students = sort_students(_exclude_home_students(
            Student.objects.filter(school=school, class_name=class_name)))
    students = list(students)
    # «Дар давлати дигар таҳсил мекунад» exemptions: blue indicators in the
    # journal + removal from the norm denominator (one grouped query).
    exemption_map = active_exemption_map(s.id for s in students)
    # In the таҳсил дар хона journal the «Дар хона таҳсил мегирад» status is
    # not an exemption — these students ARE the journal's roster and count
    # toward its norm like any ordinary student.
    journal_group = (
        teacher_group_for(request.user, class_subject)
        if class_subject is not None else None
    )
    if journal_group is not None and journal_group.is_home:
        exemption_map = {
            sid: ex for sid, ex in exemption_map.items()
            if ex.reason != 'home'}
    for s in students:
        s.exemption = exemption_map.get(s.id)

    if request.method == 'POST':
        if not can_edit_grade_journal(request.user, school, class_name, subject):
            return HttpResponse('Дастрасӣ барои тағйир додан манъ аст.', status=403)

        date_str = request.POST.get('date', '')
        try:
            date = datetime.date.fromisoformat(date_str) if date_str else datetime.date.today()
        except ValueError:
            date = datetime.date.today()
        if date > _journal_write_today():
            return HttpResponse('Санаи ояндада хол ё давомат гузошта намешавад.', status=400)
        if not is_school_day(date):
            return HttpResponse('Дар рӯзи таътил хол ё давомат гузошта намешавад.', status=400)

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
        daily_locked = is_quarter_locked(
            school, class_name, subject, daily_q,
            academic_year_start(date))
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

        from .signals import defer_quarter_recalc  # local import avoids cycles
        # Un-graded curriculum subjects (СОАТИ ТАРБИЯВӢ, ҚОИДАИ ҲАРАКАТ
        # ДАР РОҲ) never take scores — attendance/conduct stays editable.
        subject_scored = is_graded_subject(subject)
        deferred = set()
        try:
            with defer_quarter_recalc() as deferred:
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

                        score = None
                        if subject_scored:
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
                        grade.score = score
    
                        att = request.POST.get(f"attendance_{student.id}", '').strip()
                        grade.attendance = att if att in ('+', '-') else None
    
                        # Conduct is written only when the field was actually
                        # submitted — the journal disables untouched behavior selects
                        # on submit, so an absent key means 'leave the stored value
                        # alone'. This keeps a batch save from materializing the
                        # carry-over/default prefill (or a foreign subject's value)
                        # into rows for students the teacher never edited.
                        beh_key = f"behavior_{student.id}"
                        if beh_key in request.POST:
                            beh = request.POST.get(beh_key, '').strip()
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
    
                    # Attestation entry (quarter 0) stays teacher-editable — it is a
                    # source mark for the final grade. Quarter grades 1-4 are derived
                    # automatically from Grade rows; posted q_* values are ignored.
                    # The mark is keyed to the CURRENT academic year — a
                    # prior-year attestation under the same class label is a
                    # different record.
                    att_year = academic_year_start()
                    if subject_scored and not is_quarter_locked(
                            school, class_name, subject, 0, att_year):
                        qval = request.POST.get(f"att_{student.id}", '').strip()
                        if qval:
                            try:
                                qscore = float(qval)
                                if 1 <= qscore <= 10:
                                    QuarterGrade.objects.update_or_create(
                                        student=student,
                                        class_name=class_name,
                                        subject=subject,
                                        quarter=0,
                                        academic_year=att_year,
                                        defaults={'att_grade': int(qscore)}
                                    )
                            except ValueError:
                                pass
                        else:
                            QuarterGrade.objects.filter(
                                student=student, class_name=class_name,
                                subject=subject, quarter=0,
                                academic_year=att_year,
                            ).delete()
        finally:
            # Re-derive every (student, subject, quarter) the batch
            # touched in one bulk pass — and drain even when the batch
            # aborted partway, so committed source rows never sit
            # beside a stale stored quarter.
            _drain_deferred_quarter_recalc(deferred)
        back = f"?date={date.isoformat()}"
        if post_lesson is not None:
            back += f"&lesson={post_lesson.id}"
        # The submit may come from the Ҷадвали чорякҳо tab — the hidden
        # `tab` field lets the redirect reopen that tab instead of
        # always landing on Холҳои ҳаррӯза.
        tab = request.POST.get('tab', '')
        if tab in ('daily', 'quarterly'):
            back += f"&tab={tab}"
        return redirect(
            reverse('grade_entry', kwargs={
                'school_id': school.id, 'class_name': class_name, 'subject': subject
            }) + back
        )

    date_str = request.GET.get('date', '')
    try:
        selected_date = datetime.date.fromisoformat(date_str) if date_str else timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()

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
        student__is_archived=False,
        subject=subject,
        period='Холҳои ҷорӣ (Онлайн)',
        date=selected_date,
        # Assessment-linked rows belong to Назорат mode only — they must
        # never leak into the Ҷорӣ daily cells for the same date.
        assessment__isnull=True,
        lesson=selected_lesson,
    ).order_by('pk'):
        # First-pk-wins per field — the same survivor get_or_create_grade
        # keeps (and the fill order its merge applies) when same-cell
        # duplicates exist, so the shown value is the one calculations use.
        if g.score is not None and g.student_id not in daily_grades:
            daily_grades[g.student_id] = int(g.score) if g.score.is_integer() else g.score
        if g.attendance and g.student_id not in daily_attendance:
            daily_attendance[g.student_id] = g.attendance
        if g.behavior_score is not None and g.student_id not in daily_behavior:
            daily_behavior[g.student_id] = g.behavior_score

    # Most recent prior behavior score per student for THIS subject only —
    # conduct is subject-specific: a Physics mark must never carry over as
    # the default for Algebra (or vice versa).
    prior_behavior = {}
    # Per-student behavior history for THIS subject (non-assessment rows),
    # newest first. Control mode resolves each assessment's display default
    # against the assessment's OWN date — the same row used for the daily
    # carry-over, so no extra query.
    prior_behavior_hist = defaultdict(list)
    for g in Grade.objects.filter(
        student__in=students,
        subject=subject,
        behavior_score__isnull=False,
        date__lt=selected_date,
        assessment__isnull=True,
    ).order_by('student_id', '-date', '-pk'):
        if g.student_id not in prior_behavior:
            prior_behavior[g.student_id] = g.behavior_score
        prior_behavior_hist[g.student_id].append((g.date, g.behavior_score))

    # Display chain per student: today's slot value -> latest stored value
    # FOR THIS SUBJECT -> neutral default 5. The 5 is display-only: untouched
    # behavior selects are excluded from batch submits and saveAll, and the
    # server writes behavior_score only when the field is actually posted.
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
        subject=subject,
        academic_year=academic_year_start(),
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
    ay_year = academic_year_start()
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
                    grades_by_student.get(sid, ()), q, ay_year
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

    locked_quarters = _locked_quarters_for_year(
        school, class_name, subject, academic_year_start())
    daily_quarter_locked = is_quarter_locked(
        school, class_name, subject, get_date_quarter(selected_date),
        academic_year_start(selected_date))

    # Control (Назорат) assessments for this ClassSubject, for the picker.
    control_assessments = []
    date_is_control = False
    if class_subject is not None:
        assessments_qs = Assessment.objects.filter(
            class_subject=class_subject, category='control'
        )
        if _is_regular_teacher(request.user) and class_subject.groups.filter(
                _canonical_group_q()).exists():
            assessments_qs = assessments_qs.filter(
                _visible_assessments_q(request.user, class_subject)
            )
        elif _is_regular_teacher(request.user):
            # A таҳсил дар хона group alone does not group-scope the class
            # journal — but the home teacher's own assessments stay scoped
            # to their home journal.
            own = teacher_group_for(request.user, class_subject)
            if own is not None and own.is_home:
                assessments_qs = assessments_qs.filter(
                    _visible_assessments_q(request.user, class_subject))
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
                'locked': is_quarter_locked(
                    school, class_name, subject, a.quarter,
                    academic_year_start(a.date)),
            })
            # Mode lock is slot-scoped: with a lesson selected only that
            # lesson's assessments lock it; otherwise only the legacy
            # lesson-less slot counts — a lesson-scoped assessment never
            # flips the whole date.
            if a.date == selected_date and a.lesson_id == (
                selected_lesson.id if selected_lesson else None
            ):
                date_is_control = True

    # Умумӣ only shows lesson-less rows, so Ҷорӣ rows stored under a
    # lesson slot (e.g. saved through the monthly journal) are invisible
    # here even though they exist. One grouped count lets each chip flag
    # the waiting rows without merging the slots or a per-lesson query.
    lesson_slot_counts = {}
    if selected_lesson is None and day_lessons:
        lesson_slot_counts = {
            row['lesson_id']: row['n']
            for row in Grade.objects.filter(
                student__school=school,
                student__class_name=class_name,
                student__is_archived=False,
                subject=subject,
                period='Холҳои ҷорӣ (Онлайн)',
                date=selected_date,
                assessment__isnull=True,
            ).values('lesson_id').annotate(n=Count('id'))
        }

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
            'grade_count': lesson_slot_counts.get(l.id, 0),
        })

    # Saved per-student results for each assessment: {assessment_id: {student_id: display}}
    # Display follows the work type's semantics: paired-grade works show the
    # journal pair ('8/9'), legacy/untyped rows show the combined score.
    assessment_grade_map = {}
    # Attendance/behavior stored on assessment-linked rows: {assessment_id:
    # {student_id: {att, beh}}} — same queryset, no extra query. Control mode
    # renders these into the Давомат/Тарбия selects.
    assessment_marks = {}
    if control_assessments:
        sem_by_id = {}
        for a in Assessment.objects.filter(id__in=[a['id'] for a in control_assessments]):
            sem_by_id[a.id] = result_semantics_for(a)
        for g in Grade.objects.filter(
            student__in=students,
            subject=subject,
            assessment_id__in=[a['id'] for a in control_assessments],
        ).select_related('assessment').order_by('pk'):
            # First-pk-wins per (assessment, student): the same survivor
            # canonical_grade_rows and get_or_create_grade keep.
            display_map = assessment_grade_map.setdefault(g.assessment_id, {})
            if g.student_id not in display_map:
                display_map[g.student_id] = result_display(
                    g, sem_by_id.get(g.assessment_id)
                )
            if g.attendance or g.behavior_score is not None:
                marks = assessment_marks.setdefault(g.assessment_id, {})
                if g.student_id not in marks:
                    marks[g.student_id] = {
                        'att': g.attendance or '',
                        'beh': g.behavior_score if g.behavior_score is not None else '',
                    }
        # Behavior display chain for control mode, same semantics as Ҷорӣ:
        # stored mark on THIS assessment -> latest subject behavior before
        # the assessment's own date -> neutral 5. Display-only: the value
        # posts only when the teacher actually changes the select.
        for a in control_assessments:
            adate = datetime.date.fromisoformat(a['date'])
            per_student = assessment_marks.setdefault(a['id'], {})
            for s in students:
                entry = per_student.setdefault(s.id, {'att': '', 'beh': ''})
                if entry['beh'] == '':
                    entry['beh'] = next(
                        (b for d, b in prior_behavior_hist.get(s.id, ())
                         if d < adate),
                        5,
                    )

    today = timezone.localdate()
    # The teacher's own group (таҳсил дар хона гуруҳи included) shifts the
    # journal norm to that group's weekly hours when they differ —
    # `journal_group` was resolved above with the student roster.
    journal = _journal_norm_status(
        class_name, subject, class_subject, students,
        today, exempt_ids=set(exemption_map), group=journal_group)
    from .norm_pace import norm_pace_context
    pace = norm_pace_context(
        class_name, subject, class_subject, students,
        today, exemption_map, journal['per_student'],
        journal['absences'], group=journal_group)
    for s in students:
        s.pace = pace['by_id'].get(s.id)

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
        'subject_ungraded': not is_graded_subject(subject),
        'qualitative_choices': QUALITATIVE_CHOICES,
        'quarter_numbers': [1, 2, 3, 4],
        'locked_quarters': locked_quarters,
        'daily_quarter_locked': daily_quarter_locked,
        'class_subject_id': class_subject.id if class_subject else None,
        'teacher_group': journal_group,
        'lessons': lessons_payload,
        'selected_lesson': selected_lesson,
        'selected_lesson_has_data': next(
            (l['has_data'] for l in lessons_payload
             if selected_lesson and l['id'] == selected_lesson.id), False
        ),
        'control_assessments': control_assessments,
        'assessment_grade_map': assessment_grade_map,
        'assessment_marks': assessment_marks,
        'date_is_control': date_is_control,
        'future_date': selected_date > timezone.localdate(),
        'holiday_date': not is_school_day(selected_date),
        'work_type_choices': work_type_choices(),
        'exemption_map': exemption_map,
        # Quarter-level journal completion (Пурра/Қисман/Холӣ) + the
        # time-aware norm percentage — display-only, never gates grade entry.
        'journal': journal,
        'pace': pace,
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
    # Resolve the regular teacher's group context once: active-group
    # existence and own membership are shared by the student list, the
    # `grouped` flag and the lesson/assessment filters below instead of
    # being re-queried by each helper.
    is_regular = _is_regular_teacher(request.user)
    has_groups = False
    teacher_group = None
    if class_subject is not None and is_regular:
        has_groups = class_subject.groups.filter(_canonical_group_q()).exists()
        teacher_group = teacher_group_for(request.user, class_subject)
    if class_subject is not None:
        students = list(visible_students_for(
            request.user, class_subject,
            group=teacher_group, has_groups=has_groups))
    else:
        students = sort_students(_exclude_home_students(
            Student.objects.filter(school=school, class_name=class_name)))
    # «Дар давлати дигар таҳсил мекунад» exemptions: blue indicators in
    # the student column + removal from the norm denominator (one query).
    exemption_map = active_exemption_map(s.id for s in students)
    # In the таҳсил дар хона journal the home status is not an exemption —
    # these students ARE this journal's roster.
    if teacher_group is not None and teacher_group.is_home:
        exemption_map = {
            sid: ex for sid, ex in exemption_map.items()
            if ex.reason != 'home'}
    for s in students:
        s.exemption = exemption_map.get(s.id)

    # A regular teacher who reached this point passed the can_view gate
    # above, which for ROLE_TEACHER delegates to can_edit_grade_journal —
    # the edit result is already proven and need not be re-queried.
    can_edit = (
        True if is_regular
        else can_edit_grade_journal(request.user, school, class_name, subject)
    )
    locked_quarters = _locked_quarters_for_year(
        school, class_name, subject, academic_year_start(month_start))

    # On grouped subjects a regular teacher sees only their own group's
    # lesson columns plus legacy group=NULL slots.
    grouped = class_subject is not None and is_regular and (
        has_groups or teacher_group is not None)

    lessons_by_date = defaultdict(list)
    if class_subject is not None:
        lesson_qs = Lesson.objects.filter(
            class_subject=class_subject,
            date__range=(month_start, month_end),
        ).select_related('group')
        if grouped:
            lesson_qs = lesson_qs.filter(
                _visible_lessons_q(request.user, class_subject,
                                   group=teacher_group))
        elif is_regular:
            # No own group: home-education lessons belong to the home
            # journal, never to the regular class journal.
            lesson_qs = lesson_qs.exclude(group__is_home=True)
        for l in lesson_qs.order_by('date', 'lesson_number'):
            lessons_by_date[l.date].append(l)

    # Control assessments in the month, keyed by (date, lesson_id) so each
    # column can show its own Назорат marker. A column is writable as
    # control only when its slot holds exactly one untyped assessment —
    # typed works keep their dedicated daily-journal entry so work-type
    # semantics are never bypassed from the month grid.
    control_slots = set()
    ctl_by_slot = defaultdict(list)
    if class_subject is not None:
        assessment_qs = Assessment.objects.filter(
            class_subject=class_subject,
            category='control',
            date__range=(month_start, month_end),
        )
        if grouped:
            assessment_qs = assessment_qs.filter(
                _visible_assessments_q(request.user, class_subject,
                                       group=teacher_group)
            )
        for a in assessment_qs:
            control_slots.add((a.date, a.lesson_id))
            ctl_by_slot[(a.date, a.lesson_id)].append(a)
    slot_simple_ctl = {
        key: rows[0].id for key, rows in ctl_by_slot.items()
        if len(rows) == 1 and not rows[0].work_type
    }

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
        for g in canonical_grade_rows(month_grades):
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
    today = timezone.localdate()
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
            col['assessment_id'] = slot_simple_ctl.get(
                (d, col['lesson_id']))
            # A cell is writable in an unlocked quarter for a plain slot,
            # and also for a slot whose single control assessment is
            # untyped — writes on it land on the assessment-linked row
            # through the shared journal pipeline.
            col['editable'] = (
                can_edit
                and (not col['control'] or col['assessment_id'] is not None)
                and day_quarter not in locked_quarters
                and d <= today
                and is_school_day(d)
            )
            col['can_toggle'] = (
                can_edit and day_quarter not in locked_quarters
                and is_school_day(d)
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

    journal = _journal_norm_status(
        class_name, subject, class_subject, students,
        today, exempt_ids=set(exemption_map), group=teacher_group)
    from .norm_pace import norm_pace_context
    pace = norm_pace_context(
        class_name, subject, class_subject, students,
        today, exemption_map, journal['per_student'],
        journal['absences'], group=teacher_group)
    for s in students:
        s.pace = pace['by_id'].get(s.id)

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
        'class_subject_id': class_subject.id if class_subject else '',
        'teacher_group': teacher_group,
        'subject_ungraded': not is_graded_subject(subject),
        # Quarter-level journal completion (Пурра/Қисман/Холӣ) + the
        # time-aware norm percentage — distinct metrics, one aggregate query.
        'journal': journal,
        'pace': pace,
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
    grades = sorted(
        Grade.objects.filter(
            student__in=students,
            subject=subject,
            period='Холҳои ҷорӣ (Онлайн)',
            date__range=(month_start, month_end),
        ).select_related('student', 'assessment', 'lesson'),
        key=lambda g: (
            name_key(g.student.full_name), g.date,
            g.lesson.lesson_number if g.lesson_id else -1,
            g.pk))

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


_MEMO_MISS = object()


def _save_journal_cell(user, item, ctx=None, affected=None):
    """Apply one staged journal cell edit; returns (ok, message).

    `affected`, when given, is a dict that receives an entry
    {student_id: {'student': ..., 'subject': ..., 'quarters': set()}} for
    every successfully written cell so the caller can report which derived
    quarter values changed (the recalc itself runs via Grade signals).

    Shared write pipeline for monthly_save and daily_save — identical
    permission, lesson-scope, control-mode and quarter-lock checks as the
    per-cell save_grade_ajax path, so batching adds no second business
    rule. Only the daily field whitelist (score/attendance/behavior_score/
    sticker) is writable; assessment-linked and quarterly cells keep their
    dedicated endpoints.

    `ctx` is an optional per-request memo dict shared across one batch.
    Permission, ClassSubject, group-membership, lesson-scope and
    quarter-lock lookups depend only on (user, school, class, subject,
    date, lesson) — fixed inputs within a single request — so each
    distinct combination is resolved once and reused. Grade rows are
    never cached: every item performs its own fetch-modify-write exactly
    as before. When ctx is omitted each item gets a fresh dict, which is
    identical to the old per-item lookups.
    """
    ctx = {} if ctx is None else ctx
    try:
        skey = ('student', item.get('student_id'))
        student = ctx.get(skey)
        if student is None:
            student = Student.all_objects.select_related('school').get(
                pk=item.get('student_id'))
            ctx[skey] = student
    except (Student.DoesNotExist, AttributeError, ValueError, TypeError):
        return False, 'Хонанда ёфт нашуд.'
    if student.is_archived:
        return False, 'Хонанда ба бойгонӣ гузаронида шудааст.'
    subject = normalize_subject(str(item.get('subject', '')))
    class_name = normalize_class_name(student.class_name)
    pkey = ('can_edit', student.school_id, class_name, subject)
    allowed = ctx.get(pkey, _MEMO_MISS)
    if allowed is _MEMO_MISS:
        allowed = can_edit_grade_journal(
            user, student.school, class_name, subject)
        ctx[pkey] = allowed
    if not allowed:
        return False, 'Дастрасӣ манъ аст.'
    ckey = ('cs', student.school_id, class_name, subject)
    cs = ctx.get(ckey, _MEMO_MISS)
    if cs is _MEMO_MISS:
        cs = _class_subject_for(student.school, class_name, subject)
        ctx[ckey] = cs
    if cs is not None:
        wkey = ('write_scope', cs.pk)
        writable = ctx.get(wkey, _MEMO_MISS)
        if writable is _MEMO_MISS:
            # Mirrors _teacher_can_write_student: privileged roles and
            # ungrouped CS may write anyone; a grouped CS limits a regular
            # teacher to students of their own active group.
            if _is_regular_teacher(user) and cs.groups.filter(
                    is_active=True).exists():
                writable = frozenset(
                    SubjectGroupMembership.objects.filter(
                        class_subject=cs,
                        group__is_active=True,
                        group__teacher__user=user,
                    ).values_list('student_id', flat=True)
                )
            else:
                writable = True
            ctx[wkey] = writable
        if writable is not True and student.pk not in writable:
            return False, 'Дастрасӣ манъ аст.'
    try:
        date = datetime.date.fromisoformat(str(item.get('date', '')))
        scope_key = (
            'scope', student.school_id, class_name, subject,
            date.isoformat(), str(item.get('lesson_id') or ''),
        )
        outcome = ctx.get(scope_key, _MEMO_MISS)
        if outcome is _MEMO_MISS:
            try:
                outcome = (True, _resolve_daily_scope(
                    student, subject, date, item.get('lesson_id') or '',
                    user=user, allow_control=True,
                ))
            except ValueError as e:
                outcome = (False, str(e))
            ctx[scope_key] = outcome
        if not outcome[0]:
            raise ValueError(outcome[1])
        lesson = outcome[1]
        qkey = ('qlock', student.school_id, class_name, subject,
                get_date_quarter(date), academic_year_start(date))
        locked = ctx.get(qkey, _MEMO_MISS)
        if locked is _MEMO_MISS:
            locked = is_quarter_locked(
                student.school, class_name, subject, get_date_quarter(date),
                academic_year_start(date))
            ctx[qkey] = locked
        if locked:
            raise ValueError('Ин чоряк баста шудааст.')
        # Slot-mode resolution: when the (date, lesson) column is marked
        # Назорат, the cell writes to the control assessment's linked row —
        # a control slot can never acquire a stray plain row. Typed works
        # keep their dedicated daily-journal entry (component semantics
        # are never bypassed from the month grid).
        assessment = None
        if cs is not None:
            akey = ('slot_ctl', student.school_id, class_name, subject,
                    date.isoformat(), lesson.pk if lesson else None)
            assessment = ctx.get(akey, _MEMO_MISS)
            if assessment is _MEMO_MISS:
                assessment = Assessment.objects.filter(
                    class_subject=cs, category='control',
                    date=date, lesson=lesson).first()
                ctx[akey] = assessment
            if assessment is not None and assessment.work_type:
                raise ValueError('Ин сана кори санҷишии махсус дорад — холҳоро дар журнали ҳаррӯза ворид кунед.')
        grade, _ = get_or_create_grade(
            student=student,
            subject=subject,
            period='Холҳои ҷорӣ (Онлайн)',
            date=date,
            assessment=assessment,
            lesson=lesson,
            defaults={'score': None, 'attendance': None,
                      'behavior_score': None, 'sticker': None},
        )
        values = {
            k: item[k]
            for k in ('score', 'attendance', 'behavior_score', 'sticker')
            if k in item
        }
        if not is_graded_subject(subject):
            values.pop('score', None)
        kept = _apply_grade_fields(grade, values)
        if kept and assessment is not None and 'score' in values:
            # A grid cell is always a plain single score — stale slash
            # components or test points from an earlier entry must not
            # shadow the new value.
            if grade.components or grade.points_achieved is not None \
                    or grade.points_possible is not None:
                grade.components = None
                grade.points_achieved = None
                grade.points_possible = None
                grade.save(update_fields=[
                    'components', 'points_achieved', 'points_possible'])
    except (ValueError, TypeError) as e:
        return False, str(e)
    if affected is not None:
        info = affected.setdefault(
            student.pk, {'student': student, 'subject': subject,
                         'quarters': set()})
        info['quarters'].add(get_date_quarter(date))
    return True, ''


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
    from .signals import defer_quarter_recalc  # local import avoids cycles
    deferred = set()
    try:
        with defer_quarter_recalc() as deferred:
            for item in payload:
                key = item.get('key') if isinstance(item, dict) else None
                ok, message = _save_journal_cell(request.user, item)
                if ok:
                    saved += 1
                    results.append({'key': key, 'ok': True})
                else:
                    results.append({'key': key, 'ok': False, 'message': message})
    finally:
        # One bulk recalc for every (student, subject, quarter) the
        # batch touched — also drained on a partway abort so
        # committed rows never sit beside a stale stored quarter.
        _drain_deferred_quarter_recalc(deferred)
    return JsonResponse(
        {'success': True, 'saved': saved, 'results': results}, status=200
    )


@login_required
@require_POST
def daily_save(request):
    """Batched daily-journal write — one POST for many changed fields.

    Transport-level batching of the save_grade_ajax daily path: each item
    is keyed by the input's stable name (e.g. 'score_42') so the page can
    map per-item results back to the exact field, and routed through the
    same _save_journal_cell pipeline as monthly_save. Assessment-linked
    (ascore_) and quarterly cells are never sent here by the client.

    Atomicity: the whole batch commits in a single transaction instead of
    ~one autocommit per cell — on SQLite that is one write-lock window per
    batch rather than N interleaved ones. Per-item business rejections
    (validation, permission, scope, lock) are caught inside the helper and
    reported per item without aborting the batch; any unexpected failure
    rolls the transaction back and returns success=False, and because no
    item is reported saved in that case the client keeps the entire batch
    pending for retry — a committed-never-reported state is impossible.
    """
    try:
        payload = json.loads(request.POST.get('payload', '[]'))
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'success': False, 'message': 'Формати нодуруст.'}, status=200)
    if not isinstance(payload, list):
        return JsonResponse({'success': False, 'message': 'Формати нодуруст.'}, status=200)

    results = []
    saved = 0
    # Per-batch memo: invariant lookups (permission, ClassSubject, scope,
    # lock) are resolved once per distinct key inside this transaction
    # instead of once per item. Request-scoped only — nothing crosses
    # requests, and Grade writes are never cached.
    ctx = {}
    affected = {}
    try:
        with transaction.atomic():
            # SQLite WAL reservation (B3): a deferred transaction upgrades
            # to the write lock on its first statement. If that statement
            # were a read, a WAL snapshot would be pinned first and the
            # later write could fail with SQLITE_BUSY_SNAPSHOT once a
            # concurrent writer commits. This zero-row UPDATE must run
            # before every ORM query below so the reservation is taken
            # while no snapshot exists; it matches no rows, writes
            # nothing, and bypasses model signals (a no-op needs no
            # cache invalidation).
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE portal_grade SET score = score WHERE id = -1'
                )
            from .signals import defer_quarter_recalc  # local import
            with defer_quarter_recalc() as deferred:
                for item in payload:
                    if not isinstance(item, dict):
                        results.append({
                            'key': None, 'ok': False,
                            'message': 'Формати нодуруст.',
                        })
                        continue
                    ok, message = _save_journal_cell(request.user, item, ctx,
                                                     affected)
                    if ok:
                        saved += 1
                        results.append({'key': item.get('key'), 'ok': True})
                    else:
                        results.append({
                            'key': item.get('key'), 'ok': False,
                            'message': message,
                        })
            # One bulk pass over the collected (student, subject, quarter)
            # keys inside the same transaction — identical result to the
            # per-save signal path without re-running the lookups per row.
            recalculate_quarter_grades_bulk(deferred)
    except Exception:
        # The transaction rolled back — nothing was written, so nothing may
        # be reported as saved; the client keeps all items pending.
        return JsonResponse(
            {'success': False, 'message': 'Сабт нашуд. Такрор кунед.'},
            status=200,
        )
    # Quarter cells are read-only now — return the recalculated value for
    # every student the batch touched so the journal refreshes the derived
    # Ч/Нимсола/Солона/Ҷамъбастӣ cells without a reload.
    students_payload = {}
    infos = list(affected.values())
    payloads = _live_quarter_payloads(infos)
    for info in infos:
        st = info['student']
        qmap, live_flags = payloads.get(st.pk, ({}, set()))
        entry = calc_quarterly(qmap, live_flags)
        entry['quarters'] = [
            {'quarter': q, 'value': qmap.get(q), 'live': q in live_flags}
            for q in sorted(info['quarters'])
        ]
        students_payload[str(st.id)] = entry
    return JsonResponse(
        {'success': True, 'saved': saved, 'results': results,
         'students': students_payload}, status=200
    )


@login_required
def add_remove_subject(request, school_id, class_name):
    school = get_object_or_404(School, id=school_id)
    if not is_academic_school(school) or not _can_manage_school(request.user, school):
        return redirect('dashboard')
    class_name = normalize_class_name(class_name)

    if request.method == 'POST':
        action = request.POST.get('action')
        subject = request.POST.get('subject', '').strip()
        subject = normalize_subject(subject)
        if action == 'add' and subject:
            with transaction.atomic():
                # Registry first: a canonical Subject + class-scoped availability
                # keep the subject alive across ensure_class_subjects() sweeps —
                # the ClassSubject row alone used to be silently deactivated.
                # Official curriculum names need no registry entry.
                canonical = None
                if subject not in official_subjects():
                    canonical = canonical_subject_for(subject, user=request.user)
                    if canonical is not None:
                        SubjectAvailability.objects.update_or_create(
                            subject=canonical, school=school, class_name=class_name,
                            defaults={'is_active': True},
                        )
                else:
                    # Re-adding a curriculum subject clears any opt-out an
                    # earlier 'remove' wrote (see below).
                    SubjectAvailability.objects.filter(
                        subject__name=subject, school=school,
                        class_name=class_name, is_active=False,
                    ).update(is_active=True)
                obj, _ = get_or_create_unique(
                    ClassSubject,
                    school=school,
                    class_name=class_name,
                    subject=subject,
                    defaults={'is_active': True, 'is_default': False}
                )
                if not obj.is_active:
                    obj.is_active = True
                    obj.save(update_fields=['is_active'])
                # Explicit re-add lifts an approved deactivation so the next
                # ensure_class_subjects() sweep keeps the row active.
                reverse_approved_deactivations(school, class_name, subject)
        elif action == 'remove' and subject:
            ClassSubject.objects.filter(
                school=school,
                class_name=class_name,
                subject=subject
            ).update(is_active=False)
            # Always record an explicit opt-out (not only when a canonical
            # Subject already exists) so neither ensure_class_subjects() nor
            # the class-move reactivation path can re-add a subject that was
            # intentionally removed.
            canonical = canonical_subject_for(subject, user=request.user)
            if canonical is not None:
                SubjectAvailability.objects.filter(
                    subject=canonical, school=school, class_name=class_name
                ).update(is_active=False)
                SubjectAvailability.objects.get_or_create(
                    subject=canonical, school=school, class_name=class_name,
                    defaults={'is_active': False},
                )
    return redirect('class_detail', school_id=school.id, class_name=class_name)


def _registry_class_choices(schools):
    """Class names selectable for the given schools."""
    return sorted({
        c for c in ClassSubject.objects.filter(
            school__in=schools, is_active=True
        ).values_list('class_name', flat=True)
    } | {
        c for c in Student.objects.filter(
            school__in=schools
        ).values_list('class_name', flat=True)
    }, key=class_numeric_part)


def _registry_subject_choices():
    """Selectable names: official curriculum ∪ canonical registry."""
    registry_names = Subject.objects.filter(
        is_active=True).values_list('name', flat=True)
    return sorted(set(official_subjects()) | set(registry_names))


def _registry_name_taken(name):
    """True when the normalized name is already selectable in the list."""
    return name in official_subjects() or Subject.objects.filter(
        name=name).exists()


def _configured_classes_for(subject_name, school):
    """Classes of `school` where the subject is currently available.

    Sources: an active school-wide/global availability row, active
    class-scoped availability rows (minus explicit opt-outs), or an already
    active ClassSubject row (e.g. an official curriculum subject).
    Returns (configured_set, whole_school_bool).
    """
    if not school:
        return set(), False
    base = SubjectAvailability.objects.filter(
        subject__name=subject_name, subject__is_active=True)
    whole_school = base.filter(
        Q(school__isnull=True, class_name='') |
        Q(school=school, class_name=''),
        is_active=True,
    ).exists()
    active = set(base.filter(
        school=school, is_active=True,
    ).exclude(class_name='').values_list('class_name', flat=True))
    opted_out = set(base.filter(
        school=school, is_active=False,
    ).exclude(class_name='').values_list('class_name', flat=True))
    cs_active = set(ClassSubject.objects.filter(
        school=school, subject=subject_name, is_active=True,
    ).values_list('class_name', flat=True))
    return (active - opted_out) | cs_active, whole_school


def _pending_removal_classes(subject_name, school):
    """Classes with a pending deactivation request for this subject."""
    if not school:
        return set()
    return set(ClassSubject.objects.filter(
        school=school, subject=subject_name,
        subjectdeactivationrequest__status='pending',
    ).values_list('class_name', flat=True))


def _wizard_subject_name(request):
    """Resolve the subject being configured from POST: picked or new."""
    pick = normalize_subject(request.POST.get('pick_name', ''))
    new = normalize_subject(request.POST.get('new_name', ''))
    if pick:
        return pick, False
    return new, bool(new)


@login_required
def subject_registry(request):
    """Controlled 'Фани нав' workflow on top of the canonical registry.

    Steps (server-driven; state is carried via hidden inputs):
      select  — pick an existing subject or create a new canonical one
      classes — choose the school/classes scope (zavuch locked to own school)
      preview — Пешнамоиш before saving
      confirm — double safety confirmation
      save    — apply availability and return to Тақсимоти дарсҳо

    A plain POST without 'action' keeps the legacy single-form behaviour
    (name + scope + schools + classes) used by older tests and scripts.
    """
    user = request.user
    is_zavuch = _is_zavuch(user)
    privileged = user.is_superuser or get_user_role(user) in (
        settings.ROLE_DIRECTOR, settings.ROLE_PRINCIPAL)
    if not (privileged or is_zavuch):
        return HttpResponse('Дастрасӣ манъ аст.', status=403)

    user_school = get_user_school(user)
    restricted = is_zavuch and not privileged
    if restricted:
        schools = [user_school] if user_school else []
        allow_global = False
    else:
        schools = list(School.objects.filter(id__in=academic_school_ids()).order_by('name'))
        allow_global = True

    base_ctx = {
        'schools': schools,
        'allow_global': allow_global,
        'user_school': user_school,
        'restricted': restricted,
    }

    def render_select(extra=None):
        ctx = dict(base_ctx)
        ctx.update({
            'step': 'select',
            'subject_names': _registry_subject_choices(),
            'registry': (
                Subject.objects.filter(is_active=True)
                .prefetch_related('availabilities__school')
                .order_by('name')
            ),
        })
        if extra:
            ctx.update(extra)
        return render(request, 'portal/subject_registry.html', ctx)

    def wizard_state():
        """Scope/class selections re-derived (and re-validated) per step."""
        scope = request.POST.get('scope', 'classes')
        if restricted:
            scope = 'classes'
            target_schools = [user_school] if user_school else []
        else:
            ids = {str(i) for i in request.POST.getlist('schools')}
            target_schools = [s for s in schools if str(s.id) in ids]
            if scope == 'all' and not allow_global:
                scope = 'classes'
        class_names = [normalize_class_name(c)
                       for c in request.POST.getlist('classes') if c.strip()]
        if scope == 'classes' and target_schools:
            allowed = set(_registry_class_choices(target_schools))
            class_names = [c for c in class_names if c in allowed]
        return scope, target_schools, class_names

    def render_classes(name, is_new, scope=None, class_names=None, extra=None):
        scope = scope or 'classes'
        if restricted:
            target_schools = [user_school] if user_school else []
            checked_school_ids = [s.id for s in target_schools]
        else:
            ids = {str(i) for i in request.POST.getlist('schools')}
            if not ids and user_school:
                ids = {str(user_school.id)}
            target_schools = [s for s in schools if str(s.id) in ids]
            checked_school_ids = [s.id for s in target_schools]
        class_choices = _registry_class_choices(schools)
        configured, pending, whole_schools = set(), set(), []
        if not is_new:
            for sch in target_schools:
                conf, whole = _configured_classes_for(name, sch)
                if whole:
                    whole_schools.append(sch.name)
                    conf |= set(_registry_class_choices([sch]))
                configured |= conf
                pending |= _pending_removal_classes(name, sch)
        checked = set(class_names or []) | configured
        ctx = dict(base_ctx)
        ctx.update({
            'step': 'classes',
            'subj_name': name,
            'is_new': is_new,
            'scope': scope,
            'checked_school_ids': checked_school_ids,
            'class_choices': class_choices,
            'checked_classes': checked,
            'configured': configured,
            'pending_removal': pending,
            'whole_schools': whole_schools,
        })
        if extra:
            ctx.update(extra)
        return render(request, 'portal/subject_registry.html', ctx)

    def render_preview(name, is_new, scope, target_schools, class_names):
        ctx = dict(base_ctx)
        ctx.update({
            'step': 'preview',
            'subj_name': name,
            'is_new': is_new,
            'scope': scope,
            'target_schools': target_schools,
            'class_names': class_names,
        })
        return render(request, 'portal/subject_registry.html', ctx)

    def render_confirm(name, is_new, scope, target_schools, class_names):
        ctx = dict(base_ctx)
        ctx.update({
            'step': 'confirm',
            'subj_name': name,
            'is_new': is_new,
            'scope': scope,
            'target_schools': target_schools,
            'class_names': class_names,
        })
        return render(request, 'portal/subject_registry.html', ctx)

    def scope_error(scope, target_schools, class_names):
        """Validation shared by preview/confirm/save steps."""
        if scope == 'all':
            return None if allow_global else 'Дастрасӣ манъ аст.'
        if not target_schools:
            return 'Муассиса ё синф интихоб нашудааст.'
        if scope == 'classes' and not class_names:
            return 'Синфҳоро интихоб кунед.'
        return None

    if request.method == 'POST':
        action = request.POST.get('action')

        # Legacy single-form save (no 'action'): identical to the original
        # behaviour so existing tests and scripts keep working.
        if action is None:
            name = request.POST.get('name', '').strip()
            scope = request.POST.get('scope', 'classes')
            school_ids = request.POST.getlist('schools')
            class_names = request.POST.getlist('classes')
            subject = canonical_subject_for(name, user=user) if name else None
            if subject is None:
                messages.error(request, 'Номи фан холӣ аст.')
                return redirect('subject_registry')

            if restricted:
                school_ids = [str(user_school.id)] if user_school else []
                if scope == 'all':
                    scope = 'school'

            if scope == 'all' and allow_global:
                SubjectAvailability.objects.update_or_create(
                    subject=subject, school=None, class_name='',
                    defaults={'is_active': True},
                )
            else:
                targets = [s for s in schools if str(s.id) in school_ids]
                saved = False
                for sch in targets:
                    if scope == 'classes':
                        for cn in class_names:
                            cn = normalize_class_name(cn)
                            with transaction.atomic():
                                SubjectAvailability.objects.update_or_create(
                                    subject=subject, school=sch, class_name=cn,
                                    defaults={'is_active': True},
                                )
                                reverse_approved_deactivations(
                                    sch, cn, subject.name)
                                ensure_class_subjects(sch, cn)
                            saved = True
                    else:
                        SubjectAvailability.objects.update_or_create(
                            subject=subject, school=sch, class_name='',
                            defaults={'is_active': True},
                        )
                        saved = True
                if not saved:
                    messages.error(request, 'Муассиса ё синф интихоб нашудааст.')
                    return redirect('subject_registry')
            messages.success(request, f'Фан «{subject.name}» захира шуд.')
            return redirect('subject_registry')

        # ---------------- wizard steps ----------------
        if action == 'choose_subject':
            name, is_new = _wizard_subject_name(request)
            if not name:
                messages.error(request, 'Фанро интихоб кунед.')
                return redirect('subject_registry')
            if is_new:
                if _registry_name_taken(name):
                    messages.error(
                        request,
                        'Ин фан аллакай дар рӯйхат мавҷуд аст. '
                        'Лутфан фанни мавҷударо интихоб кунед.')
                    return redirect('subject_registry')
            elif not _registry_name_taken(name):
                messages.error(request, 'Фанро интихоб кунед.')
                return redirect('subject_registry')
            return render_classes(name, is_new)

        if action == 'back_classes':
            name, is_new = _wizard_subject_name(request)
            if not name:
                return redirect('subject_registry')
            scope = request.POST.get('scope', 'classes')
            class_names = [normalize_class_name(c)
                           for c in request.POST.getlist('classes') if c.strip()]
            return render_classes(name, is_new, scope=scope,
                                  class_names=class_names)

        if action in ('preview', 'back_preview', 'confirm', 'save'):
            name, is_new = _wizard_subject_name(request)
            if not name:
                messages.error(request, 'Фанро интихоб кунед.')
                return redirect('subject_registry')
            scope, target_schools, class_names = wizard_state()
            error = scope_error(scope, target_schools, class_names)
            if error:
                if action == 'back_preview':
                    return render_preview(name, is_new, scope,
                                          target_schools, class_names)
                messages.error(request, error)
                return render_classes(name, is_new, scope=scope,
                                      class_names=class_names)
            if action == 'preview':
                return render_preview(name, is_new, scope,
                                      target_schools, class_names)
            if action == 'back_preview':
                return render_preview(name, is_new, scope,
                                      target_schools, class_names)
            if action == 'confirm':
                return render_confirm(name, is_new, scope,
                                      target_schools, class_names)

            # action == 'save' — the second confirmation screen is the only
            # source of ack=yes; anything else lands back on it.
            if request.POST.get('ack') != 'yes':
                return render_confirm(name, is_new, scope,
                                      target_schools, class_names)
            subject = canonical_subject_for(name, user=user)
            if scope == 'all':
                SubjectAvailability.objects.update_or_create(
                    subject=subject, school=None, class_name='',
                    defaults={'is_active': True},
                )
            else:
                for sch in target_schools:
                    if scope == 'classes':
                        for cn in class_names:
                            with transaction.atomic():
                                SubjectAvailability.objects.update_or_create(
                                    subject=subject, school=sch, class_name=cn,
                                    defaults={'is_active': True},
                                )
                                reverse_approved_deactivations(
                                    sch, cn, subject.name)
                                ensure_class_subjects(sch, cn)
                    else:
                        SubjectAvailability.objects.update_or_create(
                            subject=subject, school=sch, class_name='',
                            defaults={'is_active': True},
                        )
            messages.success(request, 'Фан бо муваффақият илова карда шуд.')
            if user.is_superuser and target_schools:
                return redirect(
                    f'{reverse("lesson_allocation")}?school_id={target_schools[0].id}')
            return redirect('lesson_allocation')

        return redirect('subject_registry')

    return render_select()


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
    from .signals import defer_quarter_recalc  # local import avoids cycles
    deferred = set()
    try:
        with defer_quarter_recalc() as deferred:
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
                was_archived = (Student.all_objects
                                .filter(id=student_id, is_archived=True)
                                .exists())
                student, _ = Student.all_objects.update_or_create(
                    id=student_id,
                    defaults={'full_name': full_name, 'class_name': c_name,
                              'school': school, 'is_archived': False,
                              'archived_at': None}
                )
                if was_archived:
                    StudentMovement.objects.create(
                        school=school, student=student,
                        student_name=full_name, class_name=c_name,
                        gender=student.gender or '',
                        movement_type='arrival',
                        date=datetime.date.today(),
                        created_by=request.user)
    
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
    
    finally:
        # One bulk derived-grade pass for every (student, subject,
        # quarter) the import touched — also drained on a partway
        # abort so committed rows never sit beside a stale quarter.
        _drain_deferred_quarter_recalc(deferred)
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
            student = Student.all_objects.get(id=student_id)
        except Student.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Хонанда ёфт нашуд.'}, status=200)
        if student.is_archived:
            return JsonResponse({'success': False, 'message': 'Хонанда ба бойгонӣ гузаронида шудааст.'}, status=200)

        class_name = normalize_class_name(student.class_name)

        # Un-graded curriculum subjects keep attendance/conduct marks but
        # never accept scores or attestation writes.
        subject_graded = is_graded_subject(subject)

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
                if date > _journal_write_today():
                    return JsonResponse({'success': False, 'message': 'Санаи ояндада хол ё давомат гузошта намешавад.'}, status=200)
                if not is_school_day(date):
                    return JsonResponse({'success': False, 'message': 'Дар рӯзи таътил хол ё давомат гузошта намешавад.'}, status=200)
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

            if is_quarter_locked(
                    student.school, class_name, subject, lock_quarter,
                    academic_year_start(date)):
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

            if subject_graded and assessment is not None and ('score' in request.POST or points_p is not None):
                grade.score = parsed_score
                grade.components = parsed_components
                grade.points_achieved = points_a
                grade.points_possible = points_p

            values = {}
            if subject_graded and assessment is None and 'score' in request.POST:
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
            if not subject_graded:
                return JsonResponse({'success': False, 'message': 'Ин фан баҳогузорӣ намешавад.'}, status=200)
            try:
                quarter = int(quarter_str)
            except ValueError:
                return JsonResponse({'success': False, 'message': 'Чораки нодуруст.'}, status=200)

            # Quarter grades 1-4 are derived automatically from daily and
            # control-assessment grades — manual writes are rejected for
            # everyone. Only the attestation mark (quarter 0) stays editable.
            if quarter != 0:
                return JsonResponse({'success': False, 'message': 'Холи чоряк автоматӣ ҳисоб мешавад.'}, status=200)

            att_year = academic_year_start()
            if is_quarter_locked(
                    student.school, class_name, subject, quarter, att_year):
                return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

            try:
                score = _parse_score(score_raw)
            except ValueError as e:
                return JsonResponse({'success': False, 'message': str(e)}, status=200)

            if score is not None and 1 <= score <= 10:
                QuarterGrade.objects.update_or_create(
                    student=student,
                    class_name=class_name,
                    subject=subject,
                    quarter=0,
                    academic_year=att_year,
                    defaults={'att_grade': score}
                )
            else:
                QuarterGrade.objects.filter(
                    student=student,
                    class_name=class_name,
                    subject=subject,
                    quarter=0,
                    academic_year=att_year,
                ).delete()

            qmap = {}
            for g in QuarterGrade.objects.filter(
                    student=student, class_name=class_name, subject=subject,
                    academic_year=att_year):
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
    # recalculate_quarter_grades_bulk shares the single derived-grade
    # formula and lock handling with the automatic path — a quarter with no
    # remaining source scores loses its stored row. The button re-derives
    # the current academic year's cells.
    recalculate_quarter_grades_bulk(
        (student.pk, subject, q, academic_year_start())
        for student in students for q in (1, 2, 3, 4)
    )
    # Bulk writes bypass the QuarterGrade signals and no source Grade was
    # saved on this path, so ranking caches need explicit invalidation.
    from .signals import invalidate_ranking_caches  # local import
    invalidate_ranking_caches(None)
    # Return to the same journal state the button was pressed in: the
    # posted date and the Ҷадвали чорякҳо tab (hidden `tab` field).
    back = []
    date_str = request.POST.get('date', '').strip()
    try:
        back.append('date=' + datetime.date.fromisoformat(date_str).isoformat())
    except ValueError:
        pass
    tab = request.POST.get('tab', '') or request.GET.get('tab', '')
    if tab in ('daily', 'quarterly'):
        back.append(f'tab={tab}')
    url = reverse('grade_entry', kwargs={
        'school_id': school.id, 'class_name': class_name, 'subject': subject
    })
    if back:
        url += '?' + '&'.join(back)
    return redirect(url)


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
        own_group = teacher_group_for(request.user, cs) if is_regular else None
        # A таҳсил дар хона group does not switch the class into group
        # mode — but a teacher who owns ONLY the home group is scoped to it.
        cs_grouped = cs.groups.filter(_canonical_group_q()).exists() or (
            own_group is not None and own_group.is_home)

        try:
            date = datetime.date.fromisoformat(date_str) if date_str else (
                assessment.date if assessment is not None else datetime.date.today()
            )
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Санаи нодуруст.'}, status=200)
        if not is_school_day(date):
            return JsonResponse({'success': False, 'message': 'Дар рӯзи таътил кори санҷишӣ яратилмайди.'}, status=200)
        quarter = get_date_quarter(date)

        # Lock check covers both the (quarter, year) cell the assessment
        # currently sits in and the one it is being moved to (edits change
        # results; a date can cross an academic-year boundary).
        cells_to_check = {(quarter, academic_year_start(date))}
        if assessment is not None:
            cells_to_check.add(
                (assessment.quarter, academic_year_start(assessment.date)))
        for q, q_year in cells_to_check:
            if is_quarter_locked(
                    cs.school, cs.class_name, cs.subject, q, q_year):
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
                if date > _journal_write_today():
                    return JsonResponse({'success': False, 'message': 'Санаи ояндада санҷиш яратилмайди.'}, status=200)
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
        if _is_regular_teacher(request.user):
            own_group = teacher_group_for(request.user, cs)
            cs_grouped = cs.groups.filter(_canonical_group_q()).exists() or (
                own_group is not None and own_group.is_home)
            if cs_grouped and (own_group is None or assessment.lesson_id is None
                    or assessment.lesson.group_id != own_group.id):
                return JsonResponse({'success': False, 'message': 'Санҷиш ба гурӯҳи шумо тааллуқ надорад.'}, status=200)
        if is_quarter_locked(
                cs.school, cs.class_name, cs.subject, assessment.quarter,
                academic_year_start(assessment.date)):
            return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)
        deleted_quarter = assessment.quarter
        affected_ids = list(
            Grade.objects.filter(assessment=assessment).values_list('student_id', flat=True).distinct()
        )
        from .signals import defer_quarter_recalc  # local import
        with transaction.atomic(), defer_quarter_recalc() as deferred:
            # Deliberate, narrowly scoped exception to the "grades are never
            # silently deleted" rule: ONLY rows linked to this Assessment are
            # removed, and they must be deleted rather than detached — letting
            # Grade.assessment SET_NULL fire would silently convert control
            # results into ordinary Ҷорӣ daily grades on that date.
            Grade.objects.filter(assessment=assessment).delete()
            assessment.delete()
            recalculate_quarter_grades_bulk(deferred)
        # Return the recalculated quarter for every affected student so the
        # page can refresh quarter cells immediately — the deleted grades no
        # longer contribute to the projection.
        students_payload = {}
        infos = [
            {'student': st, 'subject': cs.subject}
            for st in Student.objects.filter(pk__in=affected_ids)
        ]
        payloads = _live_quarter_payloads(infos)
        for info in infos:
            st = info['student']
            qmap, live_flags = payloads.get(st.pk, ({}, set()))
            entry = calc_quarterly(qmap, live_flags)
            entry['quarter'] = deleted_quarter
            entry['quarter_value'] = qmap.get(deleted_quarter)
            entry['quarter_live'] = deleted_quarter in live_flags
            students_payload[str(st.id)] = entry
        return JsonResponse({'success': True, 'students': students_payload}, status=200)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=200)


@login_required
@require_POST
def assessment_toggle(request):
    """Mark/unmark one journal slot (date + optional lesson) as Назорат.

    Marking creates an auto-numbered, untitled, untyped control Assessment
    and re-links the slot's existing plain Grade rows to it, so marks the
    teacher already entered as Ҷорӣ become the control work's results in
    place — no re-typing. Unmarking detaches the linked rows back into
    plain Ҷорӣ rows and removes the single Assessment; scores,
    attendance and behavior are preserved in both directions.

    Slots with more than one control assessment are refused — they are
    managed in the daily journal picker. Permission, group-scope and
    quarter-lock rules mirror assessment_save/assessment_delete.
    """
    try:
        class_subject_id = request.POST.get('class_subject_id', '').strip()
        date_str = request.POST.get('date', '').strip()
        lesson_id = request.POST.get('lesson_id', '').strip()

        try:
            cs = ClassSubject.objects.select_related('school').get(pk=class_subject_id)
        except (ClassSubject.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Фан ёфт нашуд.'}, status=200)
        if not cs.is_active:
            return JsonResponse({'success': False, 'message': 'Фан фаъол нест.'}, status=200)
        if not can_edit_grade_journal(request.user, cs.school, cs.class_name, cs.subject):
            return JsonResponse({'success': False, 'message': 'Дастрасӣ барои тағйир додан манъ аст.'}, status=403)
        try:
            date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Санаи нодуруст.'}, status=200)
        quarter = get_date_quarter(date)
        if is_quarter_locked(
                cs.school, cs.class_name, cs.subject, quarter,
                academic_year_start(date)):
            return JsonResponse({'success': False, 'message': 'Ин чоряк баста шудааст.'}, status=200)

        lesson = None
        if lesson_id:
            try:
                lesson = Lesson.objects.get(pk=lesson_id)
            except (Lesson.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'success': False, 'message': 'Дарс ёфт нашуд.'}, status=200)
            if lesson.class_subject_id != cs.id or lesson.date != date:
                return JsonResponse({'success': False, 'message': 'Дарс ба ин сана ё фан тааллуқ надорад.'}, status=200)

        is_regular = _is_regular_teacher(request.user)
        own_group = teacher_group_for(request.user, cs) if is_regular else None
        # таҳсил дар хона group alone does not group-scope the CS, but a
        # teacher owning ONLY the home group works inside it.
        cs_grouped = cs.groups.filter(_canonical_group_q()).exists() or (
            own_group is not None and own_group.is_home)
        if cs_grouped and is_regular and (
                own_group is None or lesson is None
                or lesson.group_id != own_group.id):
            return JsonResponse({'success': False, 'message': 'Ин сутун ба гурӯҳи шумо тааллуқ надорад.'}, status=200)

        slot_qs = Assessment.objects.filter(
            class_subject=cs, category='control', date=date, lesson=lesson)
        slot_count = slot_qs.count()
        if slot_count > 1:
            return JsonResponse({'success': False, 'message': 'Ин сана чанд кори санҷишӣ дорад — дар журнали ҳаррӯза идора кунед.'}, status=200)

        from .signals import defer_quarter_recalc  # local import avoids cycles

        if slot_count == 1:
            assessment = slot_qs.first()
            if cs_grouped and is_regular and (
                    assessment.lesson_id is None
                    or assessment.lesson.group_id != own_group.id):
                return JsonResponse({'success': False, 'message': 'Санҷиш ба гурӯҳи шумо тааллуқ надорад.'}, status=200)
            with transaction.atomic(), defer_quarter_recalc() as deferred:
                for g in Grade.objects.filter(assessment=assessment).order_by('pk'):
                    sibling = Grade.objects.filter(
                        student=g.student, subject=g.subject, period=g.period,
                        date=g.date, lesson=g.lesson,
                        assessment__isnull=True,
                    ).exclude(pk=g.pk).first()
                    if sibling is None:
                        g.assessment = None
                        g.save()
                    else:
                        # A plain row already occupies the slot for this
                        # student — merge the control values into it (never
                        # overwriting what it already holds) and drop the
                        # linked row, so no duplicate Ҷорӣ rows are left.
                        changed = False
                        for field in ('score', 'attendance', 'behavior_score',
                                      'sticker', 'components',
                                      'points_achieved', 'points_possible'):
                            if getattr(sibling, field) in (None, '') \
                                    and getattr(g, field) not in (None, ''):
                                setattr(sibling, field, getattr(g, field))
                                changed = True
                        if changed:
                            sibling.save()
                        g.delete()
                assessment.delete()
                recalculate_quarter_grades_bulk(deferred)
            return JsonResponse({'success': True, 'control': False}, status=200)

        if date > _journal_write_today():
            return JsonResponse({'success': False, 'message': 'Санаи ояндада санҷиш яратилмайди.'}, status=200)
        if not is_school_day(date):
            return JsonResponse({'success': False, 'message': 'Дар рӯзи таътил кори санҷишӣ яратилмайди.'}, status=200)
        max_number = Assessment.objects.filter(
            class_subject=cs, quarter=quarter, category='control',
        ).aggregate(m=Max('number'))['m'] or 0
        with transaction.atomic(), defer_quarter_recalc() as deferred:
            assessment, created = get_or_create_unique(
                Assessment,
                class_subject=cs, date=date, category='control',
                number=max_number + 1,
                defaults={
                    'quarter': quarter,
                    'title': '',
                    'is_ajm': False,
                    'lesson': lesson,
                    'purpose': 'summative',
                    'work_type': '',
                    'summative_scope': 'none',
                },
            )
            converted = 0
            for g in Grade.objects.filter(
                    student__school=cs.school,
                    student__class_name=cs.class_name,
                    student__is_archived=False,
                    subject=cs.subject,
                    period='Холҳои ҷорӣ (Онлайн)',
                    date=date, lesson=lesson,
                    assessment__isnull=True).order_by('pk'):
                g.assessment = assessment
                g.save()
                converted += 1
            recalculate_quarter_grades_bulk(deferred)

        grades_payload = {}
        marks_payload = {}
        sem = result_semantics_for(assessment)
        for g in Grade.objects.filter(
                assessment=assessment).select_related('assessment').order_by('pk'):
            key = str(g.student_id)
            if key not in grades_payload:
                grades_payload[key] = result_display(g, sem)
            if g.attendance or g.behavior_score is not None:
                marks_payload[key] = {
                    'att': g.attendance or '',
                    'beh': g.behavior_score if g.behavior_score is not None else '',
                }
        return JsonResponse({'success': True, 'control': True,
                             'converted': converted,
                             'assessment': {
                                 'id': assessment.id,
                                 'label': _assessment_label(assessment),
                                 'number': assessment.number,
                                 'title': assessment.title,
                                 'is_ajm': assessment.is_ajm,
                                 'date': assessment.date.isoformat(),
                                 'lesson_id': assessment.lesson_id,
                                 'purpose': assessment.purpose,
                                 'work_type': assessment.work_type,
                                 'work_type_label': work_type_label(assessment.work_type),
                                 'summative_scope': assessment.summative_scope,
                                 'semantics': result_semantics_for(assessment).get('semantics'),
                                 'locked': False,
                             },
                             'grades': grades_payload,
                             'marks': marks_payload}, status=200)
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
        # A таҳсил дар хона group alone does NOT force group scoping: the
        # class teacher keeps creating shared (group=NULL) lessons, while
        # a regular teacher who owns ONLY the home group still gets it
        # attached — their journal is the home journal.
        group = None
        if cs.groups.filter(is_active=True).exists():
            if _is_regular_teacher(request.user):
                group = teacher_group_for(request.user, cs)
                if group is None and cs.groups.filter(_canonical_group_q()).exists():
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
        if not is_school_day(date):
            return JsonResponse({'success': False, 'message': 'Дар рӯзи таътил дарс яратилмайди.'}, status=200)

        if is_quarter_locked(
                cs.school, cs.class_name, cs.subject, get_date_quarter(date),
                academic_year_start(date)):
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
        if is_quarter_locked(
                cs.school, cs.class_name, cs.subject,
                get_date_quarter(lesson.date),
                academic_year_start(lesson.date)):
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


def _student_subject_quarter_scores(student, quarter):
    """Return ({normalized_subject: grade}, live_subjects) for `quarter`.

    Per-subject quarter value for the student personal page, computed with
    the same rules as the journal's quarter cells: a stored QuarterGrade
    for the student's class always wins; otherwise the quarter is projected
    live from the scored Grade rows through the shared
    _split_quarter_grades / calc_quarter_value pipeline (АҶЧ = АТ when no
    АҶМ exists; (АТ + АҶМ) / 2 otherwise, each assessment counted once).
    Subjects with no value for the quarter are omitted; `live_subjects`
    marks the ones shown as a projection rather than an official grade.
    """
    scores, live_subjects = {}, set()
    if quarter not in (1, 2, 3, 4):
        return scores, live_subjects
    ay_year = academic_year_start()
    official = {}
    for qg in QuarterGrade.objects.filter(
            student=student,
            class_name=normalize_class_name(student.class_name),
            quarter=quarter, grade__isnull=False,
            academic_year=ay_year):
        official[normalize_subject(qg.subject)] = qg.grade
    grade_pool = defaultdict(list)
    for g in Grade.objects.filter(
            student=student, period='Холҳои ҷорӣ (Онлайн)',
            score__isnull=False).select_related('assessment'):
        grade_pool[normalize_subject(g.subject)].append(g)
    for subj, rows in grade_pool.items():
        if subj in official:
            continue
        value = calc_quarter_value(
            *_split_quarter_grades(rows, quarter, ay_year))
        if value is not None:
            scores[subj] = std_round(value)
            live_subjects.add(subj)
    scores.update(official)
    return scores, live_subjects


def _all_student_gpas():
    """Return {student_id: overall_gpa} for every student using Grade and QuarterGrade.

    Scoped to the CURRENT academic year (Sep..Aug): Grade rows count by
    source date; stored quarter/attestation rows by their academic_year
    key — legacy NULL-year and prior-year rows never count (same rule as
    the ranking engines).
    """
    ay_start, ay_end = academic_year_bounds()
    ay = academic_year_start()
    totals = {}
    for r in canonical_grade_values(Grade.objects.filter(
            score__isnull=False,
            date__range=(ay_start, ay_end)).order_by().values(
            'pk', 'student_id', 'subject', 'period', 'date',
            'assessment_id', 'lesson_id', 'score').iterator()):
        if not is_graded_subject(r['subject']):
            continue
        _add_score(totals, r['student_id'], r['score'], 1)
    for row in QuarterGrade.objects.filter(
            quarter__in=(1, 2, 3, 4), grade__isnull=False,
            academic_year=ay).values('student_id', 'subject').annotate(
            total=Sum('grade'), count=Count('grade')):
        if not is_graded_subject(row['subject']):
            continue
        _add_score(totals, row['student_id'], row['total'], row['count'])
    for row in QuarterGrade.objects.filter(
            quarter=0, att_grade__isnull=False,
            academic_year=ay).values('student_id', 'subject').annotate(
            total=Sum('att_grade'), count=Count('att_grade')):
        if not is_graded_subject(row['subject']):
            continue
        _add_score(totals, row['student_id'], row['total'], row['count'])

    gpas = {}
    for student_id in Student.objects.order_by().values_list('id', flat=True):
        total, count = totals.get(student_id, (0.0, 0))
        gpas[student_id] = round(total / count, 2) if count else 0.0
    return gpas


def all_student_gpas():
    """Return {student_id: overall_gpa} through the bounded-refresh cache.

    The map is user-independent, so every gunicorn worker shares the one
    file-cached entry instead of each re-running the district-wide
    aggregation per student_detail request. Same semantics as the
    dashboard rankings: a ranking-input save drops the main key and
    stamps dirty, readers serve the shadow copy inside
    RANKING_REFRESH_INTERVAL, and recomputation goes through the
    best-effort single-flight lock. FileBasedCache.add() is not atomic
    across processes, so two workers may occasionally both recompute;
    that duplicates work but stores equivalent results."""
    return _ranking_cached(RANKING_GPAS_CACHE_KEY, _all_student_gpas)


def student_detail(request, student_id):
    # all_objects: an archived (рафт) student's history page stays
    # readable for authorized staff — the roster only hides them.
    student = get_object_or_404(Student.all_objects, id=student_id)
    if request.user.is_authenticated and not has_school_access(request.user, student.school):
        return redirect('dashboard')

    non_graded = is_non_graded(student.class_name)

    today = datetime.date.today()
    try:
        selected_quarter = int(request.GET.get('quarter', ''))
    except ValueError:
        selected_quarter = None
    if selected_quarter not in (1, 2, 3, 4):
        selected_quarter = get_date_quarter(today) or 1

    if non_graded:
        subject_scores, live_subjects = {}, set()
    else:
        subject_scores, live_subjects = _student_subject_quarter_scores(
            student, selected_quarter)
    subjects = sorted(subject_scores.keys())
    scores = [subject_scores[s] for s in subjects]
    score_live = [s in live_subjects for s in subjects]

    # Тарбия: current conduct per subject — latest stored behavior_score
    # ('-date','-pk') or the display-only default 5. Student.conduct_map is
    # a cached projection, so the badge, this chart and the advice share
    # one fetch; no Grade rows are created or modified here.
    conduct_map = student.conduct_map
    conduct_subjects = sorted(conduct_map)
    conduct_scores = [conduct_map[s] for s in conduct_subjects]
    conduct_advice = _conduct_advice(conduct_map)

    try:
        with rankings_read_only():
            all_gpas = None if non_graded else all_student_gpas()
    except RankingRefreshBusy:
        # Cold GPA map: refresh_rankings has not filled it yet — show
        # placeholders instead of scanning the district per request.
        all_gpas = None
    gpa_pending = not non_graded and all_gpas is None
    if non_graded:
        student_gpa = None
        class_rank = None
        school_rank = None
        district_rank = None
    elif gpa_pending:
        student_gpa = class_rank = school_rank = district_rank = '—'
    else:
        student_gpa = all_gpas.get(student.id, 0.0)

        class_gpas = []
        school_gpas = []
        district_gpas = []
        # Rank only against graded students of academic schools (same population as dashboard rankings)
        academic_ids = {s.id for s in School.objects.all() if is_academic_school(s)}
        # Ranks only use GPA sets, so the per-request population pass
        # reads plain unordered tuples (no School join/sort, no model
        # instances) and evaluates is_non_graded once per class name.
        non_graded_by_class = {}
        for s_id, s_school_id, s_class in Student.objects.order_by().values_list(
                'id', 'school_id', 'class_name'):
            if s_school_id not in academic_ids:
                continue
            ng = non_graded_by_class.get(s_class)
            if ng is None:
                ng = non_graded_by_class[s_class] = is_non_graded(s_class)
            if ng:
                continue
            g = all_gpas.get(s_id, 0.0)
            district_gpas.append(g)
            if s_school_id == student.school_id:
                school_gpas.append(g)
            if s_school_id == student.school_id and s_class == student.class_name:
                class_gpas.append(g)

        class_rank = len({g for g in class_gpas if g > student_gpa}) + 1
        school_rank = len({g for g in school_gpas if g > student_gpa}) + 1
        district_rank = len({g for g in district_gpas if g > student_gpa}) + 1

    if non_graded:
        recent_stickers = Grade.objects.filter(student=student, sticker__isnull=False).order_by('-date')
    else:
        recent_stickers = Grade.objects.filter(student=student, sticker__isnull=False).order_by('-date')[:15]

    # One display line per logical slot. Accidental duplicate rows (same
    # subject/score AND same lesson/assessment slot) collapse into a single
    # line; legitimate different slots keep their own labelled line.
    today_grades = []
    seen_today_grades = set()
    for g in (
        Grade.objects
        .filter(student=student, date=today, score__isnull=False)
        .select_related('lesson', 'assessment')
        .order_by('subject', 'lesson__lesson_number', 'assessment__number', 'pk')
    ):
        key = (g.subject, g.lesson_id, g.assessment_id, g.score)
        if key in seen_today_grades:
            continue
        seen_today_grades.add(key)
        today_grades.append({
            'subject': _grade_slot_label(g),
            'score': int(g.score) if g.score == int(g.score) else g.score,
        })

    today_attendance = Grade.objects.filter(student=student, date=today, attendance__in=['+', '-'])
    today_excused = today_attendance.filter(attendance='+').count()
    today_unexcused = today_attendance.filter(attendance='-').count()

    # Behavior stats dedupe accidental identical rows per logical slot
    # (subject, lesson, assessment); the first row per slot wins — the same
    # row get_or_create_grade keeps when merging duplicates. Legitimate
    # different slots (D1/D2, assessment) each keep their own entry.
    today_behavior_rows = {}
    for g in (
        Grade.objects
        .filter(student=student, date=today, behavior_score__isnull=False)
        .select_related('lesson', 'assessment')
        .order_by('pk')
    ):
        today_behavior_rows.setdefault(
            (g.subject, g.lesson_id, g.assessment_id), g
        )

    today_behavior_breakdown = {}
    today_total_infractions = 0
    today_behavior_values = []
    for g in today_behavior_rows.values():
        today_behavior_values.append(g.behavior_score)
        infractions = 5 - g.behavior_score
        label = _grade_slot_label(g)
        if label not in today_behavior_breakdown:
            today_behavior_breakdown[label] = {
                'score': g.behavior_score, 'infractions': infractions,
            }
        today_total_infractions += infractions
    today_behavior_status = 'Намунавӣ' if today_total_infractions == 0 else 'Нигаронкунанда'

    today_avg = (
        sum(today_behavior_values) / len(today_behavior_values)
        if today_behavior_values else None
    )

    previous_day = Grade.objects.filter(
        student=student, behavior_score__isnull=False, date__lt=today
    ).order_by('-date').values_list('date', flat=True).first()
    yesterday_avg = None
    if previous_day:
        yesterday_values = {}
        for g in (
            Grade.objects
            .filter(student=student, date=previous_day, behavior_score__isnull=False)
            .order_by('pk')
        ):
            yesterday_values.setdefault(
                (g.subject, g.lesson_id, g.assessment_id), g.behavior_score
            )
        if yesterday_values:
            yesterday_avg = sum(yesterday_values.values()) / len(yesterday_values)

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
        'gpa_pending': gpa_pending,
        'non_graded': non_graded,
        'class_rank': class_rank,
        'school_rank': school_rank,
        'district_rank': district_rank,
        'subjects': subjects,
        'scores': scores,
        'score_live': score_live,
        'has_live_scores': bool(live_subjects),
        'selected_quarter': selected_quarter,
        'quarter_choices': (1, 2, 3, 4),
        'conduct_subjects': conduct_subjects,
        'conduct_scores': conduct_scores,
        'conduct_advice': conduct_advice,
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
            teacher.is_password_private = (
                getattr(tp, 'custom_password_set', False)
                or tp.user.last_login is not None)
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
    teacher_profile = TeacherProfile.objects.filter(
        school=school, full_name=full_name).first()
    archived = False
    if teacher_profile and not teacher_profile.is_archived:
        has_history = teacher_profile.tabel_months.exists()
        if not has_history:
            try:
                # No recorded months — legacy hard-delete is still safe.
                teacher_profile.user.delete()
            except ProtectedError:
                # A tabel month was recorded between the exists() check and
                # the delete — fall back to the archive path rather than
                # returning a 500 for this expected race.
                has_history = True
        if has_history:
            # Historical tabel records must survive: archive the profile,
            # deactivate the login, and clear live allocations — the same
            # attribution cleanup the old cascade performed via SET_NULL.
            with transaction.atomic():
                teacher_profile.is_archived = True
                teacher_profile.save(update_fields=['is_archived'])
                user = teacher_profile.user
                user.is_active = False
                user.save(update_fields=['is_active'])
                ClassSubject.objects.filter(teacher=user).update(teacher=None)
                ClassSubject.objects.filter(
                    allocated_teacher=teacher_profile).update(
                    allocated_teacher=None)
                TeachingGroup.objects.filter(
                    teacher=teacher_profile).update(teacher=None)
            archived = True
    teacher.delete()

    if archived:
        messages.success(
            request,
            f'Омӯзгор {full_name} бойгонӣ шуд — таърихи табел ҳифз шуд.')
    else:
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
    list_url = f"{reverse('teacher_list', args=[school.id])}#teacher-{teacher.id}"
    profiles = list(
        TeacherProfile.objects
        .filter(school=school, full_name=old_name, is_archived=False)
        .select_related('user'))

    # Ambiguous or missing account identity: abort the entire edit so a
    # partial save can never leave Teacher/profile/user out of sync.
    if len(profiles) > 1:
        messages.warning(
            request,
            'Барои ин омӯзгор чанд ҳисоби такрорӣ ёфт шуд. '
            'Ҳеҷ тағйир сабт нашуд; ба маъмури система муроҷиат кунед.')
        return redirect(list_url)
    if not profiles:
        messages.warning(
            request,
            'Ҳисоби воридшавии омӯзгор ёфт нашуд. Ҳеҷ тағйир сабт нашуд.')
        return redirect(list_url)

    teacher.name = full_name
    teacher.phone = phone
    teacher.subject = subject
    teacher.save()

    teacher_profile = profiles[0]
    teacher_profile.full_name = full_name
    teacher_profile.phone = phone
    teacher_profile.specialty = subject
    if new_password:
        teacher_profile.custom_password_set = True
    teacher_profile.save()
    if teacher_profile.user:
        teacher_profile.user.first_name = full_name
        if new_password:
            teacher_profile.user.set_password(new_password)
        teacher_profile.user.save()

    messages.success(request, f'Омӯзгор {full_name} таҳрир шуд.')
    return redirect(list_url)


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
    teachers = TeacherProfile.objects.filter(school=school, is_archived=False).select_related('user').order_by('full_name')

    user_to_profile = {
        tp.user_id: tp.id for tp in teachers if tp.user_id is not None
    }
    for cs in class_subjects:
        cs.selected_profile_id = cs.allocated_teacher_id or user_to_profile.get(cs.teacher_id)
        # Explicitly saved weekly hours win; otherwise show the national
        # curriculum recommendation as a suggested (muted) value. str()
        # keeps the HTML number input locale-free («0.5», not «0,5»).
        cs.hours_display = str(weekly_hours(cs.class_name, cs.subject, cs))
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
        'can_manage_subjects': _can_manage_subjects(request.user),
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
        tp.id: tp for tp in TeacherProfile.objects.filter(
            school=school, is_archived=False)
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
                new_hours = Decimal(raw.replace(',', '.'))
            except InvalidOperation:
                invalid_hours.append(cs)
                continue
            # 0.5 steps only: combined cross-class groups split one
            # weekly hour into 0.5 + 0.5 per class.
            if not (Decimal('0.5') <= new_hours <= MAX_WEEKLY_HOURS
                    and new_hours % Decimal('0.5') == 0):
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
            'Соати нодуруст рад шуд (аз 0,5 то %d, қадами 0,5): %s' % (
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
    # Home-education students are managed in the «Таҳсил дар хона» tab —
    # they are never part of the two-group split.
    students = sort_students(_exclude_home_students(
        Student.objects.filter(school=school, class_name=cs.class_name)))
    students_json = [
        {'id': s.id, 'name': s.full_name, 'gender': s.gender or ''}
        for s in students
    ]
    teachers = TeacherProfile.objects.filter(
        school=school, is_archived=False).order_by('full_name')

    groups = sorted(
        (g for g in cs.groups.filter(_canonical_group_q())
            .prefetch_related('memberships')),
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
        for tp in TeacherProfile.objects.filter(
            school=school, is_archived=False)
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

    student_ids = set(
        Student.objects.filter(school=school, class_name=cs.class_name)
        .values_list('id', flat=True)
    ) - home_student_ids(
        Student.objects.filter(school=school, class_name=cs.class_name)
        .values_list('id', flat=True))
    errors = validate_assignment(student_ids, member_lists[0], member_lists[1])
    if errors:
        messages.error(
            request,
            'Тақсимоти хонандагон нодуруст аст: ' +
            ' '.join(_GROUP_SAVE_ERRORS[e] for e in errors)
        )
        return redirect('class_groups', cs_id=cs.id)

    # This UI manages exactly the two canonical labels. If unexpected
    # non-home groups exist (created outside this flow), refuse rather
    # than orphaning their memberships silently.
    if cs.groups.filter(is_home=False).exclude(label__in=GROUP_LABELS).exists():
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
        # Only canonical-group memberships are rebuilt here — the
        # home-education group and its members stay untouched.
        SubjectGroupMembership.objects.filter(
            class_subject=cs, group__is_home=False).delete()
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

    # Only the canonical two-group split is deleted here — home-education
    # groups belong to the «Таҳсил дар хона» tab and survive.
    groups = list(cs.groups.filter(is_home=False))
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


# ---------------------------------------------------------------------------
# Таҳсил дар хона — home-education assignments (Zavuch/Admin)
#
# A home-education student stays formally attached to their class but is
# taught through an is_home TeachingGroup by a dedicated teacher: the
# student never appears in the class journal (the «Дар хона таҳсил
# мегирад» exemption hides them from rosters and norms), the home teacher
# keeps the «Журнали таҳсил дар хона» via the standard group scoping, and
# the group's explicit «Соат дар як ҳафта» lands in workload/tabel as its
# own «ФАН — Таҳсил дар хона» line.
# ---------------------------------------------------------------------------

def _home_education_school(request):
    """School context: a zavuch's own school; the superuser may inspect or
    manage any academic school through ?school_id= / POST school_id."""
    school = get_user_school(request.user)
    if request.user.is_superuser:
        school_id = request.GET.get('school_id') or request.POST.get('school_id')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
            except (ValueError, School.DoesNotExist):
                school = None
    return school


def _home_education_allowed(user, school):
    """Zavuch of this school or superuser — same gate as group management."""
    if not user.is_authenticated or school is None:
        return False
    return _can_manage_school(user, school)


@login_required
def home_education(request):
    """«Таҳсил дар хона» registry page for one school (Admin/Zavuch)."""
    school = _home_education_school(request)
    if not _home_education_allowed(request.user, school):
        messages.error(request, 'Дастрасӣ маҳдуд аст. Таҳсил дар хонаро танҳо маъмурият ё завучи ҳамон муассиса идора мекунад.')
        return redirect('dashboard')

    groups = (
        TeachingGroup.objects
        .filter(is_active=True, is_home=True,
                class_subject__school=school,
                class_subject__is_active=True)
        .select_related('class_subject', 'teacher')
        .prefetch_related('memberships__student')
        .order_by('class_subject__class_name', 'class_subject__subject', 'label'))
    rows = []
    for g in groups:
        members = sorted(
            g.memberships.all(),
            key=lambda m: m.student.full_name)
        rows.append({
            'group': g,
            'subject': g.class_subject.subject,
            'class_name': g.class_subject.class_name,
            'teacher': g.teacher,
            'hours': disp_hours(g.hours_per_week),
            'members': members,
        })

    # Class list: existing classes from students and allocations.
    class_names = sorted(
        set(Student.objects.filter(school=school)
            .values_list('class_name', flat=True))
        | set(ClassSubject.objects.filter(school=school, is_active=True)
              .values_list('class_name', flat=True)),
        key=lambda c: (class_numeric_part(c) or 0, c))
    # student_id -> {name, class_name} and class_name -> subjects maps
    # drive the dependent selects in the template.
    # JSON rendered into inline <script> — escape '<' so a student name
    # can never break out of the script tag.
    students_json = json.dumps([
        {'id': s.id, 'name': s.full_name, 'class_name': s.class_name}
        for s in Student.objects.filter(school=school).order_by('class_name', 'full_name')
    ], ensure_ascii=False).replace('<', '\\u003c')
    subjects_map = {}
    for cs in ClassSubject.objects.filter(school=school, is_active=True):
        subjects_map.setdefault(cs.class_name, [])
        if cs.subject not in subjects_map[cs.class_name]:
            subjects_map[cs.class_name].append(cs.subject)
    subjects_json = json.dumps(
        subjects_map, ensure_ascii=False).replace('<', '\\u003c')
    teachers = TeacherProfile.objects.filter(
        school=school, is_archived=False).order_by('full_name')
    all_schools = ([s for s in School.objects.all().order_by('name')
                    if is_academic_school(s)]
                   if request.user.is_superuser else None)

    return render(request, 'portal/home_education.html', {
        'school': school,
        'rows': rows,
        'class_names': class_names,
        'students_json': students_json,
        'subjects_json': subjects_json,
        'teachers': teachers,
        'all_schools': all_schools,
        'is_superuser': request.user.is_superuser,
        'max_weekly_hours': MAX_WEEKLY_HOURS,
        'home_label': HOME_GROUP_LABEL,
    })


@login_required
@require_POST
def home_education_add(request):
    """Create one home-education assignment: (student, subject) -> a
    is_home TeachingGroup of this class's ClassSubject + the student's
    «Дар хона таҳсил мегирад» exemption."""
    school = _home_education_school(request)
    if not _home_education_allowed(request.user, school):
        messages.error(request, 'Дастрасӣ маҳдуд аст.')
        return redirect('dashboard')

    redirect_url = reverse('home_education')
    if request.user.is_superuser and school:
        redirect_url += f'?school_id={school.id}'

    class_name = _validate_class_name(request.POST.get('class_name'))
    subject_raw = request.POST.get('subject', '')
    if subject_raw == '__other__':
        subject_raw = request.POST.get('subject_new', '')
    subject = normalize_subject(subject_raw)
    document = request.POST.get('document', '').strip()[:255]

    try:
        teacher_id = int(request.POST.get('teacher_id', ''))
    except (ValueError, TypeError):
        teacher_id = None
    teacher = TeacherProfile.objects.filter(
        pk=teacher_id, school=school, is_archived=False).first()

    raw_hours = request.POST.get('hours', '').strip()
    try:
        hours = Decimal(raw_hours.replace(',', '.'))
    except InvalidOperation:
        hours = None
    hours_ok = (
        hours is not None
        and Decimal('0.5') <= hours <= MAX_WEEKLY_HOURS
        and hours % Decimal('0.5') == 0)

    errors = []
    if class_name is None:
        errors.append('Синф нодуруст интихоб шудааст.')
    if not subject:
        errors.append('Фан нодуруст интихоб шудааст.')
    if teacher is None:
        errors.append('Омӯзгор нодуруст интихоб шудааст.')
    if not hours_ok:
        errors.append(f'Соатҳо нодуруст: аз 0,5 то {MAX_WEEKLY_HOURS}, қадами 0,5.')

    # The student: an existing roster pick or a free-text «Ному насаб»
    # creating a new record formally attached to this class.
    student = None
    student_id = request.POST.get('student_id', '').strip()
    new_name = ' '.join(request.POST.get('new_name', '').split())[:255]
    if student_id:
        student = Student.objects.filter(
            pk=student_id, school=school).first()
        if student is None:
            errors.append('Хонанда ёфт нашуд.')
        elif student.class_name != class_name:
            errors.append('Хонанда ба интихобшудаи синф тааллуқ надорад.')
    elif new_name:
        student = Student.all_objects.filter(
            school=school, class_name=class_name, full_name=new_name).first()
        if student is not None and student.is_archived:
            errors.append('Ин хонанда бойгонӣ шудааст — аввал аз бойгонӣ баргардонед.')
            student = None
    else:
        errors.append('Хонанда интихоб кунед ё номи навро ёзед.')

    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect(redirect_url)

    cs = _class_subject_for(school, class_name, subject)
    if cs is None:
        cs = ClassSubject.objects.create(
            school=school, class_name=class_name, subject=subject,
            is_default=False, is_active=True)
    # One membership per (class, subject) is DB-enforced — check both
    # sides before the insert: a canonical group, or another home group
    # (a different teacher for the same subject).
    if student is not None:
        existing = SubjectGroupMembership.objects.filter(
            class_subject=cs, student=student).select_related('group').first()
        if existing is not None and not existing.group.is_home:
            messages.error(
                request,
                f'{student.full_name} аллакай дар гурӯҳи оддии «{subject}» аст — '
                'аввал уро аз он гурӯҳ хориҷ кунед.')
            return redirect(redirect_url)
        if existing is not None and existing.group.is_home:
            other = existing.group.teacher
            messages.warning(
                request,
                f'{student.full_name} аллакай таҳсил дар хонада сабт шудааст '
                f'({other.full_name if other else "—"}).')
            return redirect(redirect_url)

    with transaction.atomic():
        if student is None:
            student = Student.objects.create(
                school=school, class_name=class_name, full_name=new_name)
        group = cs.groups.filter(
            is_active=True, is_home=True, teacher=teacher).first()
        if group is None:
            label = HOME_GROUP_LABEL
            i = 2
            while cs.groups.filter(label=label).exists():
                label = f'{HOME_GROUP_LABEL} {i}'
                i += 1
            group = TeachingGroup.objects.create(
                class_subject=cs, label=label, teacher=teacher,
                is_home=True, hours_per_week=hours)
        elif group.hours_per_week != hours:
            group.hours_per_week = hours
            group.save(update_fields=['hours_per_week'])
        membership, created = SubjectGroupMembership.objects.get_or_create(
            class_subject=cs, group=group, student=student)
        if not created:
            messages.warning(
                request,
                f'{student.full_name} аллакай дар ин гурӯҳи таҳсил дар хона аст.')
            return redirect(redirect_url)
        # One active «Дар хона таҳсил мегирад» exemption per student —
        # created once, it hides them from the class journal and norms
        # while their marks keep flowing to the class's quarter documents.
        today = timezone.localdate()
        if not student.exemptions.filter(
                reason='home', cancelled_at__isnull=True,
                start_date__lte=today).exclude(end_date__lt=today).exists():
            StudentExemption.objects.create(
                student=student, reason='home', document=document,
                start_date=today, created_by=request.user)

    messages.success(
        request,
        f'{student.full_name} — {subject} — таҳсил дар хона сабт шуд '
        f'({teacher.full_name}, {disp_hours(hours)} соат дар як ҳафта).')
    return redirect(redirect_url)


@login_required
@require_POST
def home_education_remove(request):
    """Remove one student from a home group; when no home memberships
    remain anywhere, their «Дар хона таҳсил мегирад» exemption ends."""
    school = _home_education_school(request)
    if not _home_education_allowed(request.user, school):
        messages.error(request, 'Дастрасӣ маҳдуд аст.')
        return redirect('dashboard')

    redirect_url = reverse('home_education')
    if request.user.is_superuser and school:
        redirect_url += f'?school_id={school.id}'

    try:
        membership = SubjectGroupMembership.objects.select_related(
            'group', 'student', 'class_subject').get(
            pk=int(request.POST.get('membership_id', '')),
            class_subject__school=school,
            group__is_home=True)
    except (ValueError, TypeError, SubjectGroupMembership.DoesNotExist):
        messages.error(request, 'Сабт ёфт нашуд.')
        return redirect(redirect_url)

    student = membership.student
    group = membership.group
    with transaction.atomic():
        membership.delete()
        if not group.memberships.exists():
            group.is_active = False
            group.save(update_fields=['is_active'])
        still_home = SubjectGroupMembership.objects.filter(
            student=student,
            group__is_active=True, group__is_home=True).exists()
        if not still_home:
            for ex in student.exemptions.filter(
                    reason='home', cancelled_at__isnull=True):
                ex.cancelled_at = timezone.now()
                ex.cancelled_by = request.user
                ex.save(update_fields=['cancelled_at', 'cancelled_by'])

    messages.success(
        request,
        f'{student.full_name} аз таҳсил дар хона хориҷ карда шуд.')
    return redirect(redirect_url)


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
    # Same explicit opt-out the approval flow writes — a later student
    # arrival must not silently reactivate an intentional removal.
    reg_subject = canonical_subject_for(cs.subject)
    if reg_subject is not None:
        SubjectAvailability.objects.update_or_create(
            subject=reg_subject, school=cs.school,
            class_name=cs.class_name,
            defaults={'is_active': False},
        )
    messages.success(request, f'Фани «{cs.subject}» барои {cs.class_name} хориҷ шуд.')
    return redirect(f'{reverse("lesson_allocation")}?school_id={cs.school_id}')


@login_required
@require_POST
def request_deactivation(request):
    if not _is_zavuch(request.user, get_user_role(request.user)):
        messages.error(request, 'Танҳо завучҳо метавонанд дархости хориҷкунӣ ирсол кунанд.')
        return redirect('lesson_allocation')
    school = get_user_school(request.user)
    if not school or not is_academic_school(school) or not has_school_access(request.user, school):
        messages.error(request, 'Шумо метавонед танҳо барои муассисаи худ дархост диҳед.')
        return redirect('lesson_allocation')
    reason = request.POST.get('reason', '').strip()
    wants_json = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.headers.get('Accept', '').startswith('application/json')
    )

    # Batch mode from the registry workflow: subject name + class list.
    subject_name = normalize_subject(request.POST.get('subject', ''))
    if subject_name:
        class_names = [normalize_class_name(c)
                       for c in request.POST.getlist('classes') if c.strip()]
        made = 0
        for cn in class_names:
            cs = ClassSubject.objects.filter(
                school=school, class_name=cn, subject=subject_name,
                is_active=True).first()
            if cs is None:
                # Configured via availability but the ClassSubject row was
                # never materialized — let ensure create it so the request
                # has a concrete scope, then approve/deactivate normally.
                active_avail = SubjectAvailability.objects.filter(
                    subject__name=subject_name, school=school,
                    class_name=cn, is_active=True).exists()
                if not active_avail:
                    continue
                ensure_class_subjects(school, cn)
                cs = ClassSubject.objects.filter(
                    school=school, class_name=cn, subject=subject_name,
                    is_active=True).first()
            if cs is None:
                continue
            if SubjectDeactivationRequest.objects.filter(
                    class_subject=cs,
                    status__in=['pending', 'approved']).exists():
                continue
            SubjectDeactivationRequest.objects.create(
                class_subject=cs, requested_by=request.user,
                status='pending', notes=reason)
            made += 1
        if wants_json:
            status = 'ok' if made else 'exists'
            return JsonResponse({'status': status})
        if made:
            messages.success(
                request, 'Дархост барои хориҷ кардани фан фиристода шуд.')
        else:
            messages.warning(
                request, 'Дархост фиристода нашуд. Синфҳои интихобшударо санҷед.')
        return redirect('subject_registry')

    cs_id = request.POST.get('cs_id')
    if not cs_id:
        return redirect('lesson_allocation')
    try:
        cs = ClassSubject.objects.get(pk=int(cs_id))
    except (ValueError, ClassSubject.DoesNotExist):
        messages.error(request, 'Фан ёфт нашуд.')
        return redirect('lesson_allocation')
    if cs.school != school:
        messages.error(request, 'Шумо метавонед танҳо барои муассисаи худ дархост диҳед.')
        return redirect('lesson_allocation')
    if SubjectDeactivationRequest.objects.filter(class_subject=cs, status__in=['pending', 'approved']).exists():
        if wants_json:
            return JsonResponse({'status': 'exists', 'message': 'Дархости мутаносиб аллакай вуҷуд дорад'})
        messages.warning(request, 'Дархости мутаносиб аллакай вуҷуд дорад.')
        return redirect(f'{reverse("lesson_allocation")}?class={cs.class_name}')
    deact_req, _created = SubjectDeactivationRequest.objects.get_or_create(
        class_subject=cs,
        requested_by=request.user,
        status='pending',
        defaults={'notes': reason},
    )
    if wants_json:
        return JsonResponse({'status': 'ok', 'message': 'Дархост ирсол шуд',
                             'request_id': deact_req.id})
    messages.success(request, f'Дархости хориҷкунии «{cs.subject}» барои {cs.class_name} ирсол шуд.')
    return redirect(f'{reverse("lesson_allocation")}?class={cs.class_name}')


@user_passes_test(lambda u: u.is_superuser)
def deactivation_requests(request):
    pending = SubjectDeactivationRequest.objects.filter(
        status='pending'
    ).select_related('class_subject', 'class_subject__school', 'requested_by').order_by(
        'class_subject__school_id', '-requested_at', '-pk')
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
        # Only the requested school/class scope is removed: deactivate the
        # matching registry availability as an explicit opt-out so
        # ensure_class_subjects() cannot re-seed it. Other schools/classes
        # and the canonical Subject record itself stay untouched.
        SubjectAvailability.objects.filter(
            subject__name=cs.subject, school=cs.school,
            class_name=cs.class_name,
        ).update(is_active=False)
        reg_subject = Subject.objects.filter(name=cs.subject).first()
        if reg_subject is not None:
            SubjectAvailability.objects.get_or_create(
                subject=reg_subject, school=cs.school,
                class_name=cs.class_name,
                defaults={'is_active': False},
            )
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
    # The reviewed row leaves the pending list; anchor to the request that
    # takes its position (next in display order, else the last remaining).
    pending_qs = SubjectDeactivationRequest.objects.filter(
        status='pending'
    ).order_by('class_subject__school_id', '-requested_at', '-pk')
    anchor_pk = pending_qs.filter(
        Q(class_subject__school_id__gt=cs.school_id)
        | Q(class_subject__school_id=cs.school_id,
            requested_at__lt=req.requested_at)
        | Q(class_subject__school_id=cs.school_id,
            requested_at=req.requested_at, pk__lt=req.pk)
    ).values_list('pk', flat=True).first()
    if anchor_pk is None:
        anchor_pk = pending_qs.values_list('pk', flat=True).last()
    target = reverse('deactivation_requests')
    if anchor_pk is not None:
        target += f'#request-{anchor_pk}'
    return redirect(target)


@login_required
@require_POST
def cancel_deactivation_request(request):
    """Cancel a still-pending deactivation request (owner only).

    Ownership and pending-state are enforced in one conditional UPDATE: a
    forged request_id can never touch another user's request, an already
    reviewed request stays untouched, and a repeated POST is a no-op. The
    row is kept so the request history stays intact.
    """
    try:
        req_id = int(request.POST.get('request_id', ''))
    except (TypeError, ValueError):
        req_id = None
    req = SubjectDeactivationRequest.objects.filter(
        pk=req_id, requested_by=request.user,
    ).select_related('class_subject').first() if req_id is not None else None
    if req is not None and SubjectDeactivationRequest.objects.filter(
            pk=req.pk, status='pending').update(status='cancelled'):
        messages.success(
            request,
            f'Дархости хориҷкунии «{req.class_subject.subject}» барои '
            f'{req.class_subject.class_name} бекор карда шуд.')
    else:
        messages.warning(
            request,
            'Танҳо дархости худро дар ҳолати интизорӣ бекор кардан мумкин аст.')
    next_url = request.POST.get('next', '')
    if next_url not in (reverse('lesson_allocation') + '?tab=requests',
                        reverse('dashboard')):
        next_url = reverse('lesson_allocation')
    return redirect(next_url)


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
    """Monitoring dashboard scoped by the centralized permission helpers."""
    if not can_access_monitoring(request.user):
        return redirect('dashboard')
    stats = _get_monitoring_stats()
    stats = [
        s for s in stats
        if s['school'].type != 'Идораи маориф'
        and 'идораи маориф' not in (s['school'].name or '').lower()
        and s['zavuch_username'] != 'zavuch_0'
    ]
    stats = scope_monitoring_stats(request.user, stats)
    return render(request, 'portal/monitoring_dashboard.html', {
        'stats': stats,
        'activity': get_activity_summary(request.user),
    })


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

    # Zavuchs get a school-scoped leaderboard: the binding comes from the
    # profile FK (get_user_school), never from the zavuch_<n> username.
    # Every downstream query is already driven by school_ids, so scoping
    # here shrinks all district-wide work to one school server-side.
    zavuch_scoped = is_zavuch and not (request.user.is_superuser or request.user.is_staff)
    if zavuch_scoped:
        user_school = get_user_school(request.user)
        schools = [user_school] if user_school and is_academic_school(user_school) else []
    else:
        schools = [s for s in School.objects.all() if is_academic_school(s)]

    # Use the exact same school-GPA formula as the academic leaderboard
    # (calculate_school_rankings) so all leaderboards show identical
    # values. Fetched here, before the readiness payload's own lock, so
    # the cached call can never nest inside the site-wide refresh gate.
    try:
        gpa_map = {
            item['school'].id: item['gpa']
            for item in calculate_school_rankings()
        }
    except RankingRefreshBusy:
        # Cold school ranking still being refreshed by another worker:
        # leave the GPA column blank rather than failing or recomputing.
        gpa_map = None

    user_school = get_user_school(request.user)
    if zavuch_scoped:
        # One school is cheap enough to compute per request.
        data, activity = _compute_school_readiness_payload(
            schools, gpa_map)
    else:
        # The district-wide payload is shared by every staff/superuser
        # viewer: run it through the same bounded-refresh cache as the
        # other rankings so concurrent opens do not each rescan the
        # quarter's Grade rows. A refresh busy past LOCK_WAIT renders
        # empty blocks instead of hanging the request.
        try:
            data, activity = _ranking_cached(
                READINESS_CACHE_KEY,
                lambda: _compute_school_readiness_payload(
                    schools, gpa_map))
        except RankingRefreshBusy:
            data, activity = [], []
    # Per-viewer flag — stamped after the (possibly cached) payload is
    # retrieved so a cached value never carries another user's mark.
    for item in activity:
        item['is_mine'] = bool(
            user_school and item['school'].id == user_school.id)

    return render(request, 'portal/school_readiness_rating.html', {
        'stats': data,
        'activity': activity,
        'min_norm_bands': MIN_GRADES_BANDS,
        'zavuch_scope': zavuch_scoped,
    })


def _compute_school_readiness_payload(schools, gpa_map,
                                      quarter_window=None):
    """Build (stats, activity) for the readiness leaderboard.

    stats — tab 1, lesson-allocation readiness per school.
    activity — tab 2, grading activity + teacher workload per school.
    Heavy: several canonical scans of the quarter's Grade rows, so the
    district-wide call is served through _ranking_cached; zavuch's
    single-school call is cheap enough to compute per request.
    gpa_map — {school_id: gpa} from calculate_school_rankings, or None
    (the GPA column is then left blank). is_mine is stamped by the view.

    quarter_window — optional (start, end) pinning the activity scan to
    one quarter's official window (the Ҳисоботҳо period selector). A
    finished quarter resolves progress to 1.0: the report then reads
    the FINAL verdict against the full norm, not the live pace.
    """
    school_ids = [s.id for s in schools]

    # Active TeachingGroups per CS — drives both the tab-1 staffing check
    # and the tab-2 per-group teacher attribution.
    all_groups = list(
        TeachingGroup.objects.filter(
            class_subject__school_id__in=school_ids,
            class_subject__is_active=True,
            is_active=True,
        ).select_related('teacher', 'class_subject')
    )
    groups_by_cs = defaultdict(list)
    for g in all_groups:
        groups_by_cs[g.class_subject_id].append(g)
    # Staffing uses canonical groups only: a CS carrying only a
    # home-education group is still staffed through its legacy fields —
    # the home group supplements, it never replaces the class teacher.
    canonical_by_cs = defaultdict(list)
    for g in all_groups:
        if not g.is_home:
            canonical_by_cs[g.class_subject_id].append(g)
    unstaffed_grouped_ids = {
        g.class_subject_id for g in all_groups
        if not g.is_home and g.teacher_id is None
    }

    totals = dict(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .values_list('school_id').annotate(n=Count('id'))
    )
    # Ungrouped CS: legacy rule (teacher/allocated_teacher set). Grouped CS:
    # assigned only when EVERY active group has a teacher — the CS-level
    # fields alone do not staff a grouped subject.
    assigned = dict(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .exclude(pk__in=set(canonical_by_cs))
        .filter(Q(teacher__isnull=False) | Q(allocated_teacher__isnull=False))
        .values_list('school_id').annotate(n=Count('id'))
    )
    assigned_grouped = defaultdict(int)
    grouped_cs_school = {g.class_subject_id: g.class_subject.school_id for g in all_groups}
    for cs_id in set(canonical_by_cs) - unstaffed_grouped_ids:
        assigned_grouped[grouped_cs_school[cs_id]] += 1

    zavuch_users = {
        u.username: u
        for u in User.objects.filter(
            username__in=[f'zavuch_{get_school_number(s)}' for s in schools]
        )
    }
    data = []
    for school in schools:
        total = totals.get(school.id, 0)
        done = assigned.get(school.id, 0) + assigned_grouped.get(school.id, 0)
        pct = round(done / total * 100, 1) if total else 0.0
        zavuch_user = zavuch_users.get(f'zavuch_{get_school_number(school)}')
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
    # Norm fulfillment is a current-quarter, time-aware metric: only Grade
    # rows dated inside today's quarter count, and only the elapsed share
    # of the quarterly minimum is expected. The 100% cap is display-only —
    # it never limits further grade entry.
    today = datetime.date.today()
    if quarter_window is not None:
        q_start, q_end = quarter_window
        # Same elapsed-share rule as _quarter_progress, resolved against
        # the chosen window: a past quarter gives progress 1.0 — the
        # report reads its final state against the full-quarter norm.
        total = (q_end - q_start).days + 1
        elapsed = min(max((today - q_start).days + 1, 1), total)
        q_progress = elapsed / total
    else:
        q_start, q_end = _current_quarter_bounds(today)
        q_progress = _quarter_progress(today)
    student_counts = dict(
        Student.objects.filter(school_id__in=school_ids)
        .exclude(class_name__regex=NON_GRADED_RE)
        .values_list('school_id').annotate(n=Count('id'))
    )
    grade_counts = dict(
        Grade.objects.filter(student__school_id__in=school_ids,
                             student__is_archived=False,
                             date__range=(q_start, q_end))
        .values_list('student__school_id').annotate(n=Count('id'))
    )
    class_student_ids = defaultdict(set)
    for r in Student.objects.filter(school_id__in=school_ids).exclude(
            class_name__regex=NON_GRADED_RE).values(
            'school_id', 'class_name', 'id'):
        class_student_ids[(r['school_id'], r['class_name'])].add(r['id'])
    # «Дар хона таҳсил мегирад» students are taught in the separate home
    # journal — they leave every class-level norm denominator and grade
    # count below (their own marks still land in the official quarter
    # documents of their formal class).
    home_ids = home_student_ids(
        sid for ids in class_student_ids.values() for sid in ids)
    for key in list(class_student_ids):
        class_student_ids[key] -= home_ids
    # Other active exemptions (дар давлати дигар таҳсил мекунад …) leave
    # the denominator too — the journal itself drops them via
    # exempt_ids, so keeping them here would pin those class-subjects
    # at «Қисман» forever. (home ids are already gone; the set-minus
    # overlap is harmless.)
    exempt_ids = set(active_exemption_map(
        sid for ids in class_student_ids.values() for sid in ids))
    for key in class_student_ids:
        class_student_ids[key] -= exempt_ids
    class_student_counts = {
        key: len(ids) for key, ids in class_student_ids.items()
    }
    # Group attribution maps. Grade has no teacher FK, so attribution is
    # derived: Grade.lesson.group wins when set (concrete teaching event);
    # legacy lesson-less / group-less rows fall back to the student's
    # current SubjectGroupMembership — an approximation for pre-split
    # history, never fabricated. Loaded before the grade scan so a single
    # pass can feed both paths — the membership JOIN fan-out the old
    # third scan used is replaced by an in-memory lookup.
    group_members = defaultdict(set)
    student_memberships = defaultdict(list)
    for m in SubjectGroupMembership.objects.filter(
            class_subject__school_id__in=school_ids,
            class_subject__is_active=True,
            group__is_active=True,
            student__is_archived=False,
    ).values('class_subject_id', 'class_subject__subject', 'group_id',
             'student_id'):
        gkey = (m['class_subject_id'], m['group_id'])
        group_members[gkey].add(m['student_id'])
        student_memberships[m['student_id']].append(
            (gkey, normalize_subject(m['class_subject__subject'])))
    # Group denominators drop non-home exemptions for the same reason —
    # home students stay (they ARE the home journal's roster).
    non_home_exempt = exempt_ids - home_ids
    for key in group_members:
        group_members[key] -= non_home_exempt

    # Per-student current-quarter rows keyed by the student's own
    # school+class+subject: one canonical pass feeds the raw pair totals,
    # the norm math (academic score__isnull=False rows only), and both
    # group-attribution paths — previously three separate scans of the
    # same quarter rows, the last one widened by the membership JOIN.
    pair_grade_counts = defaultdict(int)
    pair_student_scored = defaultdict(dict)
    # Absence-credit slots mirror absence_counts() in norm_pace: a
    # score-less '+/-' attendance mark tops the student's real mark
    # count up to the norm. The journal page (_journal_norm_status)
    # applies that credit — the batch must too, or a journal reading
    # Пурра shows up here as Қисман.
    _new_slots = lambda: {'s': set(), 'e': set(), 'n': set()}
    pair_student_slots = defaultdict(lambda: defaultdict(_new_slots))
    group_grade_counts = defaultdict(int)
    group_student_scored = defaultdict(lambda: defaultdict(int))
    group_student_slots = defaultdict(lambda: defaultdict(_new_slots))
    for r in canonical_grade_values(Grade.objects.filter(
            student__school_id__in=school_ids,
            student__is_archived=False,
            date__range=(q_start, q_end)).order_by().values(
            'pk', 'student_id', 'subject', 'period', 'date',
            'assessment_id', 'lesson_id', 'score', 'attendance',
            'student__school_id', 'student__class_name',
            'lesson__class_subject_id', 'lesson__group_id',
            'lesson__group__is_active').iterator()):
        key = (r['student__school_id'], r['student__class_name'], r['subject'])
        scored = r['score'] is not None
        att = (not scored) and r['attendance'] in ATTENDANCE_MARKS
        if r['student_id'] not in home_ids:
            # Home-education marks never enter the class-level norm totals —
            # they are attributed to the home group via lesson.group below.
            pair_grade_counts[key] += 1
            if scored:
                sid = r['student_id']
                pair_student_scored[key][sid] = pair_student_scored[key].get(sid, 0) + 1
                pair_student_slots[key][sid]['s'].add(
                    (r['date'], r['lesson_id']))
            elif att:
                slot = pair_student_slots[key][r['student_id']]
                if r['lesson_id']:
                    slot['e'].add((r['date'], r['lesson_id']))
                else:
                    slot['n'].add(r['date'])
        if r['lesson__group__is_active']:
            # Concrete teaching event: the grade belongs to the lesson's
            # own group regardless of current membership.
            gkey = (r['lesson__class_subject_id'], r['lesson__group_id'])
            group_grade_counts[gkey] += 1
            if scored:
                group_student_scored[gkey][r['student_id']] += 1
                group_student_slots[gkey][r['student_id']]['s'].add(
                    (r['date'], r['lesson_id']))
            elif att:
                slot = group_student_slots[gkey][r['student_id']]
                if r['lesson_id']:
                    slot['e'].add((r['date'], r['lesson_id']))
                else:
                    slot['n'].add(r['date'])
        else:
            subj_norm = normalize_subject(r['subject'])
            for gkey, cs_subject_norm in student_memberships.get(
                    r['student_id'], ()):
                if subj_norm != cs_subject_norm:
                    continue
                group_grade_counts[gkey] += 1
                if scored:
                    group_student_scored[gkey][r['student_id']] += 1
                    group_student_slots[gkey][r['student_id']]['s'].add(
                        (r['date'], r['lesson_id']))
                elif att:
                    slot = group_student_slots[gkey][r['student_id']]
                    if r['lesson_id']:
                        slot['e'].add((r['date'], r['lesson_id']))
                    else:
                        slot['n'].add(r['date'])
    class_subjects = list(
        ClassSubject.objects.filter(school_id__in=school_ids, is_active=True)
        .exclude(class_name__regex=NON_GRADED_RE)
        .select_related('allocated_teacher', 'teacher')
    )
    school_cs = defaultdict(list)
    for cs in class_subjects:
        school_cs[cs.school_id].append(cs)
    profiles_by_school = defaultdict(dict)
    for tp in TeacherProfile.objects.filter(
            school_id__in=school_ids, is_archived=False):
        if tp.user_id is not None:
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
                # Per-student capped total: a student counts at most
                # min_norm, so marks beyond the norm can never cover a
                # classmate left without a single mark (the Бебаҳо list).
                # Same rule the journal's done_eff applies.
                pkey = (school.id, cs.class_name, cs.subject)
                per_student = pair_student_scored.get(pkey, {})
                slots_map = pair_student_slots.get(pkey, {})
                norm_grades_done += sum(
                    min(actual + min(
                        _absence_credit(slots_map.get(sid)),
                        max(0, min_norm - actual)), min_norm)
                    for sid, actual in per_student.items()
                    if actual > 0)
            cs_groups = canonical_by_cs.get(cs.id)
            if cs_groups:
                # Grouped CS: one workload entry per actual group teacher.
                # CS-level fields never attribute a grouped subject.
                for g in cs_groups:
                    if g.teacher_id:
                        teacher_pairs[g.teacher].append((cs, g))
            else:
                profile = cs.allocated_teacher or user_to_profile.get(cs.teacher_id)
                if profile:
                    teacher_pairs[profile].append((cs, None))
            # Home-education groups always attribute separately — the class
            # teacher keeps the regular load above either way.
            for g in groups_by_cs.get(cs.id, ()):
                if g.is_home and g.teacher_id:
                    teacher_pairs[g.teacher].append((cs, g))

        teachers = []
        for profile, pairs in teacher_pairs.items():
            subj_classes = defaultdict(list)
            hours = 0
            min_grades = 0
            grades_done = 0
            norm_done = 0
            journal = []
            for cs, group in pairs:
                if group is not None:
                    # Group-scoped: grades only from this group's members,
                    # denominator is the group size, not the class size.
                    subj_classes[f"{cs.subject} — {group.label}"].append(cs.class_name)
                    member_ids = group_members.get((cs.id, group.id), ())
                    n_students = len(member_ids)
                    gkey = (cs.id, group.id)
                    per_student = group_student_scored.get(gkey, {})
                    slots_map = group_student_slots.get(gkey, {})
                    entered = group_grade_counts.get((cs.id, group.id), 0)
                else:
                    subj_classes[cs.subject].append(cs.class_name)
                    member_ids = class_student_ids.get(
                        (school.id, cs.class_name), ())
                    n_students = len(member_ids)
                    pair_key = (school.id, cs.class_name, cs.subject)
                    per_student = pair_student_scored.get(pair_key, {})
                    slots_map = pair_student_slots.get(pair_key, {})
                    entered = pair_grade_counts.get(
                        (school.id, cs.class_name, cs.subject), 0)
                hours += (
                    group_weekly_hours(group, cs) if group is not None
                    else weekly_hours(cs.class_name, cs.subject, cs))
                grades_done += entered
                # Exclude un-graded subjects (norm == 0) from required minimum.
                min_norm = (
                    group_min_norm(group, cs) if group is not None
                    else quarter_min_norm(cs.class_name, cs.subject, cs))
                if min_norm:
                    # Academic rows only: attendance-only marks, conduct-only
                    # and sticker-only rows never satisfy the grading norm.
                    scored = sum(per_student.values())
                    # Same effective count as the journal's done_eff:
                    # real scores plus each student's absence credit,
                    # the total capped AT the norm — keeps the report's
                    # fulfillment equal to what the journal page shows,
                    # and ungraded students can never be covered by
                    # over-entered marks for classmates.
                    norm_done += sum(
                        min(actual + min(
                            _absence_credit(slots_map.get(sid)),
                            max(0, min_norm - actual)), min_norm)
                        for sid, actual in per_student.items()
                        if actual > 0)
                    min_grades += min_norm * n_students
                    # Journal status is per ClassSubject (per group on
                    # grouped subjects): Пурра only when EVERY relevant
                    # student reached the FULL-quarter norm — a raw class
                    # total cannot hide students below the norm, and the
                    # elapsed share is never consulted.
                    met = sum(
                        1 for sid in member_ids
                        if per_student.get(sid, 0) > 0
                        and per_student[sid]
                        + _absence_credit(slots_map.get(sid)) >= min_norm
                    )
                    journal.append({
                        'class_name': cs.class_name,
                        'subject': cs.subject,
                        'group': group.label if group is not None else None,
                        'students_met': met,
                        'students_total': n_students,
                        'status': _journal_status(scored, met, n_students),
                    })
            journal.sort(key=lambda j: (
                class_numeric_part(j['class_name']) or 0,
                j['class_name'], j['subject'], j['group'] or ''))
            fulfillment = _norm_fulfillment(norm_done, min_grades, q_progress)
            teachers.append({
                'name': profile.full_name,
                'subjects': [
                    f"{subj}: {', '.join(sorted(set(cls), key=class_sort))}"
                    for subj, cls in sorted(subj_classes.items())
                ],
                'hours': disp_hours(hours),
                'min_grades': min_grades,
                'grades_done': grades_done,
                'fulfillment': fulfillment,
                'journal': journal,
            })
        teachers.sort(key=lambda t: (-t['fulfillment'], -t['grades_done'], t['name']))

        activity.append({
            'school': school,
            'students': students_n,
            'total_grades': grades_n,
            'fulfillment': _norm_fulfillment(norm_grades_done, expected_min, q_progress),
            'gpa': (gpa_map.get(school.id, 0.0)
                    if gpa_map is not None else None),
            'teachers': teachers,
            'is_mine': False,  # stamped per viewer by the view
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

    return data, activity


@login_required
def school_documents(request):
    """Ҳуҷҷатнигорӣ — documents hub for superusers, staff, school Zavuchs
    and school directors (principal — view/export, not journal edits).

    First document: weekly lesson-hours distribution per teacher, built from
    active ClassSubject rows (allocated_teacher preferred, teacher as fallback).
    Per-allocation ClassSubject.hours_per_week values saved in "Тақсимоти
    дарсҳо" win; rows without an explicit value fall back to
    settings.TJC_SUBJECT_HOURS / TJC_SUBJECT_HOURS_DEFAULT.
    """
    role = get_user_role(request.user)
    is_zavuch = (role and role.lower() == 'zavuch') or request.user.username.lower().startswith('zavuch_')
    can_maorif = _maorif_report_access(request.user)
    if not (request.user.is_superuser or request.user.is_staff or is_zavuch
            or role in (settings.ROLE_DIRECTOR, settings.ROLE_PRINCIPAL)):
        return redirect('dashboard')

    school = _class_report_school(request)
    can_pick_school = _maorif_report_access(request.user)
    schools = sorted(
        academic_schools(), key=lambda s: s.name) if can_pick_school else []
    workload = []
    if school:
        class_subjects = ClassSubject.objects.filter(
            school=school, is_active=True
        ).select_related('allocated_teacher', 'teacher').prefetch_related(
            'groups__teacher'
        ).order_by('class_name', 'subject')
        user_to_profile = _user_to_profile_map(
            TeacherProfile.objects.filter(school=school, is_archived=False))

        hours_map = getattr(settings, 'TJC_SUBJECT_HOURS', {})
        default_hours = getattr(settings, 'TJC_SUBJECT_HOURS_DEFAULT', 2)
        # Explicit weekly hours confirmed in "Тақсимоти дарсҳо" are the
        # authoritative per school + class + subject value.
        explicit_hours = {
            (cs.subject, cs.class_name): cs.hours_per_week
            for cs in class_subjects
            if cs.hours_per_week is not None
        }

        # teacher -> {display_subject: [(base_subject, class_name, group)]}
        grouped = {}
        for cs in class_subjects:
            cs_groups = [g for g in cs.groups.all() if g.is_active]
            canonical_groups = [g for g in cs_groups if not g.is_home]
            home_groups = [g for g in cs_groups if g.is_home]
            if canonical_groups:
                # Grouped CS: one workload item per actual group teacher,
                # labeled "ФАН — Гурӯҳи N". Each group teacher is credited
                # with the subject's full weekly load for their group.
                for g in canonical_groups:
                    if not g.teacher_id:
                        continue
                    subj_map = grouped.setdefault(g.teacher, {})
                    subj_map.setdefault(
                        f"{cs.subject} — {g.label}", []
                    ).append((cs.subject, cs.class_name, g))
            else:
                profile = cs.allocated_teacher or user_to_profile.get(cs.teacher_id)
                if profile:
                    subj_map = grouped.setdefault(profile, {})
                    subj_map.setdefault(cs.subject, []).append(
                        (cs.subject, cs.class_name, None))
            # Home-education groups always add their own load on top —
            # "Таҳсил дар хона — ФАН" with the group's explicit hours.
            for g in home_groups:
                if not g.teacher_id:
                    continue
                subj_map = grouped.setdefault(g.teacher, {})
                subj_map.setdefault(
                    f"{HOME_GROUP_LABEL} — {cs.subject}", []
                ).append((cs.subject, cs.class_name, g))

        def class_sort(cn):
            return (class_numeric_part(cn) or 0, cn)

        for profile, subj_map in grouped.items():
            items = []
            academic_hours = 0
            homeroom_hours = 0
            for subject, entries in sorted(subj_map.items(), key=lambda kv: kv[0]):
                base_subject = entries[0][0]
                classes_sorted = sorted({cn for _, cn, _ in entries}, key=class_sort)
                items.append({'subject': subject, 'classes': classes_sorted})
                norm = normalize_subject(base_subject)
                overrides = [
                    (g.hours_per_week if g is not None and g.is_home else None)
                    or explicit_hours.get((base_subject, cn))
                    for _, cn, g in entries
                ]
                rate = hours_map.get(norm, default_hours)
                hours = sum(h if h is not None else rate for h in overrides)
                if norm in UNGRADED_SUBJECTS:
                    homeroom_hours += hours
                else:
                    academic_hours += hours
            workload.append({
                'teacher': profile,
                'items': items,
                'total_hours': disp_hours(academic_hours),
                'homeroom_hours': disp_hours(homeroom_hours),
            })
        workload.sort(key=lambda w: w['teacher'].full_name)

    return render(request, 'portal/documents_landing.html', {
        'school': school,
        'workload': workload,
        'can_maorif': can_maorif,
        'can_school_docs': _tabel_access(request.user),
        'can_pick_school': can_pick_school,
        'schools': schools,
    })


# ---------------------------------------------------------------------------
# Табел — automated teacher workload tabel (monthly).
#
# Reuses the Ҳуҷҷатнигорӣ gate and school isolation; weekly hours come from
# portal.tabel.teacher_workload (curriculum_hours resolver), and the deputy
# principal's manual monthly data lives in TeacherTabelMonth.
# ---------------------------------------------------------------------------

def _tabel_access(user):
    """Same audience as school_documents: superuser, staff, school zavuch,
    the school director (principal) or the district director (мудири
    маориф — read/export only; saves stay behind _can_manage_school /
    the zavuch-only write checks)."""
    role = get_user_role(user)
    return (user.is_superuser or user.is_staff or _is_zavuch(user, role)
            or role in (settings.ROLE_PRINCIPAL, settings.ROLE_DIRECTOR))


def _tabel_school(request):
    """User's own school; district viewers may inspect another via id."""
    school = get_user_school(request.user)
    if _maorif_report_access(request.user):
        school_id = request.GET.get('school_id') or request.POST.get('school_id')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
            except (ValueError, School.DoesNotExist):
                pass
    return school


def _has_school_doc_access(user, school):
    """Read-only school-documents access: own school via has_school_access,
    or any district viewer (superuser/staff/director) inspecting a school.
    Write endpoints keep has_school_access/_can_manage_school — district
    viewers never get those through this helper."""
    return has_school_access(user, school) or _maorif_report_access(user)


def _tabel_month(request):
    """(year, month, invalid) from ?month=YYYY-MM; defaults to current month."""
    raw = request.GET.get('month') or request.POST.get('month') or ''
    parsed = parse_month_param(raw)
    if parsed:
        return parsed[0], parsed[1], False
    today = timezone.localdate()
    return today.year, today.month, bool(raw)


def _tabel_redirect(school, year, month):
    url = f"{reverse('workload_tabel')}?month={year}-{month:02d}"
    if school:
        url += f"&school_id={school.id}"
    return redirect(url)


def _tabel_display_data(school, year, month):
    """Display rows + warnings + saved-month map for one school/month."""
    workload_rows, unassigned, unbanded = teacher_workload(school)
    # Month rows are owned by the school that recorded them: a transferred
    # teacher's historical rows stay under the original school.
    monthly_map = {
        m.teacher_id: m for m in TeacherTabelMonth.objects.filter(
            school=school, year=year, month=month)
    }
    # Verified-tariff hints need the full non-archived profile list (not
    # just allocated teachers) so duplicate names can be detected. The
    # extra flat query only runs for schools that actually have data.
    rate_hints = {}
    if tariff_data_available(school):
        rate_hints = suggested_rate_map(
            school, year, month,
            TeacherProfile.objects.filter(school=school, is_archived=False))
    return (
        merge_workload_and_monthly(workload_rows, monthly_map, rate_hints),
        unassigned,
        unbanded,
        monthly_map,
    )


@login_required
def workload_tabel(request):
    """Ҳуҷҷатнигорӣ → Табел: monthly workload grid with manual entries."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _tabel_school(request)
    if school and not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    year, month, invalid_month = _tabel_month(request)
    if invalid_month:
        messages.warning(request, 'Моҳи нодуруст — моҳи ҷорӣ нишон дода шуд.')

    display_rows, unassigned, unbanded = [], [], []
    if school:
        display_rows, unassigned, unbanded, _ = _tabel_display_data(
            school, year, month)

    role = get_user_role(request.user)
    can_edit = bool(school) and (
        request.user.is_superuser
        or (_is_zavuch(request.user, role) and get_user_school(request.user) == school)
    )
    return render(request, 'portal/workload_tabel.html', {
        'school': school,
        'rows': display_rows,
        'unassigned': unassigned,
        'unassigned_count': len(unassigned),
        'unbanded': unbanded,
        'unbanded_count': len(unbanded),
        'year': year,
        'month': month,
        'month_name': MONTH_NAMES_TJ[month],
        'month_value': f'{year}-{month:02d}',
        'can_edit': can_edit,
    })


@login_required
@require_POST
def workload_tabel_save(request):
    """Persist deputy-entered tabel data for one school+month.

    TeacherProfile.monthly_rate (stable) and TeacherTabelMonth rows
    (month-scoped, owned by this school) are the only writes; the workload
    snapshot is frozen once — when the month row is first created — so
    later re-allocation or follow-up edits cannot silently rewrite a
    recorded month.
    """
    role = get_user_role(request.user)
    if not (request.user.is_superuser or _is_zavuch(request.user, role)):
        messages.error(request, 'Дастрасӣ маҳдуд аст.')
        return redirect('dashboard')
    school = _tabel_school(request)
    if not school or not has_school_access(request.user, school):
        return redirect('dashboard')
    parsed_month = parse_month_param(request.POST.get('month'))
    if not parsed_month:
        messages.error(request, 'Моҳи нодуруст.')
        return redirect('workload_tabel')
    year, month = parsed_month

    profiles = {
        tp.id: tp for tp in TeacherProfile.objects.filter(
            school=school, is_archived=False)
    }
    workload_rows, _, _ = teacher_workload(school)
    workload_by_teacher = {r['teacher'].id: r for r in workload_rows}
    monthly_map = {
        m.teacher_id: m for m in TeacherTabelMonth.objects.filter(
            school=school, year=year, month=month)
    }

    hourly_fields = [f for fields in MONTHLY_BAND_FIELDS.values() for f in fields]
    money_fields = list(MONTHLY_ALLOWANCE_FIELDS)
    invalid = []
    saved_teachers = 0
    for tp in profiles.values():
        raw_rate = request.POST.get(f'rate_{tp.id}')
        new_rate = None
        if raw_rate is not None:
            try:
                new_rate = parse_decimal(raw_rate, max_value=MAX_MONEY_VALUE)
                if tp.monthly_rate != new_rate:
                    tp.monthly_rate = new_rate
                    tp.save(update_fields=['monthly_rate'])
            except (InvalidOperation, ValueError):
                invalid.append(tp.full_name)
                new_rate = None
        # A month frozen before any rate existed keeps a NULL snapshot
        # rate and could never be completed — record the entered rate
        # into that snapshot once. A recorded non-null rate is never
        # rewritten; this only fills the hole left by the first save.
        if new_rate is not None:
            month_row = monthly_map.get(tp.id)
            if month_row is not None and month_row.rates_snapshot:
                for line in month_row.rates_snapshot:
                    if line.get('line_no') == 1:
                        if line.get('rate') is None:
                            line['rate'] = str(new_rate)
                            month_row.save(
                                update_fields=['rates_snapshot'])
                        break

        updates = {}
        has_value = False
        for field in hourly_fields + money_fields:
            raw = request.POST.get(f'{field}_{tp.id}')
            if raw is None:
                continue
            limit = MAX_MONTHLY_HOURS if field in hourly_fields else MAX_MONEY_VALUE
            try:
                value = parse_decimal(raw, max_value=limit)
            except (InvalidOperation, ValueError):
                invalid.append(tp.full_name)
                continue
            updates[field] = value
            if value is not None:
                has_value = True
        if not updates:
            continue

        row = monthly_map.get(tp.id)
        if row is None and not has_value:
            continue  # nothing entered — do not create an empty month row
        with transaction.atomic():
            if row is None:
                # get_or_create is race-safe under the uniqueness
                # constraint; the snapshot is frozen only on first save
                # and is never refreshed by later edits.
                row, created = TeacherTabelMonth.objects.get_or_create(
                    school=school, teacher=tp, year=year, month=month)
                if created:
                    workload_row = workload_by_teacher.get(tp.id)
                    row.workload_snapshot = (
                        workload_snapshot(workload_row) if workload_row else {
                            'items': [], 'hours': {b: 0 for b in BAND_KEYS},
                            'total_hours': 0, 'subjects': '',
                        })
                    # Identity + rate freeze, same once-only contract as
                    # workload_snapshot — later profile edits and later
                    # saves must never rewrite a recorded month.
                    row.teacher_name = tp.full_name
                    row.rates_snapshot = [{
                        'line_no': 1,
                        'title': '',
                        'rate': (str(tp.monthly_rate)
                                 if tp.monthly_rate is not None else None),
                    }]
            for field, value in updates.items():
                setattr(row, field, value)
            row.save()
        saved_teachers += 1

    if invalid:
        messages.warning(
            request,
            'Қиматҳои нодуруст рад шуданд: %s' % ', '.join(sorted(set(invalid))),
        )
    messages.success(request, 'Табел барои %d омӯзгор сабт шуд.' % saved_teachers)
    return _tabel_redirect(school if request.user.is_superuser else None, year, month)


@login_required
def workload_tabel_export(request):
    """Excel export of the monthly tabel, preserving columns A–AB of the
    official template including the verified payroll formulas."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _tabel_school(request)
    if not school or not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    year, month, invalid_month = _tabel_month(request)
    if invalid_month:
        messages.warning(request, 'Моҳи нодуруст — моҳи ҷорӣ содир карда мешавад.')

    display_rows, _, _, _ = _tabel_display_data(school, year, month)
    output = build_tabel_workbook(school, year, month, display_rows)

    filename = f"Табел_{get_school_number(school)}_{year}-{month:02d}.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response


# ---------------------------------------------------------------------------
# Ҳисоботи синфҳо — quarterly class report workbook.
#
# Same Ҳуҷҷатнигорӣ gate and school isolation as the tabel; the xlsx is
# built on demand from the owner's template by portal.class_report and
# recalculates on open (wb.calculation.fullCalcOnLoad).
# ---------------------------------------------------------------------------

def _class_report_school(request):
    """User's own school; district viewers may pick one via ?school_id=."""
    school = get_user_school(request.user)
    if _maorif_report_access(request.user):
        school_id = request.GET.get('school_id') or request.POST.get('school_id')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
            except (ValueError, School.DoesNotExist):
                pass
    return school


def _class_report_quarter(request):
    """(quarter 1-4, invalid) from ?quarter=; defaults to the current one."""
    raw = request.GET.get('quarter') or request.POST.get('quarter') or ''
    try:
        quarter = int(raw)
    except (TypeError, ValueError):
        quarter = 0
    if quarter in (1, 2, 3, 4):
        return quarter, False
    return get_date_quarter(timezone.localdate()), bool(raw)


def _class_report_classes(school):
    """Graded classes that actually have students, with headcounts."""
    rows = (
        Student.objects.filter(school=school)
        .exclude(class_name='')
        .values('class_name')
        .annotate(students=Count('id'))
    )
    classes = [r for r in rows if not is_non_graded(r['class_name'])]
    classes.sort(key=lambda r: (class_numeric_part(r['class_name']), r['class_name']))
    return classes


def _class_report_redirect(school, quarter):
    url = f"{reverse('class_reports')}?quarter={quarter}"
    if school:
        url += f"&school_id={school.id}"
    return redirect(url)


@login_required
def class_reports(request):
    """Ҳуҷҷатнигорӣ → Ҳисоботи синфҳо: class picker + per-class export."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if school and not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(request, 'Чораки нодуруст — чораки ҷорӣ нишон дода шуд.')

    can_pick_school = _maorif_report_access(request.user)
    schools = sorted(
        academic_schools(), key=lambda s: s.name) if can_pick_school else []
    classes = _class_report_classes(school) if school else []
    return render(request, 'portal/class_reports.html', {
        'school': school,
        'schools': schools,
        'can_pick_school': can_pick_school,
        'classes': classes,
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
    })


@login_required
def class_report_export(request):
    """On-demand Excel «Ҳисобот» for one school + class + quarter.

    The selected class's students and QuarterGrade.grade values are the
    only data written into the owner's template; band/average cells keep
    the workbook's own formulas and recalc when the file is opened.
    """
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if not school or not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    class_name = normalize_class_name(request.GET.get('class_name') or '')
    if not class_name or is_non_graded(class_name):
        messages.error(request, 'Синфи нодуруст интихоб шудааст.')
        return _class_report_redirect(school, quarter)
    if not Student.objects.filter(school=school, class_name=class_name).exists():
        messages.error(request, f'Синфи {class_name} дар муассиса ёфт нашуд.')
        return _class_report_redirect(school, quarter)
    try:
        output = build_class_report_workbook(school, class_name, quarter)
    except ClassReportError as exc:
        messages.error(request, str(exc))
        return _class_report_redirect(school, quarter)

    filename = f"Ҳисобот_{class_name}_{get_school_number(school)}_чорак{quarter}.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response


@login_required
def zavuch_report_export(request):
    """Ҳуҷҷатнигорӣ → Ҳисоботи завуч: А.Ҷ.Ч. school summary as xlsx.

    Same gate, school isolation and quarter param as the class reports.
    Band cells hold computed counts; Омад/Рафт stay blank for manual
    entry until the portal tracks student mobility.
    """
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if not school:
        messages.warning(request, 'Барои ҳисобот муассисаи таълимиро интихоб кунед.')
        return redirect('dashboard')
    if not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    output = build_zavuch_report_workbook(school, quarter)

    filename = (
        f"Ҳисоботи_завуч_{get_school_number(school)}_чорак{quarter}.xlsx")
    encoded_filename = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f"attachment; filename*=utf-8''{encoded_filename}")
    return response


@login_required
def zavuch_grades_export(request):
    """Ҳуҷҷатнигорӣ → АЧЧ form: per-level quarterly summary as xlsx.

    Same gate, school isolation and quarter param as zavuch_report_export —
    the АЧЧ variant adds per-mark (10..1) columns, group subtotals
    (1-4 / 5-9 / 10-11) and a grand-total row.
    """
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if not school:
        messages.warning(request, 'Барои ҳисобот муассисаи таълимиро интихоб кунед.')
        return redirect('dashboard')
    if not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    output = build_zavuch_grades_workbook(school, quarter)

    filename = (
        f"АЧЧ{quarter}_{get_school_number(school)}_чорак{quarter}.xlsx")
    encoded_filename = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f"attachment; filename*=utf-8''{encoded_filename}")
    return response


@login_required
def excellent_students(request):
    """Ҳуҷҷатнигорӣ → Аълохон: preview + Excel download of the
    «Рӯйхати хонандагони аълохон (бо холи 9 ва 10)» document."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if school and not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(
            request, 'Чоряки нодуруст — чоряки ҷорӣ нишон дода шуд.')

    can_pick_school = _maorif_report_access(request.user)
    schools = sorted(
        academic_schools(), key=lambda s: s.name) if can_pick_school else []
    data = None
    if school:
        from .excellent import collect_excellent_students
        data = collect_excellent_students(school, quarter)
    return render(request, 'portal/excellent_students.html', {
        'school': school,
        'schools': schools,
        'can_pick_school': can_pick_school,
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
        'data': data,
    })


@login_required
def excellent_students_export(request):
    """Excel download of the same list — same gate and school isolation."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if not school or not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    from .excellent import build_excellent_workbook
    output = build_excellent_workbook(school, quarter)
    return _maorif_xlsx_response(
        output,
        f"Аълохон_{get_school_number(school)}_чорак{quarter}.xlsx")


@login_required
def ungraded_students(request):
    """Ҳуҷҷатнигорӣ → Баҳоланмаган: per-student preview + Excel export
    of the «баҳогузорӣ нашудаанд» list — who lacks a quarter mark, in
    which subject and under which teacher."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if school and not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(
            request, 'Чоряки нодуруст — чоряки ҷорӣ нишон дода шуд.')

    can_pick_school = _maorif_report_access(request.user)
    schools = sorted(
        academic_schools(), key=lambda s: s.name) if can_pick_school else []
    data = None
    if school:
        from .ungraded import collect_ungraded_students
        data = collect_ungraded_students(school, quarter)
    return render(request, 'portal/ungraded_students.html', {
        'school': school,
        'schools': schools,
        'can_pick_school': can_pick_school,
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
        'data': data,
    })


@login_required
def ungraded_students_export(request):
    """Excel download of the same list — same gate and school isolation."""
    if not _tabel_access(request.user):
        return redirect('dashboard')
    school = _class_report_school(request)
    if not school or not _has_school_doc_access(request.user, school):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    from .ungraded import build_ungraded_workbook
    output = build_ungraded_workbook(school, quarter)
    return _maorif_xlsx_response(
        output,
        f"Бебаҳо_{get_school_number(school)}_чорак{quarter}.xlsx")


def _absence_scope(request):
    """Ғоибият scope for the current user, or None when the page is off
    limits — a regular teacher who leads no class (no СОАТИ ТАРБИЯВӢ
    allocation = not a роҳбари синф).

    Zavuch/principal see every class of their school; district viewers
    pick a school via ?school_id=; the роҳбари синф sees only the
    class(es) whose Тарбиявӣ hour is allocated to them.
    """
    from .absences import led_class_names
    district = _maorif_report_access(request.user)
    school = _class_report_school(request)
    if district or _tabel_access(request.user):
        class_options = (
            [r['class_name'] for r in _class_report_classes(school)]
            if school else [])
    else:
        led = led_class_names(request.user, school)
        if not led:
            return None
        class_options = sorted(
            led, key=lambda n: (class_numeric_part(n) or 0, n))
    raw_class = request.GET.get('class') or ''
    class_name = normalize_class_name(raw_class) if raw_class else ''
    if class_name not in class_options:
        class_name = class_options[0] if class_options else ''
    from . import reports as reports_mod  # lazy — avoids import cycles
    options = reports_mod.report_period_options()
    period = request.GET.get('period', '')
    if period not in {v for v, _ in options}:
        period = f'q{get_date_quarter(datetime.date.today()) or 1}'
    start, end, period_label, _v, _q = reports_mod.resolve_period(period)
    return {
        'school': school, 'can_pick_school': district,
        'class_options': class_options, 'class_name': class_name,
        'period_options': options, 'selected_period': period,
        'period_label': period_label, 'bounds': (start, end),
    }


def _absence_retag(request, scope):
    """Ғоибият POST: роҳбари синф / завуч re-tags a student's absence
    marks inside a date window — «−»→«+» when a маълумотнома arrives,
    «+»→«−» to undo a mistake. scope['class_name'] is already
    authorised (it came out of the user's own class_options)."""
    student_id = request.POST.get('student_id', '').strip()
    try:
        start = datetime.date.fromisoformat(
            request.POST.get('date_from', ''))
        end = datetime.date.fromisoformat(
            request.POST.get('date_to', ''))
    except (TypeError, ValueError):
        student_id = ''
    if not student_id:
        messages.warning(request, 'Сана ё хонанда нодуруст интихоб шуд.')
        return
    if start > end:
        start, end = end, start
    student = Student.objects.filter(
        pk=student_id, school=scope['school'],
        class_name=scope['class_name']).first()
    from .exemptions import home_student_ids
    if (student is None
            or student.pk in home_student_ids([student_id])):
        messages.warning(request, 'Хонанда дар ин синф нест.')
        return
    from .absences import retag_absences
    to_excused = request.POST.get('action') != 'unexcuse'
    res = retag_absences(
        scope['school'], student, start, end, to_excused=to_excused)
    word = 'бо сабаб' if to_excused else 'бе сабаб'
    if res['changed']:
        messages.success(
            request, f"{res['changed']} соат {word} қайд шуд.")
    else:
        messages.info(request, 'Дар ин фосила қайд ёфта нашуд.')
    if res['locked']:
        messages.warning(
            request,
            f"{res['locked']} соат дар чоряки ҷамъбастшуда монд — "
            "тағйир наёфт.")


@login_required
def absences(request):
    """⏰ Ғоибият — the journal's back register: per-student missed
    lesson hours (бо сабаб / бе сабаб) for one class and a period
    (month / quarter / semester / year)."""
    scope = _absence_scope(request)
    if scope is None:
        return redirect('dashboard')
    if request.method == 'POST':
        if scope['school'] and scope['class_name']:
            _absence_retag(request, scope)
        return redirect(
            f"{request.path}?{request.GET.urlencode()}")
    rows = None
    if scope['school'] and scope['class_name']:
        from .absences import collect_class_absences
        from .class_report import class_teacher_name
        rows = collect_class_absences(
            scope['school'], scope['class_name'], *scope['bounds'])
        scope['class_teacher'] = class_teacher_name(
            scope['school'], scope['class_name'])
        scope['totals'] = {
            'excused': sum(r['excused'] for r in rows),
            'unexcused': sum(r['unexcused'] for r in rows),
            'total': sum(r['total'] for r in rows),
        }
    schools = (sorted(academic_schools(), key=lambda s: s.name)
               if scope['can_pick_school'] else [])
    return render(request, 'portal/absences.html',
                  {**scope, 'rows': rows, 'schools': schools})


@login_required
def absences_export(request):
    """Excel of the same register — identical scoping."""
    scope = _absence_scope(request)
    if scope is None or not (scope['school'] and scope['class_name']):
        return redirect('absences')
    from .absences import build_absence_workbook, collect_class_absences
    rows = collect_class_absences(
        scope['school'], scope['class_name'], *scope['bounds'])
    output = build_absence_workbook(
        scope['school'], scope['class_name'], scope['period_label'], rows)
    return _maorif_xlsx_response(
        output,
        f"Ғоибият_{scope['class_name']}_{scope['period_label']}.xlsx")


def _maorif_report_access(user):
    """District summary: superuser, маориф staff and the district director.

    Zavuchs are deliberately excluded — the table compares all schools.
    """
    return (user.is_superuser or user.is_staff
            or get_user_role(user) == settings.ROLE_DIRECTOR)


def _nazorat_access(user):
    """The school-control tab: zavuch / school director (own school)
    + district viewers."""
    role = get_user_role(user)
    return (_maorif_report_access(user) or _is_zavuch(user, role)
            or role == settings.ROLE_PRINCIPAL)


@login_required
def nazorat(request):
    """🔎 Назорат — live per-school control page for завуч and маориф.

    Zavuch sees only their own school; superuser/staff/director may pick
    any academic school via ?school_id=. Sections are collected by
    portal.nazorat and appended as cards — new queries slot in later.
    """
    if not _nazorat_access(request.user):
        return redirect('dashboard')
    district_view = _maorif_report_access(request.user)

    schools = []
    if district_view:
        from .maorif_report import _district_sort_key
        schools = sorted(academic_schools(), key=_district_sort_key)
        school = get_user_school(request.user)
        school_id = request.GET.get('school_id')
        if school_id:
            try:
                school = School.objects.get(pk=int(school_id))
            except (ValueError, School.DoesNotExist):
                pass
        if school is None and schools:
            school = schools[0]
    else:
        school = get_user_school(request.user)

    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(
            request, 'Чоряки нодуруст — чоряки ҷорӣ нишон дода шуд.')

    from .nazorat import (
        ABSENCE_MIN_OPTIONS, attendance_period_options,
        collect_absence_students, collect_attendance_control,
        collect_behavior_issues, collect_grading_control,
        collect_norm_tree, collect_teacher_control)
    stats = attendance = teachers = absence = behavior = norm_tree = None
    att_options = []
    try:
        abm = int(request.GET.get('abm', 3))
    except (TypeError, ValueError):
        abm = 3
    if abm not in ABSENCE_MIN_OPTIONS:
        abm = 3
    if school is not None:
        stats = collect_grading_control(school, quarter)
        attendance = collect_attendance_control(
            school, request.GET.get('att'))
        att_options = attendance_period_options(stats['academic_year'])
        teachers = collect_teacher_control(school, quarter)
        absence = collect_absence_students(
            school, request.GET.get('att'), min_hours=abm)
        behavior = collect_behavior_issues(school)
        norm_tree = collect_norm_tree(school)
    return render(request, 'portal/nazorat.html', {
        'school': school,
        'schools': schools,
        'district_view': district_view,
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
        'stats': stats,
        'attendance': attendance,
        'att_options': att_options,
        'teachers': teachers,
        'absence': absence,
        'absence_min_options': ABSENCE_MIN_OPTIONS,
        'abm': abm,
        'behavior': behavior,
        'norm_tree': norm_tree,
    })


def _movement_scope(request):
    """(school, district_view, schools, allowed) for the Омад-рафт pages.

    Same audience and school resolution as the Назорат tab: the zavuch
    is bound to their own school; district viewers pick ?school_id=.
    """
    if not _nazorat_access(request.user):
        return None, False, [], False
    if not _maorif_report_access(request.user):
        return get_user_school(request.user), False, [], True
    from .maorif_report import _district_sort_key
    schools = sorted(academic_schools(), key=_district_sort_key)
    school = get_user_school(request.user)
    school_id = (request.GET.get('school_id')
                 or request.POST.get('school_id'))
    if school_id:
        try:
            school = School.objects.get(pk=int(school_id))
        except (ValueError, School.DoesNotExist):
            pass
    if school is None and schools:
        school = schools[0]
    return school, True, schools, True


def _movement_redirect(request, school, quarter):
    url = f"{reverse('student_movements')}?quarter={quarter}"
    if school and _maorif_report_access(request.user):
        url += f"&school_id={school.id}"
    return redirect(url + '#records')


def _movement_list_title(school):
    """Official header shown above the Қайдҳо list and reused by the
    Excel export — «Рӯйхати хонандагони ба муассиса қабулшуда…»"""
    ay = academic_year_start()
    return ('Рӯйхати хонандагони ба муассиса қабулшуда ва аз муассиса '
            f'хориҷшуда дар соли таҳсили {ay}–{ay + 1} дар {school.name}')


@login_required
def student_movements(request):
    """📋 Омад-рафт — the school's student mobility journal.

    Automatic rows are written by add_student / remove_student; the
    zavuch can add manual rows (moves that predate tracking) or delete
    mistakes. The А.Ҷ.Ч. export fills its Омад/Рафт columns from the
    same records.
    """
    school, district_view, schools, allowed = _movement_scope(request)
    if not allowed:
        return redirect('dashboard')
    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(
            request, 'Чоряки нодуруст — чоряки ҷорӣ нишон дода шуд.')

    summary = None
    class_options = []
    can_edit = bool(school) and _can_manage_school(request.user, school)
    if school is not None:
        from .movements import collect_movements
        summary = collect_movements(school, quarter)
        class_options = sorted(
            set(Student.objects.filter(school=school)
                .exclude(class_name='')
                .values_list('class_name', flat=True)),
            key=lambda n: (class_numeric_part(n) or 0, n))
    edit_row = None
    if can_edit and request.GET.get('edit'):
        # ?edit=N pre-fills the entry form for correction.
        edit_row = StudentMovement.objects.filter(
            pk=request.GET['edit'], school=school).first()
    return render(request, 'portal/movements.html', {
        'school': school,
        'schools': schools,
        'district_view': district_view,
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
        'summary': summary,
        'class_options': class_options,
        'can_edit': can_edit,
        'edit_row': edit_row,
        'list_title': _movement_list_title(school) if school else '',
        'today': timezone.localdate().isoformat(),
    })


@login_required
def movements_export(request):
    """📊 Excel of the Қайдҳо list — same quarter scope and official
    header as the printed journal page."""
    school, _, _, allowed = _movement_scope(request)
    if not allowed or school is None:
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    from .movements import collect_movements
    summary = collect_movements(school, quarter)

    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Омад-рафт'
    for col, width in zip('ABCDEFG', (6, 13, 32, 10, 10, 12, 34)):
        ws.column_dimensions[col].width = width

    ws.merge_cells('A1:G1')
    cell = ws.cell(row=1, column=1, value=_movement_list_title(school))
    cell.font = Font(size=12, bold=True)
    cell.alignment = Alignment(horizontal='center', vertical='center',
                               wrap_text=True)
    ws.row_dimensions[1].height = 48

    header_font = Font(size=11, bold=True)
    for c, label in enumerate(
            ('№', 'Сана', 'Ному насаб', 'Синф', 'Навъ', 'Ҷинс', 'Эзоҳ'),
            start=1):
        cell = ws.cell(row=3, column=c, value=label)
        cell.font = header_font
        cell.border = border
        cell.alignment = Alignment(horizontal='center', vertical='center',
                                   wrap_text=True)
        cell.fill = PatternFill('solid', start_color='E8EEF7')

    r = 4
    for i, m in enumerate(summary['movements'], start=1):
        nav = 'Омад' if m['movement_type'] == 'arrival' else 'Рафт'
        jins = {'F': 'Духтар', 'M': 'Писар'}.get(m['gender'], '—')
        values = (i, m['date'].strftime('%d.%m.%Y'), m['student_name'],
                  m['class_name'], nav, jins, m['note'] or '')
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(size=11)
            cell.border = border
            cell.alignment = Alignment(
                horizontal='left' if c in (3, 7) else 'center',
                vertical='center', wrap_text=True)
        r += 1
    if not summary['movements']:
        ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=7)
        cell = ws.cell(row=4, column=1,
                       value='Дар ин чоряк ҳаракат сабт нашудааст.')
        cell.font = Font(size=11)
        r += 1

    r += 1
    for label in ('Директори муассиса: ______________',
                  'Муовини директор оид ба таълим: ______________'):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.cell(row=r, column=1, value=label).font = Font(size=11)
        r += 1

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return _maorif_xlsx_response(
        output,
        f"Омад-рафт_{get_school_number(school)}_чорак{quarter}.xlsx")


@login_required
@require_POST
def movement_add(request):
    """Manual Омад/Рафт row — covers moves that predate tracking."""
    school, _, _, allowed = _movement_scope(request)
    if (not allowed or school is None
            or not _can_manage_school(request.user, school)):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)

    full_name = request.POST.get('full_name', '').strip()
    class_name = _validate_class_name(request.POST.get('class_name'))
    mtype = request.POST.get('movement_type', '')
    gender = request.POST.get('gender', '').strip()
    note = request.POST.get('note', '').strip()
    try:
        date = datetime.date.fromisoformat(
            request.POST.get('date', '').strip())
    except ValueError:
        date = None

    if not full_name or not class_name \
            or mtype not in ('arrival', 'departure') or date is None:
        messages.error(request, 'Иттилоот нопурра — қайд сабт нашуд.')
        return _movement_redirect(request, school, quarter)

    if gender not in ('M', 'F'):
        from .gender_detector import detect_student_gender
        gender = detect_student_gender(full_name) or ''
    student = Student.all_objects.filter(
        school=school, class_name=class_name,
        full_name=full_name).first()
    StudentMovement.objects.create(
        school=school, student=student, student_name=full_name,
        class_name=class_name, gender=gender, movement_type=mtype,
        date=date, is_manual=True, note=note, created_by=request.user)
    messages.success(request, 'Қайд сабт шуд.')
    return _movement_redirect(request, school, quarter)


@login_required
@require_POST
def movement_delete(request):
    """Remove a journal row — only rows of the resolved school."""
    school, _, _, allowed = _movement_scope(request)
    if (not allowed or school is None
            or not _can_manage_school(request.user, school)):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    StudentMovement.objects.filter(
        pk=request.POST.get('movement_id'), school=school).delete()
    messages.success(request, 'Қайд нест карда шуд.')
    return _movement_redirect(request, school, quarter)


@login_required
@require_POST
def movement_edit(request):
    """Correct a journal row — same validation as movement_add.

    Manual and auto rows are both editable (e.g. fixing a departure
    recorded with the wrong date); the is_manual badge keeps marking
    where the row came from.
    """
    school, _, _, allowed = _movement_scope(request)
    if (not allowed or school is None
            or not _can_manage_school(request.user, school)):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    movement = StudentMovement.objects.filter(
        pk=request.POST.get('movement_id'), school=school).first()
    if movement is None:
        messages.error(request, 'Қайд топилмади.')
        return _movement_redirect(request, school, quarter)

    full_name = request.POST.get('full_name', '').strip()
    class_name = _validate_class_name(request.POST.get('class_name'))
    mtype = request.POST.get('movement_type', '')
    gender = request.POST.get('gender', '').strip()
    note = request.POST.get('note', '').strip()
    try:
        date = datetime.date.fromisoformat(
            request.POST.get('date', '').strip())
    except ValueError:
        date = None

    if not full_name or not class_name \
            or mtype not in ('arrival', 'departure') or date is None:
        messages.error(request, 'Иттилоот нопурра — тағйирот сабт нашуд.')
        return _movement_redirect(request, school, quarter)

    if gender not in ('M', 'F'):
        from .gender_detector import detect_student_gender
        gender = detect_student_gender(full_name) or ''
    movement.student = Student.all_objects.filter(
        school=school, class_name=class_name,
        full_name=full_name).first()
    movement.student_name = full_name
    movement.class_name = class_name
    movement.gender = gender
    movement.movement_type = mtype
    movement.date = date
    movement.academic_year = None   # save() recomputes from the new date
    movement.note = note
    movement.save()
    messages.success(request, 'Қайд таҳрир шуд.')
    return _movement_redirect(request, school, quarter)


def _years_redirect(request, school):
    url = reverse('academic_years')
    if school and _maorif_report_access(request.user):
        url += f"?school_id={school.id}"
    return redirect(url)


@login_required
def academic_years(request):
    """📁 Солҳои таҳсил — per-year quarter status + archive download.

    Each quarter card shows how many class-subjects are locked, the
    official window and an Excel archive download. Closing happens on
    the separate confirmation page (quarter_close), so an accidental
    click can never lock a quarter.
    """
    school, district_view, schools, allowed = _movement_scope(request)
    if not allowed:
        return redirect('dashboard')

    years_data = []
    if school is not None:
        from .quarter_close import (archive_path, auto_close_due,
                                    collect_academic_years,
                                    collect_quarter_close)
        # Automatic closeout: a quarter whose grading window has ended
        # locks itself (with archive) the next time this page opens.
        for q, ay in auto_close_due(school):
            messages.info(
                request,
                f'Чоряки {q} соли {ay}-{ay + 1} муддати баҳогузорӣ '
                'гузашта буд — автоматик ҷамъбаст шуд.')
        for ay in collect_academic_years(school):
            quarters = []
            for q in (1, 2, 3, 4):
                qs = collect_quarter_close(school, q, ay)
                qs['archive_ready'] = (
                    qs['closed']
                    or archive_path(school, q, ay).exists())
                quarters.append(qs)
            years_data.append({'year': ay, 'quarters': quarters})
    return render(request, 'portal/academic_years.html', {
        'school': school,
        'schools': schools,
        'district_view': district_view,
        'years': years_data,
        'can_close': bool(school) and _can_manage_school(
            request.user, school),
        'can_reopen': _maorif_report_access(request.user),
        'current_year': academic_year_start(),
        # Default «то <date>» for the маориф reopen input — a 10-day
        # window; the field stays editable for any other date.
        'reopen_default': (
            datetime.date.today() + datetime.timedelta(days=10)
        ).isoformat(),
    })


@login_required
def quarter_close(request):
    """Якуни чоряк — two-step closeout for the завуч.

    GET  renders the confirmation page: deficiencies (students without
    a чорак grade per class-subject) + the warning the user must read.
    POST requires the literal word ТАСДИҚ — only then are QuarterLock
    rows written and the Excel archive frozen to media/archives/.
    """
    school, _, _, allowed = _movement_scope(request)
    if (not allowed or school is None
            or not _can_manage_school(request.user, school)):
        return redirect('dashboard')

    from .quarter_close import (close_quarter, collect_quarter_close,
                                save_quarter_archive)
    try:
        quarter = int(request.POST.get('quarter') or
                      request.GET.get('quarter'))
        year = int(request.POST.get('year') or request.GET.get('year'))
    except (TypeError, ValueError):
        return _years_redirect(request, school)
    if quarter not in (1, 2, 3, 4):
        return _years_redirect(request, school)

    qs = collect_quarter_close(school, quarter, year)
    if qs['closed']:
        messages.info(request, 'Ин чоряк аллакай ҷамъбаст шудааст.')
        return _years_redirect(request, school)

    if request.method == 'POST':
        if request.POST.get('confirm', '').strip().upper() != 'ТАСДИҚ':
            messages.error(
                request, 'Барои ҷамъбаст калимаи «ТАСДИҚ» навишта шавад.')
            return render(request, 'portal/quarter_close.html', {
                'school': school, 'qs': qs})
        n = close_quarter(school, quarter, year)
        save_quarter_archive(school, quarter, year)
        messages.success(
            request,
            f'Чоряки {qs["ordinal"]} ҷамъбаст шуд — '
            f'{n} фан баста гашт ва архив сабт гардид.')
        return _years_redirect(request, school)

    return render(request, 'portal/quarter_close.html', {
        'school': school, 'qs': qs})


@login_required
@require_POST
def quarter_reopen(request):
    """Маориф/админ only: unlock a previously closed quarter."""
    school, _, _, allowed = _movement_scope(request)
    if not allowed or school is None:
        return redirect('dashboard')
    if not _maorif_report_access(request.user):
        messages.error(request, 'Кушодани чоряк танҳо маориф метавонад.')
        return _years_redirect(request, school)
    try:
        quarter = int(request.POST.get('quarter'))
        year = int(request.POST.get('year'))
    except (TypeError, ValueError):
        return _years_redirect(request, school)
    if quarter in (1, 2, 3, 4):
        # Optional «то <date>» reopen: after that day the cron's
        # auto_close_due locks the quarter again on its own.
        until = None
        raw = request.POST.get('until', '').strip()
        if raw:
            try:
                until = datetime.date.fromisoformat(raw)
            except ValueError:
                messages.error(request, 'Санаи нодуруст.')
                return _years_redirect(request, school)
        from .quarter_close import reopen_quarter
        n = reopen_quarter(school, quarter, year, until=until)
        if until:
            messages.success(
                request, f'Чоряк то {until:%d.%m.%Y} кушода шуд — '
                         f'{n} фан озод гардид.')
        else:
            messages.success(
                request, f'Чоряк кушода шуд — {n} фан озод гардид.')
    return _years_redirect(request, school)


@login_required
def quarter_archive_download(request):
    """Serve the frozen archive; regenerates when the file is missing
    (e.g. archived before this feature existed)."""
    school, _, _, allowed = _movement_scope(request)
    if not allowed or school is None:
        return redirect('dashboard')
    try:
        quarter = int(request.GET.get('quarter'))
        year = int(request.GET.get('year'))
    except (TypeError, ValueError):
        return _years_redirect(request, school)
    if quarter not in (1, 2, 3, 4):
        return _years_redirect(request, school)

    from .quarter_close import (archive_path, build_quarter_archive,
                                save_quarter_archive)
    path = archive_path(school, quarter, year)
    if path.exists():
        output = io.BytesIO(path.read_bytes())
    else:
        output = build_quarter_archive(school, quarter, year)
        if collect_quarter_close_marker(school, quarter, year):
            save_quarter_archive(school, quarter, year)
    return _maorif_xlsx_response(
        output,
        f"Архив_{get_school_number(school)}_чорак{quarter}_{year}-{year + 1}.xlsx")


def collect_quarter_close_marker(school, quarter, year):
    """True when the quarter is fully closed — archive snapshots are
    then persisted so later downloads stay byte-identical."""
    from .quarter_close import collect_quarter_close
    return collect_quarter_close(school, quarter, year)['closed']


@login_required
def maorif_reports(request):
    """Ҳуҷҷатнигорӣ → Ҳисоботи маориф: list of district Excel reports.

    District-level audience only (superuser / staff / ROLE_DIRECTOR).
    New report files register as rows on this page.
    """
    if not _maorif_report_access(request.user):
        return redirect('dashboard')
    quarter, invalid_quarter = _class_report_quarter(request)
    if invalid_quarter:
        messages.warning(
            request, 'Чоряки нодуруст — чоряки ҷорӣ нишон дода шуд.')
    return render(request, 'portal/maorif_reports.html', {
        'quarter': quarter,
        'quarters': [1, 2, 3, 4],
    })


def _maorif_xlsx_response(output, filename):
    encoded = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = (
        f"attachment; filename*=utf-8''{encoded}")
    return response


@login_required
def maorif_report_export(request):
    """District А.Ҷ.Ч. summary as xlsx — one row per academic school."""
    if not _maorif_report_access(request.user):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    prepared_by = request.user.get_full_name() or request.user.username
    output = build_maorif_report_workbook(quarter, prepared_by=prepared_by)
    return _maorif_xlsx_response(
        output, f"Ҳисобот_АҶЧ{quarter}_муассисаҳо.xlsx")


@login_required
def maorif_report_total_export(request):
    """District А.Ҷ.Ч. summary as xlsx — single Хамагӣ (district total) row."""
    if not _maorif_report_access(request.user):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    prepared_by = request.user.get_full_name() or request.user.username
    output = build_maorif_total_workbook(quarter, prepared_by=prepared_by)
    return _maorif_xlsx_response(
        output, f"Ҳисобот_АҶЧ{quarter}_ҳамагӣ.xlsx")


@login_required
def maorif_report_grades_export(request):
    """Oblast form: per-grade Хол (10..1) columns, one row per school."""
    if not _maorif_report_access(request.user):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    prepared_by = request.user.get_full_name() or request.user.username
    output = build_maorif_grades_workbook(quarter, prepared_by=prepared_by)
    return _maorif_xlsx_response(
        output, f"Ҳисобот_АҶЧ{quarter}_холҳо_муассисаҳо.xlsx")


@login_required
def maorif_report_grades_district_export(request):
    """Oblast form: single «ноҳияи Зафаробод» row + Хамагӣ."""
    if not _maorif_report_access(request.user):
        return redirect('dashboard')
    quarter, _ = _class_report_quarter(request)
    prepared_by = request.user.get_full_name() or request.user.username
    output = build_maorif_grades_district_workbook(
        quarter, prepared_by=prepared_by)
    return _maorif_xlsx_response(
        output, f"Ҳисобот_АҶЧ{quarter}_холҳо_ноҳия.xlsx")


@login_required
def export_monitoring_excel(request):
    """Export the monitoring dashboard data as a styled Excel workbook.

    Uses the same centralized scope as the dashboard: district viewers get
    every school's full detail, principals only their own school, and
    zavuchs get aggregate-only rows for other schools.
    """
    if not can_access_monitoring(request.user):
        return redirect('dashboard')

    stats = scope_monitoring_stats(request.user, _get_monitoring_stats())
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

        if row.get('masked'):
            login_status = '—'
        else:
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


# ---------------------------------------------------------------------------
# Ахбор ва хабарҳо — district news module
# ---------------------------------------------------------------------------

def _news_forbidden(request):
    """Redirect unauthorized users away from news management."""
    return redirect('dashboard')


def _news_allowed(request):
    from .news import can_manage_news
    return can_manage_news(request.user)


def news_archive(request):
    """Public archive: published news by category, paginated."""
    from django.core.paginator import Paginator
    from .models import News

    qs = (News.objects.filter(status=News.STATUS_PUBLISHED)
          .select_related('school')
          .prefetch_related('photos'))
    category = request.GET.get('cat', '')
    if category:
        qs = qs.filter(category=category)
    school_id = request.GET.get('school_id', '')
    if school_id.isdigit():
        qs = qs.filter(school_id=int(school_id))

    page = Paginator(qs, 9).get_page(request.GET.get('page'))
    return render(request, 'portal/news_archive.html', {
        'page': page,
        'categories': News.CATEGORY_CHOICES,
        'category': category,
        'school_id': school_id,
        'schools': School.objects.all().order_by('name'),
        'can_manage': _news_allowed(request),
    })


def news_detail(request, pk):
    """Full news page. Public items only; managers may preview any status."""
    from .models import News

    news = get_object_or_404(
        News.objects.select_related('school', 'author')
        .prefetch_related('photos'), pk=pk)
    is_manager = _news_allowed(request)
    if news.status != News.STATUS_PUBLISHED and not is_manager:
        return redirect('news_archive')
    from .news import video_enabled
    return render(request, 'portal/news_detail.html', {
        'news': news,
        'can_manage': is_manager,
        'is_preview': news.status != News.STATUS_PUBLISHED,
        'show_video': (video_enabled()
                       and news.media_type == News.MEDIA_VIDEO
                       and bool(news.video_code or news.video_file)),
    })


@login_required
def news_admin(request):
    """Management list with status tabs + per-item actions."""
    if not _news_allowed(request):
        return _news_forbidden(request)
    from .models import News

    status = request.GET.get('status', 'all')
    qs = (News.objects.select_related('school', 'author')
          .prefetch_related('photos')
          .order_by('-updated_at'))
    if status in dict(News.STATUS_CHOICES):
        qs = qs.filter(status=status)
    status_rows = [
        (s, label, News.objects.filter(status=s).count())
        for s, label in News.STATUS_CHOICES]
    return render(request, 'portal/news_admin.html', {
        'items': qs,
        'status': status,
        'status_rows': status_rows,
    })


def _news_form_context(news=None, errors=None, form=None):
    from .models import News
    from .news import video_enabled
    return {
        'news': news,
        'errors': errors or [],
        'form': form or {},
        'categories': News.CATEGORY_CHOICES,
        'schools': School.objects.all().order_by('name'),
        'max_photos': 5,
        'video_enabled': video_enabled(),
    }


def _validate_news_post(request, news=None):
    """Collect + validate form fields. Returns (data, files, errors)."""
    from .models import News
    from .news import (
        canonical_video_url, extract_video_code, validate_video_file,
        video_enabled,
    )

    data = {
        'category': (request.POST.get('category') or '').strip(),
        'title': (request.POST.get('title') or '').strip(),
        'school_id': (request.POST.get('school_id') or '').strip(),
        'event_date': (request.POST.get('event_date') or '').strip(),
        'summary': (request.POST.get('summary') or '').strip(),
    }
    errors = []
    if data['category'] not in dict(News.CATEGORY_CHOICES):
        errors.append('Категорияро интихоб кунед.')
    if not data['title']:
        errors.append('Сарлавҳаро нависед.')
    if not data['event_date']:
        errors.append('Санаи воқеаро интихоб кунед.')
    else:
        try:
            data['event_date'] = datetime.date.fromisoformat(
                data['event_date'])
        except ValueError:
            errors.append('Санаи воқеа нодуруст аст.')
    if not data['summary']:
        errors.append('Тавсифи кӯтоҳро нависед.')

    school = None
    if data['school_id']:
        try:
            school = School.objects.get(pk=int(data['school_id']))
        except (School.DoesNotExist, ValueError):
            errors.append('Муассиса нодуруст интихоб шуд.')
    data['school'] = school

    # Media type: only a posted value is honoured; with the video feature
    # switched off (or a stale client) the stored type is kept untouched.
    posted_type = request.POST.get('media_type') or ''
    if posted_type in dict(News.MEDIA_CHOICES) and (
            posted_type != News.MEDIA_VIDEO or video_enabled()):
        data['media_type'] = posted_type
    elif news is not None:
        data['media_type'] = news.media_type
    else:
        data['media_type'] = News.MEDIA_PHOTO

    data['video_url'] = (request.POST.get('video_url') or '').strip()
    data['video_code'] = ''
    data['video_file'] = request.FILES.get('video_file')
    if data['media_type'] == News.MEDIA_VIDEO:
        if data['video_file'] is not None:
            # Local file wins over the Instagram link when both are sent.
            try:
                validate_video_file(data['video_file'])
            except ValueError as e:
                errors.append(str(e))
            else:
                data['video_url'] = ''
        elif data['video_url']:
            code = extract_video_code(data['video_url'])
            if code is None:
                errors.append('Ҳаволаи нодуруст — танҳо ҳаволаи расмии '
                              'Instagram Reels қабул мешавад.')
            else:
                data['video_code'] = code
                data['video_url'] = canonical_video_url(code)
        elif news is None or not (news.video_file or news.video_code):
            errors.append('Видео файлини бор кунед ё ҳаволаи '
                          'Instagram Reels-ро ворид кунед.')

    files = request.FILES.getlist('photos')
    existing = news.photos.count() if news else 0
    if existing + len(files) > 5:
        errors.append(f'Аксҳо наметавонанд зиёда аз 5 бошанд '
                      f'(ҳоло {existing}, нав {len(files)}).')
    elif existing + len(files) < 1 \
            and data['media_type'] != News.MEDIA_VIDEO:
        errors.append('Ҳадди ақал як акс зарур аст.')
    return data, files, errors


@login_required
def news_create(request):
    if not _news_allowed(request):
        return _news_forbidden(request)
    from .models import News
    from .news import save_photo

    if request.method == 'POST':
        data, files, errors = _validate_news_post(request)
        if not errors:
            news = News.objects.create(
                category=data['category'], title=data['title'],
                school=data['school'], event_date=data['event_date'],
                summary=data['summary'], author=request.user,
                media_type=data['media_type'],
                video_url=(data['video_url']
                           if data['media_type'] == News.MEDIA_VIDEO
                           else ''),
                video_code=data['video_code'],
                video_file=(data['video_file']
                            if data['media_type'] == News.MEDIA_VIDEO
                            else None))
            try:
                for i, f in enumerate(files):
                    save_photo(news, f, order=i, is_main=(i == 0))
            except ValueError as e:
                news.delete()
                errors.append(str(e))
            else:
                if news.video_code:
                    # Cover fetched once at save time — never on the
                    # public request path; a failure only falls back to
                    # the styled placeholder cover.
                    from .news import fetch_video_thumb
                    fetch_video_thumb(news)
                    if news.video_thumb:
                        news.save(update_fields=['video_thumb'])
                messages.success(request,
                                 'Хабар ҳамчун лоиҳа нигоҳ дошта шуд.')
                return redirect('news_admin')
        return render(request, 'portal/news_form.html',
                      _news_form_context(errors=errors, form=data))
    return render(request, 'portal/news_form.html', _news_form_context())


@login_required
def news_edit(request, pk):
    if not _news_allowed(request):
        return _news_forbidden(request)
    from .models import News
    from .news import save_photo

    news = get_object_or_404(
        News.objects.prefetch_related('photos'), pk=pk)
    if request.method == 'POST':
        data, files, errors = _validate_news_post(request, news)
        if not errors:
            old_code = news.video_code
            news.category = data['category']
            news.title = data['title']
            news.school = data['school']
            news.event_date = data['event_date']
            news.summary = data['summary']
            news.media_type = data['media_type']
            if data['media_type'] == News.MEDIA_VIDEO:
                if data['video_file'] is not None:
                    # Replacing with a local file drops the Instagram
                    # link and frees the old file from storage.
                    if news.video_file:
                        news.video_file.delete(save=False)
                    news.video_file = data['video_file']
                    news.video_url = ''
                    news.video_code = ''
                elif data['video_code']:
                    # Switching to an Instagram link drops a stored file.
                    if news.video_file:
                        news.video_file.delete(save=False)
                        news.video_file = ''
                    news.video_url = data['video_url']
                    news.video_code = data['video_code']
            else:
                news.video_url = ''
                news.video_code = ''
                if news.video_thumb:
                    news.video_thumb.delete(save=False)
                if news.video_file:
                    news.video_file.delete(save=False)
                    news.video_file = ''
            news.save()
            try:
                start = news.photos.count()
                for i, f in enumerate(files):
                    save_photo(news, f, order=start + i)
            except ValueError as e:
                errors.append(str(e))
            else:
                if not news.photos.filter(is_main=True).exists():
                    first = news.photos.first()
                    if first:
                        first.is_main = True
                        first.save(update_fields=['is_main'])
                if news.video_code and (
                        news.video_code != old_code
                        or not news.video_thumb):
                    from .news import fetch_video_thumb
                    fetch_video_thumb(news)
                    if news.video_thumb:
                        news.save(update_fields=['video_thumb'])
                messages.success(request, 'Хабар нав карда шуд.')
                return redirect('news_admin')
        form = dict(data)
        form['school_id'] = data['school_id']
        form['event_date'] = request.POST.get('event_date', '')
        return render(request, 'portal/news_form.html',
                      _news_form_context(news, errors, form))
    form = {
        'category': news.category, 'title': news.title,
        'school_id': news.school_id or '',
        'event_date': news.event_date.isoformat(),
        'summary': news.summary,
        'media_type': news.media_type,
        'video_url': news.video_url,
    }
    return render(request, 'portal/news_form.html',
                  _news_form_context(news, form=form))


@login_required
@require_POST
def news_action(request, pk):
    """Status transitions: publish / unpublish / archive / restore / delete."""
    if not _news_allowed(request):
        return _news_forbidden(request)
    from .models import News
    from . import news as news_mod

    news = get_object_or_404(News, pk=pk)
    action = request.POST.get('action', '')
    if action == 'publish':
        from .news import video_enabled
        has_media = news.photos.exists() or (
            video_enabled()
            and news.media_type == News.MEDIA_VIDEO
            and bool(news.video_code or news.video_file))
        if not has_media:
            messages.error(request, 'Бе акс ё видео нашр кардан мумкин нест.')
        else:
            news_mod.publish(news)
            messages.success(request, 'Хабар нашр шуд.')
    elif action == 'unpublish':
        news_mod.unpublish(news)
    elif action == 'archive':
        news_mod.archive(news)
    elif action == 'restore':
        news_mod.restore(news)
    elif action == 'delete':
        news.delete()
        messages.success(request, 'Хабар нест карда шуд.')
    else:
        messages.error(request, 'Амали нодуруст.')
    return redirect(request.POST.get('next') or 'news_admin')


@login_required
@require_POST
def news_photo_action(request, photo_id):
    """Per-photo actions: delete / set-main."""
    if not _news_allowed(request):
        return _news_forbidden(request)
    from .models import NewsPhoto
    from .news import replace_main_photo

    photo = get_object_or_404(NewsPhoto.objects.select_related('news'),
                            pk=photo_id)
    news_id = photo.news_id
    action = request.POST.get('action', '')
    if action == 'delete':
        # A published item needs at least one medium — a stored video
        # shortcode counts, so video news may drop its last photo.
        if photo.news.photos.count() <= 1 \
                and photo.news.status == 'published' \
                and not (photo.news.media_type == 'video'
                         and (photo.news.video_code
                              or photo.news.video_file)):
            messages.error(
                request,
                'Хабари нашршуда бояд ҳадди ақал як акс дошта бошад.')
        else:
            was_main = photo.is_main
            photo.delete()
            if was_main:
                first = NewsPhoto.objects.filter(news_id=news_id).first()
                if first:
                    first.is_main = True
                    first.save(update_fields=['is_main'])
    elif action == 'main':
        replace_main_photo(photo)
    return redirect('news_edit', pk=news_id)


# ---------------------------------------------------------------------------
# Ҳисоботлар — live role-scoped reports (director district / zavuch school)
# ---------------------------------------------------------------------------

def _collect_report(request):
    """Shared scope resolution for the reports page and its export.

    Returns (data, period, can_district, school, options); `school` is
    set only for the per-school view (zavuch/principal always, director
    after picking a school). None when the user may not see reports.
    """
    role = get_user_role(request.user)
    is_zavuch = _is_zavuch(request.user, role)
    can_district = (request.user.is_superuser or request.user.is_staff
                    or role == settings.ROLE_DIRECTOR)
    if not (can_district or is_zavuch
            or role == settings.ROLE_PRINCIPAL):
        return None

    from . import reports as reports_mod  # lazy — avoids import cycles
    options = reports_mod.report_period_options()
    valid = {v for v, _ in options}
    period = request.GET.get('period', '')
    if period not in valid:
        period = f'q{get_date_quarter(datetime.date.today()) or 1}'

    school = None
    if can_district:
        sid = request.GET.get('school', '').strip()
        if sid.isdigit():
            cand = School.objects.filter(pk=int(sid)).first()
            if cand is not None and is_academic_school(cand):
                school = cand
    else:
        school = get_user_school(request.user)
        if school is None:
            return None

    data = (reports_mod.collect_school_report(school, period)
            if school is not None
            else reports_mod.collect_district_report(period))
    # The school selector stays rendered inside a school view too —
    # the district payload carries its own list, but the per-school
    # payload does not; without it the period form dropped the school
    # param and bounced the user back to the district page.
    school_options = ([s for s in School.objects.all()
                       if is_academic_school(s)]
                      if can_district else [])
    return data, period, can_district, school, options, school_options


@login_required
def reports_view(request):
    resolved = _collect_report(request)
    if resolved is None:
        return redirect('dashboard')
    data, period, can_district, school, options, school_options = resolved
    return render(request, 'portal/reports.html', {
        'data': data,
        'period_options': options,
        'selected_period': period,
        'selected_school': school,
        'can_district': can_district,
        'school_options': school_options,
    })


@login_required
def reports_export(request):
    resolved = _collect_report(request)
    if resolved is None:
        return redirect('dashboard')
    data, _period, _cd, _s, _o, _so = resolved
    from . import reports as reports_mod
    output = reports_mod.build_report_workbook(data)
    label = (data['school'].name if data['kind'] == 'school'
             else 'Ноҳия')
    filename = f"Ҳисобот_{label}_{data['period_label']}.xlsx"
    encoded_filename = urllib.parse.quote(filename)
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f"attachment; filename*=utf-8''{encoded_filename}"
    return response

