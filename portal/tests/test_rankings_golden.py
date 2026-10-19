"""Golden-master tests for the four ranking engines.

Deterministic pool (subject МАТЕМАТИКА unless noted):

    s1 (School A, 7-А): daily 8, 10 + quarter1=9 + attestation=9  -> 9.00
    s2 (School A, 7-А): daily 6 + derived quarter1=6              -> 6.00
    s3 (School B, 7-Б): daily 7 + quarter1=8                      -> 7.50
    s4 (School A, 1-А): non-graded class, custom-subject score 10
    s5 (Idora,    5-А): non-academic school, custom-subject 10
    s6 (School A, 8-Б): student with no grades

Derived quarter rows are now persisted automatically by the Grade signals,
so every student with a source score also contributes its QuarterGrade to
the ranking pools (s2 gains quarter1=6, and the explicitly seeded rows
land on the same values the signals derived).

Frozen expectations:
    school_rankings : A = (8+10+9+9+6+6)/6 = 8.00 rank 1; B = 7.50 rank 2
    class_rankings  : 7-А 8.00 (d1,s1); 7-Б 7.50 (d2,s1); 8-Б 0.00 (d3,s2)
    top_students    : s1 9.00 (d1,s1); s3 7.50 (d2,s1); s2 6.00 (d3,s2)
    subject_rankings: МАТЕМАТИКА = 63/8 = 7.88 rank 1; other official
                      subjects 0.00 rank 2; non-official subject hidden
"""
from django.test import TestCase

from portal.tests.helpers import (
    CUSTOM_SUBJECT, D_PREV_Q1, D_Q1, D_Q1_B, MATH,
    GoldenBase, _AY, make_class_subject, make_grade, make_quarter_grade,
    make_school, make_student,
)
from portal.views import (
    calculate_class_rankings,
    calculate_school_rankings,
    calculate_subject_rankings,
    calculate_top_students,
)


