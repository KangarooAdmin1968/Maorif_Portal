import re
import datetime
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.conf import settings


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


class Student(models.Model):
    GENDER_CHOICES = [
        ('M', 'Писар'),
        ('F', 'Духтар'),
    ]

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

    class Meta:
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

    @property
    def behavior_status(self):
        """Calculates the overall behavioral status badge in Tajik.

        Each subject contributes equally: the per-subject average of behavior
        marks is computed first, then averaged across all subjects.
        """
        from django.db.models import Avg
        per_subject = self.grade_set.filter(
            behavior_score__isnull=False
        ).values('subject').annotate(avg=Avg('behavior_score'))
        count = per_subject.count()
        if not count:
            return "Маълумот нест"
        avg_behavior = sum(r['avg'] for r in per_subject) / count
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


class ClassSubject(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    class_name = models.CharField('Синф', max_length=20)
    subject = models.CharField('Фан', max_length=100)
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Омӯзгор')
    allocated_teacher = models.ForeignKey('TeacherProfile', on_delete=models.SET_NULL, blank=True, null=True, related_name='allocated_subjects', verbose_name='Омӯзгори масъул')
    hours_per_week = models.PositiveSmallIntegerField('Соат дар ҳафта', blank=True, null=True)
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
    date = models.DateField('Сана', default=datetime.date.today)
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
    grade = models.IntegerField('Хол', blank=True, null=True)
    att_grade = models.IntegerField('Холи атт.', blank=True, null=True)

    class Meta:
        unique_together = ['student', 'class_name', 'subject', 'quarter']
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
    locked = models.BooleanField('Баста шудааст', default=True)
    locked_at = models.DateTimeField('Санаи бастан', auto_now_add=True)

    class Meta:
        unique_together = [['school', 'class_name', 'subject', 'quarter']]
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

    class Meta:
        verbose_name = 'Профили омӯзгор'
        verbose_name_plural = 'Профилҳои омӯзгорон'

    def __str__(self):
        return f"{self.full_name} — {self.school}"


class SubjectDeactivationRequest(models.Model):
    class_subject = models.ForeignKey(ClassSubject, on_delete=models.CASCADE, verbose_name='Фани синф')
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deactivation_requests', verbose_name='Дархосткунанда')
    requested_at = models.DateTimeField('Санаи дархост', auto_now_add=True)
    status = models.CharField('Ҳолат', max_length=20, choices=[
        ('pending', 'Дар тайёри'),
        ('approved', 'Тасдиқ шуд'),
        ('rejected', 'Рад шуд'),
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
