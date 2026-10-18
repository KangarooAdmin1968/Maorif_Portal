# -*- coding: utf-8 -*-
"""Read-only audit: stored QuarterGrade rows vs the current journal data.

Re-derives every (student, subject, quarter) cell through the same
_split_quarter_grades / calc_quarter_value pipeline the signals and the
journal live-projection use, then reports where a stored value disagrees.

QuarterGrade has no academic-year field, so comparisons are scoped to one
academic year (Sep..Aug): only Grade rows dated inside the year window
feed the expected value — the calculator is never given mixed-year input.
A stored row that cannot be attributed to the selected year is reported
as ambiguous rather than as a confirmed error.

    STALE              unlocked cell, current-year sources derive a value
                       that disagrees with the stored grade
    MISSING            unlocked cell, current-year sources derive a value
                       but no stored row exists (displays show the live
                       projection; the stored cache is simply absent)
    ORPHAN             unlocked cell, stored grade exists but no source
                       rows of ANY year bucket to that quarter — nothing
                       anywhere supports it
    AMBIGUOUS          stored grade exists, no current-year sources
                       bucket to the quarter, but out-of-year rows do —
                       likely a prior-year value; its year cannot be
                       established safely enough to call it an error
    FROZEN-CONFLICT    locked cell whose frozen value disagrees with the
                       current-year sources — reported, never "fixed"
    FROZEN-EMPTY       locked cell with a stored row and no supporting
                       source rows of any year — kept by design
    STALE-LABEL        stored row whose class_name no longer matches the
                       student's current class — invisible to every
                       display and recalc key, lingers forever
    DUPLICATE          two stored rows normalize to the same
                       (student, class, subject, quarter) key — the raw
                       unique_together lets them coexist (bulk_create and
                       QuerySet.update bypass save() normalization), and
                       the recalc key sees only one
    CONFLICT-ASSESSMENT assessment whose own date falls outside its
                       declared quarter window, or that has linked grade
                       rows outside the academic year — Assessment.quarter
                       stays authoritative for bucketing, the record is
                       only flagged for review
    OUT-OF-YEAR-ROWS   source rows dated outside the academic year —
                       excluded from the expected value (reported per
                       school, counted as rows)

Nothing is written: detection only. Repairs are an explicit operator
decision (e.g. the Ҳисобкунӣ button on the journal page re-derives one
class+subject, or a targeted admin action for a frozen cell).
"""
import calendar
import datetime

from collections import defaultdict

from django.core.management.base import BaseCommand

from portal.models import (
    Assessment, Grade, QuarterGrade, QuarterLock, School, Student,
    normalize_class_name, normalize_subject,
)

SOURCE_PERIOD = 'Холҳои ҷорӣ (Онлайн)'
QUARTERS = (1, 2, 3, 4)


def _default_academic_year(today=None):
    """September-start year of the academic year containing today."""
    today = today or datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


