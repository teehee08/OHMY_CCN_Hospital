"""Full-population fairness, independent of the scheduling implementation."""

from copy import deepcopy
import json
import math
import unittest

from dynamic_analysis import fairness_report


def reports(before, after):
    patients = [{"id": f"P{i}", "hospital_id": "A" if i < 8 else "B"}
                for i in range(len(before))]
    scenario = {"patients": patients, "hospitals": [{"id": h} for h in ("A", "B", "Empty")]}
    def result(waits):
        return {"patients": [{"patient_id": p["id"], "hospital_id": p["hospital_id"], "wait_hours": wait}
                             for p, wait in zip(patients, waits)]}
    return scenario, result(before), result(after)


class FairnessTests(unittest.TestCase):
    def test_lower_total_can_worsen_p90_and_another_hospital(self):
        s, before, after = reports([10] + [1] * 8 + [0], [0] * 8 + [4, 4])
        report = fairness_report(s, before, after)
        self.assertTrue(report["defined"])
        self.assertEqual(report["baseline"]["total_wait_hours"], 18)
        self.assertEqual(report["ccn"]["total_wait_hours"], 8)
        self.assertAlmostEqual(report["baseline"]["p90_wait_hours"], 1.9)
        self.assertEqual(report["ccn"]["p90_wait_hours"], 4)
        self.assertEqual(report["patients"]["improved_count"], 8)
        self.assertEqual(report["patients"]["worsened_count"], 2)
        self.assertEqual(report["patients"]["worsened_fraction"], .2)
        self.assertEqual(report["patients"]["max_increase_wait_hours"], 4)
        a, b, empty = report["hospitals"]
        self.assertEqual(a["status"], "improved")
        self.assertEqual(b["baseline_total_wait_hours"], 1)
        self.assertEqual(b["ccn_total_wait_hours"], 8)
        self.assertEqual(b["delta_total_wait_hours"], 7)
        self.assertEqual(b["delta_mean_wait_hours"], 3.5)
        self.assertEqual(b["worsened_patient_count"], 2)
        self.assertEqual(empty["status"], "empty")
        self.assertIsNone(empty["baseline_mean_wait_hours"])
        self.assertEqual(report["hospital_comparison"], {"improved_count": 1, "worsened_count": 1, "unchanged_count": 0})
        self.assertEqual(report["ccn"]["worst_hospital_mean_wait_hours"], 4)
        json.dumps(report, allow_nan=False)

    def test_unplaceable_patient_invalidates_all_population_statistics(self):
        s, before, after = reports([1] * 9 + [None], [0] * 9 + [None])
        report = fairness_report(s, before, after)
        self.assertFalse(report["defined"])
        self.assertEqual(report["undefined_patient_ids"], {"baseline": ["P9"], "ccn": ["P9"]})
        for policy in ("baseline", "ccn"):
            self.assertTrue(all(v is None for v in report[policy].values()))
        for key, value in report["patients"].items():
            if key != "rows":
                self.assertIsNone(value, key)
        self.assertTrue(report["hospitals"][0]["defined"])
        self.assertFalse(report["hospitals"][1]["defined"])
        self.assertIsNone(report["hospitals"][1]["baseline_total_wait_hours"])
        self.assertIsNone(report["hospital_comparison"]["improved_count"])
        self.assertEqual(report["patients"]["rows"][-1]["status"], "undefined")

    def test_total_improvement_does_not_imply_lower_maximum_patient_wait(self):
        s, before, after = reports([5, 5, 5], [0, 0, 10])
        report = fairness_report(s, before, after)
        self.assertLess(report["ccn"]["total_wait_hours"], report["baseline"]["total_wait_hours"])
        self.assertGreater(report["ccn"]["max_wait_hours"], report["baseline"]["max_wait_hours"])
        self.assertEqual(report["patients"]["worsened_count"], 1)
        self.assertEqual(report["patients"]["max_increase_wait_hours"], 5)

    def test_one_policy_missing_wait_never_becomes_paired_subset(self):
        s, before, after = reports([1, 2], [0, None])
        report = fairness_report(s, before, after)
        self.assertFalse(report["defined"])
        self.assertEqual(report["undefined_patient_ids"]["baseline"], [])
        self.assertEqual(report["undefined_patient_ids"]["ccn"], ["P1"])
        self.assertIsNone(report["baseline"]["total_wait_hours"])
        self.assertEqual(len(report["patients"]["rows"]), 2)

    def test_wrong_population_or_hospital_raises_instead_of_dropping_rows(self):
        s, before, after = reports([1, 2], [0, 0])
        variants = []
        missing = deepcopy(after)
        missing["patients"].pop()
        variants.append(missing)
        duplicate = deepcopy(after)
        duplicate["patients"][1] = deepcopy(duplicate["patients"][0])
        variants.append(duplicate)
        extra = deepcopy(after)
        extra["patients"].append({"patient_id": "unknown", "hospital_id": "A", "wait_hours": 0})
        variants.append(extra)
        wrong = deepcopy(after)
        wrong["patients"][0]["hospital_id"] = "B"
        variants.append(wrong)
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                fairness_report(s, before, changed)

    def test_nonfinite_or_negative_wait_is_explicitly_undefined(self):
        for value in (math.nan, math.inf, -1, True):
            s, before, after = reports([1], [value])
            report = fairness_report(s, before, after)
            self.assertFalse(report["defined"])
            self.assertIsNone(report["patients"]["rows"][0]["ccn_wait_hours"])
            json.dumps(report, allow_nan=False)

    def test_float_tolerance_does_not_create_spurious_harm(self):
        s, before, after = reports([.3], [.1 + .2])
        report = fairness_report(s, before, after)
        self.assertEqual(report["patients"]["unchanged_count"], 1)
        self.assertEqual(report["hospital_comparison"]["unchanged_count"], 1)

    def test_empty_population_has_zero_totals_and_undefined_ratios(self):
        s, before, after = reports([], [])
        report = fairness_report(s, before, after)
        self.assertTrue(report["defined"])
        self.assertEqual(report["baseline"]["total_wait_hours"], 0)
        self.assertIsNone(report["baseline"]["mean_wait_hours"])
        self.assertIsNone(report["patients"]["improved_fraction"])
        self.assertIsNone(report["patients"]["max_increase_wait_hours"])
        self.assertEqual(report["hospital_comparison"]["unchanged_count"], 0)


if __name__ == "__main__":
    unittest.main()
