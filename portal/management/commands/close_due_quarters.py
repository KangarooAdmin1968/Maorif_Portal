# -*- coding: utf-8 -*-
"""Automatic quarter closeout for every school.

A quarter becomes due the day the next one opens — break-period marks
still write to the finished quarter until then, so the deadline is
`grading_until` (day before the next quarter's official start). Due
quarters are locked (QuarterLock per class-subject) and their frozen
Excel archive is written, exactly like a manual «Ҷамъбаст».

Quarters that already have QuarterLock rows are skipped — a маориф
«Кушодан» leaves locked=False rows behind, and a reopened quarter is
never silently re-locked by automation.

The Солҳои таҳсил page runs the same check lazily per school; this
command exists for scheduled runs (nightly Task Scheduler / cron) and
manual sweeps. DEFAULT IS DRY-RUN: without --apply nothing is written.
"""
from django.core.management.base import BaseCommand

from portal.models import School


class Command(BaseCommand):
    help = ('Чорякҳои муддаташон гузаштаро автоматик ҷамъбаст мекунад; '
            'аз пешфарз dry-run, барои татбиқ --apply лозим.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--school', type=int, default=None,
            help='Limit to one school ID.')
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually lock the due quarters. Default: dry-run.')

    def handle(self, *args, **options):
        from portal.quarter_close import (auto_close_due,
                                          collect_quarter_close)
        from portal.views import academic_year_start
        import datetime

        today = datetime.date.today()
        year = academic_year_start(today)
        schools = School.objects.order_by('id')
        if options['school'] is not None:
            schools = schools.filter(pk=options['school'])

        mode = 'APPLY' if options['apply'] else 'DRY-RUN'
        self.stdout.write(f'Сана: {today} | соли таҳсил: {year} | {mode}')
        if not options['apply']:
            self.stdout.write(
                'DRY-RUN: ягон тағйирот анҷом намешавад. '
                'Барои татбиқ --apply лозим аст.')

        total_closed = 0
        for school in schools:
            if options['apply']:
                closed = auto_close_due(school, today=today)
            else:
                closed = [
                    (q, year) for q in (1, 2, 3, 4)
                    if self._is_due(school, q, year, today)]
            for q, y in closed:
                self.stdout.write(
                    f'  мактаб={school.pk} «{school.name}» — чоряки {q} '
                    f'({y}-{y + 1}) ҷамъбаст мешавад')
            total_closed += len(closed)

        self.stdout.write(f'Хулоса: {total_closed} чоряк — {mode}')

    @staticmethod
    def _is_due(school, quarter, year, today):
        """Dry-run mirror of auto_close_due's guard set."""
        from portal.models import QuarterLock
        from portal.quarter_close import collect_quarter_close
        status = collect_quarter_close(school, quarter, year, today)
        if status['closed'] or today <= status['grading_until']:
            return False
        return not QuarterLock.objects.filter(
            school=school, quarter=quarter, academic_year=year).exists()
