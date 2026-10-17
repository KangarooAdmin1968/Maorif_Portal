import contextlib
import contextvars
import logging
import time

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.signals import (
    post_delete, post_save, pre_delete, pre_save,
)
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    Assessment, ClassSubject, DailyActivity, Grade, QuarterGrade, School,
    Student,
)

logger = logging.getLogger(__name__)


def _activity_category(user):
    """Map a user to a DailyActivity category.

    Order matters: production zavuch accounts carry role='teacher' with a
    'zavuch_' username, so the zavuch check must run before the role check.
    get_user_role() defaults to ROLE_TEACHER for anonymous/profile-less
    users, so is_authenticated must be checked first.
    """
    from .views import _is_zavuch, get_user_role  # local import avoids cycles

    if not user or not user.is_authenticated:
        return 'parent_student'
    if user.is_superuser or user.is_staff:
        return 'staff'
    role = get_user_role(user)
    if _is_zavuch(user, role):
        return 'zavuch'
    if role == settings.ROLE_DIRECTOR:
        return 'director'
    if role == settings.ROLE_PRINCIPAL:
        return 'principal'
    return 'teacher'


def record_daily_activity(user, count_login=False):
    """Single source of truth for writing DailyActivity rows.

    Authenticated users get one row per (user, local date); anonymous
    visitors share the single (user=None, date) aggregate row enforced by
    the dailyactivity_anon_date partial unique constraint. count_login=True
    marks a real login event; False marks a once-per-day presence update
    (future middleware). Never overwrites first_seen.
    """
    from .views import get_user_school  # local import avoids cycles

    now = timezone.now()
    today = timezone.localdate()

    if user is not None and user.is_authenticated:
        username = user.username or ''
        category = _activity_category(user)
        school = get_user_school(user)
    else:
        user = None
        username = ''
        category = 'parent_student'
        school = None

    defaults = {
        'username': username,
        'category': category,
        'school': school,
        'login_count': 1 if count_login else 0,
        'first_seen': now,
        'last_seen': now,
    }
    try:
        row, created = DailyActivity.objects.get_or_create(
            user=user, date=today, defaults=defaults
        )
    except IntegrityError:
        # SQLite race: another worker inserted the first row concurrently.
        row = DailyActivity.objects.filter(user=user, date=today).first()
        created = False
        if row is None:
            return
    if created:
        return

    updates = {'last_seen': now}
    if username and row.username != username:
        updates['username'] = username
    if count_login:
        updates['login_count'] = F('login_count') + 1
    DailyActivity.objects.filter(pk=row.pk).update(**updates)


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    """Record the login on DailyActivity. Never breaks the login itself."""
    try:
        record_daily_activity(user, count_login=True)
    except Exception:
        logger.exception('Failed to record daily activity on login')


def invalidate_ranking_caches(sender, **kwargs):
    """Drop the global ranking-result cache keys when ranking inputs change.

    Deliberately cheap: cache deletes plus one dirty-timestamp write — no
    ranking computation, no model writes, no queries. Shadow copies stay
    so rate-limited readers still have data; the dirty stamp catches a
    save that lands while a refresh is mid-flight. A failure to
    invalidate must never break the save/delete that triggered it; the
    60s TTL is the backstop.
    """
    from .views import (  # local import avoids cycles
        RANKING_CACHE_KEYS, RANKING_DIRTY_KEY, RANKING_DIRTY_TTL,
    )

    try:
        cache.delete_many(RANKING_CACHE_KEYS)
        cache.set(RANKING_DIRTY_KEY, time.time(), timeout=RANKING_DIRTY_TTL)
    except Exception:
        logger.exception('Failed to invalidate ranking caches')

    # post_save fires inside the writer's transaction: a refresh that
    # starts between this stamp and the commit can still read pre-commit
    # data and mark its result fresh. Re-stamping on commit marks that
    # entry stale so the change lands within the refresh window. Runs
    # immediately when no transaction is open; rollback queues nothing.
    transaction.on_commit(_restamp_ranking_dirty)


def _restamp_ranking_dirty():
    """Refresh the ranking dirty marker at transaction commit time."""
    from .views import RANKING_DIRTY_KEY, RANKING_DIRTY_TTL

    try:
        cache.set(RANKING_DIRTY_KEY, time.time(), timeout=RANKING_DIRTY_TTL)
    except Exception:
        logger.exception('Failed to re-stamp ranking dirty marker')


for _model in (Grade, QuarterGrade, Student, School, ClassSubject):
    post_save.connect(invalidate_ranking_caches, sender=_model)
    post_delete.connect(invalidate_ranking_caches, sender=_model)
del _model


# --- Automatic derived quarter grades ---------------------------------------
# QuarterGrade rows for quarters 1-4 are a derived aggregate of the source
# Grade rows (daily + control-assessment results). Every source write —
# daily cells, monthly journal cells, assessment-linked marks, imports,
# admin edits — funnels through these handlers so the stored value is always
# recalculated for exactly the affected student/subject/quarter. Attestation
# (quarter 0) is a teacher-entered source mark and is never touched here.