def _quarter_windows(year):
    """Inclusive (start, end) dates per quarter of the Sep..Aug year."""
    feb_last = calendar.monthrange(year + 1, 2)[1]
    return {
        1: (datetime.date(year, 9, 1), datetime.date(year, 11, 30)),
        2: (datetime.date(year, 12, 1), datetime.date(year + 1, 2, feb_last)),
        3: (datetime.date(year + 1, 3, 1), datetime.date(year + 1, 5, 31)),
        4: (datetime.date(year + 1, 6, 1), datetime.date(year + 1, 8, 31)),
    }


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
            _split_quarter_grades, calc_quarter_value, std_round,
        )

        quarters = (options['quarter'],) if options['quarter'] else QUARTERS
        schools = School.objects.order_by('id')
        if options['school'] is not None:
            schools = schools.filter(pk=options['school'])

        year = options['year'] or _default_academic_year()
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

            locked = {
                (normalize_class_name(c), normalize_subject(s), q)
                for c, s, q in QuarterLock.objects.filter(
                    school=school, locked=True,
                ).values_list('class_name', 'subject', 'quarter')
            }

            stored = {}
            stale_label = []
            duplicates = []
            for qg in QuarterGrade.objects.filter(
                    student__school=school, quarter__in=QUARTERS,
            ).iterator():
                st = students.get(qg.student_id)
                if st is None:
                    continue
                cls = normalize_class_name(qg.class_name)
                if cls != normalize_class_name(st.class_name):
                    stale_label.append(qg)
                    continue
                key = (qg.student_id, cls, normalize_subject(qg.subject),
                       qg.quarter)
                if key in stored:
                    # Raw unique_together passed (save() normalization is
                    # bypassed by bulk writes) but the recalc key sees one
                    # row — flag the pair, keep the first for comparison.
                    duplicates.append((key, stored[key], qg.grade))
                    continue
                stored[key] = qg.grade

            # Assessments conflicting with the year: own date outside its
            # declared quarter window, or linked rows outside the window.
            # {assessment_id: [assessment, out_of_window_linked_rows]}
            conflicts = {}
            for a in Assessment.objects.filter(
                    class_subject__school=school,
            ).select_related('class_subject').iterator():
                w = windows.get(a.quarter)
                if w is None or not (w[0] <= a.date <= w[1]):
                    conflicts.setdefault(a.id, [a, 0])

            all_pools = defaultdict(list)   # every source-period row
            year_pools = defaultdict(list)  # rows inside the year window
            out_of_year = 0
            for g in Grade.objects.filter(
                    student__school=school, period=SOURCE_PERIOD,
                    score__isnull=False,
            ).select_related('assessment').iterator(chunk_size=2000):
                key = (g.student_id, normalize_subject(g.subject))
                all_pools[key].append(g)
                in_year = ay_start <= g.date <= ay_end
                if in_year:
                    year_pools[key].append(g)
                else:
                    out_of_year += 1
                a = g.assessment if g.assessment_id else None
                if a is not None:
                    w = windows.get(a.quarter)
                    if (not in_year or w is None
                            or not (w[0] <= g.date <= w[1])):
                        entry = conflicts.setdefault(a.id, [a, 0])
                        entry[1] += 1

            # Cells to check: every (student, subject) with source rows or
            # a stored row under the student's CURRENT class label.
            cells = set(all_pools)
            cells.update((sid, subj) for sid, cls, subj, q in stored)

            for sid, subj in cells:
                st = students.get(sid)
                if st is None:
                    continue
                cls = normalize_class_name(st.class_name)
                year_rows = year_pools.get((sid, subj), ())
                all_rows = all_pools.get((sid, subj), ())
                for q in quarters:
                    value = calc_quarter_value(
                        *_split_quarter_grades(year_rows, q))
                    expected = std_round(value) if value is not None else None
                    got = stored.get((sid, cls, subj, q))
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
                        # Stored row with no current-year derivation:
                        # a quarter supported only by out-of-year rows is
                        # most likely last year's value — flag it rather
                        # than treating a historical row as an error.
                        any_cur, any_ajm = _split_quarter_grades(
                            all_rows, q)
                        has_support = bool(any_cur or any_ajm)
                        if is_locked:
                            kind = ('AMBIGUOUS' if has_support
                                    else 'FROZEN-EMPTY')
                        else:
                            kind = ('AMBIGUOUS' if has_support
                                    else 'ORPHAN')
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
                     'FROZEN-CONFLICT', 'FROZEN-EMPTY', 'STALE-LABEL',
                     'DUPLICATE', 'CONFLICT-ASSESSMENT',
                     'OUT-OF-YEAR-ROWS'):
            self.stdout.write(f'  {kind}: {totals.get(kind, 0)}')
        if not any(totals.values()):
            self.stdout.write('  Ҳама ҳолҳои чорякӣ мувофиқанд.')