class RankingBase(GoldenBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.idora = make_school('Идораи маорифи ноҳия', type='Идораи маориф')
        cls.s4 = make_student(cls.school_a, '1-А', 'Сабрина Юсупова')
        cls.s5 = make_student(cls.idora, '5-А', 'Камол Саидов')
        cls.s6 = make_student(cls.school_a, '8-Б', 'Шаҳром Одилов')

        make_grade(cls.s1, MATH, 8, D_Q1)
        make_grade(cls.s1, MATH, 10, D_Q1_B)
        make_quarter_grade(cls.s1, '7-А', MATH, 1, grade=9)
        make_quarter_grade(cls.s1, '7-А', MATH, 0, att=9)
        make_grade(cls.s2, MATH, 6, D_Q1)
        make_grade(cls.s3, MATH, 7, D_Q1)
        make_quarter_grade(cls.s3, '7-Б', MATH, 1, grade=8)
        # Excluded populations get a NON-official subject so they cannot
        # silently pollute the МАТЕМАТИКА pool.
        make_grade(cls.s4, CUSTOM_SUBJECT, 10, D_Q1)
        make_grade(cls.s5, CUSTOM_SUBJECT, 10, D_Q1)


class SchoolRankingGoldenTests(RankingBase):

    def test_school_gpa_pools_daily_quarter_and_attestation(self):
        data = {item['school'].id: item for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)
        self.assertAlmostEqual(data[self.school_b.id]['gpa'], 7.50)

    def test_school_ranking_order_and_dense_rank(self):
        data = calculate_school_rankings()
        self.assertEqual(data[0]['school'].id, self.school_a.id)
        self.assertEqual(data[0]['rank'], 1)
        self.assertEqual(data[1]['school'].id, self.school_b.id)
        self.assertEqual(data[1]['rank'], 2)

    def test_non_academic_school_excluded(self):
        ids = [item['school'].id for item in calculate_school_rankings()]
        self.assertNotIn(self.idora.id, ids)

    def test_non_graded_class_scores_excluded(self):
        # s4's score-10 in class 1-А must NOT lift School A's GPA —
        # if non-graded classes leaked in, A would show (48+10)/7 = 8.29.
        data = {item['school'].id: item for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)

    def test_non_graded_quarter_and_attestation_excluded(self):
        # QuarterGrade rows are excluded on their own class_name — a '1-А'
        # quarter or attestation grade must not lift School A either.
        # Leak would give (48+10+10)/8 = 8.50.
        make_quarter_grade(self.s4, '1-А', MATH, 1, grade=10)
        make_quarter_grade(self.s4, '1-А', MATH, 0, att=10)
        data = {item['school'].id: item for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)

    def test_school_with_no_grades_present_with_zero(self):
        empty = make_school('Мактаб №99')
        data = {item['school'].id: item for item in calculate_school_rankings()}
        self.assertEqual(data[empty.id]['gpa'], 0.0)


class ClassRankingGoldenTests(RankingBase):

    def _by_key(self, data):
        return {(d['school_id'], d['class_name']): d for d in data}

    def test_class_gpa_district_and_school_ranks(self):
        data = self._by_key(calculate_class_rankings())
        a7 = data[(self.school_a.id, '7-А')]
        self.assertAlmostEqual(a7['gpa'], 8.00)
        self.assertEqual(a7['district_rank'], 1)
        self.assertEqual(a7['school_rank'], 1)
        b7 = data[(self.school_b.id, '7-Б')]
        self.assertAlmostEqual(b7['gpa'], 7.50)
        self.assertEqual(b7['district_rank'], 2)
        self.assertEqual(b7['school_rank'], 1)
        a8 = data[(self.school_a.id, '8-Б')]
        self.assertEqual(a8['gpa'], 0.0)
        self.assertEqual(a8['district_rank'], 3)
        self.assertEqual(a8['school_rank'], 2)

    def test_classes_seeded_from_students_even_without_grades(self):
        data = self._by_key(calculate_class_rankings())
        self.assertIn((self.school_a.id, '8-Б'), data)  # s6 has no grades

    def test_class_with_subjects_but_no_students_absent(self):
        # Seeding is driven by Student rows, not ClassSubject rows.
        make_class_subject(self.school_a, '9-Д', MATH)
        data = self._by_key(calculate_class_rankings())
        self.assertNotIn((self.school_a.id, '9-Д'), data)

    def test_non_graded_class_excluded(self):
        data = self._by_key(calculate_class_rankings())
        self.assertNotIn((self.school_a.id, '1-А'), data)

    def test_non_academic_school_class_excluded(self):
        data = self._by_key(calculate_class_rankings())
        self.assertNotIn((self.idora.id, '5-А'), data)

    def test_non_graded_quarter_grade_row_excluded(self):
        # A prior-year QuarterGrade carrying non-graded class_name '1-А'
        # must not create a '1-А' entry even though it belongs to a
        # graded 7-А student.
        make_quarter_grade(self.s1, '1-А', MATH, 1, grade=10,
                           academic_year=_AY - 1)
        data = self._by_key(calculate_class_rankings())
        self.assertNotIn((self.school_a.id, '1-А'), data)
        self.assertAlmostEqual(data[(self.school_a.id, '7-А')]['gpa'], 8.00)

    def test_prior_class_quarter_row_creates_no_ghost_entry(self):
        # s1's QuarterGrade is stamped '9-Д' in the PRIOR academic year —
        # explicit year identity, not the class label: it creates no
        # '9-Д' entry and does not lift '7-А' either.
        make_quarter_grade(self.s1, '9-Д', MATH, 1, grade=6,
                           academic_year=_AY - 1)
        data = self._by_key(calculate_class_rankings())
        self.assertNotIn((self.school_a.id, '9-Д'), data)
        self.assertAlmostEqual(data[(self.school_a.id, '7-А')]['gpa'], 8.00)

    def test_same_year_move_row_counts_under_its_own_class(self):
        # A row stamped THIS year under another label is a mid-year move —
        # it legitimately appears under the class it was earned in.
        s8 = make_student(self.school_a, '9-Д', 'Ҳаракаткарда')
        make_quarter_grade(s8, '9-Д', MATH, 1, grade=10)
        # Move to 7-А — the 9-Д row keeps its year and its label.
        s8.class_name = '7-А'
        s8.save()
        data = self._by_key(calculate_class_rankings())
        self.assertIn((self.school_a.id, '9-Д'), data)
        self.assertAlmostEqual(data[(self.school_a.id, '9-Д')]['gpa'], 10.00)

    def test_school_filter_by_id(self):
        data = calculate_class_rankings(school_filter=str(self.school_a.id))
        self.assertTrue(all(d['school_id'] == self.school_a.id for d in data))

    def test_same_subject_different_classes_kept_separate(self):
        # TEST 7: the same subject in two classes must not merge class entries.
        data = self._by_key(calculate_class_rankings())
        self.assertAlmostEqual(data[(self.school_a.id, '7-А')]['gpa'], 8.00)
        self.assertAlmostEqual(data[(self.school_b.id, '7-Б')]['gpa'], 7.50)


class RankingAcademicYearTests(RankingBase):
    """The ranking pools are scoped to the current academic year:
    prior-year Grade rows (by date) and QuarterGrade rows stamped with a
    different academic_year must not feed current-year GPA."""

    def test_prior_year_daily_grade_not_counted(self):
        s7 = make_student(self.school_a, '7-А', 'Кӯҳна Хонанда')
        make_grade(s7, MATH, 10, D_PREV_Q1)
        data = {item['school'].id: item
                for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)
        top = {d['student_id']: d for d in calculate_top_students()}
        self.assertNotIn(s7.id, top)

    def test_prior_year_quarter_row_not_counted(self):
        # A quarter row stamped with the prior academic year is history —
        # it lifts neither the school GPA nor the student's own GPA,
        # even under a matching class label.
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=6,
                           academic_year=_AY - 1)
        data = {item['school'].id: item
                for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)
        top = {d['student_id']: d for d in calculate_top_students()}
        self.assertAlmostEqual(top[self.s1.id]['gpa'], 9.00)

    def test_prior_year_attestation_row_not_counted(self):
        make_quarter_grade(self.s1, '7-А', MATH, 0, att=6,
                           academic_year=_AY - 1)
        data = {item['school'].id: item
                for item in calculate_school_rankings()}
        self.assertAlmostEqual(data[self.school_a.id]['gpa'], 8.00)

    def test_subject_ranking_ignores_prior_year_rows(self):
        make_grade(self.s1, MATH, 10, D_PREV_Q1)
        make_quarter_grade(self.s1, '7-А', MATH, 1, grade=10,
                           academic_year=_AY - 1)
        data = {d['subject']: d for d in calculate_subject_rankings()}
        self.assertAlmostEqual(data[MATH]['gpa'], 7.88)  # still 63/8


