# -*- coding: utf-8 -*-
"""Batch classify missing student genders using the bicultural name detector."""
from django.db.models import Q
from django.core.management.base import BaseCommand

from portal.models import Student
from portal.gender_detector import detect_student_gender


class Command(BaseCommand):
    help = 'Классификатсияи ҷинси хонандагон аз рӯи ном'

    def handle(self, *args, **options):
        queryset = Student.objects.filter(Q(gender__isnull=True) | Q(gender=''))
        total = queryset.count()

        if total == 0:
            self.stdout.write(self.style.WARNING('Хонандаи бе ҷинс ёфт нашуд.'))
            return

        males = 0
        females = 0
        unclassified = 0
        to_update = []

        for student in queryset.iterator():
            detected = detect_student_gender(student.full_name)
            if detected:
                student.gender = detected
                to_update.append(student)
                if detected == 'M':
                    males += 1
                else:
                    females += 1
            else:
                unclassified += 1

        if to_update:
            Student.objects.bulk_update(to_update, ['gender'])

        self.stdout.write(
            f'Скан шуд: {total}\n'
            f'Писарон: {males}\n'
            f'Духтарон: {females}\n'
            f'Классификатсия нашуда: {unclassified}'
        )
