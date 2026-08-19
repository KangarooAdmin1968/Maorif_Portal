import re
import datetime
from django.db import models
from django.contrib.auth.models import User
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


def normalize_class_name(raw):
    """10a, 10-a, 10_a -> 10-А (Cyrillic uppercase)"""
    if not raw:
        return ''
    raw = str(raw).strip().upper()
    # unify separators
    raw = raw.replace('_', '-')
    raw = raw.replace(' ', '-')
    match = re.search(r'(\d+)\s*[-]?\s*([A-ZА-ЯЁ])', raw)
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
    id = models.CharField('Рамзи ID', max_length=255, primary_key=True)
    full_name = models.CharField('Ному насаб', max_length=255)
    class_name = models.CharField('Синф', max_length=20)
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')

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
        """Calculates the overall behavioral status badge in Tajik."""
        grades = self.grade_set.filter(behavior_score__isnull=False)
        if not grades.exists():
            return "Маълумот нест"
        avg_behavior = sum(g.behavior_score for g in grades) / grades.count()
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


class ClassSubject(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, verbose_name='Муассиса')
    class_name = models.CharField('Синф', max_length=20)
    subject = models.CharField('Фан', max_length=100)
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='Омӯзгор')
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


class Grade(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, verbose_name='Хонанда')
    subject = models.CharField('Фан', max_length=100)
    score = models.FloatField('Хол', blank=True, null=True)
    period = models.CharField('Давра', max_length=100, default='Холҳои ҷорӣ (Онлайн)')
    date = models.DateField('Сана', default=datetime.date.today)
    attendance = models.CharField('Давомат', max_length=10, blank=True, null=True, choices=[('+', '+ (босабаб)'), ('-', '- (бесабаб)')])
    behavior_score = models.IntegerField('Хулқ-атвор', blank=True, null=True, choices=[(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

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
