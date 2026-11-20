import math
import unittest

import dynamic_model as model
import staffing
from staffing_scenarios import case, catalog, example, stress


class StaffingTests(unittest.TestCase):
    def small(self):
        data = example()
        data['patients'] = data['patients'][:2]
        for p in data['patients']:
            p.update(ready_hour=0, length_of_stay_hours=8, eligible_snf_ids=['SNF_1'],
                     care_hours_per_day={'RN': 24, 'LPN': 0, 'CNA': 0})
        for shift in data['staffing']['shifts']:
            shift.pop('staff_count')
            shift.pop('direct_care_hours_per_staff_per_day')
            shift['direct_care_hours'] = {'RN': 8, 'LPN': 0, 'CNA': 0}
        return data

    def test_cross_hospital_shared_role_and_handover(self):
        result = model.compare(self.small())
        for key in ('baseline', 'ccn'):
            admissions = sorted(p['admission_hour'] for p in result[key]['patients'])
            self.assertEqual(admissions, [0, 8])
            self.assertEqual(result[key]['summary']['full_plan_wait_hours'], 8)
            for row in result[key]['staffing_audit']:
                for role in staffing.ROLES:
                    self.assertLessEqual(row['used_hours_per_hour'][role], row['available_hours_per_hour'][role]+1e-9)

    def test_future_shortage_checks_entire_stay(self):
        data = self.small()
        data['patients'] = data['patients'][:1]
        data['patients'][0].update(ready_hour=6, length_of_stay_hours=4)
        for shift in data['staffing']['shifts']:
            if shift['start_hour'] == 8:
                shift['direct_care_hours']['RN'] = 0
        result = model.compare(data)
        for key in ('baseline', 'ccn'):
            self.assertEqual(result[key]['patients'][0]['admission_hour'], 16)

    def test_partial_shift_has_no_banked_care(self):
        data = self.small()
        data['patients'][0].update(ready_hour=7.5, length_of_stay_hours=.5)
        s = model.Scenario.from_dict(data)
        booking = model.Booking(0, 7.5, 8)
        self.assertIsNone(staffing.violation(s, s.patients[1], 1, 8, {0: booking}))
        self.assertIsNotNone(staffing.violation(s, s.patients[1], 1, 7.75, {0: booking}))

    def test_no_substitution_and_unplaceable_metrics(self):
        data = self.small()
        for shift in data['staffing']['shifts']:
            shift['direct_care_hours'] = {'RN': 0, 'LPN': 1000, 'CNA': 1000}
        result = model.compare(data)
        self.assertFalse(result['observation']['complete'])
        self.assertIsNone(result['comparison']['full_plan_wait_hours_saved'])
        self.assertEqual(result['ccn']['summary']['placed_count'], 0)

    def test_plan_validator_rejects_resource_overlap(self):
        s = model.Scenario.from_dict(self.small())
        with self.assertRaisesRegex(ValueError, 'staffing capacity'):
            model.validate_plan(s, {0: model.Booking(0,0,8), 1: model.Booking(1,0,8)})

    def test_roundtrip(self):
        s = model.Scenario.from_dict(example())
        self.assertEqual(model.Scenario.from_dict(s.to_dict()), s)
        self.assertEqual(sum(row['staff_count']['RN'] for row in s.staffing['shifts']), 6)
        self.assertEqual(s.staffing['shifts'][0]['direct_care_hours']['CNA'], 12)

    def test_headcount_and_daily_hours_drive_supply(self):
        data = example()
        first = data['staffing']['shifts'][0]
        first['staff_count']['RN'] = 2
        first['direct_care_hours_per_staff_per_day']['RN'] = 4
        shift = model.Scenario.from_dict(data).staffing['shifts'][0]
        self.assertEqual(shift['direct_care_hours']['RN'], 8)
        for bad in (-1, 1.5):
            data = example()
            data['staffing']['shifts'][0]['staff_count']['RN'] = bad
            with self.assertRaises(ValueError):
                model.Scenario.from_dict(data)

    def test_stress_preserves_roster_and_first_cohort(self):
        base, case = example(), stress()
        self.assertEqual((len(case['patients']), len(case['beds'])), (36, 6))
        self.assertEqual(case['patients'][:18], base['patients'])
        self.assertEqual(case['staffing'], base['staffing'])
        model.Scenario.from_dict(case)

    def test_pressure_catalog_and_larger_scenarios(self):
        levels = [(row['id'], row['patient_count'], row['free_bed_count']) for row in catalog()['cases']]
        self.assertEqual(levels, [('base', 18, 12), ('stress_36', 36, 6),
                                  ('stress_72', 72, 4), ('stress_144', 144, 4)])
        for case_id, patients, beds in levels:
            scenario = case(case_id)
            self.assertEqual((len(scenario['patients']), len(scenario['beds'])), (patients, beds))
            self.assertEqual(scenario['staffing'], example()['staffing'])
            model.Scenario.from_dict(scenario)
        result = model.compare(case('stress_72'))
        self.assertEqual(result['ccn']['summary']['placed_count'], 72)
        self.assertTrue(result['observation']['complete'])
        with self.assertRaisesRegex(ValueError, 'Unknown staffing scenario'):
            case('invalid')

    def test_invalid_input_rejected(self):
        for mutate in (
            lambda d: d['staffing']['shifts'].pop(),
            lambda d: d['staffing']['shifts'][0].update(end_hour=9),
            lambda d: d['staffing']['shifts'][0]['direct_care_hours'].update(RN=-1),
            lambda d: d['patients'][0].pop('care_hours_per_day'),
            lambda d: d['patients'][0]['care_hours_per_day'].update(RN=math.nan),
        ):
            data = self.small()
            mutate(data)
            with self.assertRaises(ValueError):
                model.Scenario.from_dict(data)

    def test_scenarios_change_only_supply_and_remain_valid(self):
        for factor in (1, .6, .3):
            data = example()
            for row in data['staffing']['shifts']:
                for role in staffing.ROLES:
                    row['direct_care_hours_per_staff_per_day'][role] *= factor
            result = model.compare(data)
            self.assertTrue(result['observation']['complete'])
            self.assertLessEqual(result['ccn']['summary']['full_plan_wait_hours'], result['baseline']['summary']['full_plan_wait_hours'])


if __name__ == '__main__':
    unittest.main()