class TopStudentsGoldenTests(RankingBase):

    def _by_student(self, data):
        return {d['student_id']: d for d in data}

    def test_student_gpa_pools_daily_quarter_and_attestation(self):
        data = self._by_student(calculate_top_students())
        self.assertAlmostEqual(data[self.s1.id]['gpa'], 9.00)
        self.assertAlmostEqual(data[self.s2.id]['gpa'], 6.00)
        self.assertAlmostEqual(data[self.s3.id]['gpa'], 7.50)

    def test_district_and_school_dense_ranks(self):
        data = self._by_student(calculate_top_students())
        self.assertEqual(data[self.s1.id]['district_rank'], 1)
        self.assertEqual(data[self.s1.id]['school_rank'], 1)
        self.assertEqual(data[self.s3.id]['district_rank'], 2)
        self.assertEqual(data[self.s3.id]['school_rank'], 1)
        self.assertEqual(data[self.s2.id]['district_rank'], 3)
        self.assertEqual(data[self.s2.id]['school_rank'], 2)

    def test_tied_students_share_dense_rank(self):
        s3b = make_student(self.school_b, '7-Б', 'Парвина Одинаева')
        make_grade(s3b, MATH, 7, D_Q1)
        make_quarter_grade(s3b, '7-Б', MATH, 1, grade=8)  # same 7.50 pool
        data = self._by_student(calculate_top_students())
        self.assertEqual(data[s3b.id]['district_rank'], 2)
        self.assertEqual(data[self.s3.id]['district_rank'], 2)
        self.assertEqual(data[s3b.id]['school_rank'], 1)

    def test_limit_cutoff_includes_gpa_ties(self):
        data = calculate_top_students(limit=1)
        self.assertEqual([d['student_id'] for d in data], [self.s1.id])
        data = calculate_top_students(limit=2)
        self.assertEqual(len(data), 2)

    def test_non_graded_and_non_academic_students_excluded(self):
        ids = [d['student_id'] for d in calculate_top_students()]
        self.assertNotIn(self.s4.id, ids)
        self.assertNotIn(self.s5.id, ids)


