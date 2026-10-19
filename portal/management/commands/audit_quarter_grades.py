# -*- coding: utf-8 -*-
"""Read-only audit: stored QuarterGrade rows vs the current journal data.

Re-derives every (student, subject, quarter) cell through the same
_split_quarter_grades / calc_quarter_value pipeline the signals and the
journal live-projection use, then reports where a stored value disagrees.

QuarterGrade carries an explicit September-start academic_year, so
comparisons are scoped to the selected academic year (Sep..Aug): only
source rows whose attributed year matches feed the expected value — the
calculator is never given mixed-year input. Rows stamped with a different
year are left to that year's audit; rows with academic_year=NULL are
legacy records whose year cannot be established — they are reported as
AMBIGUOUS for manual attribution, never used as the year's official value.

    STALE              unlocked cell, year-matched sources derive a value
                       that disagrees with the stored grade
    MISSING            unlocked cell, year-matched sources derive a value
                       but no year-matched stored row exists (displays
                       show the live projection; the stored cache is
                       simply absent)
    ORPHAN             unlocked cell, year-stamped stored grade exists but
                       no source rows of that same year support the
                       quarter — nothing in its claimed year backs it
    AMBIGUOUS          stored row with academic_year=NULL — a legacy
                       record whose year cannot be established; kept for
                       review, never treated as the year's official value
    AMBIGUOUS-ATTESTATION stored quarter=0 attestation mark with
                       academic_year=NULL — teacher-entered, so no source
                       data can establish its year; preserved for manual
                       attribution, excluded from every current-year
                       display until then
    FROZEN-CONFLICT    cell locked for this year whose frozen value
                       disagrees with the year's sources — reported,
                       never "fixed"
    FROZEN-EMPTY       cell locked for this year with a stored row and no
                       supporting same-year source rows — kept by design
    STALE-LABEL        stored row of the CURRENT year whose class_name no
                       longer matches the student's current class —
                       invisible to every display and recalc key
    DUPLICATE          two stored rows normalize to the same
                       (student, class, subject, quarter, year) key —
                       save() normalization is bypassed by bulk writes,
                       so the raw unique_together lets them coexist
    CONFLICT-ASSESSMENT assessment whose own date falls outside its
                       declared quarter window, or that has linked grade
                       rows attributed to a different academic year —
                       Assessment.quarter stays authoritative for
                       bucketing, the record is only flagged for review
    OUT-OF-YEAR-ROWS   source rows attributed to a different academic
                       year — excluded from the expected value (reported
                       per school, counted as rows)

Nothing is written: detection only. Repairs are an explicit operator
decision (e.g. the Ҳисобкунӣ button on the journal page re-derives one
class+subject, or a targeted admin action for a frozen cell).
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from portal.models import (
    Assessment, Grade, QuarterGrade, QuarterLock, School, Student,
    normalize_class_name, normalize_subject,
)
from portal.views import (
    academic_year_quarter_windows, academic_year_start,
)

SOURCE_PERIOD = 'Холҳои ҷорӣ (Онлайн)'
QUARTERS = (1, 2, 3, 4)


def _default_academic_year(today=None):
    """September-start year of the academic year containing today.

    Shared with the runtime pipeline — the audit compares against exactly
    the same Sep..Aug window the recalculation code uses."""
    return academic_year_start(today)


def _quarter_windows(year):
    """Inclusive (start, end) dates per quarter of the Sep..Aug year."""
    return academic_year_quarter_windows(year)


class Command(BaseCommand):
    help = ('Танҳо-хондан: фарқиятҳои ҳолҳои чорякии захирашуда бо '
            'маълумоти ҷории журналро гузориш медиҳад.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--school', type=int, default=None,
            help='ID of one school to audit (default: all schools).')
        parser.add_argument(
            '--quarter', type=int, choices=QUARTERS, default=None,
            help='Audit only this quarter (default: all four).')
        parser.add_argument(
            '--year', type=int, default=None,
            help='September-start year of the academic year to compare '
                 '(e.g. 2026 -> 2026-09-01..2027-08-31; default: the year '
                 'containing today).')
        parser.add_argument(
            '--no-lines', action='store_true',
            help='Print only the summary counts, not each discrepancy.')

    def handle(self, *args, **options):
        from portal.views import (  # local import: same pipeline as runtime
            _grade_row_year_quarter, _split_quarter_grades,
            calc_quarter_value, std_round,
        )

        quarters = (options['quarter'],) if options['quarter'] else QUARTERS
        schools = School.objects.order_by('id')
        if options['school'] is not None:
            schools = schools.filter(pk=options['school'])

        year = options['year'] or _default_academic_year()
        live_year = _default_academic_year()
        windows = _quarter_windows(year)
        ay_start, ay_end = windows[1][0], windows[4][1]
        self.stdout.write(
            f'Соли таҳсил: {ay_start}..{ay_end} (--year {year})')

        totals = defaultdict(int)

        for school in schools.iterator():
            students = {
                s.pk: s for s in
                Student.objects.filter(school=school)
                .only('id', 'class_name')
            }
            if not students:
                continue

            # Locks effective for the audited year: year-stamped rows match
            # only their own year; legacy NULL-year rows are attributed by
            # their own locked_at date.
            locked = {
                (normalize_class_name(c), normalize_subject(s), q)
                for c, s, q, ay, lat in QuarterLock.objects.filter(
                    school=school, locked=True,
                ).values_list(
                    'class_name', 'subject', 'quarter',
                    'academic_year', 'locked_at')
                if ay == year or (
                    ay is None and lat is not None
                    and academic_year_start(timezone.localdate(lat)) == year)
            }

            # Year-keyed comparison: stored[(sid, subj, q)] is the row
            # stamped with the audited year regardless of its class label —
            # a prior-year row legitimately keeps the class it was earned
            # in, so the label is NOT part of the match for year-keyed
            # rows (only for duplicate and stale-label detection).
            stored = {}
            stored_keys = set()
            stale_label = []
            duplicates = []
            legacy = []
            legacy_attestation = []
            for qg in QuarterGrade.objects.filter(
                    student__school=school,
            ).iterator():
                st = students.get(qg.student_id)
                if st is None:
                    continue
                cls = normalize_class_name(qg.class_name)
                if qg.academic_year is None:
                    # Legacy row: its year cannot be established — flagged
                    # for manual attribution, never compared as this year.
                    # Attestation rows are teacher-entered, so no source
                    # evidence can ever resolve their year — they get a
                    # dedicated category so the review list is complete.
                    if qg.quarter == 0:
                        legacy_attestation.append(qg)
                    else:
                        legacy.append(qg)
                    continue
                if qg.quarter not in QUARTERS:
                    # Year-stamped attestation rows carry no derivable
                    # counterpart — there is nothing to compare.
                    continue
                if qg.academic_year != year:
                    # A record of a different year — that year's audit
                    # covers it; it is not this year's stored value.
                    continue
                if (year == live_year
                        and cls != normalize_class_name(st.class_name)):
                    stale_label.append(qg)
                    continue
                subj_n = normalize_subject(qg.subject)
                full_key = (qg.student_id, cls, subj_n, qg.quarter)
                if full_key in stored_keys:
                    # Raw unique_together passed (save() normalization is
                    # bypassed by bulk writes) but the recalc key sees one
                    # row — flag the pair, keep the first for comparison.
                    duplicates.append(
                        (full_key, stored.get(
                            (qg.student_id, subj_n, qg.quarter)), qg.grade))
                    continue
                stored_keys.add(full_key)
                stored.setdefault(
                    (qg.student_id, subj_n, qg.quarter), qg.grade)

            # Assessments conflicting with the year: own date outside its
            # declared quarter window, or linked rows whose attributed
            # year disagrees with the assessment's own year.
            # {assessment_id: [assessment, out_of_window_linked_rows]}
            conflicts = {}
            for a in Assessment.objects.filter(
                    class_subject__school=school,
            ).select_related('class_subject').iterator():
                w = windows.get(a.quarter)
                if w is None or not (w[0] <= a.date <= w[1]):
                    conflicts.setdefault(a.id, [a, 0])

            year_pools = defaultdict(list)  # rows attributed to this year
            out_of_year = 0
            for g in Grade.objects.filter(
                    student__school=school, period=SOURCE_PERIOD,
                    score__isnull=False,
            ).select_related('assessment').iterator(chunk_size=2000):
                key = (g.student_id, normalize_subject(g.subject))
                row_year, row_quarter = _grade_row_year_quarter(g)
                if row_year == year:
                    year_pools[key].append(g)
                else:
                    out_of_year += 1
                a = g.assessment if g.assessment_id else None
                if a is not None:
                    # The row inherits (quarter, year) from its
                    # assessment — flag it when its OWN date disagrees:
                    # outside the assessment's quarter window or in a
                    # different academic year than the assessment.
                    w = windows.get(a.quarter)
                    a_year = academic_year_start(a.date)
                    if (academic_year_start(g.date) != a_year
                            or w is None
                            or not (w[0] <= a.date <= w[1])):
                        entry = conflicts.setdefault(a.id, [a, 0])
                        entry[1] += 1

            # Cells to check: every (student, subject) with year-matched
            # source rows or a stored row under the audited year.
            cells = set(year_pools)
            cells.update((sid, subj) for sid, subj, q in stored)

            for sid, subj in cells:
                st = students.get(sid)
                if st is None:
                    continue
                cls = normalize_class_name(st.class_name)
                rows = year_pools.get((sid, subj), ())
                for q in quarters:
                    value = calc_quarter_value(
                        *_split_quarter_grades(rows, q, year))
                    expected = std_round(value) if value is not None else None
                    got = stored.get((sid, subj, q))
                    is_locked = (cls, subj, q) in locked
                    if got == expected:
                        continue
                    if got is None:
                        # A locked cell with no stored row projects live
                        # by design; unlocked it is a missing cache row.
                        if is_locked:
                            continue
                        kind = 'MISSING'
                    elif expected is None:
                        # Year-stamped row with no same-year derivation —
                        # nothing in its claimed year backs it.
                        if is_locked:
                            kind = 'FROZEN-EMPTY'
                        else:
                            kind = 'ORPHAN'
                    elif is_locked:
                        kind = 'FROZEN-CONFLICT'
                    else:
                        kind = 'STALE'
                    totals[kind] += 1
                    if not options['no_lines']:
                        self.stdout.write(
                            f'{kind}  мактаб={school.pk} хонанда={sid} '
                            f'синф={cls} фан={subj} чорак={q} '
                            f'захирашуда={got} ҳисобшуда={expected}'
                        )

            for key, first_grade, dup_grade in duplicates:
                totals['DUPLICATE'] += 1
                if not options['no_lines']:
                    sid, cls, subj, q = key
                    self.stdout.write(
                        f'DUPLICATE  мактаб={school.pk} хонанда={sid} '
                        f'синф={cls} фан={subj} чорак={q} '
                        f'ҳолҳо={first_grade}/{dup_grade}'
                    )

            for qg in stale_label:
                totals['STALE-LABEL'] += 1
                if not options['no_lines']:
                    st = students.get(qg.student_id)
                    self.stdout.write(
                        f'STALE-LABEL  мактаб={school.pk} '
                        f'хонанда={qg.student_id} '
                        f'синф={normalize_class_name(qg.class_name)}'
                        f'(ҳозира: {st.class_name if st else "?"}) '
                        f'фан={normalize_subject(qg.subject)} '
                        f'чорак={qg.quarter} захирашуда={qg.grade}'
                    )

            for qg in legacy:
                totals['AMBIGUOUS'] += 1
                if not options['no_lines']:
                    st = students.get(qg.student_id)
                    self.stdout.write(
                        f'AMBIGUOUS  мактаб={school.pk} '
                        f'хонанда={qg.student_id} '
                        f'синф={normalize_class_name(qg.class_name)}'
                        f'(ҳозира: {st.class_name if st else "?"}) '
                        f'фан={normalize_subject(qg.subject)} '
                        f'чорак={qg.quarter} захирашуда={qg.grade} '
                        f'— соли таҳсил номуъайян (legacy)'
                    )

            for qg in legacy_attestation:
                totals['AMBIGUOUS-ATTESTATION'] += 1
                if not options['no_lines']:
                    st = students.get(qg.student_id)
                    self.stdout.write(
                        f'AMBIGUOUS-ATTESTATION  мактаб={school.pk} '
                        f'хол={qg.pk} хонанда={qg.student_id} '
                        f'синф={normalize_class_name(qg.class_name)}'
                        f'(ҳозира: {st.class_name if st else "?"}) '
                        f'фан={normalize_subject(qg.subject)} '
                        f'атт={qg.att_grade} '
                        f'— соли таҳсил номуъайян (legacy)'
                    )

            for a, bad_rows in conflicts.values():
                totals['CONFLICT-ASSESSMENT'] += 1
                if not options['no_lines']:
                    cs = a.class_subject
                    self.stdout.write(
                        f'CONFLICT-ASSESSMENT  мактаб={school.pk} '
                        f'баҳогузорӣ={a.id} '
                        f'синф={normalize_class_name(cs.class_name)} '
                        f'фан={normalize_subject(cs.subject)} '
                        f'чорак={a.quarter} сана={a.date} '
                        f'сатрҳои_конфликт={bad_rows}'
                    )

            if out_of_year:
                totals['OUT-OF-YEAR-ROWS'] += out_of_year
                if not options['no_lines']:
                    self.stdout.write(
                        f'OUT-OF-YEAR-ROWS  мактаб={school.pk} '
                        f'сатрҳо={out_of_year} — сана берун аз соли '
                        f'таҳсили интихобшуда ({ay_start}..{ay_end}) аст '
                        f'ва аз ҳисоби соли ҷорӣ хориҷ карда шуд'
                    )

        self.stdout.write('')
        self.stdout.write('Хулоса:')
        for kind in ('STALE', 'MISSING', 'ORPHAN', 'AMBIGUOUS',
                     'AMBIGUOUS-ATTESTATION',
                     'FROZEN-CONFLICT', 'FROZEN-EMPTY', 'STALE-LABEL',
                     'DUPLICATE', 'CONFLICT-ASSESSMENT',
                     'OUT-OF-YEAR-ROWS'):
            self.stdout.write(f'  {kind}: {totals.get(kind, 0)}')
        if not any(totals.values()):
            self.stdout.write('  Ҳама ҳолҳои чорякӣ мувофиқанд.')
