import re
import os
import io
import pandas as pd
from django.db.models import Avg, Count, Q
from django.conf import settings
from .models import School, Student, Grade, ClassSubject, SubjectDeactivationRequest, normalize_class_name, normalize_subject, is_litsey


# Default national curriculum subjects by grade
TJC_SUBJECTS = settings.TJC_SUBJECTS
NON_GRADED_CLASSES = settings.NON_GRADED_CLASSES


def is_academic_school(school):
    """Return True for schools that participate in academic GPA rankings."""
    type_lower = (school.type or '').lower()
    name_lower = (school.name or '').lower()
    if 'идор' in name_lower or 'идор' in type_lower:
        return False
    if 'томактаб' in name_lower or 'томактаб' in type_lower:
        return False
    if 'гимн' in type_lower or 'лит' in type_lower or 'лиц' in type_lower:
        return True
    if 'муассисаи таҳсилоти умумии' in type_lower or 'мактаб' in type_lower:
        return True
    if 'гимн' in name_lower or 'лит' in name_lower or 'лиц' in name_lower:
        return True
    return False


def academic_schools():
    """Return the list of eligible general-education schools.

    Single source of truth for "which institutions are schools" in
    school-facing areas; preschool/kindergarten and education-department
    records are excluded by type/name via is_academic_school().
    """
    return [s for s in School.objects.all() if is_academic_school(s)]


def academic_school_ids():
    """Return the id set of eligible general-education schools."""
    return {s.id for s in School.objects.all() if is_academic_school(s)}


def official_subjects():
    """Return a set of all normalized official subjects across all grades."""
    subjects = set()
    for subject_list in TJC_SUBJECTS.values():
        for subj in subject_list:
            subjects.add(normalize_subject(subj))
    return subjects


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
    """Return the password base for a School (same as the number slug, no M/G/L prefix)."""
    return get_school_number(school)


def class_numeric_part(class_name):
    m = re.search(r'(\d+)', str(class_name))
    return int(m.group(1)) if m else 0


def default_subjects_for_class(class_name):
    """Return default subjects for a normalized class label."""
    num = str(class_numeric_part(class_name))
    return TJC_SUBJECTS.get(num, TJC_SUBJECTS.get('5', []))


# School+class-scoped subject configuration layered on top of the national
# curriculum (TJC_SUBJECTS). Keys are get_school_number() slugs — NOT
# database ids — so the config survives id changes and stays readable.
#
#   'extra'    — subjects seeded as additional ClassSubject rows
#                (is_default=False). They belong to the class's official
#                set, so ensure_class_subjects() never deactivates them.
#   'preserve' — subjects that must not be deactivated by
#                ensure_class_subjects() if a row already exists, but are
#                NOT auto-created when absent.
SCHOOL_CLASS_SUBJECTS = {
    '5': {  # МТМУ №5 — Uzbek-language classes
        '3-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '4-Ғ': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '4-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '5-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '5-Е': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '6-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '6-Е': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '7-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '8-Е': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
        '9-Д': {'extra': ('Забон ва адабиёти ӯзбек',), 'preserve': ('Забони давлатӣ',)},
    },
}


def _school_subject_overrides(school, class_name):
    if school is None:
        return {}
    return SCHOOL_CLASS_SUBJECTS.get(get_school_number(school), {}).get(
        normalize_class_name(class_name), {}
    )


def extra_subjects_for(school, class_name):
    """School+class-scoped subjects seeded on top of the curriculum."""
    return [
        normalize_subject(s)
        for s in _school_subject_overrides(school, class_name).get('extra', ())
    ]


def preserved_subjects_for(school, class_name):
    """Existing school-scoped rows ensure_class_subjects() must not deactivate."""
    return [
        normalize_subject(s)
        for s in _school_subject_overrides(school, class_name).get('preserve', ())
    ]


def official_subjects_for(school, class_name):
    """Subject set visible for a school+class: curriculum ∪ extras ∪ preserved."""
    return (
        official_subjects()
        | set(extra_subjects_for(school, class_name))
        | set(preserved_subjects_for(school, class_name))
    )


def is_non_graded(class_name):
    num = str(class_numeric_part(class_name))
    return num in NON_GRADED_CLASSES or num == '0'


def ensure_class_subjects(school, class_name):
    """Create default ClassSubject records for a school/class and deactivate non-official ones."""
    class_name = normalize_class_name(class_name)
    subjects = default_subjects_for_class(class_name)
    extras = extra_subjects_for(school, class_name)
    official = (
        {normalize_subject(s) for s in subjects}
        | set(extras)
        | set(preserved_subjects_for(school, class_name))
    )
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
        elif obj.is_active:
            if not obj.is_default:
                obj.is_default = True
                obj.save()
        # Preserve any approved deactivation: do not reactivate and clear assigned teacher.
        if obj.is_active and SubjectDeactivationRequest.objects.filter(class_subject=obj, status='approved').exists():
            obj.is_active = False
            obj.teacher = None
            obj.allocated_teacher = None
            obj.save()

    # School-scoped extras (e.g. the Uzbek classes at School No. 5) are
    # additions to the curriculum, not defaults — is_default stays False but
    # they are part of `official` so the sweep below never removes them.
    for subj in extras:
        obj, c = ClassSubject.objects.get_or_create(
            school=school,
            class_name=class_name,
            subject=subj,
            defaults={'is_default': False, 'is_active': True}
        )
        if c:
            created += 1
        elif not obj.is_active:
            obj.is_active = True
            obj.save()
        if obj.is_active and SubjectDeactivationRequest.objects.filter(class_subject=obj, status='approved').exists():
            obj.is_active = False
            obj.teacher = None
            obj.allocated_teacher = None
            obj.save()

    # Deactivate any class subjects that are not part of the official curriculum
    ClassSubject.objects.filter(school=school, class_name=class_name).exclude(subject__in=official).update(is_active=False)
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
    for school in academic_schools():
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
                        assessment=None,
                        lesson=None,
                        defaults={'score': score}
                    )
                    imported += 1
            except (ValueError, TypeError):
                continue
    return imported
