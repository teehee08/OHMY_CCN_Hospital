"""Complete-cohort reporting, automatic bounds and undefined outcomes."""

from copy import deepcopy
import unittest

from dynamic_experiments import catalog
from dynamic_model import compare
from dynamic_scenarios import generate
from run_dynamic_experiments import aggregate, audit_result, summarize


def conflict(horizon=1):
    return {"name": "Objective and observation are different scopes", "horizon_hours": horizon,
            "hospitals": [{"id": "A"}, {"id": "B"}], "snfs": [{"id": "S"}],
            "beds": [{"id": "one", "snf_id": "S", "gender": "any"}],
            "patients": [{"id": "long", "hospital_id": "A", "gender": "female", "ready_hour": 0,
                          "length_of_stay_hours": 10, "eligible_snf_ids": ["S"]},
                         {"id": "short", "hospital_id": "B", "gender": "male", "ready_hour": 1,
                          "length_of_stay_hours": 1, "eligible_snf_ids": ["S"]}]}


class DynamicMetricContractTests(unittest.TestCase):
    def test_medium_primary_objective_and_percentage_are_full_cohort(self):
        scenario = next(e["scenario"] for e in catalog(42) if e["id"] == "load_medium")
        result = compare(scenario)
        self.assertEqual(result["objective"], {"id": "full_cohort_total_wait", "direction": "minimize",
                         "unit": "patient_hours", "time_scope": "through_eventual_admission",
                         "includes_all_supplied_patients": True, "defined": True})
        self.assertEqual(result["baseline"]["summary"]["full_plan_wait_hours"], 635)
        self.assertEqual(result["ccn"]["summary"]["full_plan_wait_hours"], 490)
        self.assertEqual(result["comparison"]["full_plan_wait_hours_saved"], 145)
        self.assertAlmostEqual(result["comparison"]["full_plan_wait_reduction_percent"], 100 * 145 / 635)
        self.assertEqual(result["comparison"]["blocked_bed_hours_saved"], 145)
        self.assertEqual(result["observation"]["start_hour"], 8)
        self.assertEqual(result["observation"]["end_hour"], 257)
        self.assertEqual(result["observation"]["duration_hours"], 249)
        self.assertEqual(result["baseline"]["summary"]["time_to_completion_hours"], 249)
        self.assertEqual(result["ccn"]["summary"]["time_to_completion_hours"], 228)
        for policy in ("baseline", "ccn"):
            self.assertEqual(result[policy]["summary"]["completed_count"], 96)
            self.assertEqual(result[policy]["summary"]["waiting_count"], 0)
            self.assertEqual(result[policy]["summary"]["snf_occupied_bed_hours"], 3660)
            self.assertAlmostEqual(result[policy]["summary"]["snf_utilization"], 3660 / (24 * 249))
            audit_result(result["scenario"], result[policy])

    def test_less_waiting_can_still_have_later_final_completion(self):
        result = compare(conflict())
        before, after = (result[p]["summary"] for p in ("baseline", "ccn"))
        self.assertEqual((before["full_plan_wait_hours"], after["full_plan_wait_hours"]), (9, 2))
        self.assertEqual((before["blocked_bed_hours"], after["blocked_bed_hours"]), (9, 2))
        self.assertEqual(result["comparison"]["full_plan_wait_hours_saved"], 7)
        self.assertEqual(result["comparison"]["blocked_bed_hours_saved"], 7)
        self.assertEqual(result["comparison"]["time_to_completion_hours_saved"], -1)
        self.assertAlmostEqual(result["comparison"]["full_plan_wait_reduction_percent"], 700 / 9)
        self.assertEqual(before["mean_admitted_wait_hours"], 4.5)
        self.assertEqual(before["mean_full_plan_wait_hours"], 4.5)
        self.assertEqual(after["mean_full_plan_wait_hours"], 1)

    def test_arrival_span_metadata_does_not_cut_off_or_pad_fixed_cohort(self):
        short, long = compare(conflict()), compare(conflict(100))
        self.assertEqual(short["observation"], long["observation"])
        self.assertEqual(short["observation"]["end_hour"], 12)
        for policy in ("baseline", "ccn"):
            with self.subTest(policy=policy):
                a, b = short[policy], long[policy]
                bookings = lambda r: [(p["patient_id"], p["bed_id"], p["planned_admission_hour"], p["planned_release_hour"]) for p in r["patients"]]
                self.assertEqual(bookings(a), bookings(b))
                self.assertEqual(a["summary"]["full_plan_wait_hours"], b["summary"]["full_plan_wait_hours"])
                self.assertEqual(b["summary"]["blocked_bed_hours"], b["summary"]["full_plan_wait_hours"])
                self.assertGreater(a["summary"]["last_planned_admission_hour"], short["scenario"]["horizon_hours"])
                for row in a["patients"]:
                    if row["planned_admission_hour"] > 1:
                        self.assertEqual(row["admission_hour"], row["planned_admission_hour"])
                        self.assertEqual(row["release_hour"], row["planned_release_hour"])
                audit_result(short["scenario"], a)
                audit_result(long["scenario"], b)

    def test_unplaceable_patient_never_disappears_from_full_population_statistics(self):
        data = conflict(5)
        data["beds"][0]["gender"] = "female"
        result = compare(data)
        self.assertFalse(result["objective"]["defined"])
        self.assertFalse(result["observation"]["complete"])
        self.assertIsNone(result["observation"]["end_hour"])
        self.assertIsNone(result["observation"]["duration_hours"])
        self.assertIsNone(result["comparison"]["full_plan_wait_hours_saved"])
        self.assertIsNone(result["comparison"]["full_plan_wait_reduction_percent"])
        for policy in ("baseline", "ccn"):
            report = result[policy]
            self.assertIsNone(report["summary"]["full_plan_wait_hours"])
            self.assertIsNone(report["summary"]["mean_full_plan_wait_hours"])
            self.assertIsNone(report["summary"]["blocked_bed_hours"])
            self.assertIsNone(report["summary"]["time_to_completion_hours"])
            self.assertIsNone(report["summary"]["snf_utilization"])
            self.assertEqual(report["summary"]["planned_patient_count"], 1)
            self.assertEqual(report["summary"]["structurally_unplaceable_patient_ids"], ["short"])
            self.assertEqual(report["solver"]["objective_population"], "compatible_patients_only")
            self.assertFalse(report["solver"]["full_cohort_objective_defined"])
            audit_result(result["scenario"], report)
        row = summarize({"id": "blocked", "cohort_id": "same"}, 42, result, 0)
        for policy in ("baseline", "ccn"):
            self.assertIsNone(row[f"{policy}_p90_eventual_wait_hours"])
            self.assertIsNone(row[f"{policy}_max_eventual_wait_hours"])
            self.assertIsNone(row[f"{policy}_worst_hospital_mean_wait_hours"])
            self.assertEqual(row[f"{policy}_structurally_unplaceable_patient_count"], 1)
        stats = aggregate([row])[0]
        self.assertIsNone(stats["baseline_full_plan_wait_hours_mean"])
        self.assertIsNone(stats["full_plan_wait_hours_pooled_saved_percent"])
        self.assertEqual(stats["baseline_full_plan_wait_hours_defined_seed_count"], 0)

    def test_undefined_seed_is_not_silently_dropped_from_aggregate(self):
        result = compare(conflict(100))
        row = summarize({"id": "mixed", "cohort_id": "same"}, 42, result, 0)
        undefined = deepcopy(row)
        undefined.update(seed=84, baseline_full_plan_wait_hours=None, ccn_full_plan_wait_hours=None,
                         full_plan_wait_hours_saved=None)
        stats = aggregate([row, undefined])[0]
        self.assertEqual(stats["seed_count"], 2)
        self.assertEqual(stats["baseline_full_plan_wait_hours_defined_seed_count"], 1)
        self.assertIsNone(stats["baseline_full_plan_wait_hours_mean"])
        self.assertIsNone(stats["full_plan_wait_hours_pooled_saved_percent"])
        self.assertIsNotNone(stats["blocked_bed_hours_saved_mean"])

    def test_empty_no_bed_case_has_zero_total_and_undefined_means_or_ratios(self):
        data = conflict(5)
        data.update(patients=[], beds=[])
        result = compare(data)
        self.assertTrue(result["objective"]["defined"])
        self.assertEqual(result["observation"]["duration_hours"], 0)
        for policy in ("baseline", "ccn"):
            summary = result[policy]["summary"]
            self.assertEqual(summary["full_plan_wait_hours"], 0)
            self.assertIsNone(summary["mean_full_plan_wait_hours"])
            self.assertIsNone(summary["snf_utilization"])
            audit_result(result["scenario"], result[policy])
        row = summarize({"id": "empty", "cohort_id": "same"}, 42, result, 0)
        self.assertIsNone(row["offered_load"])
        self.assertIsNone(aggregate([row])[0]["full_plan_wait_hours_pooled_saved_percent"])

    def test_audit_rejects_wrong_mean_and_utilization_fields(self):
        result = compare(conflict(100))
        for field in ("mean_wait_hours", "mean_admitted_wait_hours", "mean_observed_wait_hours",
                      "mean_full_plan_wait_hours", "snf_utilization"):
            changed = deepcopy(result["ccn"])
            changed["summary"][field] = 777
            with self.subTest(field=field), self.assertRaises(ValueError):
                audit_result(result["scenario"], changed)

    def test_full_32_bit_seed_remains_valid_in_model(self):
        data = conflict()
        data["tie_break_seed"] = 4294967295
        self.assertEqual(compare(data)["scenario"]["tie_break_seed"], 4294967295)

    def test_nonzero_start_excludes_empty_time_before_first_patient(self):
        data = conflict()
        data["patients"] = data["patients"][:1]
        data["patients"][0].update(ready_hour=24, length_of_stay_hours=12)
        data.pop("horizon_hours")
        result = compare(data)
        self.assertEqual(result["observation"]["start_hour"], 24)
        self.assertEqual(result["observation"]["end_hour"], 36)
        for policy in ("baseline", "ccn"):
            self.assertEqual(result[policy]["summary"]["time_to_completion_hours"], 12)
            self.assertEqual(result[policy]["summary"]["snf_utilization"], 1)
            self.assertEqual(result[policy]["snapshots"][0]["time_hour"], 24)
            self.assertEqual(result[policy]["snapshots"][-1]["time_hour"], 36)
            audit_result(result["scenario"], result[policy])

    def test_ready_after_legacy_span_is_valid_and_finishes(self):
        data = conflict()
        data["patients"][1]["ready_hour"] = 500
        result = compare(data)
        self.assertEqual(result["observation"]["end_hour"], 501)
        self.assertTrue(result["observation"]["complete"])
        self.assertEqual(result["scenario"]["arrival_span_hours"], 1)

    def test_generator_arrival_span_alias_preserves_same_cohort(self):
        self.assertEqual(generate({"arrival_span_hours": 168}), generate({"horizon_hours": 168}))
        with self.assertRaises(ValueError):
            generate({"arrival_span_hours": 168, "horizon_hours": 24})


if __name__ == "__main__":
    unittest.main()