# Batch write endpoints (daily_save, monthly_save, the journal form POST)
# write dozens of source rows in one request. Running the full recalc per
# row would redo identical lock/pool lookups each time, so inside
# defer_quarter_recalc() the handlers only collect the affected
# (student_id, subject, quarter) keys and the caller re-derives them all in
# one bulk pass afterwards.
_quarter_recalc_deferred = contextvars.ContextVar(
    'quarter_recalc_deferred', default=None)


@contextlib.contextmanager
def defer_quarter_recalc():
    """Collect affected quarter keys instead of recalculating per save."""
    bucket = set()
    token = _quarter_recalc_deferred.set(bucket)
    try:
        yield bucket
    finally:
        _quarter_recalc_deferred.reset(token)


def _grade_quarter(grade):
    """Quarter a Grade row contributes to (assessment quarter wins)."""
    from .views import get_date_quarter  # local import avoids cycles

    if grade.assessment_id:
        try:
            assessment = grade.assessment
        except Assessment.DoesNotExist:
            assessment = None
        if assessment is not None:
            return assessment.quarter
        # The assessment is gone (post-delete detach path) — the row's own
        # date sits in the same quarter by the server-side invariant.
    return get_date_quarter(grade.date)


def _recalc_student_quarter(instance):
    """Resolve the student and re-derive the stored quarter row."""
    quarter = _grade_quarter(instance)
    if quarter is None:
        return
    defer = _quarter_recalc_deferred.get()
    if defer is not None:
        # Inside a batch write — the caller bulk-recalcs the collected keys.
        if instance.student_id is not None:
            defer.add((instance.student_id, instance.subject, quarter))
        return
    from .views import (
        drop_stale_quarter_grade, recalculate_quarter_grade,
    )  # local import avoids cycles

    try:
        student = instance.student
    except Student.DoesNotExist:
        # Cascade delete of the student — their QuarterGrade rows are
        # removed by the same cascade, nothing to recalculate.
        return
    if student is None:
        return
    try:
        recalculate_quarter_grade(student, instance.subject, quarter)
    except Exception:
        logger.exception('Failed to recalculate quarter grade')
        try:
            # A stored row whose refresh failed is silently misleading —
            # every display prefers it over the live projection. Drop it
            # so the live bridge shows the projected value instead.
            drop_stale_quarter_grade(student, instance.subject, quarter)
        except Exception:
            logger.exception('Failed to drop stale quarter grade')


def _drop_stale_for_grade(row):
    """Best-effort stale-row cleanup for a handler-level failure.

    When the contribution-detection pipeline itself raises before any
    recalc ran, the stored quarter for this cell can no longer be trusted
    — dropping it falls displays back to the live projection. Locked
    quarters keep their frozen value (checked inside the drop helper).
    """
    try:
        if row is None or row.period != 'Холои ҷорӣ (Онлайн)':
            return
        quarter = _grade_quarter(row)
        if quarter is None:
            return
        student = row.student
    except Exception:
        return
    if student is None:
        return
    try:
        from .views import drop_stale_quarter_grade
        drop_stale_quarter_grade(student, row.subject, quarter)
    except Exception:
        logger.exception('Failed to drop stale quarter grade')


@receiver(pre_save, sender=Grade)
def capture_grade_prior_result(sender, instance, **kwargs):
    """Snapshot the stored row so post_save can tell whether the row's
    quarter contribution actually changed. The batch journal POST creates
    an empty placeholder per student and deletes it when nothing was
    entered — a None→None contribution is no change, so those rows must not
    trigger (or wipe) recalculation. Skipped under defer_quarter_recalc —
    the bulk pass recomputes whole cells anyway, so extra keys are
    harmless."""
    try:
        if _quarter_recalc_deferred.get() is not None:
            instance._prev_grade_row = None
            return
        instance._prev_grade_row = (
            Grade.objects.filter(pk=instance.pk).first()
            if instance.pk else None
        )
    except Exception:
        instance._prev_grade_row = None


@receiver(post_save, sender=Grade)
def recalc_quarter_on_grade_save(sender, instance, **kwargs):
    """Re-derive the stored quarter grade when a source row's contribution
    changed. Runs synchronously inside the writer's transaction so the
    derived row commits or rolls back together with the source edit. A
    failure must never break the triggering save — it is logged and the
    quarter cell falls back to the live projection until the next write.
    """
    from .views import grade_component_result  # local import avoids cycles

    try:
        if _quarter_recalc_deferred.get() is not None:
            if instance.period != 'Холҳои ҷорӣ (Онлайн)':
                return
            # Batch mode: collect cells, don't recompute. Fresh empty
            # placeholders (the journal POST creates one per student and
            # deletes it when nothing was entered) never pooled a
            # contribution — skipping them keeps an unrelated stored
            # quarter row untouched. Updates are collected unconditionally:
            # batch paths never move a row between cells, so an extra key
            # only costs one split pass.
            if not kwargs.get('created') or \
                    grade_component_result(instance) is not None:
                _recalc_student_quarter(instance)
            return
        prev = getattr(instance, '_prev_grade_row', None)
        old_result = grade_component_result(prev) if prev is not None else None
        new_result = grade_component_result(instance)
        if prev is None and new_result is None:
            # Brand-new placeholder row with no result yet — nothing to pool.
            return
        moved = prev is not None and (
            prev.subject != instance.subject
            or prev.period != instance.period
            or prev.assessment_id != instance.assessment_id
            or prev.date != instance.date
        )
        if prev is not None and not moved and old_result == new_result:
            return
        if moved and prev.period == 'Холҳои ҷорӣ (Онлайн)':
            # The row used to contribute under its old subject/date/quarter —
            # that cell lost the contribution and needs its own recalc.
            _recalc_student_quarter(prev)
        if instance.period == 'Холҳои ҷорӣ (Онлайн)':
            _recalc_student_quarter(instance)
    except Exception:
        logger.exception('Failed to recalculate quarter grade')
        _drop_stale_for_grade(instance)
        _drop_stale_for_grade(getattr(instance, '_prev_grade_row', None))


