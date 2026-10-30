"""Independent schedule oracles and larger global-search regressions."""

from copy import deepcopy
import itertools
import math
import unittest

from dynamic_model import Booking, Scenario, compare, validate_plan
from dynamic_scenarios import generate
from run_dynamic_experiments import aggregate, audit_result, summarize


def brute_force_objective(data):
    """Enumerate bed assignments and each bed's patient order independently.

    For a fixed order, starting each job as soon as ready/previously released
    minimizes its completion time and every later completion on that bed.
    """
    patients, beds = data["patients"], data["beds"]
    insurances = {s["id"]: s["accepted_insurances"] for s in data["snfs"]}
    choices = []
    for p in patients:
        choices.append([b for b, bed in enumerate(beds)
                        if bed["snf_id"] in p["eligible_snf_ids"]
                        and bed["gender"] in (p["gender"], "any")
                        and ("*" in insurances[bed["snf_id"]] or p["insurance_type"] in insurances[bed["snf_id"]])
                        and set(p["required_capabilities"]) <= set(bed["capabilities"])])
    feasible = [i for i, options in enumerate(choices) if options]
    best = math.inf
    for assignment in itertools.product(*(choices[i] for i in feasible)):
        groups = [[i for i, bed in zip(feasible, assignment) if bed == b] for b in range(len(beds))]
        for orders in itertools.product(*(itertools.permutations(group) for group in groups)):
            cost = 0
            for order in orders:
                free = 0
                for i in order:
                    p = patients[i]
                    start = max(free, p["ready_hour"])
                    cost += start - p["ready_hour"]
                    free = start + p["length_of_stay_hours"]
            best = min(best, cost)
    return best


