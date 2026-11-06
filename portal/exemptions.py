"""StudentExemption helpers — bulk maps for journals and reports.

One grouped query per call site: the journal/report resolves which
students are exempt from grading norms (муваққатан дар давлати дигар
таҳсил мекунад ва ғ.) without N+1 work.
"""
from django.utils import timezone

from .models import StudentExemption


def active_exemption_map(student_ids, today=None):
    """{student_id: StudentExemption} for exemptions in force on ``today``.

    An exemption counts when it is not cancelled, has already started and
    has not ended (NULL end_date = indefinite).
    """
    today = today or timezone.localdate()
    rows = (StudentExemption.objects
            .filter(student_id__in=list(student_ids),
                    cancelled_at__isnull=True,
                    start_date__lte=today)
            .exclude(end_date__lt=today)
            .order_by('student_id', '-start_date', '-id'))
    # First row per student wins (latest start_date).
    return {r.student_id: r for r in rows}


def exempt_student_ids(student_ids, today=None):
    """set(student_id) — the cheap form when only the flag is needed."""
    return set(active_exemption_map(student_ids, today=today))


def home_student_ids(student_ids, today=None):
    """set(student_id) with an active «Дар хона таҳсил мегирад» exemption.

    Home-education students stay formally attached to their class (their
    quarter marks land in the class's official documents) but are taught
    in the separate home journal — rosters exclude them via this map.
    """
    today = today or timezone.localdate()
    return set(
        StudentExemption.objects
        .filter(student_id__in=list(student_ids), reason='home',
                cancelled_at__isnull=True, start_date__lte=today)
        .exclude(end_date__lt=today)
        .values_list('student_id', flat=True)
    )
