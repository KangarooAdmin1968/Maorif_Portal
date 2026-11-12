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

import datetime
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

        # District Аълохон/Бебаҳо counts for the Ҳисоботҳо page — one
        # district-wide merged scan, same bounded-refresh cache the
        # reports view serves read-only (per-quarter key).
        try:
            from portal.reports import (
                excellent_ungraded_counts, report_counts_cache_key)
            from portal.views import academic_year_start, get_date_quarter
            today = datetime.date.today()
            quarter = get_date_quarter(today) or 1
            _ranking_cached(
                report_counts_cache_key(
                    quarter, academic_year_start(today)),
                lambda: excellent_ungraded_counts(schools, quarter, today))
            refreshed.append('report_counts')
        except RankingRefreshBusy:
            skipped.append('report_counts: busy')
        except Exception as exc:  # noqa: BLE001
            skipped.append(f'report_counts: {exc}')
            logger.exception('refresh_rankings: report_counts failed')

        # District АҶЧ stats payload for the Ҳисоботи маориф Excel
        # exports — the same heavy merged-marks scan the export views
        # must never run in-request (they serve it read-only).
        try:
            from portal.maorif_report import (
                collect_maorif_stats_cached)
            from portal.views import academic_year_start, get_date_quarter
            today = datetime.date.today()
            quarter = get_date_quarter(today) or 1
            collect_maorif_stats_cached(quarter, today)
            refreshed.append('maorif_stats')
        except RankingRefreshBusy:
            skipped.append('maorif_stats: busy')
        except Exception as exc:  # noqa: BLE001
            skipped.append(f'maorif_stats: {exc}')
            logger.exception('refresh_rankings: maorif_stats failed')

        # Reports' per-quarter readiness — the Ҳисоботҳо district view
        # marks past quarters it was asked for; warm exactly those under
        # the same single-flight machinery (never inside a request). The
        # same marker drives the Аълохон/Бебаҳо counts — otherwise those
        # cells stay '—' forever on a non-current quarter.
        try:
            from portal.reports import (
                excellent_ungraded_counts, report_counts_cache_key,
                wanted_report_quarters)
            from portal.views import (
                academic_year_quarter_windows,
                readiness_quarter_cache_key)
            for q, ay in sorted(wanted_report_quarters()):
                window = academic_year_quarter_windows(ay).get(q)
                if window is None:
                    continue
                key = readiness_quarter_cache_key(q, ay)
                try:
                    _ranking_cached(
                        key,
                        lambda w=window: _compute_school_readiness_payload(
                            schools, gpa_map, quarter_window=w))
                    refreshed.append(f'readiness:q{q}')
                except RankingRefreshBusy:
                    skipped.append(f'readiness:q{q}: busy')
                except Exception as exc:  # noqa: BLE001
                    skipped.append(f'readiness:q{q}: {exc}')
                    logger.exception(
                        'refresh_rankings: readiness q%s failed', q)
                try:
                    _ranking_cached(
                        report_counts_cache_key(q, ay),
                        lambda qq=q, aa=ay: excellent_ungraded_counts(
                            schools, qq))
                    refreshed.append(f'report_counts:q{q}')
                except RankingRefreshBusy:
                    skipped.append(f'report_counts:q{q}: busy')
                except Exception as exc:  # noqa: BLE001
                    skipped.append(f'report_counts:q{q}: {exc}')
                    logger.exception(
                        'refresh_rankings: report_counts q%s failed', q)
                # The АҶЧ exports only cover the current academic year
                # (collect_maorif_stats derives ay from today) — warm a
                # wanted quarter only when it belongs to this year.
                try:
                    from portal.maorif_report import (
                        collect_maorif_stats, maorif_stats_cache_key)
                    from portal.views import academic_year_start
                    if ay == academic_year_start():
                        _ranking_cached(
                            maorif_stats_cache_key(q, ay),
                            lambda qq=q: collect_maorif_stats(qq))
                        refreshed.append(f'maorif_stats:q{q}')
                except RankingRefreshBusy:
                    skipped.append(f'maorif_stats:q{q}: busy')
                except Exception as exc:  # noqa: BLE001
                    skipped.append(f'maorif_stats:q{q}: {exc}')
                    logger.exception(
                        'refresh_rankings: maorif_stats q%s failed', q)
        except Exception:  # noqa: BLE001 — optional feature, keep running
            logger.exception('refresh_rankings: quarter readiness failed')

        # Due-quarter closeout rides the same schedule: the day after a
        # grading window ends (e.g. 09.11 for Q1), every unfinished
        # quarter is locked and archived as if each завуч pressed
        # «Ҷамъбаст» themselves — teachers focus on the new quarter.
        # auto_close_due stays cheap (calendar gate + one EXISTS query
        # per quarter) and never re-locks a quarter маориф reopened.
        from portal.quarter_close import auto_close_due
        for school in schools:
            try:
                closed = auto_close_due(school)
                for q, y in closed:
                    refreshed.append(f'quarter_close: {school.name} Ч{q} {y}')
            except Exception:  # noqa: BLE001 — keep siblings running
                logger.exception(
                    'refresh_rankings: auto_close_due failed for %s',
                    school)

        self.stdout.write(
            'refreshed: %s%s' % (
                ', '.join(refreshed) or 'none',
                f' | skipped: {"; ".join(skipped)}' if skipped else ''))
