from django.db import migrations
from django.db.models import Count, Min


def clean_duplicate_teachers(apps, schema_editor):
    Teacher = apps.get_model('portal', 'Teacher')
    TeacherProfile = apps.get_model('portal', 'TeacherProfile')
    db_alias = schema_editor.connection.alias

    # Remove duplicate Teacher rows (same school + name), keeping the lowest id.
    teacher_dupes = (
        Teacher.objects.using(db_alias)
        .values('school_id', 'name')
        .annotate(count=Count('id'), min_id=Min('id'))
        .filter(count__gt=1)
    )
    for d in teacher_dupes:
        Teacher.objects.using(db_alias).filter(
            school_id=d['school_id'],
            name=d['name'],
            id__gt=d['min_id'],
        ).delete()

    # Remove duplicate TeacherProfile rows (same school + full_name)
    # and their linked User accounts.
    profile_dupes = (
        TeacherProfile.objects.using(db_alias)
        .values('school_id', 'full_name')
        .annotate(count=Count('id'), min_id=Min('id'))
        .filter(count__gt=1)
    )
    for d in profile_dupes:
        extra = TeacherProfile.objects.using(db_alias).filter(
            school_id=d['school_id'],
            full_name=d['full_name'],
            id__gt=d['min_id'],
        ).select_related('user')
        for tp in extra:
            user = tp.user
            if user:
                user.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('portal', '0008_grade_sticker'),
    ]

    operations = [
        migrations.RunPython(clean_duplicate_teachers, migrations.RunPython.noop),
    ]
