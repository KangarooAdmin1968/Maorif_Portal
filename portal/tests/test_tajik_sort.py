"""Tajik-alphabet roster ordering — portal.tajik_sort and its call sites.

SQLite BINARY collation compares codepoints, so the Tajik-specific
letters (Ғ Ӣ Қ Ӯ Ҳ Ҷ) used to sort after every basic Cyrillic letter —
«Ҷӯраева» landed past «Я» at the end of rosters and «Аҳмадова» sorted
after «Ашурова». These tests pin the official 35-letter order on the
helper functions, the rollback flag and the class roster surface.
"""
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from portal import tajik_sort
from portal.tajik_sort import (
    TAJIK_ALPHABET, name_key, sort_students, tajik_key,
)

from .helpers import GoldenBase, make_student


class TajikKeyTests(SimpleTestCase):
    """Pure-function ordering — no DB needed."""

    def test_alphabet_is_the_official_35_letters(self):
        self.assertEqual(
            TAJIK_ALPHABET, 'абвгғдеёжзиӣйкқлмнопрстуӯфхҳчҷшъэюя')

    def test_special_letters_keep_tajik_positions(self):
        # Each special letter sits immediately after its basic
        # counterpart — nowhere near the end like its codepoint implies.
        for lo, hi in [('Г', 'Ғ'), ('Ғ', 'Д'), ('И', 'Ӣ'), ('Ӣ', 'Й'),
                       ('К', 'Қ'), ('Қ', 'Л'), ('У', 'Ӯ'), ('Ӯ', 'Ф'),
                       ('Х', 'Ҳ'), ('Ҳ', 'Ч'), ('Ч', 'Ҷ'), ('Ҷ', 'Ш')]:
            self.assertLess(tajik_key(lo), tajik_key(hi), f'{lo} < {hi}')

    def test_mid_word_letters_matter(self):
        # Ҳ (position 27) precedes Ш (30) in Tajik although its
        # codepoint is higher — the exact bug users saw.
        names = ['Ашурова Наргиза', 'Аҳмадова Мухайё']
        self.assertEqual(sorted(names, key=tajik_key),
                         ['Аҳмадова Мухайё', 'Ашурова Наргиза'])

    def test_full_roster_order(self):
        names = ['Ядгаров Фирӯз', 'Ҷуманазарова Рухшона',
                 'Аҳмадова Мухайё', 'Ашурова Наргиза',
                 'Ӯзбекова Мадина']
        self.assertEqual(
            sorted(names, key=tajik_key),
            ['Аҳмадова Мухайё', 'Ашурова Наргиза', 'Ӯзбекова Мадина',
             'Ҷуманазарова Рухшона', 'Ядгаров Фирӯз'])

    def test_case_and_extra_whitespace_are_normalized(self):
        self.assertEqual(tajik_key('Алиева  Нафиса'),
                         tajik_key('алиева нафиса'))

    def test_foreign_characters_sort_after_the_alphabet(self):
        self.assertLess(tajik_key('Я'), tajik_key('Anna'))
        self.assertLess(tajik_key('Я'), tajik_key('1'))

    def test_none_and_empty_do_not_crash(self):
        self.assertEqual(tajik_key(None), ())
        self.assertEqual(tajik_key(''), ())


class RollbackFlagTests(SimpleTestCase):
    """TAJIK_SORT_ENABLED=False restores the old codepoint ordering."""

    def test_name_key_falls_back_to_plain_string(self):
        with patch.object(tajik_sort, 'TAJIK_SORT_ENABLED', False):
            self.assertEqual(name_key('Ҷаббор'), 'Ҷаббор')

    def test_sort_students_falls_back_to_codepoint_order(self):
        class Fake:
            def __init__(self, pk, name):
                self.pk, self.full_name = pk, name
        items = [Fake(1, 'Ҷӯраева Шукрона'), Fake(2, 'Ашурова Наргиза')]
        with patch.object(tajik_sort, 'TAJIK_SORT_ENABLED', False):
            got = [s.full_name for s in sort_students(items)]
        # Codepoint order: Ҷ (U+04B6) sorts after А…Я.
        self.assertEqual(got, ['Ашурова Наргиза', 'Ҷӯраева Шукрона'])


class RosterOrderTests(GoldenBase):
    """The class page and visible_students_for render Tajik order."""

    def test_class_detail_orders_by_tajik_alphabet(self):
        # GoldenBase seeds 'Фирӯз Раҳимов' + 'Мадина Каримова' in 7-А.
        make_student(self.school_a, '7-А', 'Ҷӯраева Шукрона')
        make_student(self.school_a, '7-А', 'Аҳмадова Мухайё')
        make_student(self.school_a, '7-А', 'Ашурова Наргиза')
        self.client.force_login(self.zavuch_a)
        resp = self.client.get(
            reverse('class_detail', args=[self.school_a.id, '7-А']))
        names = [s.full_name for s in resp.context['students']]
        # Codepoint order would start with 'Ашурова Наргиза'; Tajik
        # order puts 'Аҳмадова' first and 'Ҷӯраева' after 'Фирӯз'.
        self.assertEqual(
            names,
            ['Аҳмадова Мухайё', 'Ашурова Наргиза', 'Мадина Каримова',
             'Фирӯз Раҳимов', 'Ҷӯраева Шукрона'])

    def test_visible_students_for_returns_tajik_sorted_list(self):
        from portal.views import visible_students_for
        make_student(self.school_a, '7-А', 'Ҷӯраева Шукрона')
        students = visible_students_for(self.zavuch_a, self.cs_a)
        names = [s.full_name for s in students]
        self.assertEqual(
            names, ['Мадина Каримова', 'Фирӯз Раҳимов',
                    'Ҷӯраева Шукрона'])
