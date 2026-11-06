import re
import datetime
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone
from django.utils.functional import cached_property


SCHOOL_TYPE_CHOICES = [
    ('Мактаб', 'Мактаб'),
    ('Литсей', 'Литсей'),
    ('Лицей', 'Лицей'),
    ('Муассисаи давлатии таълимии томактабӣ', 'Муассисаи давлатии таълимии томактабӣ'),
    ('Идораи маориф', 'Идораи маориф'),
]

LANGUAGE_CHOICES = [
    ('Тоҷикӣ', 'Тоҷикӣ'),
    ('Ӯзбекӣ', 'Ӯзбекӣ'),
    ('Русӣ', 'Русӣ'),
]


# Official Tajik Cyrillic class letters (uppercase) in standard order
CLASS_LETTERS = 'АБВГҒДЕЁЖЗИӢЙКҚЛМНОПРСТУӮФХҲЧҶШЪЭЮЯ'


def normalize_class_name(raw):
    """10a, 10-a, 10_a -> 10-А (Cyrillic uppercase)"""
    if not raw:
        return ''
    raw = str(raw).strip().upper()
    # unify separators
    raw = raw.replace('_', '-')
    raw = raw.replace(' ', '-')
    match = re.search(rf'(\d+)\s*[-]?\s*([A-Z{CLASS_LETTERS}])', raw)
    if match:
        num = match.group(1)
        let = match.group(2)
        trans = {'A': 'А', 'B': 'Б', 'C': 'В', 'K': 'К'}
        let = trans.get(let, let)
        return f"{num}-{let}"
    return raw


def normalize_subject(raw):
    """Merge typo variants like OIH/ODH into ОИХ."""
    if not isinstance(raw, str):
        raw = str(raw)
    sub = ' '.join(raw.strip().upper().split())
    synonyms = {
        'ОДХ': 'ОИХ', 'OIX': 'ОИХ', 'OIH': 'ОИХ', 'ODX': 'ОИХ',
        'ОИҲ': 'ОИХ', 'ОИХ': 'ОИХ',
    }
    return synonyms.get(sub, sub)


def is_litsey(school_type):
    return school_type and 'лит' in school_type.lower()


class School(models.Model):
    name = models.CharField('Номи муассиса', max_length=255, unique=True)
    director = models.CharField('Директор', max_length=255, blank=True)
    phone = models.CharField('Рақами телефон', max_length=50, blank=True)
    type = models.CharField('Намуд', max_length=50, choices=SCHOOL_TYPE_CHOICES, default='Мактаб')
    language = models.CharField('Забон', max_length=50, choices=LANGUAGE_CHOICES, default='Тоҷикӣ')

    class Meta:
        verbose_name = 'Муассисаи таълимӣ'
        verbose_name_plural = 'Муассисаҳои таълимӣ'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def students_count(self):
        return Student.objects.filter(school=self).count()

    @property
    def classes_count(self):
        return Student.objects.filter(school=self).values('class_name').distinct().count()

    def rank_label(self):
        return 'Ҷойи литсейӣ' if is_litsey(self.type) else 'Ҷойи мактабӣ'


class Teacher(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    name = models.CharField('Ном', max_length=255)
    subject = models.CharField('Фан', max_length=255, blank=True)
    experience = models.CharField('Таҷриба', max_length=50, blank=True)
    category = models.CharField('Тоифа', max_length=50, blank=True)
    age = models.PositiveIntegerField('Синн', blank=True, null=True)
    phone = models.CharField('Телефон', max_length=50, blank=True)
    education = models.CharField('Маълумот', max_length=255, blank=True)
    photo = models.ImageField('Акс', upload_to='teachers/', blank=True, null=True)
    is_teacher = models.BooleanField('Омӯзгор?', default=True)

    class Meta:
        verbose_name = 'Омӯзгор ва кадр'
        verbose_name_plural = 'Омӯзгорон ва кадрҳо'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.school})"


