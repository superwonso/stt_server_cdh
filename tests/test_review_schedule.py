"""User-specified 2026-09-29 fixtures; no accounts, DB, providers or network."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from server import review_schedule as s

ROOT = Path(__file__).resolve().parents[1]
HOLIDAYS = json.loads((ROOT / 'data/review-holidays/2026.json').read_text(encoding='utf-8'))
TODAY, SEM = '2026-09-29', '2026-09-01'
SETTINGS = {'offsets': [1, 3, 7, 14, 30], 'sem_start': SEM}
CURVE = {'sameDay': True, 'nextDay': True, 'eve': False, 'curve': True}
RHYTHM = {'sameDay': True, 'nextDay': False, 'eve': True, 'curve': False}
EVE = {'sameDay': False, 'nextDay': False, 'eve': True, 'curve': False}


def timetable(mode=CURVE):
    classes = []
    for subject, days, start, end, room in (
        ('행정학의이해(eng)', [1, 3], '13:30', '14:45', 'S1415'),
        ('사회조사방법론I', [1, 3], '15:00', '16:15', 'S1546'),
        ('현대사회와심리학', [1, 3], '16:30', '17:45', 'S1217'),
        ('창업과공동체', [2], '13:30', '16:15', 'S4115'),
        ('글로벌문화', [2, 4], '16:30', '17:45', 'S4509'),
        ('공동체활성화론', [4], '12:00', '14:45', 'S1546'),
    ):
        classes.extend({'subject': subject, 'day': day, 'start': start, 'end': end, 'room': room} for day in days)
    return {'classes': classes, 'from': TODAY, 'until': None, 'through': '2026-09-28', 'mode': deepcopy(mode)}


def item(**changes):
    return {'id': 'synthetic-item', 'title': '합성 복습', 'subject': '사회조사방법론I', 'memo': '',
            'learned': '2026-09-20', 'base': '2026-09-20', 'offsets': [1, 3, 7], 'reviews': [],
            'history': [{'date': '2026-09-20', 'type': 'learn'}], 'resets': 0, 'source': 'manual',
            'source_key': None, 'catchup': False, 'skipped': None, 'moved': None, **changes}


def exams():
    return [{'id': 'mid-admin', 'subject': '행정학의이해(eng)', 'kind': 'mid', 'date': '2026-10-21', 'rounds': 3, 'lead': 7, 'done': {}},
            {'id': 'final-admin', 'subject': '행정학의이해(eng)', 'kind': 'final', 'date': '2026-12-16', 'rounds': 3, 'lead': 7, 'done': {}},
            {'id': 'mid-global', 'subject': '글로벌문화', 'kind': 'mid', 'date': '2026-10-06', 'rounds': 3, 'lead': 7, 'done': {}}]


class ReviewScheduleTests(unittest.TestCase):
    def test_kst_today_calendar_dates_and_strict_offsets(self):
        self.assertEqual(s.kst_today(datetime(2026, 9, 28, 14, 59, 59, tzinfo=timezone.utc)), '2026-09-28')
        self.assertEqual(s.kst_today(datetime(2026, 9, 28, 15, tzinfo=timezone.utc)), TODAY)
        self.assertEqual(s.add_days('2024-02-28', 1), '2024-02-29')
        self.assertEqual(s.diff_days('2026-03-01', '2026-03-09'), 8)
        self.assertEqual(s.day_of_week(TODAY), 2)
        for value in ('2026-02-29', '2026-9-29', '2026-13-01', '0000-01-01', None):
            self.assertFalse(s.valid_date(value))
        for values in ([], [True], [0, 0], [3, 1], [-1], [366], list(range(13)), [1.0]):
            self.assertFalse(s.valid_offsets(values))
        self.assertTrue(s.valid_offsets([0, 1, 365]))
        self.assertEqual(s.default_sem_start(TODAY), SEM)
        self.assertEqual(s.edited_through('2026-09-28', SEM), '2026-09-28')

    def test_spec_1_auto_generation_once_deletion_does_not_regenerate_and_tombstones(self):
        tt = timetable(); first = s.timetable_preview(tt, TODAY, SETTINGS, HOLIDAYS)
        self.assertEqual([row['title'] for row in first['items']], ['9/29(화) 창업과공동체 수업', '9/29(화) 글로벌문화 수업'])
        self.assertTrue(all(row['offsets'] == [0, 1, 3, 7, 14, 30] for row in first['items']))
        self.assertEqual(first['through'], TODAY)
        tt['through'] = first['through']
        self.assertEqual(s.timetable_preview(tt, TODAY, SETTINGS, HOLIDAYS)['items'], [])
        tt['through'] = None
        self.assertEqual(s.timetable_preview(tt, TODAY, SETTINGS, HOLIDAYS, existing_keys=[row['source_key'] for row in first['items']])['items'], [])
        self.assertEqual(s.timetable_preview(timetable(), TODAY, SETTINGS, HOLIDAYS, [{'subject': '글로벌문화', 'date': TODAY}])['items'][0]['subject'], '창업과공동체')
        tt = timetable(); tt.update({'from': '2026-09-24', 'through': '2026-09-23'})
        self.assertEqual(s.timetable_preview(tt, '2026-09-24', SETTINGS, HOLIDAYS), {'items': [], 'through': '2026-09-24'})

    def test_spec_2_rhythm_3_eve_and_tomorrow_badge_inputs(self):
        tt = timetable(RHYTHM)
        for subject, day, expected in (('글로벌문화', TODAY, [0, 1]), ('창업과공동체', TODAY, [0, 6]), ('행정학의이해(eng)', '2026-09-30', [0, 6])):
            self.assertEqual(s.offsets_for(subject, day, tt, SETTINGS, HOLIDAYS), expected)
        tt['mode'] = EVE
        self.assertEqual(s.offsets_for('글로벌문화', TODAY, tt, SETTINGS, HOLIDAYS), [1])
        expected = {'행정학의이해(eng)': TODAY, '사회조사방법론I': TODAY, '현대사회와심리학': TODAY,
                    '글로벌문화': '2026-09-30', '공동체활성화론': '2026-09-30', '창업과공동체': '2026-10-05'}
        self.assertEqual({name: s.eve_target(name, TODAY, tt, HOLIDAYS) for name in expected}, expected)
        self.assertEqual(s.next_class_date('사회조사방법론I', TODAY, tt, HOLIDAYS), s.add_days(TODAY, 1))

    def test_spec_4_catchup_38_holidays_selection_existing_and_base_spread(self):
        tt = timetable()
        generated = s.timetable_preview(tt, TODAY, SETTINGS, HOLIDAYS)['items']
        rows = s.catchup_preview(tt, SEM, TODAY, SETTINGS, HOLIDAYS, [row['source_key'] for row in generated])
        self.assertEqual(len(rows), 42)
        self.assertEqual(sum(row['existing'] for row in rows), 2)
        self.assertTrue(all(row['existing'] and not row['selected'] for row in rows if row['date'] == TODAY))
        self.assertTrue(all(not row['selected'] and row['holiday'] == '추석' for row in rows if row['date'] == '2026-09-24'))
        plan = s.catchup_plan(rows, tt, TODAY, SETTINGS, HOLIDAYS, 5)
        self.assertEqual(len(plan), 38)
        self.assertEqual(Counter(row['base'] for row in plan), {s.add_days(TODAY, n): 5 for n in range(7)} | {'2026-10-06': 3})
        self.assertEqual(plan[0]['title'], '9/1(화) 창업과공동체 수업')
        self.assertEqual(plan[0]['offsets'], [0, 1, 3, 7, 14, 30])
        self.assertTrue(all(row['catchup'] for row in plan))
        prior_through = tt['through']; tt['from'] = SEM
        self.assertEqual(tt['from'], '2026-09-01'); self.assertEqual(tt['through'], prior_through)
        eve = s.catchup_plan(rows, timetable(EVE), TODAY, SETTINGS, HOLIDAYS)
        self.assertTrue(all(row['offsets'] == [0] and row['base'] == s.eve_target(row['subject'], TODAY, timetable(EVE), HOLIDAYS) for row in eve))
        self.assertTrue(all(row['base'] == TODAY for row in s.catchup_plan(rows, timetable(), TODAY, SETTINGS, HOLIDAYS, 0)))
        rows[0]['existing'] = True
        for row in rows:
            if row['holiday']: row['selected'] = True
        self.assertEqual(len(s.catchup_plan(rows, timetable(), TODAY, SETTINGS, HOLIDAYS)), 39)

    def test_catchup_reopens_missing_holiday_after_from_moves_without_reviving_prior_sources(self):
        tt = timetable(); tt['classes'] = [tt['classes'][-1]]
        rows = s.catchup_preview(tt, SEM, TODAY, SETTINGS, HOLIDAYS)
        first = s.catchup_plan(rows, tt, TODAY, SETTINGS, HOLIDAYS, 0)
        self.assertEqual([row['learned'] for row in first], ['2026-09-03', '2026-09-10', '2026-09-17'])
        keys = [row['source_key'] for row in first]
        tt['from'] = SEM; tt['through'] = TODAY
        again = s.catchup_preview(tt, SEM, TODAY, SETTINGS, HOLIDAYS, keys)
        self.assertEqual(len(again), 4)
        self.assertTrue(all(row['existing'] and not row['selected'] for row in again[:3]))
        holiday = again[-1]
        self.assertEqual((holiday['date'], holiday['holiday'], holiday['existing'], holiday['selected']), ('2026-09-24', '추석', False, False))
        holiday['selected'] = True
        self.assertEqual([row['learned'] for row in s.catchup_plan(again, tt, TODAY, SETTINGS, HOLIDAYS)], ['2026-09-24'])
        # The source ledger also contains deleted occurrences: selection cannot recreate one.
        blocked = s.catchup_preview(tt, SEM, TODAY, SETTINGS, HOLIDAYS, keys + [holiday['source_key']])
        for row in blocked: row['selected'] = True
        self.assertEqual(s.catchup_plan(blocked, tt, TODAY, SETTINGS, HOLIDAYS), [])

    def test_catchup_includes_today_but_respects_until_exams_and_future_boundary(self):
        tt = timetable(); tt['classes'] = [tt['classes'][6]]; tt['from'] = SEM
        excluded = [{'subject': '창업과공동체', 'date': '2026-09-22'}]
        rows = s.catchup_preview(tt, '2026-09-22', TODAY, SETTINGS, HOLIDAYS, exams=excluded)
        self.assertEqual([row['date'] for row in rows], [TODAY])
        tt['until'] = TODAY
        self.assertEqual([row['date'] for row in s.catchup_preview(tt, '2026-09-22', '2026-10-06', SETTINGS, HOLIDAYS, exams=excluded)], [TODAY])
        tt['until'] = '2026-09-28'
        self.assertEqual(s.catchup_preview(tt, '2026-09-22', TODAY, SETTINGS, HOLIDAYS, exams=excluded), [])
        tt['until'] = None
        self.assertEqual(s.catchup_preview(tt, '2026-09-30', TODAY, SETTINGS, HOLIDAYS), [])

    def test_spec_5_exam_ranges_exact_session_counts_and_week_boundaries(self):
        expected = [(13, '2026-09-02', '2026-10-19', 1, 8), (15, '2026-10-26', '2026-12-14', 9, 16), (9, '2026-09-01', '2026-10-01', 1, 5)]
        for exam, (count, first, last, first_week, last_week) in zip(exams(), expected):
            rows = s.exam_sessions(exam, SEM, timetable(), HOLIDAYS, exams(), today=TODAY)
            self.assertEqual((len(rows), rows[0]['date'], rows[-1]['date'], rows[0]['week'], rows[-1]['week']), (count, first, last, first_week, last_week))
            self.assertFalse(any(row['date'] in {'2026-09-24', '2026-10-05'} for row in rows))
        self.assertEqual(s.week_no('2026-09-06', SEM), 1)
        self.assertEqual(s.week_no('2026-09-07', SEM), 2)
        with self.assertRaises(ValueError): s.validate_exam_order({**exams()[1], 'date': '2026-10-21'}, exams())

    def test_spec_6_cram_stats_27_cells_24_checkable_remaining25_target4(self):
        exam = exams()[2]; rows = s.exam_sessions(exam, SEM, timetable(), HOLIDAYS, exams(), today=TODAY)
        stats = s.exam_stats(exam, rows, TODAY)
        self.assertEqual((stats['phase'], stats['days_left'], stats['total'], stats['checkable']), ('cram', 7, 27, 24))
        exam['done'] = {rows[0]['key']: [1, 0, 0], rows[1]['key']: [True, False, False]}
        stats = s.exam_stats(exam, rows, TODAY)
        self.assertEqual((stats['remaining'], stats['target'], stats['per'], stats['next_key']), (25, 4, [2, 0, 0], rows[2]['key']))
        for day, phase in (('2026-09-28', 'before'), ('2026-10-06', 'today'), ('2026-10-07', 'over')):
            self.assertEqual(s.exam_stats(exam, rows, day)['phase'], phase)

    def test_exam_manual_skipped_future_and_duplicate_source_key(self):
        exam = exams()[2]; rows = s.exam_sessions(exam, SEM, timetable(), HOLIDAYS, exams(), today=TODAY)
        manual = item(id='manual', subject='글로벌문화', learned='2026-10-02')
        existing = item(source='timetable', source_key=rows[0]['key'], subject='글로벌문화', learned=rows[0]['date'], skipped=TODAY)
        result = s.exam_sessions(exam, SEM, timetable(), HOLIDAYS, exams(), [existing, manual], TODAY)
        self.assertEqual(len(result), 10); self.assertTrue(result[0]['skipped']); self.assertTrue(result[-1]['future'])
        self.assertEqual(s.exam_stats(exam, result, TODAY)['total'], 27)
        tt = timetable(); tt['until'] = TODAY
        self.assertEqual(len(s.exam_sessions(exam, SEM, tt, HOLIDAYS, exams(), today=TODAY)), 8)

    def test_spec_7_skip_unskip_preserves_original_schedule_and_reset_is_immutable(self):
        original = item(); saved = deepcopy(original)
        skipped = s.skip_item(original, TODAY)
        self.assertIsNone(s.next_due(skipped)); self.assertEqual(s.projected(skipped, TODAY), [])
        self.assertEqual(s.due_items([skipped], TODAY), [])
        restored = s.unskip_item(skipped, TODAY)
        self.assertEqual(s.next_due(restored), s.next_due(original))
        reset = s.reset_item(item(catchup=True, moved={'stage': 0, 'date': TODAY}), TODAY)
        self.assertEqual((reset['base'], reset['reviews'], reset['resets'], reset['moved'], reset['catchup']), (TODAY, [], 1, None, False))
        self.assertEqual(original, saved)

    def test_spec_8_late_reviews_shift_remaining_gaps_and_projected_anchor(self):
        row = item(reviews=['2026-09-21'])
        self.assertEqual(s.next_due(row), '2026-09-23')
        self.assertEqual(s.projected(row, TODAY), [{'stage': 1, 'date': '2026-09-23', 'overdue': True}, {'stage': 2, 'date': '2026-10-03', 'overdue': False}])
        done = s.complete_review(row, '2026-09-25')
        self.assertEqual(s.next_due(done), TODAY)

    def test_spec_9_move_backdate_edit_and_clear_exact_cases(self):
        row = s.timetable_preview(timetable(), TODAY, SETTINGS, HOLIDAYS)['items'][1]
        moved = s.place_review(row, '2026-09-30', TODAY)
        self.assertEqual(moved['moved'], {'stage': 0, 'date': '2026-09-30'})
        self.assertEqual(s.due_items([moved], TODAY), [])
        self.assertEqual([r['date'] for r in s.projected(moved, TODAY)[:2]], ['2026-09-30', '2026-10-01'])
        self.assertEqual(len(s.due_items([s.place_review(moved, TODAY, TODAY)], TODAY)), 1)
        self.assertEqual(s.place_review(moved, '2026-09-30', TODAY), moved)
        startup = s.timetable_preview(timetable(), TODAY, SETTINGS, HOLIDAYS)['items'][0]
        with self.assertRaises(ValueError): s.place_review(startup, '2026-09-28', TODAY)
        catchup = item(learned=SEM, base='2026-10-02', offsets=[0, 1, 3, 7, 14, 30], catchup=True)
        done = s.place_review(catchup, '2026-09-02', TODAY)
        self.assertEqual(done['reviews'], ['2026-09-02']); self.assertEqual(s.next_due(done), '2026-09-03')
        edited = s.edit_review_date(done, 0, SEM, TODAY)
        self.assertEqual(edited['history'][-1], {'date': SEM, 'type': 'review', 'stage': 0})
        self.assertEqual(s.undo_last_review(edited)['reviews'], [])
        with self.assertRaises(ValueError): s.edit_review_date(done, 0, '2026-08-31', TODAY)
        with self.assertRaises(ValueError): s.edit_review_date(done, 0, '2026-09-30', TODAY)

    def test_spec_10_subject_groups_order_and_batch_copy_allows_full_undo(self):
        study = [item(id=f's{i}', learned=day, subject='사회조사방법론I') for i, day in enumerate(['2026-09-28', '2026-09-23', '2026-09-21', '2026-09-16', '2026-09-14', '2026-09-09', '2026-09-07'])]
        study += [item(id=f'a{i}', subject='행정학의이해(eng)', learned=day) for i, day in enumerate(['2026-09-09', '2026-09-07'])]
        before = deepcopy(study); grouped = s.group_due_items(study, TODAY)
        self.assertEqual([g['subject'] for g in grouped], ['사회조사방법론I', '행정학의이해(eng)'])
        self.assertEqual([len(g['items']) for g in grouped], [7, 2])
        self.assertEqual([r['learned'] for r in grouped[0]['items']][:3], ['2026-09-07', '2026-09-09', '2026-09-14'])
        changed = [s.complete_review(row, TODAY) for row in grouped[0]['items']]
        self.assertTrue(all(row['reviews'] == [TODAY] for row in changed)); self.assertEqual(study, before)

    def test_spec_11_routine_numbers_holidays_progress_streak_and_end(self):
        routine = {'name': '단어', 'days': [], 'start': '2026-09-28', 'end': None, 'count': 6, 'numbered': True, 'num_start': 1, 'done': []}
        for day, number in [('2026-09-28', 1), (TODAY, 2), ('2026-10-03', 6), ('2026-10-04', 0)]:
            self.assertEqual(s.routine_n(routine, day), number)
            if number: self.assertEqual(s.routine_title(routine, number), f'단어 ({number})')
        routine = s.toggle_routine(s.toggle_routine(routine, '2026-09-28', TODAY, True), TODAY, TODAY, True)
        self.assertEqual((s.routine_stats(routine, TODAY)['total'], s.routine_stats(routine, TODAY)['streak']), (2, 2))
        thu = {**routine, 'days': [2, 4], 'start': TODAY, 'count': 0, 'done': []}
        self.assertEqual([s.routine_n(thu, day) for day in [TODAY, '2026-10-01', '2026-10-05']], [1, 2, 0])
        with self.assertRaises(ValueError): s.toggle_routine(thu, '2026-10-01', TODAY, True)
        self.assertTrue(s.routine_stats(routine, '2026-10-04')['ended'])
        future = {**routine, 'start': '2026-12-01', 'done': []}
        self.assertFalse(s.routine_stats(future, TODAY)['ended'])

    def test_retention_mode_reapply_holiday_data_limits_and_safe_parser(self):
        row = item(catchup=True, learned=SEM, base='2026-10-02')
        self.assertAlmostEqual(s.retention(row, TODAY), math.exp(-28))
        self.assertAlmostEqual(s.retention(item(reviews=['2026-09-28']), TODAY), math.exp(-1 / 2.5))
        row.update(source='timetable', subject='창업과공동체')
        changed = s.apply_mode(row, timetable(EVE), SETTINGS, HOLIDAYS, TODAY)
        self.assertEqual((changed['base'], changed['offsets']), ('2026-10-05', [0]))
        completed = item(source='timetable', reviews=['2026-09-21', '2026-09-23', TODAY])
        self.assertEqual(s.apply_mode(completed, timetable(EVE), SETTINGS, HOLIDAYS, TODAY), completed)
        self.assertEqual(HOLIDAYS['source'], 'user-provided'); self.assertNotIn('2026-05-01', s.holiday_map(HOLIDAYS))
        self.assertEqual(s.offsets_for('없는 과목', TODAY, timetable(EVE), SETTINGS, HOLIDAYS), [1])
        self.assertEqual(s.catchup_offsets(timetable({'sameDay': False, 'nextDay': False, 'eve': False, 'curve': True}), {'offsets': [0, 1]}), [0])
        clean = s.clean_classes([{'subject': '글로벌문화', 'day': '화', 'start': '13:00', 'end': '14:15'},
            {'subject': '월 수업', 'day': '월요일', 'start': '15시', 'end': '14:00'}, {'subject': 'Thu', 'day': 'Thu', 'start': '09:00'},
            {'subject': '', 'day': 1}, {'subject': 'bool', 'day': True}, {'subject': 'safe\x00', 'day': 0}])
        self.assertEqual(clean[0], {'subject': '글로벌문화', 'day': 2, 'start': '13:00', 'end': '14:15', 'room': ''})
        self.assertEqual((clean[1]['day'], clean[1]['start'], clean[1]['end'], clean[2]['day']), (1, '15:00', '', 4))
        self.assertEqual(len(clean), 4); self.assertEqual(clean[-1]['subject'], 'safe')
        self.assertEqual(len(s.clean_classes([clean[0]] * 70)), 60)
        for invalid in ('25:00', '12:61', '13:00 junk', '<script>', '١٣:٠٠'):
            self.assertEqual(s.norm_time(invalid), '')

    def test_eve_reapply_clears_only_unreviewed_catchup_move_even_when_base_unchanged(self):
        moved = {'stage': 0, 'date': '2026-10-10'}
        for base in ('2026-10-02', '2026-09-30'):
            row = item(source='timetable', subject='글로벌문화', catchup=True, learned=SEM, base=base, moved=moved)
            before = deepcopy(row)
            changed = s.apply_mode(row, timetable(EVE), SETTINGS, HOLIDAYS, TODAY)
            self.assertEqual(changed['base'], '2026-09-30')
            self.assertIsNone(changed['moved'])
            self.assertEqual(s.next_due(changed), '2026-09-30')
            self.assertEqual(row, before)
        for changes in ({'source': 'manual', 'catchup': True}, {'source': 'timetable', 'catchup': False},
                        {'source': 'timetable', 'catchup': True, 'reviews': ['2026-09-20'], 'moved': {'stage': 1, 'date': '2026-10-10'}}):
            row = item(moved=moved, **{key: value for key, value in changes.items() if key != 'moved'})
            row.update(changes)
            self.assertEqual(s.apply_mode(row, timetable(RHYTHM), SETTINGS, HOLIDAYS, TODAY)['moved'], row['moved'])

    def test_python_and_javascript_results_match_on_spec_and_edge_cases(self):
        node = os.environ.get('STT_TEST_NODE_BINARY') or shutil.which('node')
        if not node: self.skipTest('Node is needed for cross-language parity')
        tt = timetable(); ex = exams()[2]
        sessions = s.exam_sessions(ex, SEM, tt, HOLIDAYS, exams(), today=TODAY)
        cases = [('nextDue', 'next_due', [item()]), ('projected', 'projected', [item(reviews=['2026-09-21']), TODAY]),
                 ('retention', 'retention', [item(reviews=['2026-09-28']), TODAY]),
                 ('timetablePreview', 'timetable_preview', [tt, TODAY, SETTINGS, HOLIDAYS]),
                 ('catchupPreview', 'catchup_preview', [tt, SEM, TODAY, SETTINGS, HOLIDAYS]),
                 ('catchupPlan', 'catchup_plan', [s.catchup_preview(tt, SEM, TODAY, SETTINGS, HOLIDAYS), tt, TODAY, SETTINGS, HOLIDAYS, 5]),
                 ('examSessions', 'exam_sessions', [ex, SEM, tt, HOLIDAYS, exams(), [], TODAY]),
                 ('examStats', 'exam_stats', [ex, sessions, TODAY]),
                 ('cleanClasses', 'clean_classes', [[{'subject': '글로벌문화', 'day': 'Thu', 'start': '15시', 'end': '13:00'}, {'subject': '숫자 검사', 'day': 2, 'start': '١٣:٠٠'}]])]
        routine = {'name': '단어', 'start': '2026-09-28', 'days': [], 'count': 6, 'done': ['2026-09-28', TODAY]}
        cases.extend([('routineStats', 'routine_stats', [routine, TODAY]), ('placeReview', 'place_review', [item(catchup=True, learned=SEM, base='2026-10-02'), '2026-09-02', TODAY]),
                      ('applyMode', 'apply_mode', [item(source='timetable', subject='글로벌문화', catchup=True, learned=SEM, base='2026-09-30', moved={'stage': 0, 'date': '2026-10-10'}), timetable(EVE), SETTINGS, HOLIDAYS, TODAY])])
        script = 'import * as s from ' + json.dumps((ROOT / 'web/review-schedule.js').as_uri()) + ';let x="";for await(const c of process.stdin)x+=c;process.stdout.write(JSON.stringify(JSON.parse(x).map(v=>s[v.name](...v.args))));'
        result = subprocess.run([node, '--input-type=module', '-e', script], input=json.dumps([{'name': js, 'args': args} for js, _, args in cases]),
                                text=True, encoding='utf-8', capture_output=True, timeout=20, check=True)
        actual = json.loads(result.stdout)
        expected = [getattr(s, py)(*args) for _, py, args in cases]
        for i, (left, right) in enumerate(zip(actual, expected)):
            if isinstance(right, float): self.assertAlmostEqual(left, right)
            else: self.assertEqual(left, right, cases[i][0])


if __name__ == '__main__':
    unittest.main()