class GlobalSolverTests(unittest.TestCase):
    def test_small_solutions_match_independent_exhaustive_oracle(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                data = generate({"patient_count": 5, "hospital_count": 2, "snf_count": 3,
                                 "bed_count": 3, "horizon_hours": 24, "seed": seed})
                # Compress arrivals to create genuine conflicts, not only easy zero-wait cases.
                for i, p in enumerate(data["patients"]):
                    p["ready_hour"] = (seed + i) % 4
                    p["length_of_stay_hours"] = 1 + (seed * 3 + i * 2) % 7
                data = Scenario.from_dict(data).to_dict()
                result = compare(data)
                self.assertEqual(result["ccn"]["solver"]["status"], "optimal")
                self.assertEqual(result["ccn"]["summary"]["full_plan_wait_hours"], brute_force_objective(data))
                for policy in ("baseline", "ccn"):
                    self.assertTrue(audit_result(result["scenario"], result[policy])["passed"])

    def test_original_96_patient_input_improves_without_changing_baseline(self):
        original = Scenario.from_dict(generate()).to_dict()
        result = compare(original)
        self.assertEqual(original, result["scenario"])
        self.assertEqual(result["baseline"]["summary"]["full_plan_wait_hours"], 1029)
        self.assertEqual(result["baseline"]["summary"]["blocked_bed_hours"], 912)
        solver = result["ccn"]["solver"]
        self.assertEqual(solver["initial_objective_hours"], 858)
        self.assertLessEqual(result["ccn"]["summary"]["full_plan_wait_hours"], 574)
        self.assertLessEqual(result["ccn"]["summary"]["blocked_bed_hours"], 531)
        self.assertEqual(solver["candidates_evaluated"], 1000)
        self.assertEqual(solver["status"], "heuristic")
        self.assertEqual(solver["lower_bound_hours"], 0)
        self.assertEqual(solver["objective_hours"], result["ccn"]["summary"]["full_plan_wait_hours"])
        second = compare(deepcopy(original))
        self.assertEqual(result["ccn"], second["ccn"])
        for policy in ("baseline", "ccn"):
            audit_result(original, result[policy])

    def test_larger_random_inputs_keep_feasible_incumbent_and_validate_accounting(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                data = generate({"patient_count": 16 + seed, "hospital_count": 3,
                                 "snf_count": 4, "bed_count": 8, "horizon_hours": 72, "seed": seed})
                before = deepcopy(data)
                result = compare(data)
                self.assertEqual(before, data)
                solver = result["ccn"]["solver"]
                self.assertLessEqual(solver["objective_hours"], solver["initial_objective_hours"])
                self.assertLessEqual(solver["objective_hours"], result["baseline"]["summary"]["full_plan_wait_hours"])
                self.assertLessEqual(solver["candidates_evaluated"], solver["candidate_budget"])
                for policy in ("baseline", "ccn"):
                    audit_result(result["scenario"], result[policy])

    def test_plan_validator_rejects_omission_early_start_stay_and_overlap(self):
        data = {"name": "validator", "horizon_hours": 24,
                "hospitals": [{"id": "A"}], "snfs": [{"id": "S"}],
                "beds": [{"id": "b", "snf_id": "S", "gender": "any"}],
                "patients": [{"id": str(i), "hospital_id": "A", "gender": "female",
                              "ready_hour": 1, "length_of_stay_hours": 2, "eligible_snf_ids": ["S"]}
                             for i in range(2)]}
        s = Scenario.from_dict(data)
        validate_plan(s, {0: Booking(0, 1, 3), 1: Booking(0, 3, 5)})
        invalid = [{0: Booking(0, 1, 3)}, {0: Booking(0, 0, 2), 1: Booking(0, 3, 5)},
                   {0: Booking(0, 1, 4), 1: Booking(0, 4, 6)},
                   {0: Booking(0, 1, 3), 1: Booking(0, 2, 4)},
                   {0: Booking(8, 1, 3), 1: Booking(0, 3, 5)},
                   {0: Booking(0, math.nan, 3), 1: Booking(0, 3, 5)}]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                validate_plan(s, plan)
        bad = deepcopy(data)
        bad["beds"][0]["gender"] = "male"
        with self.assertRaises(ValueError):
            validate_plan(Scenario.from_dict(bad), {0: Booking(0, 1, 3), 1: Booking(0, 3, 5)})

    def test_independent_audit_detects_wrong_metrics_and_future_occupancy(self):
        data = generate({"patient_count": 12, "hospital_count": 2, "snf_count": 2,
                         "bed_count": 2, "horizon_hours": 24, "seed": 42})
        result = compare(data)
        modified = deepcopy(result["ccn"])
        modified["summary"]["blocked_bed_hours"] += 1
        with self.assertRaises(ValueError):
            audit_result(result["scenario"], modified)
        modified = deepcopy(result["ccn"])
        modified["patients"][0]["wait_hours"] += 1
        with self.assertRaises(ValueError):
            audit_result(result["scenario"], modified)
        modified = deepcopy(result["ccn"])
        modified["snapshots"][-1]["occupied_count"] += 1
        with self.assertRaises(ValueError):
            audit_result(result["scenario"], modified)

    def test_batch_aggregation_retains_zero_and_negative_window_savings(self):
        data = generate({"patient_count": 12, "hospital_count": 2, "snf_count": 2,
                         "bed_count": 2, "horizon_hours": 24, "seed": 42})
        result = compare(data)
        experiment = {"id": "test", "cohort_id": "fixed"}
        row = summarize(experiment, 42, result, .5)
        self.assertIsNotNone(row["baseline_worst_hospital_mean_wait_hours"])
        self.assertIsNotNone(row["ccn_worst_hospital_mean_wait_hours"])
        second = deepcopy(row)
        second.update(seed=84, baseline_blocked_bed_hours=20, ccn_blocked_bed_hours=25, blocked_bed_hours_saved=-5)
        first = deepcopy(row)
        first.update(baseline_blocked_bed_hours=0, ccn_blocked_bed_hours=0, blocked_bed_hours_saved=0)
        stats = aggregate([first, second])[0]
        self.assertEqual(stats["seed_count"], 2)
        self.assertEqual(stats["blocked_bed_hours_saved_mean"], -2.5)
        self.assertEqual(stats["blocked_bed_hours_pooled_saved_percent"], -25)


if __name__ == "__main__":
    unittest.main()
