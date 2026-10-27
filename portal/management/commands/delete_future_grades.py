# -*- coding: utf-8 -*-
"""«Санаи оянда» сатрларини тозалаш — dry-run аз пешфарз.

Journal writes dated after today are phantom entries: the lesson has not
happened yet, so no real score/attendance can exist there — but they sit
in the quarter pools and silently distort quarterly grades and reports.
The runtime now rejects such writes; this command removes the rows that
already slipped in.

DEFAULT IS DRY-RUN: without --apply nothing is written — it only prints
the scope grouped by school/class/subject.

--apply deletes, inside one transaction:
  * every Grade row with date > today (any period — none can be real);
  * every Assessment dated after today (a future control slot can no
    longer hold grades, and its ghost «Н» column would only confuse);
  then re-derives every affected (student, subject, quarter, year) cell
  through recalculate_quarter_grades_bulk — the same authoritative
  pipeline the signals and the Ҳисобкунӣ button use. Locked quarters
  keep their frozen values (the recalc honours locks as usual).

Idempotent: re-running finds an empty scope and writes nothing.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from portal.models import Assessment, Grade


class Command(BaseCommand):
    help = ('Санаи ояндадаги холҳо/давомат сатрларини ўчиради: '
            'dry-run аз пешфарз, --apply ҳақиқатан тозалайди ва '
            'ҳолҳои чорякиро аз нав ҳосил мекунад.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--school', type=int, default=None,
            help='Limit to one school ID (default: all schools).')
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete the rows. Default: dry-run report.')

    def handle(self, *args, **options):
        today = timezone.localdate()
        grades = Grade.objects.filter(date__gt=today)
        assessments = Assessment.objects.filter(date__gt=today)
        if options['school'] is not None:
            grades = grades.filter(student__school_id=options['school'])
            assessments = assessments.filter(
                class_subject__school_id=options['school'])

        # Scope report grouped by (school, class, subject).
        groups = defaultdict(lambda: [0, None, None])
        for row in grades.values(
                'student__school_id', 'student__school__name',
                'student__class_name', 'subject', 'date'):
            key = (row['student__school_id'],
                   row['student__school__name'],
                   row['student__class_name'], row['subject'])
            g = groups[key]
            g[0] += 1
            d = row['date']
            g[1] = d if g[1] is None else min(g[1], d)
            g[2] = d if g[2] is None else max(g[2], d)

        total = sum(g[0] for g in groups.values())
        self.stdout.write(f'Имрӯз: {today} — санаи оянда = {today} дан кейин')
        for (sid, sname, cls, subj), (cnt, d_min, d_max) in sorted(
                groups.items(), key=lambda kv: (
                    kv[0][0] or 0, kv[0][2] or '', kv[0][3] or '')):
            self.stdout.write(
                f'  мактаб={sid} ({sname}) синф={cls} фан={subj} '
                f'сатрҳо={cnt} ({d_min}..{d_max})')
        a_count = assessments.count()
        self.stdout.write(f'Жами хол-сатрҳои оянда: {total}')
        self.stdout.write(f'Санҷишҳои санаи оянда: {a_count}')

        if not options['apply']:
            self.stdout.write(
                'DRY-RUN: ягон тағйирот анҷом нашуд. '
                'Барои ўчириш --apply лозим аст.')
            return

        if not total and not a_count:
            self.stdout.write('Тозаланадиган чиз нест.')
            return

        from portal.signals import defer_quarter_recalc
        from portal.views import recalculate_quarter_grades_bulk

        with transaction.atomic(), defer_quarter_recalc() as deferred:
            # Queryset deletes fire per-row signals — each removed row
            # queues its (student, subject, quarter, year) cell for the
            # bulk re-derivation below. Grades go first so a deleted
            # future assessment captures no linked rows left.
            deleted_grades = grades.delete()[0]
            deleted_assessments = assessments.delete()[0]
            recalculate_quarter_grades_bulk(deferred)

        # Bulk writes bypass QuarterGrade signals — refresh ranking caches.
        from portal.signals import invalidate_ranking_caches
        invalidate_ranking_caches(None)

        self.stdout.write(
            f'Ўчирилди: хол-сатрҳо={deleted_grades} '
            f'санҷишҳо={deleted_assessments} — '
            f'чоряк ҳоллари аз нав ҳисобланди ({len(deferred)} ҳуҷайра).')
