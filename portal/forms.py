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
