from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse

from .forms import ClassSubjectAdminForm, BulkClassSubjectForm
from .models import (
    School, Teacher, Student, Grade, QuarterGrade, QuarterLock,
    ClassSubject, UserProfile, normalize_class_name, normalize_subject,
)
from .utils import class_numeric_part


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
    form = ClassSubjectAdminForm
    change_form_template = 'admin/portal/classsubject/change_form.html'
    change_list_template = 'admin/portal/classsubject/change_list.html'
    list_display = ['school', 'class_name', 'subject', 'teacher', 'is_default', 'is_active']
    list_filter = ['school', 'is_default', 'is_active']
    search_fields = ['subject', 'class_name', 'teacher__username']
    list_editable = ['teacher', 'is_active']

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path('school-classes/', self.admin_site.admin_view(self.school_classes_json),
                 name=f'{info[0]}_{info[1]}_school_classes'),
            path('bulk-assign/', self.admin_site.admin_view(self.bulk_assign_view),
                 name=f'{info[0]}_{info[1]}_bulk_assign'),
        ] + super().get_urls()

    def school_classes_json(self, request):
        school_id = request.GET.get('school')
        if not school_id:
            return JsonResponse([], safe=False)
        try:
            school_id = int(school_id)
        except (ValueError, TypeError):
            return JsonResponse([], safe=False)
        school = get_object_or_404(School, pk=school_id)
        names = set()
        names.update(ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct())
        names.update(Student.objects.filter(school=school).values_list('class_name', flat=True).distinct())
        names = {normalize_class_name(n) for n in names if n}
        names = sorted(names, key=lambda c: (class_numeric_part(c) or 0, c))
        return JsonResponse(list(names), safe=False)

    def bulk_assign_view(self, request):
        if not (request.user.is_superuser or request.user.is_staff):
            raise PermissionDenied
        created = None
        if request.method == 'POST':
            form = BulkClassSubjectForm(request.POST)
            if form.is_valid():
                grade = form.cleaned_data['grade']
                subject = normalize_subject(form.cleaned_data['subject'])
                created = 0
                for school in School.objects.all():
                    class_names = set()
                    class_names.update(ClassSubject.objects.filter(school=school).values_list('class_name', flat=True).distinct())
                    class_names.update(Student.objects.filter(school=school).values_list('class_name', flat=True).distinct())
                    for cn in class_names:
                        if str(class_numeric_part(cn)) == grade:
                            _, was_created = ClassSubject.objects.get_or_create(
                                school=school,
                                class_name=normalize_class_name(cn),
                                subject=subject,
                                defaults={'is_active': True, 'is_default': False},
                            )
                            if was_created:
                                created += 1
                self.message_user(
                    request,
                    f'{created} сабтҳои "{subject}" барои синфи {grade} илова шуд.',
                    level=messages.SUCCESS,
                )
                return HttpResponseRedirect(reverse('admin:portal_classsubject_changelist'))
        else:
            form = BulkClassSubjectForm()
        return render(request, 'admin/portal/classsubject/bulk_assign.html', {
            'form': form,
            'title': 'Оммавий иловаи фан',
            'created_count': created,
        })


@admin.register(QuarterLock)
class QuarterLockAdmin(admin.ModelAdmin):
    list_display = ['school', 'class_name', 'subject', 'quarter', 'locked', 'locked_at']
    list_filter = ['locked', 'quarter', 'school']
    search_fields = ['class_name', 'subject']


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'school', 'assigned_class', 'assigned_subject']
    list_filter = ['role', 'school']
