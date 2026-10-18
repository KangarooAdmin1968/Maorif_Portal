# -*- coding: utf-8 -*-
"""Read-only audit: stored QuarterGrade rows vs the current journal data.

Re-derives every (student, subject, quarter) cell through the same
_split_quarter_grades / calc_quarter_value pipeline the signals and the
journal live-projection use, then reports where a stored value disagrees:

    STALE            unlocked cell, stored grade != derived value
    ORPHAN           unlocked cell, stored grade exists but no source
                     grades remain to support it
    MISSING          unlocked cell, source grades derive a value but no
                     stored row exists (displays show the live
                     projection; the stored cache is simply absent)
    FROZEN-CONFLICT  locked cell whose frozen value disagrees with the
                     current journal data — reported, never "fixed"
    FROZEN-EMPTY     locked cell with a stored row but no remaining
                     source grades — kept by design, listed for review
    STALE-LABEL      stored row whose class_name no longer matches the
                     student's current class — invisible to every
                     display and recalc key, lingers forever
    DUPLICATE        two stored rows normalize to the same
                     (student, class, subject, quarter) key — the raw
                     unique_together lets them coexist (bulk_create and
                     QuerySet.update bypass save() normalization), and
                     the recalc key sees only one

Nothing is written: detection only. Repairs are an explicit operator
decision (e.g. the Ҳисобкунӣ button on the journal page re-derives one
class+subject, or a targeted admin action for a frozen cell).
"""
import datetime

from collections import defaultdict

from django.core.management.base import BaseCommand

from portal.models import (
    Grade, QuarterGrade, QuarterLock, School, Student,
    normalize_class_name, normalize_subject,
)

SOURCE_PERIOD = 'Холҳои ҷорӣ (Онлайн)'
QUARTERS = (1, 2, 3, 4)


def _current_academic_year(today=None):
    """(start, end) dates of the Sep..Aug academic year containing today."""
    today = today or datetime.date.today()
    start_year = today.year if today.month >= 9 else today.year - 1
    return (datetime.date(start_year, 9, 1),
            datetime.date(start_year + 1, 8, 31))


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

        totals = defaultdict(int)
        ay_start, ay_end = _current_academic_year()

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

            pools = defaultdict(list)
            out_of_year = 0
            for g in Grade.objects.filter(
                    student__school=school, period=SOURCE_PERIOD,
                    score__isnull=False,
            ).select_related('assessment').iterator(chunk_size=2000):
                pools[(g.student_id,
                       normalize_subject(g.subject))].append(g)
                if not (ay_start <= g.date <= ay_end):
                    out_of_year += 1

            # Cells to check: every (student, subject) with source rows or
            # a stored row under the student's CURRENT class label.
            cells = set(pools)
            cells.update((sid, subj) for sid, cls, subj, q in stored)

            for sid, subj in cells:
                st = students.get(sid)
                if st is None:
                    continue
                cls = normalize_class_name(st.class_name)
                rows = pools.get((sid, subj), ())
                for q in quarters:
                    value = calc_quarter_value(
                        *_split_quarter_grades(rows, q))
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
                    elif is_locked:
                        kind = ('FROZEN-EMPTY' if expected is None
                                else 'FROZEN-CONFLICT')
                    elif expected is None:
                        kind = 'ORPHAN'
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

            if out_of_year:
                totals['OUT-OF-YEAR-ROWS'] += out_of_year
                if not options['no_lines']:
                    self.stdout.write(
                        f'OUT-OF-YEAR-ROWS  мактаб={school.pk} '
                        f'сатрҳо={out_of_year} — сана берун аз соли '
                        f'таҳсили ҷорӣ ({ay_start}..{ay_end}) аст вале '
                        f'ба ҳавзаи чорак дохил мешавад'
                    )

        self.stdout.write('')
        self.stdout.write('Хулоса:')
        for kind in ('STALE', 'ORPHAN', 'MISSING', 'FROZEN-CONFLICT',
                     'FROZEN-EMPTY', 'STALE-LABEL', 'DUPLICATE',
                     'OUT-OF-YEAR-ROWS'):
            self.stdout.write(f'  {kind}: {totals.get(kind, 0)}')
        if not any(totals.values()):
            self.stdout.write('  Ҳама ҳолҳои чорякӣ мувофиқанд.')
