from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('schools/', views.school_list, name='school_list'),
    path('schools/add/', views.school_add, name='school_add'),
    path('school/<int:school_id>/classes/', views.class_list, name='class_list'),
    path('school/<int:school_id>/class/<str:class_name>/', views.class_detail, name='class_detail'),
    path('school/<int:school_id>/class/<str:class_name>/grade/<str:subject>/', views.grade_entry, name='grade_entry'),
    path('school/<int:school_id>/class/<str:class_name>/grade/<str:subject>/calc-quarter/', views.calc_quarter_from_daily, name='calc_quarter_from_daily'),
    path('save-grade-ajax/', views.save_grade_ajax, name='save_grade_ajax'),
    path('school/<int:school_id>/class/<str:class_name>/subjects/', views.add_remove_subject, name='add_remove_subject'),
    path('class/<str:class_name>/download-template/', views.download_template, name='download_template'),
    path('class/<str:class_name>/import/', views.import_excel, name='import_excel_class'),
    path('school/<int:school_id>/import_excel/', views.import_excel, name='import_excel'),
    path('school/<int:school_id>/teachers/', views.teacher_list, name='teacher_list'),
    path('teachers/template/download/', views.download_teacher_template, name='download_teacher_template'),
    path('teachers/import/', views.import_teachers, name='import_teachers'),
    path('schools/allocation/', views.lesson_allocation, name='lesson_allocation'),
    path('schools/allocation/save/', views.save_lesson_allocation, name='save_lesson_allocation'),
    path('student/<str:student_id>/', views.student_detail, name='student_detail'),
]
