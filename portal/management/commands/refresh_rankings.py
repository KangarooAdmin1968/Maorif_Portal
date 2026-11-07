"""Warm the shared ranking caches outside request workers.

Requests serve rankings read-only (rankings_read_only scope): a fresh or
shadow cache entry, else an empty block — they never run the memory-heavy
full-year Grade scans that OOM-killed Gunicorn workers. This command is
the only place those refreshes are meant to run; schedule it from cron
every few minutes:

    */5 * * * * cd /var/www/maorif_zafarobod && \\
        /var/www/maorif_zafarobod/venv/bin/python \\
        manage.py refresh_rankings >> /var/log/refresh_rankings.log 2>&1

All four leaderboard computes share one current-year Grade scan via
sharing_ranking_grade_scan(). Each key still refreshes under its own
single-flight locks, so an overlap with a leftover request-side or
previous-run refresh is handled by the existing machinery.
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Refresh ranking/readiness caches (run from cron).'

    def handle(self, *args, **options):
        from portal.models import School
        from portal.views import (
            READINESS_CACHE_KEY, RankingRefreshBusy,
            _compute_school_readiness_payload, _ranking_cached,
            all_student_gpas, calculate_class_rankings,
            calculate_school_rankings, calculate_subject_rankings,
            calculate_top_students,
            is_academic_school, sharing_ranking_grade_scan,
        )

        refreshed, skipped = [], []
        with sharing_ranking_grade_scan():
            for name, fn in (
                    ('school', calculate_school_rankings),
                    ('class', calculate_class_rankings),
                    ('subject', calculate_subject_rankings),
                    ('top_students', calculate_top_students),
                    ('gpas', all_student_gpas)):
                try:
                    fn()
                    refreshed.append(name)
                except RankingRefreshBusy:
                    skipped.append(f'{name}: busy')
                except Exception as exc:  # noqa: BLE001 — keep siblings running
                    skipped.append(f'{name}: {exc}')
                    logger.exception('refresh_rankings: %s failed', name)

        # District-wide readiness leaderboard — same bounded-refresh cache
        # the view serves read-only. gpa_map reuses the school ranking
        # refreshed above when available.
        try:
            gpa_map = {
                item['school'].id: item['gpa']
                for item in calculate_school_rankings()
            }
        except RankingRefreshBusy:
            gpa_map = None
        schools = [s for s in School.objects.all() if is_academic_school(s)]
        try:
            _ranking_cached(
                READINESS_CACHE_KEY,
                lambda: _compute_school_readiness_payload(schools, gpa_map))
            refreshed.append('readiness')
        except RankingRefreshBusy:
            skipped.append('readiness: busy')
        except Exception as exc:  # noqa: BLE001
            skipped.append(f'readiness: {exc}')
            logger.exception('refresh_rankings: readiness failed')

        self.stdout.write(
            'refreshed: %s%s' % (
                ', '.join(refreshed) or 'none',
                f' | skipped: {"; ".join(skipped)}' if skipped else ''))
