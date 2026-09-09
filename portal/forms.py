from django import forms
from .models import School, Teacher, Student, Grade, ClassSubject


class LoginForm(forms.Form):
    username = forms.CharField(label='Номи корбар')
    password = forms.CharField(label='Рамз', widget=forms.PasswordInput)


class SchoolForm(forms.ModelForm):
    class Meta:
        model = School
        fields = ['name', 'director', 'phone', 'type', 'language']


class TeacherForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = ['school', 'name', 'subject', 'experience', 'category', 'age', 'phone', 'education', 'photo', 'is_teacher']


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ['full_name', 'class_name', 'school']


class GradeForm(forms.ModelForm):
    class Meta:
        model = Grade
        fields = ['student', 'subject', 'score', 'period']


class ClassSubjectForm(forms.ModelForm):
    class Meta:
        model = ClassSubject
        fields = ['school', 'class_name', 'subject', 'is_active']


class ClassSubjectAdminForm(forms.ModelForm):
    class Meta:
        model = ClassSubject
        fields = ['school', 'class_name', 'subject', 'is_active']

    class_name = forms.ChoiceField(label='Синф', choices=[], required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        school = self.initial.get('school') or self.data.get('school')
        if not school and self.instance and self.instance.pk:
            school = self.instance.school_id
        try:
            school = int(school) if school is not None else None
        except (ValueError, TypeError):
            school = None
        if school:
            self.fields['class_name'].choices = self._class_choices(school)
        else:
            self.fields['class_name'].choices = [('', 'Аввал муассисаро интихоб кунед')]

    @staticmethod
    def _class_choices(school_id):
        from .models import School, Student, ClassSubject, normalize_class_name
        from .utils import class_numeric_part
        try:
            school = School.objects.get(pk=school_id)
        except School.DoesNotExist:
            return [('', 'Муассиса ёфт нашуд')]
        names = set()
        names.update(ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct())
        names.update(Student.objects.filter(school=school).values_list('class_name', flat=True).distinct())
        names = {normalize_class_name(n) for n in names if n}
        names = sorted(names, key=lambda c: (class_numeric_part(c) or 0, c))
        return [('', 'Синфро интихоб кунед...')] + [(n, n) for n in names]


class BulkClassSubjectForm(forms.Form):
    grade = forms.ChoiceField(label='Синф (рақам)', choices=[], required=True)
    subject = forms.ChoiceField(label='Фан', choices=[], required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.conf import settings
        from .utils import official_subjects
        grades = sorted(settings.TJC_SUBJECTS.keys(), key=lambda x: int(x))
        self.fields['grade'].choices = [('', 'Рақами синфро интихоб кунед...')] + [(g, g) for g in grades]
        subjects = sorted(official_subjects())
        self.fields['subject'].choices = [('', 'Фанро интихоб кунед...')] + [(s, s) for s in subjects]