@receiver(pre_delete, sender=Grade)
def capture_deleted_grade_result(sender, instance, **kwargs):
    """Snapshot the stored row before deletion — the in-memory instance has
    already been cleared by _apply_grade_fields when it deletes an emptied
    cell, so its score no longer reflects what the pools contained. Under
    defer_quarter_recalc the snapshot is taken only when the in-memory
    fields are empty — that is exactly the _apply_grade_fields
    clear-then-delete ambiguity (fresh placeholder vs. cleared real row);
    rows deleted with fields intact (queryset deletes, cascades) carry
    their contribution in memory and skip the query."""
    from .views import grade_component_result  # local import avoids cycles

    try:
        if _quarter_recalc_deferred.get() is not None and \
                grade_component_result(instance) is not None:
            instance._prev_grade_row = instance
            return
        instance._prev_grade_row = (
            Grade.objects.filter(pk=instance.pk).first() or instance
        )
    except Exception:
        instance._prev_grade_row = instance


@receiver(post_delete, sender=Grade)
def recalc_quarter_on_grade_delete(sender, instance, **kwargs):
    """Re-derive the stored quarter grade when a contributing source row is
    removed. A row that had no result never entered the pools — its deletion
    changes nothing, so it is skipped (this also keeps the journal POST's
    create-then-delete placeholder churn from touching quarter cells)."""
    from .views import grade_component_result  # local import avoids cycles

    try:
        prev = getattr(instance, '_prev_grade_row', instance)
        if prev.period != 'Холҳои ҷорӣ (Онлайн)':
            return
        if grade_component_result(prev) is None:
            return
        _recalc_student_quarter(prev)
    except Exception:
        logger.exception('Failed to recalculate quarter grade')
        _drop_stale_for_grade(
            getattr(instance, '_prev_grade_row', None) or instance)


def _assessment_recalc(instance, fallback_rows=None):
    """Recalc quarter grades for every (student, subject) an assessment
    touched. `fallback_rows` supplies the linked set for post_delete, where
    the FK has already been detached by the collector."""
    try:
        rows = fallback_rows
        if rows is None:
            rows = Grade.objects.filter(assessment=instance).values_list(
                'student_id', 'subject').distinct()
        from .views import recalculate_quarter_grades_bulk  # local import

        defer = _quarter_recalc_deferred.get()
        if defer is not None:
            for student_id, subject in rows:
                defer.add((student_id, subject, instance.quarter))
            return
        recalculate_quarter_grades_bulk(
            (student_id, subject, instance.quarter)
            for student_id, subject in rows
        )
    except Exception:
        logger.exception('Failed to recalculate quarter grade')
        try:
            # Same stale-row cleanup as the Grade handlers — drop the
            # affected stored rows (locked cells keep their frozen value)
            # so displays fall back to the live projection.
            from .views import drop_stale_quarter_grades
            drop_stale_quarter_grades(
                (sid, subj, instance.quarter) for sid, subj in (rows or ()))
        except Exception:
            logger.exception('Failed to drop stale quarter grades')


@receiver(pre_delete, sender=Assessment)
def capture_assessment_grade_scope(sender, instance, **kwargs):
    """Snapshot linked (student, subject) pairs before the collector detaches
    or deletes the Grade rows — post_delete can no longer see the link."""
    try:
        instance._recalc_rows = list(
            Grade.objects.filter(assessment=instance).values_list(
                'student_id', 'subject').distinct()
        )
    except Exception:
        instance._recalc_rows = []


@receiver(post_save, sender=Assessment)
def recalc_quarter_on_assessment_save(sender, instance, **kwargs):
    """is_ajm/number/lesson edits repool linked grades in-place — recalc."""
    _assessment_recalc(instance)


@receiver(post_delete, sender=Assessment)
def recalc_quarter_on_assessment_delete(sender, instance, **kwargs):
    """Deletion detaches linked grades to the Ҷорӣ pool (SET_NULL) — recalc
    the students captured in pre_delete."""
    _assessment_recalc(instance, getattr(instance, '_recalc_rows', []))
