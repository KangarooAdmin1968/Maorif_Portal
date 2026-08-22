import re
import os
import io
import pandas as pd
from django.db.models import Avg, Count, Q
from django.conf import settings
from .models import School, Student, Grade, ClassSubject, normalize_class_name, normalize_subject, is_litsey


# Default national curriculum subjects by grade
TJC_SUBJECTS = settings.TJC_SUBJECTS
NON_GRADED_CLASSES = settings.NON_GRADED_CLASSES


def get_school_number(school):
    """Return the physical school number/slug for a School, e.g. '20', 'g1', 'l1', 'mdtt4', '0'."""
    name = (school.name or '').strip()
    type_ = (school.type or '').lower()
    name_lower = name.lower()

    # Explicit name-to-number overrides for schools without a visible № in their name
    NAME_TO_NUMBER = {
        'кӯдакистони деҳаи лоҷин': 'mdtt4',
        'гимназияи ноҳияи зафаробод': 'g1',
        'афсона хусусӣ': '0',
        'табассум': '0',
    }
    if name_lower in NAME_TO_NUMBER:
        return NAME_TO_NUMBER[name_lower]

    # Kindergarten #4 in the village of Lojin has no visible number
    if 'кӯдакистон' in name_lower and 'ло' in name_lower and 'ин' in name_lower:
        return 'mdtt4'

    # Try to find an explicit №N number in the name
    m = re.search(r'№\s*(\d+)', name)
    if m:
        num = m.group(1)
        if 'лит' in type_ or 'литсей' in name_lower or 'лицей' in name_lower:
            return f'l{num}'
        if 'гимн' in name_lower:
            return f'g{num}'
        if 'томактаб' in type_ or 'кӯдакистон' in name_lower or 'mdtt' in name_lower or 'мдтт' in name_lower:
            return f'mdtt{num}'
        return num

    # Gymnasium fallback
    if 'гимн' in name_lower:
        return 'g1'

    # Litsey fallback
    if 'лит' in type_ or 'литсей' in name_lower or 'лицей' in name_lower:
        return 'l1'

    # Kindergarten / MDITT fallback
    if 'томактаб' in type_ or 'кӯдакистон' in name_lower or 'mdtt' in name_lower or 'мдтт' in name_lower:
        return 'mdtt0'

    # Unknown / private kindergartens
    return '0'


def get_school_password_base(school):
    """Return the password base for a School, e.g. 'M20', 'G1', 'L1', 'Mdtt4', '0'."""
    number = get_school_number(school)
    if number == '0':
        return '0'
    if number.startswith('mdtt'):
        return 'Mdtt' + number[4:]
    if number.startswith('g'):
        return 'G' + number[1:]
    if number.startswith('l'):
        return 'L' + number[1:]
    if number.isdigit():
        return 'M' + number
    return number.capitalize()


def class_numeric_part(class_name):
    m = re.search(r'(\d+)', str(class_name))
    return int(m.group(1)) if m else 0


def default_subjects_for_class(class_name):
    """Return default subjects for a normalized class label."""
    num = str(class_numeric_part(class_name))
    return TJC_SUBJECTS.get(num, TJC_SUBJECTS.get('5', []))


def is_non_graded(class_name):
    num = str(class_numeric_part(class_name))
    return num in NON_GRADED_CLASSES or num == '0'


def ensure_class_subjects(school, class_name):
    """Create default ClassSubject records for a school/class if missing."""
    class_name = normalize_class_name(class_name)
    subjects = default_subjects_for_class(class_name)
    created = 0
    for subj in subjects:
        subj = normalize_subject(subj)
        obj, c = ClassSubject.objects.get_or_create(
            school=school,
            class_name=class_name,
            subject=subj,
            defaults={'is_default': True, 'is_active': True}
        )
        if c:
            created += 1
    return created


def school_gpa(school):
    avg = Grade.objects.filter(student__school=school, score__isnull=False).aggregate(avg=Avg('score'))['avg']
    return round(avg, 2) if avg else 0.0


def class_gpa(school_name, class_name):
    avg = Grade.objects.filter(
        student__school__name=school_name,
        student__class_name=class_name,
        score__isnull=False
    ).aggregate(avg=Avg('score'))['avg']
    return round(avg, 2) if avg else 0.0


