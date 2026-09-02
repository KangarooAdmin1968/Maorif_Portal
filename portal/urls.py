from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('schools/', views.school_list, name='school_list'),
    path('schools/add/', views.school_add, name='school_add'),
    path('school/<int:school_id>/classes/', views.class_list, name='class_list'),
    path('school/<int:school_id>/class/<str:class_name>/', views.class_detail, name='class_detail'),
    path('school/<int:school_id>/class/<str:class_name>/stickers/', views.sticker_entry, name='sticker_entry'),
    path('school/<int:school_id>/class/<str:class_name>/student/add/', views.add_student, name='add_student'),
    path('school/<int:school_id>/class/<str:class_name>/student/remove/', views.remove_student, name='remove_student'),
    path('school/<int:school_id>/class/<str:class_name>/student/edit/', views.edit_student, name='edit_student'),
    path('school/<int:school_id>/class/<str:class_name>/grade/<str:subject>/', views.grade_entry, name='grade_entry'),
    path('school/<int:school_id>/class/<str:class_name>/grade/<str:subject>/calc-quarter/', views.calc_quarter_from_daily, name='calc_quarter_from_daily'),
    path('save-grade-ajax/', views.save_grade_ajax, name='save_grade_ajax'),
    path('school/<int:school_id>/class/<str:class_name>/subjects/', views.add_remove_subject, name='add_remove_subject'),
    path('class/<str:class_name>/download-template/', views.download_template, name='download_template'),
    path('class/<str:class_name>/import/', views.import_excel, name='import_excel_class'),
    path('school/<int:school_id>/import_excel/', views.import_excel, name='import_excel'),
    path('school/<int:school_id>/download-template/', views.download_template, name='download_template_school'),
    path('school/<int:school_id>/teachers/', views.teacher_list, name='teacher_list'),
    path('school/<int:school_id>/teachers/add/', views.add_teacher, name='add_teacher'),
    path('school/<int:school_id>/teachers/remove/', views.remove_teacher, name='remove_teacher'),
    path('school/<int:school_id>/teachers/edit/', views.edit_teacher, name='edit_teacher'),
    path('school/<int:school_id>/teachers/template/download/', views.download_teacher_template, name='download_teacher_template'),
    path('school/<int:school_id>/teachers/import/', views.import_teachers, name='import_teachers'),
    path('schools/allocation/', views.lesson_allocation, name='lesson_allocation'),
    path('schools/allocation/save/', views.save_lesson_allocation, name='save_lesson_allocation'),
    path('student/<str:student_id>/', views.student_detail, name='student_detail'),
    path('google3c6e6431fb434e83.html', views.google_verification, name='google_verification'),
    path('password_change/', auth_views.PasswordChangeView.as_view(
        template_name='registration/password_change_form.html',
        success_url='/password_change/done/'
    ), name='password_change'),
    path('password_change/done/', auth_views.PasswordChangeDoneView.as_view(
        template_name='registration/password_change_done.html'
    ), name='password_change_done'),
    path('monitoring-dashboard/', views.monitoring_dashboard, name='monitoring_dashboard'),
    path('monitoring-dashboard/export/', views.export_monitoring_excel, name='export_monitoring_excel'),
    path('top-students/', views.top_students_ajax, name='top_students_ajax'),
    path('class-rankings/', views.class_rankings_ajax, name='class_rankings_ajax'),
]