class SubjectRankingGoldenTests(RankingBase):

    def _by_subject(self, data):
        return {d['subject']: d for d in data}

    def test_subject_gpa_aggregation_normalized(self):
        data = self._by_subject(calculate_subject_rankings())
        self.assertAlmostEqual(data[MATH]['gpa'], 7.88)  # 63/8
        self.assertEqual(data[MATH]['rank'], 1)

    def test_all_official_subjects_present_with_zero(self):
        data = self._by_subject(calculate_subject_rankings())
        self.assertIn('АЛГЕБРА', data)
        self.assertEqual(data['АЛГЕБРА']['gpa'], 0.0)
        self.assertEqual(data['АЛГЕБРА']['rank'], 2)  # dense rank after MATH

    def test_non_official_subject_hidden_from_ranking(self):
        data = self._by_subject(calculate_subject_rankings())
        self.assertNotIn(CUSTOM_SUBJECT, data)

    def test_subject_pool_includes_quarter_and_attestation(self):
        # If quarter/attestation leaked out of the pool, MATH would be
        # (8+10+6+7)/4 = 7.75 instead of 7.88.
        data = self._by_subject(calculate_subject_rankings())
        self.assertAlmostEqual(data[MATH]['gpa'], 7.88)

    def test_subject_ranking_counts_non_graded_scores_current_behavior(self):
        # GOLDEN QUIRK: unlike school/class/student rankings, the subject
        # engine has NO is_non_graded or academic-school filter — a score in
        # a grade-1 class DOES enter the subject pool today.
        make_grade(self.s4, MATH, 10, D_Q1)  # '1-А' student
        data = self._by_subject(calculate_subject_rankings())
        self.assertAlmostEqual(data[MATH]['gpa'], 8.30)  # (63+10+10)/10

    def test_same_subject_across_schools_aggregated(self):
        # TEST 7 counterpart: subject ranking intentionally pools across
        # schools/classes — classes must NOT become separate subject rows.
        data = calculate_subject_rankings()
        math_rows = [d for d in data if d['subject'] == MATH]
        self.assertEqual(len(math_rows), 1)

    def test_homeroom_subject_excluded_from_ranking(self):
        # 'Соати тарбиявӣ' is in TJC_SUBJECTS for every grade and is seeded
        # as a ClassSubject for 7-А/7-Б, but as an un-graded curriculum
        # subject (UNGRADED_SUBJECTS) it must never appear ranked.
        data = self._by_subject(calculate_subject_rankings())
        self.assertNotIn('СОАТИ ТАРБИЯВӢ', data)
        self.assertIn(MATH, data)

    def test_homeroom_scores_do_not_leak_into_ranking(self):
        # Real scores stored under a mixed-case spelling still normalize to
        # the excluded canonical subject: no ranking row, and other
        # subjects' GPA/rank stay untouched.
        make_grade(self.s1, 'Соати тарбиявӣ', 10, D_Q1)
        make_quarter_grade(self.s1, '7-А', 'Соати тарбиявӣ', 1, grade=10)
        make_quarter_grade(self.s1, '7-А', 'Соати тарбиявӣ', 0, att=10)
        data = self._by_subject(calculate_subject_rankings())
        self.assertNotIn('СОАТИ ТАРБИЯВӢ', data)
        self.assertAlmostEqual(data[MATH]['gpa'], 7.88)
        self.assertEqual(data[MATH]['rank'], 1)
        self.assertEqual(data['АЛГЕБРА']['rank'], 2)