def subject_gpa(subject):
    avg = Grade.objects.filter(subject=subject, score__isnull=False).aggregate(avg=Avg('score'))['avg']
    return round(avg, 2) if avg else 0.0


def schools_leaderboard():
    data = []
    for school in School.objects.all():
        data.append({
            'school': school,
            'gpa': school_gpa(school),
            'students': school.students_count,
            'type': school.type,
        })
    data.sort(key=lambda x: x['gpa'], reverse=True)
    for idx, item in enumerate(data, 1):
        item['rank'] = idx
    return data


def classes_leaderboard(school_filter=None):
    qs = Student.objects.all()
    if school_filter:
        qs = qs.filter(school__name__iexact=school_filter)
    class_keys = qs.values_list('school__name', 'class_name').distinct()
    data = []
    for s_name, c_name in class_keys:
        gpa = class_gpa(s_name, c_name)
        data.append({
            'school_name': s_name,
            'class_name': c_name,
            'gpa': gpa,
        })
    data.sort(key=lambda x: x['gpa'], reverse=True)
    for idx, item in enumerate(data, 1):
        item['district_rank'] = idx
    # school rank (within each school)
    schools_seen = {}
    for item in data:
        s = item['school_name']
        schools_seen[s] = schools_seen.get(s, 0) + 1
        item['school_rank'] = schools_seen[s]
    return data


def subjects_leaderboard():
    subjects = Grade.objects.filter(score__isnull=False).values('subject').distinct()
    data = []
    for row in subjects:
        subj = row['subject']
        avg = Grade.objects.filter(subject=subj, score__isnull=False).aggregate(avg=Avg('score'))['avg']
        data.append({
            'subject': subj,
            'gpa': round(avg, 2) if avg else 0.0,
        })
    data.sort(key=lambda x: x['gpa'], reverse=True)
    for idx, item in enumerate(data, 1):
        item['rank'] = idx
    return data


def generate_excel_template(school, class_name):
    """Generate a clean Excel template with subjects as columns."""
    class_name = normalize_class_name(class_name)
    ensure_class_subjects(school, class_name)
    subjects = list(ClassSubject.objects.filter(
        school=school,
        class_name=class_name,
        is_active=True
    ).values_list('subject', flat=True))
    columns = ['№', 'Синф', 'Ном ва насаб'] + subjects
    df = pd.DataFrame(columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Холҳо')
    output.seek(0)
    return output


def parse_import_excel(school, class_name, file_obj):
    """Parse uploaded Excel and create/update Student/Grade records."""
    class_name = normalize_class_name(class_name)
    ensure_class_subjects(school, class_name)
    df = pd.read_excel(file_obj)
    df.columns = [str(c).strip() for c in df.columns]

    name_variants = ['Ном ва насаб', 'Ном  ва насаб', 'Номи хонанда', 'ФИО', 'Ф.И.О', 'full_name']
    name_col = None
    for v in name_variants:
        if v in df.columns:
            name_col = v
            break
    if name_col is None:
        if len(df.columns) > 2:
            name_col = df.columns[2]
        else:
            return 0

    class_col = 'Синф' if 'Синф' in df.columns else None
    fixed_cols = {name_col, class_col} if class_col else {name_col}

    imported = 0
    for _, row in df.iterrows():
        ism = str(row.get(name_col, '')).strip()
        if not ism or ism.lower() in ('nan', 'none', ''):
            continue
        if class_col and pd.notna(row.get(class_col)):
            c_name = str(row[class_col]).strip()
        else:
            c_name = class_name
        c_name = normalize_class_name(c_name)

        student, _ = Student.objects.update_or_create(
            id=f"{school.name}__{c_name}__{ism}",
            defaults={
                'full_name': ism,
                'class_name': c_name,
                'school': school,
            }
        )

        for col in df.columns:
            if col in fixed_cols:
                continue
            low = col.lower()
            if 'unnamed' in low or 'жами' in low or 'рейтинг' in low or '№' in col or col.strip() == '':
                continue
            subj = normalize_subject(col)
            val = row.get(col)
            if val is None or str(val).lower() in ('nan', 'none', ''):
                continue
            try:
                score = float(val)
                if 1 <= score <= 10:
                    Grade.objects.update_or_create(
                        student=student,
                        subject=subj,
                        period='Холҳои ҷорӣ (Онлайн)',
                        defaults={'score': score}
                    )
                    imported += 1
            except (ValueError, TypeError):
                continue
    return imported