class ActiveStudents(models.Manager):
    """Default manager: only non-archived students.

    Roster queries (journals, norms, counts, reports) stay unchanged —
    archived (рафт) students simply do not appear. ``all_objects`` reaches
    every row for write paths, admin and re-enrollment. The unfiltered
    manager is also declared Meta.base_manager so related-object lookups
    (``grade.student``, cascades, dumps) still resolve archived rows.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_archived=False)


class Student(models.Model):
    GENDER_CHOICES = [
        ('M', 'Писар'),
        ('F', 'Духтар'),
    ]

    objects = ActiveStudents()
    all_objects = models.Manager()

    id = models.CharField('Рамзи ID', max_length=255, primary_key=True)
    full_name = models.CharField('Ному насаб', max_length=255)
    class_name = models.CharField('Синф', max_length=20)
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    gender = models.CharField(
        'Ҷинс',
        max_length=1,
        choices=GENDER_CHOICES,
        blank=True,
        null=True,
        db_index=True,
    )
    # Soft-delete for «Рафт»: the row (and all its grades) stays in the
    # database; roster queries hide it, archived students keep their
    # history and can be reactivated on re-enrollment.
    is_archived = models.BooleanField('Бойгонӣ', default=False)
    archived_at = models.DateTimeField('Санаи бойгонӣ', blank=True, null=True)

    class Meta:
        base_manager_name = 'all_objects'
        verbose_name = 'Хонанда'
        verbose_name_plural = 'Хонандагон'
        ordering = ['school', 'class_name', 'full_name']

    def __str__(self):
        return f"{self.full_name} — {self.class_name} — {self.school}"

    @property
    def is_graded(self):
        # Extract the grade level number from class_name (e.g., from "10-А" or "10a" extract "10")
        import re
        match = re.match(r'^(\d+)', self.class_name)
        if match:
            grade_level = match.group(1)
            return grade_level not in settings.NON_GRADED_CLASSES
        return True

    @cached_property
    def conduct_map(self):
        """Current conduct value per subject — read-side projection.

        The LATEST stored behavior_score per normalized subject wins:
        ``order_by('-date', '-pk')`` picks the newest day first, and for
        same-day multi-slot rows the most recently written record. Class
        subjects with no stored value contribute the display-only default
        5, which is never persisted. Historical Grade rows stay intact —
        only reads interpret them, so academic data is untouched.
        """
        latest = {}
        for subj, val in (
            self.grade_set
            .filter(behavior_score__isnull=False)
            .order_by('-date', '-pk')
            .values_list('subject', 'behavior_score')
        ):
            latest.setdefault(normalize_subject(subj), val)
        subjects = set(latest)
        subjects.update(
            normalize_subject(s) for s in
            ClassSubject.objects
            .filter(school=self.school, class_name=self.class_name,
                    is_active=True)
            .values_list('subject', flat=True)
        )
        return {s: latest.get(s, 5) for s in subjects}

    @property
    def behavior_status(self):
        """Overall conduct badge — mean of CURRENT per-subject values.

        Each subject counts once at its latest value (see conduct_map) or
        the display-only default 5; the existing thresholds are unchanged.
        A student with no entered conduct reads as Намунавӣ since every
        relevant subject is assumed to be at the good-behavior default.
        """
        values = list(self.conduct_map.values())
        avg_behavior = sum(values) / len(values) if values else 5
        if avg_behavior >= 4.5:
            return "Намунавӣ"
        elif avg_behavior >= 3.0:
            return "Қаноатбахш"
        else:
            return "Ноқаноатбахш"

    @property
    def total_excused(self):
        return self.grade_set.filter(attendance='+').count()

    @property
    def total_unexcused(self):
        return self.grade_set.filter(attendance='-').count()

    @property
    def attendance_percentage(self):
        total_days = Grade.objects.filter(
            student__class_name=self.class_name,
            student__school=self.school
        ).values('date', 'subject').distinct().count()
        if total_days == 0:
            return 100.0
        pct = ((total_days - self.total_unexcused) / total_days) * 100
        return max(round(pct, 1), 0.0)

    def save(self, *args, **kwargs):
        if self.full_name and not self.gender:
            from .gender_detector import detect_student_gender
            self.gender = detect_student_gender(self.full_name)
        self.class_name = normalize_class_name(self.class_name)
        self.id = f"{self.school.name}__{self.class_name}__{self.full_name}"
        super().save(*args, **kwargs)

        # Auto-populate default subjects for this class from the national curriculum
        import re
        match = re.match(r'^(\d+)', self.class_name)
        if match:
            grade_level = match.group(1)
            subjects = settings.TJC_SUBJECTS.get(grade_level, [])
            from django.apps import apps
            ClassSubject = apps.get_model('portal', 'ClassSubject')
            for subject in subjects:
                subj = normalize_subject(subject)
                ClassSubject.objects.get_or_create(
                    school=self.school,
                    class_name=self.class_name,
                    subject=subj,
                    defaults={'is_default': True, 'is_active': True}
                )
            # School-scoped extras (e.g. the Uzbek classes at School No. 5)
            from .utils import extra_subjects_for
            for subj in extra_subjects_for(self.school, self.class_name):
                ClassSubject.objects.get_or_create(
                    school=self.school,
                    class_name=self.class_name,
                    subject=subj,
                    defaults={'is_default': False, 'is_active': True}
                )


class StudentMovement(models.Model):
    """Омад/рафт journal — one row per student joining or leaving.

    student_name / class_name / gender are frozen snapshots: the record
    must keep its meaning after the Student row is deleted (рафт) or
    renamed. The optional ``student`` FK survives while the student
    exists. ``academic_year`` is the September-start year (same
    convention as QuarterGrade) so reports filter without re-deriving.
    Same-school class transfers never write a row — _move_student only
    re-keys the Student record, it is not a school-level move.
    """
    MOVEMENT_TYPES = [
        ('arrival', 'Омад'),
        ('departure', 'Рафт'),
    ]

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name='student_movements',
        verbose_name='Муассиса')
    student = models.ForeignKey(
        Student, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='movements', verbose_name='Хонанда')
    student_name = models.CharField('Ному насаб', max_length=255)
    class_name = models.CharField('Синф', max_length=20)
    gender = models.CharField(
        'Ҷинс', max_length=1, choices=Student.GENDER_CHOICES, blank=True)
    movement_type = models.CharField(
        'Навъ', max_length=10, choices=MOVEMENT_TYPES)
    date = models.DateField('Сана', default=datetime.date.today)
    academic_year = models.PositiveSmallIntegerField('Соли таҳсил')
    is_manual = models.BooleanField('Дастӣ ворид шуд', default=False)
    note = models.CharField('Эзоҳ', max_length=255, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='recorded_movements', verbose_name='Сабткунанда')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Ҳаракати хонанда'
        verbose_name_plural = 'Ҳаракатҳои хонандагон'
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['school', 'academic_year', 'date']),
        ]

    def __str__(self):
        return (f"{self.student_name} — {self.class_name} — "
                f"{self.get_movement_type_display()} — {self.date}")

    def save(self, *args, **kwargs):
        self.class_name = normalize_class_name(self.class_name)
        if not self.academic_year:
            self.academic_year = (
                self.date.year if self.date.month >= 9
                else self.date.year - 1)
        super().save(*args, **kwargs)


class StudentExemption(models.Model):
    """Муваққатан аз баҳогузорӣ озодшуда — e.g. «Дар давлати дигар таҳсил
    мекунад».

    The student stays on the roster (counted, listed, shown in every
    journal) but is exempt from grading norms and ungraded/deficiency
    lists while the exemption is in force. This is a DISTINCT status from
    is_archived (рафт): archived students leave the roster entirely.
    Set directly by завуч/директор in the class edit dialog — no
    proposal/approval workflow.
    """
    REASON_CHOICES = [
        ('abroad', 'Дар давлати дигар таҳсил мекунад'),
        ('home', 'Дар хона таҳсил мегирад'),
        ('temporary', 'Муваққатан баҳогузорӣ карда намешавад'),
        ('other', 'Сабаби дигар'),
    ]

    student = models.ForeignKey(
        Student, on_delete=models.CASCADE,
        related_name='exemptions', verbose_name='Хонанда')
    reason = models.CharField(
        'Сабаб', max_length=20, choices=REASON_CHOICES, default='abroad')
    # Free-text reference (document name/number) — no file upload.
    document = models.CharField('Ҳуҷҷати асос', max_length=255, blank=True)
    start_date = models.DateField('Санаи оғоз')
    # NULL = бемаҳдуд — the exemption stays in force until cancelled.
    end_date = models.DateField('Санаи анҷом', blank=True, null=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='created_exemptions', verbose_name='Сабткунанда')
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField('Санаи бекоркунӣ', blank=True, null=True)
    cancelled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='cancelled_exemptions', verbose_name='Бекоркунанда')

    class Meta:
        verbose_name = 'Истиснои хонанда'
        verbose_name_plural = 'Истисноҳои хонандагон'
        ordering = ['-start_date', '-id']

    def __str__(self):
        return f"{self.student} — {self.get_reason_display()}"

    def is_active(self, today=None):
        today = today or timezone.localdate()
        return (
            self.cancelled_at is None
            and self.start_date <= today
            and (self.end_date is None or self.end_date >= today)
        )


class ClassSubject(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    class_name = models.CharField('Синф', max_length=20)
    subject = models.CharField('Фан', max_length=100)
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Омӯзгор')
    allocated_teacher = models.ForeignKey('TeacherProfile', on_delete=models.SET_NULL, blank=True, null=True, related_name='allocated_subjects', verbose_name='Омӯзгори масъул')
    hours_per_week = models.DecimalField(
        'Соат дар ҳафта', max_digits=4, decimal_places=1,
        blank=True, null=True)
    is_default = models.BooleanField('Стандарт?', default=False)
    is_active = models.BooleanField('Фаъол?', default=True)

    class Meta:
        unique_together = ['school', 'class_name', 'subject']
        verbose_name = 'Фани синф'
        verbose_name_plural = 'Фанҳои синфӣ'

    def __str__(self):
        return f"{self.class_name}: {self.subject}"

    def save(self, *args, **kwargs):
        self.class_name = normalize_class_name(self.class_name)
        self.subject = normalize_subject(self.subject)
        super().save(*args, **kwargs)


class Subject(models.Model):
    """Canonical subject registry — one row per distinct subject name.

    TJC_SUBJECTS remains the official curriculum source; this registry holds
    intentionally added custom subjects (e.g. 'Забон ва адабиёти ӯзбек') so
    they have one identity instead of per-class ad-hoc ClassSubject strings.
    Grade/QuarterGrade stay string-based and keep matching by name.
    """
    name = models.CharField('Фан', max_length=100, unique=True)
    is_active = models.BooleanField('Фаъол?', default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True,
                                   related_name='created_subjects', verbose_name='Эҷодкарда')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Фан (реестр)'
        verbose_name_plural = 'Реестри фанҳо'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = normalize_subject(self.name)
        super().save(*args, **kwargs)


class SubjectAvailability(models.Model):
    """Where a canonical subject may be used.

    Precedence (most specific wins):
      1. (subject, school, class_name) — per-class rule
      2. (subject, school, '')         — whole school
      3. (subject, NULL, '')           — Ҳама муассисаҳо
    An inactive more-specific row is an explicit opt-out over a broader
    active row (e.g. subject enabled school-wide but removed from 5-Д).
    """
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE,
                                related_name='availabilities', verbose_name='Фан')
    school = models.ForeignKey(School, on_delete=models.CASCADE, blank=True, null=True,
                               related_name='subject_availabilities', verbose_name='Муассиса')
    class_name = models.CharField('Синф', max_length=20, blank=True, default='')
    is_active = models.BooleanField('Фаъол?', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Дастрасии фан'
        verbose_name_plural = 'Дастрасии фанҳо'

    def __str__(self):
        scope = self.school.name if self.school_id else 'Ҳама муассисаҳо'
        if self.class_name:
            return f"{self.subject.name} — {scope} / {self.class_name}"
        return f"{self.subject.name} — {scope}"

    def save(self, *args, **kwargs):
        if self.class_name:
            self.class_name = normalize_class_name(self.class_name)
        super().save(*args, **kwargs)


class TeachingGroup(models.Model):
    """A teaching subgroup inside one canonical ClassSubject.

    One subject stays one ClassSubject row (no 'English 2' duplicates);
    groups let different teachers teach the same subject to different
    student subsets. Group-scoped permission/UI wiring lands in later
    phases — this model is storage only.
    """
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.CASCADE, related_name='groups',
        verbose_name='Фани синф')
    label = models.CharField('Гурӯҳ', max_length=50)
    teacher = models.ForeignKey(
        'TeacherProfile', on_delete=models.SET_NULL, blank=True, null=True,
        related_name='teaching_groups', verbose_name='Омӯзгор')
    is_active = models.BooleanField('Фаъол?', default=True)
    # Home-education group (таҳсил дар хона): lives alongside the class
    # subject without switching it into group-scoped mode — the regular
    # class teacher keeps full journal access and only the home students
    # are split out. Weekly hours are explicit because a home group never
    # carries the class's full subject load.
    is_home = models.BooleanField('Таҳсил дар хона?', default=False)
    hours_per_week = models.DecimalField(
        'Соат дар як ҳафта', max_digits=3, decimal_places=1,
        blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['class_subject', 'label'],
                name='unique_teaching_group_label',
            ),
        ]
        verbose_name = 'Гурӯҳи таълимӣ'
        verbose_name_plural = 'Гурӯҳҳои таълимӣ'

    def __str__(self):
        return f"{self.class_subject} — {self.label}"

    def clean(self):
        if self.teacher_id and self.teacher.school_id != self.class_subject.school_id:
            raise ValidationError('Омӯзгори гурӯҳ бояд аз ҳамон муассиса бошад.')


class SubjectGroupMembership(models.Model):
    """One student's membership in a TeachingGroup.

    The denormalized class_subject FK makes the DB-level rule "a student
    belongs to at most one group per ClassSubject" enforceable via a plain
    unique constraint. Cross-FK consistency (membership.group belongs to
    membership.class_subject) is validated at application level in clean().
    """
    class_subject = models.ForeignKey(
        ClassSubject, on_delete=models.CASCADE,
        related_name='group_memberships', verbose_name='Фани синф')
    group = models.ForeignKey(
        TeachingGroup, on_delete=models.CASCADE,
        related_name='memberships', verbose_name='Гурӯҳ')
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE,
        related_name='subject_group_memberships', verbose_name='Хонанда')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['class_subject', 'student'],
                name='unique_group_membership_per_subject',
            ),
        ]
        verbose_name = 'Узвияти гурӯҳӣ'
        verbose_name_plural = 'Узвиятҳои гурӯҳӣ'

    def __str__(self):
        return f"{self.student} — {self.group}"

    def clean(self):
        if self.group_id and self.group.class_subject_id != self.class_subject_id:
            raise ValidationError('Гурӯҳ ба ҳамин фан тааллуқ надорад.')
        if self.student_id and (
            self.student.class_name != self.class_subject.class_name
            or self.student.school_id != self.class_subject.school_id
        ):
            raise ValidationError('Хонанда ба ҳамин синф тааллуқ надорад.')


class Lesson(models.Model):
    class_subject = models.ForeignKey(ClassSubject, on_delete=models.CASCADE)
    group = models.ForeignKey(
        TeachingGroup, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='lessons', verbose_name='Гурӯҳ')
    date = models.DateField('Сана')
    lesson_number = models.PositiveSmallIntegerField('Рақами дарс', default=1)
    topic = models.CharField('Мавзӯъ', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('class_subject', 'date', 'lesson_number')
        ordering = ['-date', 'lesson_number']

    def __str__(self):
        return f"{self.class_subject} — {self.date} — дарси {self.lesson_number}"

    def clean(self):
        if self.group_id and self.group.class_subject_id != self.class_subject_id:
            raise ValidationError('Гурӯҳ ба ҳамин дарс тааллуқ надорад.')


class Assessment(models.Model):
    CATEGORY_CHOICES = [
        ('current', 'Ҷорӣ'),
        ('control', 'Назорат'),
    ]
    PURPOSE_CHOICES = [
        ('diagnostic', 'Арзёбии ташхисӣ'),
        ('formative', 'Арзёбии ташаккулдиҳанда'),
        ('summative', 'Арзёбии ҷамъбастӣ'),
    ]
    SCOPE_CHOICES = [
        ('none', '—'),
        ('intermediate', 'Миёна'),
        ('final', 'Ниҳоӣ'),
        ('quarter', 'Чоряк'),
        ('semester', 'Нимсола'),
        ('annual', 'Солона'),
        ('attestation', 'Аттестатсия'),
    ]

    class_subject = models.ForeignKey(ClassSubject, on_delete=models.CASCADE)
    lesson = models.ForeignKey(Lesson, on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateField('Сана')
    quarter = models.IntegerField('Чорак', choices=[(1, '1'), (2, '2'), (3, '3'), (4, '4')])
    category = models.CharField('Категория', max_length=20, choices=CATEGORY_CHOICES)
    number = models.PositiveSmallIntegerField('Рақам', null=True, blank=True)
    title = models.CharField('Ном', max_length=255, blank=True)
    is_ajm = models.BooleanField('АҶМ', default=False)
    # Methodology layer (neutral codes; legacy rows stay blank -> explicit
    # compatibility mapping in assessment_catalog.result_semantics_for()).
    purpose = models.CharField('Мақсад', max_length=20, choices=PURPOSE_CHOICES, blank=True, default='')
    work_type = models.CharField('Навъи кор', max_length=30, blank=True, default='')
    summative_scope = models.CharField('Доираи ҷамъбастӣ', max_length=20, choices=SCOPE_CHOICES, default='none', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['class_subject', 'quarter', 'category']),
        ]

    def __str__(self):
        return f"{self.get_category_display()} — {self.class_subject} — {self.date}"


class Grade(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, verbose_name='Хонанда')
    subject = models.CharField('Фан', max_length=100)
    score = models.FloatField('Хол', blank=True, null=True)
    period = models.CharField('Давра', max_length=100, default='Холҳои ҷорӣ (Онлайн)')
    date = models.DateField('Сана', default=datetime.date.today, db_index=True)
    attendance = models.CharField('Давомат', max_length=10, blank=True, null=True, choices=[('+', '+ (босабаб)'), ('-', '- (бесабаб)')])
    behavior_score = models.IntegerField('Хулқ-атвор', blank=True, null=True, choices=[(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
    sticker = models.CharField('Стикер', max_length=10, blank=True, null=True, choices=[('⭐', 'Ситора'), ('☀️', 'Офтобак'), ('🌸', 'Гул'), ('📖', 'Китоб')])
    lesson = models.ForeignKey(Lesson, on_delete=models.SET_NULL, null=True, blank=True)
    assessment = models.ForeignKey(Assessment, on_delete=models.SET_NULL, null=True, blank=True)
    components = models.JSONField('Ҷузъҳо', null=True, blank=True)
    # Raw test points (converted_points semantics): enough data is stored to
    # re-derive percentage and the 10-point score if the conversion table
    # changes. Both are NULL for non-test rows.
    points_achieved = models.FloatField('Холҳои гирифташуда', blank=True, null=True)
    points_possible = models.FloatField('Холҳои имконпазир', blank=True, null=True)

    class Meta:
        verbose_name = 'Хол'
        verbose_name_plural = 'Холҳо'
        ordering = ['-date']
        indexes = [
            # Quarter pools + per-cell recalc: (student, subject, period)
            # equality then the date window of the contributing quarter.
            models.Index(fields=['student', 'subject', 'period', 'date'],
                         name='grade_stu_subj_per_date'),
            # School-wide month/norm scans: subject + date range.
            models.Index(fields=['subject', 'date'], name='grade_subj_date'),
        ]

    def __str__(self):
        return f"{self.student} — {self.subject} — {self.score} — {self.attendance} — {self.behavior_score}"

    def save(self, *args, **kwargs):
        self.subject = normalize_subject(self.subject)
        super().save(*args, **kwargs)


class QuarterGrade(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, verbose_name='Хонанда')
    class_name = models.CharField('Синф', max_length=20)
    subject = models.CharField('Фан', max_length=100)
    quarter = models.IntegerField('Чорак', choices=[(0, 'Аттестатсия'), (1, '1'), (2, '2'), (3, '3'), (4, '4')])
    # September-start year of the academic year this result belongs to
    # (2026 -> 2026-09-01..2027-08-31). NULL marks a legacy row whose year
    # could not be attributed reliably — it is preserved for review but
    # never presented as the current year's official grade.
    academic_year = models.PositiveSmallIntegerField(
        'Соли таҳсил', blank=True, null=True)
    grade = models.IntegerField('Хол', blank=True, null=True)
    att_grade = models.IntegerField('Холи атт.', blank=True, null=True)

    class Meta:
        unique_together = [
            'student', 'class_name', 'subject', 'quarter', 'academic_year']
        verbose_name = 'Холи чоряк'
        verbose_name_plural = 'Холҳои чорякӣ'

    def save(self, *args, **kwargs):
        self.class_name = normalize_class_name(self.class_name)
        self.subject = normalize_subject(self.subject)
        super().save(*args, **kwargs)


class QuarterLock(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    class_name = models.CharField('Синф', max_length=20)
    subject = models.CharField('Фан', max_length=100)
    quarter = models.IntegerField('Чорак', choices=[(0, 'Аттестатсия'), (1, '1'), (2, '2'), (3, '3'), (4, '4')])
    # September-start academic year the lock applies to — a lock on Q1 of
    # one year must never freeze Q1 of another. NULL is a legacy row;
    # is_quarter_locked attributes it by its own locked_at date.
    academic_year = models.PositiveSmallIntegerField(
        'Соли таҳсил', blank=True, null=True)
    locked = models.BooleanField('Баста шудааст', default=True)
    locked_at = models.DateTimeField('Санаи бастан', auto_now_add=True)

    class Meta:
        unique_together = [
            ['school', 'class_name', 'subject', 'quarter', 'academic_year']]
        verbose_name = 'Бастани чоряк'
        verbose_name_plural = 'Бастани чорякҳо'
        ordering = ['-locked_at']

    def __str__(self):
        return f"{self.school} — {self.class_name} — {self.subject} — {self.get_quarter_display()}"

    def save(self, *args, **kwargs):
        self.class_name = normalize_class_name(self.class_name)
        self.subject = normalize_subject(self.subject)
        super().save(*args, **kwargs)


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name='Корбар')
    role = models.CharField('Рол', max_length=50, choices=settings.ROLE_CHOICES, default=settings.ROLE_TEACHER)
    school = models.ForeignKey(School, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Муассиса')
    assigned_class = models.CharField('Синфи вобаста', max_length=20, blank=True)
    assigned_subject = models.CharField('Фани вобаста', max_length=100, blank=True)

    class Meta:
        verbose_name = 'Профили корбар'
        verbose_name_plural = 'Профилҳои корбарон'

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"


class TeacherProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name='Корбар')
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    full_name = models.CharField('Ному насаб', max_length=255)
    phone = models.CharField('Телефон', max_length=50, blank=True, null=True)
    education = models.CharField('Маълумот', max_length=255, blank=True, null=True)
    specialty = models.CharField('Ихтисос', max_length=255, blank=True, null=True)
    custom_password_set = models.BooleanField('Рамзи шахсӣ таъин шуд', default=False)
    # Stable payroll datum for the Ҳуҷҷатнигорӣ workload tabel (column D).
    # Entered by the deputy principal; NULL renders blank, never zero.
    monthly_rate = models.DecimalField(
        'Музди меҳнати моҳона (1 ставка)', max_digits=10, decimal_places=2,
        blank=True, null=True)
    # Archived instead of deleted when TeacherTabelMonth history exists:
    # the profile (and its months) survive; the login is deactivated and
    # all live allocations are cleared. Archived profiles can never
    # receive new allocations or new tabel entries.
    is_archived = models.BooleanField('Бойгонӣ шуд', default=False)

    class Meta:
        verbose_name = 'Профили омӯзгор'
        verbose_name_plural = 'Профилҳои омӯзгорон'

    def __str__(self):
        return f"{self.full_name} — {self.school}"


class TeacherTabelMonth(models.Model):
    """Month-scoped tabel data for one teacher (Ҳуҷҷатнигорӣ → Табел).

    Only data that has no other source of truth lives here: missed lessons,
    substitutions, notebook-check hours and the payroll supplements the
    deputy principal enters by hand. Weekly workload itself is computed
    from ClassSubject allocations; ``workload_snapshot`` freezes a copy of
    it the first time a month is recorded so that later re-allocation does
    not silently rewrite a recorded month. NULLs mean "not entered" and
    render as blank — never as zero.

    ``school`` records which school owned the month at capture time, so a
    teacher who transfers keeps their historical rows under the original
    school instead of leaking them into the new school's tabel.
    """
    school = models.ForeignKey(
        School, on_delete=models.CASCADE,
        related_name='tabel_months', verbose_name='Мактаб')
    # PROTECT: no deletion path (view, admin, shell) may silently destroy
    # recorded payroll history — removal must archive the profile instead.
    teacher = models.ForeignKey(
        TeacherProfile, on_delete=models.PROTECT,
        related_name='tabel_months', verbose_name='Омӯзгор')
    year = models.PositiveSmallIntegerField('Сол')
    month = models.PositiveSmallIntegerField('Моҳ')
    # Identity frozen at first save so a recorded month keeps its name and
    # rate after live profile edits. rates_snapshot is a list of rate-line
    # dicts ({'line_no', 'title', 'rate'}; rate stored as a Decimal-safe
    # string) — forward-compatible with the upcoming multi-rate design.
    # NULL on legacy rows means "no snapshot": display falls back to live
    # values rather than inventing history.
    teacher_name = models.CharField(
        'Ному насаб (акси сабт)', max_length=255, blank=True, null=True)
    rates_snapshot = models.JSONField(
        'Акси ставкаҳо', blank=True, null=True)
    # Миқдори соатҳои нагузашта (missed lessons), per grade band
    missed_1_4 = models.DecimalField('Нагузашта 1–4', max_digits=6, decimal_places=2, blank=True, null=True)
    missed_5_9 = models.DecimalField('Нагузашта 5–9', max_digits=6, decimal_places=2, blank=True, null=True)
    missed_10_11 = models.DecimalField('Нагузашта 10–11', max_digits=6, decimal_places=2, blank=True, null=True)
    # Ивазкунии дарсҳо (substitutions), per grade band
    substitution_1_4 = models.DecimalField('Ивазкунӣ 1–4', max_digits=6, decimal_places=2, blank=True, null=True)
    substitution_5_9 = models.DecimalField('Ивазкунӣ 5–9', max_digits=6, decimal_places=2, blank=True, null=True)
    substitution_10_11 = models.DecimalField('Ивазкунӣ 10–11', max_digits=6, decimal_places=2, blank=True, null=True)
    # Тафтиши дафтар (notebook check), per grade band
    notebook_1_4 = models.DecimalField('Тафтиши дафтар 1–4', max_digits=6, decimal_places=2, blank=True, null=True)
    notebook_5_9 = models.DecimalField('Тафтиши дафтар 5–9', max_digits=6, decimal_places=2, blank=True, null=True)
    notebook_10_11 = models.DecimalField('Тафтиши дафтар 10–11', max_digits=6, decimal_places=2, blank=True, null=True)
    # Иловапулиҳо — entered amounts; the portal does not compute payroll.
    allowance_class_head = models.DecimalField('Иловапулӣ роҳбари синф', max_digits=10, decimal_places=2, blank=True, null=True)
    allowance_admin = models.DecimalField('Сарбории маъмурият', max_digits=10, decimal_places=2, blank=True, null=True)
    allowance_methodical = models.DecimalField('Иловапулӣ роҳбари иттиҳоди методӣ', max_digits=10, decimal_places=2, blank=True, null=True)
    allowance_language = models.DecimalField('Иловапулӣ забони русӣ/англисӣ', max_digits=10, decimal_places=2, blank=True, null=True)
    workload_snapshot = models.JSONField('Акси сарборӣ', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['school', 'teacher', 'year', 'month'],
                name='unique_tabel_month_per_teacher_school',
            ),
        ]
        verbose_name = 'Табели моҳонаи омӯзгор'
        verbose_name_plural = 'Табелҳои моҳонаи омӯзгорон'
        ordering = ['year', 'month', 'teacher']

    def __str__(self):
        return f"{self.teacher} — {self.year}-{self.month:02d}"

    def clean(self):
        if not self.month or not 1 <= self.month <= 12:
            raise ValidationError('Моҳ бояд аз 1 то 12 бошад.')


class SubjectDeactivationRequest(models.Model):
    class_subject = models.ForeignKey(ClassSubject, on_delete=models.CASCADE, verbose_name='Фани синф')
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deactivation_requests', verbose_name='Дархосткунанда')
    requested_at = models.DateTimeField('Санаи дархост', auto_now_add=True)
    status = models.CharField('Ҳолат', max_length=20, choices=[
        ('pending', 'Дар тайёри'),
        ('approved', 'Тасдиқ шуд'),
        ('rejected', 'Рад шуд'),
        ('cancelled', 'Бекор шуд'),
        ('reversed', 'Бекор гардид — фан барқарор шуд'),
    ], default='pending')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_deactivations', verbose_name='Баррасикунанда')
    reviewed_at = models.DateTimeField('Санаи баррасӣ', null=True, blank=True)
    notes = models.TextField('Эзоҳ', blank=True)

    class Meta:
        verbose_name = 'Дархости хориҷкунии фан'
        verbose_name_plural = 'Дархостҳои хориҷкунии фан'
        ordering = ['-requested_at']


class DailyActivity(models.Model):
    """One activity row per user per local (Asia/Dushanbe) day.

    Authenticated users are keyed by (user, date). Rows with user=NULL are
    the single daily aggregate for anonymous parent/student portal visitors
    — the partial unique constraint below keeps it to exactly one row per
    date. `username` is a snapshot so history stays identifiable if the
    User row is later deleted (remove_teacher drops auth rows).
    """
    CATEGORY_CHOICES = [
        ('staff', 'Корманди маориф'),
        ('director', 'Раиси маориф'),
        ('principal', 'Директори муассиса'),
        ('zavuch', 'Завуч'),
        ('teacher', 'Муаллим'),
        ('parent_student', 'Волид/хонанда'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Корбар')
    username = models.CharField('Номи корбар', max_length=150, blank=True)
    date = models.DateField('Сана', db_index=True)
    category = models.CharField('Категория', max_length=20, choices=CATEGORY_CHOICES)
    school = models.ForeignKey(School, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Муассиса')
    login_count = models.PositiveIntegerField('Миқдори воридшавӣ', default=0)
    first_seen = models.DateTimeField('Аввалин фаъолият')
    last_seen = models.DateTimeField('Охирин фаъолият')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date'],
                name='dailyactivity_user_date',
            ),
            models.UniqueConstraint(
                fields=['date'],
                condition=models.Q(user__isnull=True),
                name='dailyactivity_anon_date',
            ),
        ]
        indexes = [
            models.Index(fields=['date', 'school']),
            models.Index(fields=['date', 'category']),
        ]
        verbose_name = 'Фаъолияти ҳаррӯза'
        verbose_name_plural = 'Фаъолияти ҳаррӯза'
        ordering = ['-date']

    def __str__(self):
        who = self.username or 'Анонимӣ'
        return f"{who} — {self.date} — {self.get_category_display()}"


class News(models.Model):
    """Официальная хабар/аксҳои Шуъбаи маориф — «Ахбор ва хабарҳо».

    Status flow: draft -> published -> (unpublished | archived) -> restore.
    Only the district chief (ROLE_DIRECTOR) and superusers manage news —
    a published item never requires a second approval step.
    """
    CATEGORY_VISIT = 'visit'
    CATEGORY_MEETING = 'meeting'
    CATEGORY_EVENT = 'event'
    CATEGORY_ACHIEVEMENT = 'achievement'
    CATEGORY_RENOVATION = 'renovation'
    CATEGORY_CHOICES = [
        (CATEGORY_VISIT, 'Боздидҳо'),
        (CATEGORY_MEETING, 'Ҷаласаҳо'),
        (CATEGORY_EVENT, 'Чорабиниҳо'),
        (CATEGORY_ACHIEVEMENT, 'Дастовардҳо'),
        (CATEGORY_RENOVATION, 'Навгониҳо'),
    ]

    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_UNPUBLISHED = 'unpublished'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Лоиҳа'),
        (STATUS_PUBLISHED, 'Нашршуда'),
        (STATUS_UNPUBLISHED, 'Аз нашр гирифташуда'),
        (STATUS_ARCHIVED, 'Бойгонӣ'),
    ]

    MEDIA_PHOTO = 'photo'
    MEDIA_VIDEO = 'video'
    MEDIA_CHOICES = [
        (MEDIA_PHOTO, 'Акс'),
        (MEDIA_VIDEO, 'Видео'),
    ]

    category = models.CharField(
        'Категория', max_length=20, choices=CATEGORY_CHOICES)
    title = models.CharField('Сарлавҳа', max_length=200)
    # Nullable: a news item may belong to the district department itself
    # rather than one school. display label falls back to the dept name.
    school = models.ForeignKey(
        School, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='news_items', verbose_name='Муассиса')
    event_date = models.DateField('Санаи воқеа')
    summary = models.TextField('Тавсифи кӯтоҳ')
    # Media type: 'photo' keeps the classic photo flow; 'video' embeds an
    # Instagram Reel via the official /embed/ endpoint. Only the validated
    # shortcode is stored and rendered — raw Instagram HTML never enters
    # the page. video_thumb is a cover fetched once at save time so the
    # detail page needs no Instagram call before the user presses play.
    media_type = models.CharField(
        'Навъи медиа', max_length=10, choices=MEDIA_CHOICES,
        default=MEDIA_PHOTO)
    video_url = models.CharField(
        'Ҳаволаи видеои Instagram', max_length=300, blank=True)
    video_code = models.CharField(max_length=30, blank=True)
    video_thumb = models.ImageField(
        'Муқоваи видео', upload_to='news/%Y/%m/video/', blank=True)
    video_file = models.FileField(
        'Файли видео', upload_to='news/%Y/%m/video_file/', blank=True)
    status = models.CharField(
        'Ҳолат', max_length=20, choices=STATUS_CHOICES,
        default=STATUS_DRAFT, db_index=True)
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True,
        related_name='news_items', verbose_name='Муаллиф')
    published_at = models.DateTimeField(
        'Санаи нашр', blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Хабар'
        verbose_name_plural = 'Хабарҳо'
        ordering = ['-published_at', '-created_at']
        indexes = [
            models.Index(fields=['status', 'category']),
            models.Index(fields=['status', '-published_at']),
        ]

    def __str__(self):
        return f"{self.title} — {self.get_category_display()}"

    @property
    def school_name(self):
        return self.school.name if self.school else 'Шуъбаи маорифи ноҳия'

    @property
    def main_photo(self):
        # photos.all() hits the prefetch cache when the caller used
        # prefetch_related('photos') — no extra queries per row.
        photos = list(self.photos.all())
        for p in photos:
            if p.is_main:
                return p
        return photos[0] if photos else None

    @property
    def video_embed_url(self):
        if not self.video_code:
            return ''
        return (
            'https://www.instagram.com/reel/'
            + self.video_code + '/embed/')


class NewsPhoto(models.Model):
    """One photo of a news item (1–5 per item).

    Two processed WebP renditions are stored — slider (1200×675 cover)
    and gallery (max 1600px). The uploaded original is discarded after
    processing so unmanaged originals never accumulate on the server.
    """
    news = models.ForeignKey(
        News, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField('Акс (галерея)', upload_to='news/%Y/%m/')
    slider_image = models.ImageField(
        'Акс (слайдер)', upload_to='news/%Y/%m/slider/')
    order = models.PositiveSmallIntegerField('Тартиб', default=0)
    is_main = models.BooleanField('Акси асосӣ', default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Акси хабар'
        verbose_name_plural = 'Аксҳои хабарҳо'
        ordering = ['order', 'id']

    def __str__(self):
        return f"Акс #{self.pk} — {self.news_id}"
