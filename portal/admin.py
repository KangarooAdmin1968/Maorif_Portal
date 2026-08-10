from django.contrib import admin
from .models import School, Teacher, Student, Grade, QuarterGrade, QuarterLock, ClassSubject, UserProfile

admin.site.site_header = "Шӯъбаи маорифи ноҳияи Зафаробод"
admin.site.site_title = "Портали маориф"
admin.site.index_title = "Маркази идоракунии тизим"


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ['name', 'type', 'director', 'phone', 'language', 'students_count', 'classes_count']
    list_filter = ['type', 'language']
    search_fields = ['name', 'director']


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ['name', 'school', 'subject', 'experience', 'category', 'is_teacher']
    list_filter = ['school', 'is_teacher']
    search_fields = ['name', 'subject']


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ['id', 'full_name', 'class_name', 'school']
    list_filter = ['school', 'class_name']
    search_fields = ['full_name', 'id']


@admin.register(Grade)
class GradeAdmin(admin.ModelAdmin):
    list_display = ['student', 'subject', 'score', 'period', 'date']
    list_filter = ['subject', 'date']
    search_fields = ['student__full_name', 'subject']


@admin.register(QuarterGrade)
class QuarterGradeAdmin(admin.ModelAdmin):
    list_display = ['student', 'class_name', 'subject', 'quarter', 'grade', 'att_grade']
    list_filter = ['quarter', 'class_name']


@admin.register(ClassSubject)
class ClassSubjectAdmin(admin.ModelAdmin):
    list_display = ['school', 'class_name', 'subject', 'is_default', 'is_active']
    list_filter = ['school', 'is_default', 'is_active']
    search_fields = ['subject', 'class_name']


@admin.register(QuarterLock)
class QuarterLockAdmin(admin.ModelAdmin):
    list_display = ['school', 'class_name', 'subject', 'quarter', 'locked', 'locked_at']
    list_filter = ['locked', 'quarter', 'school']
    search_fields = ['class_name', 'subject']


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'school', 'assigned_class', 'assigned_subject']
    list_filter = ['role', 'school']
