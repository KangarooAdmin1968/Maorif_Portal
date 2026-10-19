# -*- coding: utf-8 -*-
"""Controlled academic-year reconciliation for stored QuarterGrade rows.

Companion to audit_quarter_grades (which stays read-only). Uses the exact
same scan_school_year classification and the same recalculation pipeline
(recalculate_quarter_grades_bulk) the journal, the signals, and the
Ҳисобкунӣ button use — no separate formula exists here.

DEFAULT IS DRY-RUN: without --apply nothing is written.

Actionable kinds (apply mode, unlocked + unambiguous + year-matched only):
    STALE    stored year-stamped grade != derived value -> re-derive the
             (student, subject, quarter, year) cell via the runtime recalc
    MISSING  sources derive a value but no year-keyed stored row exists ->
             re-derive creates it. This matches the established runtime
             contract: signals persist a stored row whenever a source
             grade saves, the Ҳисобкунӣ button materializes them for a
             whole class, and every ranking/GPA engine reads stored
             QuarterGrade rows — a missing stored row means the student
             is absent from rankings even though the journal projects
             the value live. Creation is therefore the intended state,
             performed through the same authoritative mechanism.

Report-only kinds (never touched automatically):
    ORPHAN, AMBIGUOUS, AMBIGUOUS-ATTESTATION, FROZEN-CONFLICT,
    FROZEN-EMPTY, STALE-LABEL, DUPLICATE, CONFLICT-ASSESSMENT,
    OUT-OF-YEAR-ROWS — retained for explicit manual review; NULL-year
    rows are never stamped, overwritten, or deleted, locked quarters are
    never altered, and prior-year rows are out of scope by key.

Bounded and resumable: one school is scanned and applied at a time;
every processed school is logged so an interrupted run resumes with
--from-school. All writes are idempotent update_or_create/bulk writes
keyed on (student, class, subject, quarter, year), so re-running or
resuming can never double-count or duplicate rows.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from portal.models import School
from portal.management.commands.audit_quarter_grades import (
    QUARTERS, _default_academic_year, scan_school_year,
)

APPLY_KINDS = ('STALE', 'MISSING')
REPORT_KINDS = (
    'ORPHAN', 'AMBIGUOUS', 'AMBIGUOUS-ATTESTATION',
    'FROZEN-CONFLICT', 'FROZEN-EMPTY', 'STALE-LABEL',
    'DUPLICATE', 'CONFLICT-ASSESSMENT', 'OUT-OF-YEAR-ROWS',
)


class Command(BaseCommand):
    help = ('Ҳамоҳангсозии ҳолҳои чорякӣ: dry-run аз пешфарз; '
            '--apply танҳо ҳолҳои STALE/MISSING-и беқуфлу ҳиллашударо '
            'аз манбаъҳои ҳамон сол аз нав ҳосил мекунад.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--year', type=int, default=None,
            help='September-start academic year (default: current).')
        parser.add_argument(
            '--school', type=int, default=None,
            help='Limit to one school ID.')
        parser.add_argument(
            '--quarter', type=int, choices=QUARTERS, default=None,
            help='Limit to one quarter.')
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually re-derive actionable cells. Default: dry-run.')
        parser.add_argument(
            '--from-school', type=int, default=None,
            help='Resume: skip schools with id < this value.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Bound the run to at most this many schools.')
        parser.add_argument(
            '--no-lines', action='store_true',
            help='Print only aggregate counts, not each planned action.')

    def handle(self, *args, **options):
        year = options['year'] or _default_academic_year()
        live_year = _default_academic_year()
        quarters = (options['quarter'],) if options['quarter'] else QUARTERS

        schools = School.objects.order_by('id')
        if options['school'] is not None:
            schools = schools.filter(pk=options['school'])
        if options['from_school'] is not None:
            schools = schools.filter(pk__gte=options['from_school'])
        schools = list(schools[:options['limit']])

        mode = 'APPLY' if options['apply'] else 'DRY-RUN'
        self.stdout.write(
            f'Соли таҳсил: {year}-09-01..{year + 1}-08-31 | реҷа: {mode} | '
            f'мактабҳо: {len(schools)}')
        if not options['apply']:
            self.stdout.write(
                'DRY-RUN: ягон тағйирот анҷом намешавад. '
                'Барои татбиқ --apply лозим аст.')

        planned = defaultdict(int)
        report = defaultdict(int)
        applied = 0

        for school in schools:
            scan = scan_school_year(
                school, year, quarters=quarters, live_year=live_year)
            if not scan['students']:
                continue

            keys = []
            for f in scan['findings']:
                if f['kind'] in APPLY_KINDS:
                    planned[f['kind']] += 1
                    keys.append((
                        f['student_id'], f['subject'], f['quarter'], year))
                    if not options['no_lines']:
                        self.stdout.write(
                            f"PLAN-{f['kind']}  мактаб={school.pk} "
                            f"хонанда={f['student_id']} "
                            f"синф={f['class_name']} фан={f['subject']} "
                            f"чорак={f['quarter']} "
                            f"захирашуда={f['stored']} "
                            f"ҳисобшуда={f['expected']}")
                else:
                    report[f['kind']] += 1
                    if not options['no_lines']:
                        self.stdout.write(
                            f"REVIEW-{f['kind']}  мактаб={school.pk} "
                            f"хонанда={f['student_id']} "
                            f"синф={f['class_name']} фан={f['subject']} "
                            f"чорак={f['quarter']} "
                            f"захирашуда={f['stored']} "
                            f"ҳисобшуда={f['expected']}")

            report['DUPLICATE'] += len(scan['duplicates'])
            report['STALE-LABEL'] += len(scan['stale_label'])
            report['AMBIGUOUS'] += len(scan['legacy'])
            report['AMBIGUOUS-ATTESTATION'] += len(
                scan['legacy_attestation'])
            report['CONFLICT-ASSESSMENT'] += len(scan['conflicts'])
            report['OUT-OF-YEAR-ROWS'] += scan['out_of_year']

            if options['apply'] and keys:
                from portal.views import recalculate_quarter_grades_bulk
                recalculate_quarter_grades_bulk(keys)
                applied += len(keys)

            self.stdout.write(
                f'мактаб={school.pk} ҳолҳо={len(scan["findings"])} '
                f'ҳолҳои-тадбирӣ={len(keys)} — коркард шуд')

        self.stdout.write('')
        self.stdout.write(f'Хулоса ({mode}):')
        self.stdout.write(
            f"  PLAN-STALE (аз нав ҳисоб): {planned['STALE']}")
        self.stdout.write(
            f"  PLAN-MISSING (эҷод): {planned['MISSING']}")
        for kind in REPORT_KINDS:
            self.stdout.write(f'  REVIEW-{kind}: {report.get(kind, 0)}')
        if options['apply']:
            # Bulk writes bypass model signals — invalidate ranking
            # caches so rankings/GPAs re-read the refreshed rows.
            from portal.signals import invalidate_ranking_caches
            invalidate_ranking_caches(None)
            self.stdout.write(f'  ТАТБИҚШУДА: {applied} ҳолҳо')
            # After-summary: re-scan the same schools to show the
            # remaining discrepancies (bounded by the same filters).
            remaining = defaultdict(int)
            for school in schools:
                scan = scan_school_year(
                    school, year, quarters=quarters,
                    live_year=live_year)
                for f in scan['findings']:
                    remaining[f['kind']] += 1
                remaining['DUPLICATE'] += len(scan['duplicates'])
                remaining['STALE-LABEL'] += len(scan['stale_label'])
                remaining['AMBIGUOUS'] += len(scan['legacy'])
                remaining['AMBIGUOUS-ATTESTATION'] += len(
                    scan['legacy_attestation'])
                remaining['CONFLICT-ASSESSMENT'] += len(scan['conflicts'])
                remaining['OUT-OF-YEAR-ROWS'] += scan['out_of_year']
            self.stdout.write('  БАЪД аз татбиқ:')
            for kind in APPLY_KINDS + REPORT_KINDS:
                self.stdout.write(f'    {kind}: {remaining.get(kind, 0)}')
