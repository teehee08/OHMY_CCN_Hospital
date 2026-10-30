"""Crossed seeds and cohort-level uncertainty, using tiny reproducible cohorts."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from dynamic_model import Scenario, compare
from run_dynamic_experiments import (aggregate_nested, audit_result, run_suite,
                                     DEFAULT_REQUEST_SEEDS, _hash_json)


def tiny_catalog(seed):
    scenario = {"name": "Small crossed experiment", "arrival_span_hours": 24,
                "hospitals": [{"id": "A"}, {"id": "B"}], "snfs": [{"id": "S"}],
                "beds": [{"id": "bed", "snf_id": "S", "gender": "any"}],
                "patients": [{"id": "long", "hospital_id": "A", "gender": "female",
                              "ready_hour": 0, "length_of_stay_hours": 10 + seed % 3,
                              "eligible_snf_ids": ["S"]},
                             {"id": "short", "hospital_id": "B", "gender": "male",
                              "ready_hour": 0, "length_of_stay_hours": 1,
                              "eligible_snf_ids": ["S"]}],
                "baseline_policy": "local", "tie_break_seed": seed}
    return [{"id": "tiny", "title": "Tiny crossed experiment", "changed_factor": "test",
             "cohort_id": f"tiny_{seed}", "scenario": Scenario.from_dict(scenario).to_dict(),
             "design": {"family": "test", "held_constant": [], "comparison_note": "test"}}]


def row(cohort, request, saved, optimizer=20261029):
    return {"experiment_id": "x", "cohort_id": f"fixed_{cohort}", "seed": cohort,
            "cohort_seed": cohort, "request_seed": request, "optimizer_seed": optimizer,
            "preference_variant": "supplied", "baseline_full_plan_wait_hours": 100,
            "ccn_full_plan_wait_hours": 100 - saved if saved is not None else None,
            "full_plan_wait_hours_saved": saved}


class RandomizedExperimentTests(unittest.TestCase):
    def test_float_observation_duration_passes_and_wrong_duration_is_rejected(self):
        scenario = tiny_catalog(42)[0]["scenario"]
        scenario["patients"] = [scenario["patients"][0]]
        scenario["patients"][0].update(ready_hour=.1, length_of_stay_hours=.2)
        result = compare(scenario)
        for policy in ("baseline", "ccn"):
            self.assertTrue(audit_result(result["scenario"], result[policy])["passed"])
            changed = deepcopy(result[policy])
            changed["observation"]["duration_hours"] += .001
            with self.assertRaises(ValueError):
                audit_result(result["scenario"], changed)

    def test_default_request_draws_are_twenty_and_independent_of_cohort_seed(self):
        self.assertEqual(DEFAULT_REQUEST_SEEDS, tuple(range(20)))
        with patch("run_dynamic_experiments.catalog", tiny_catalog):
            report = run_suite(seeds=(42, 84, 126), compact=True)
        self.assertEqual(len(report["runs"]), 60)
        self.assertEqual(report["request_seeds"], list(range(20)))
        self.assertEqual(report["optimizer_seeds"], [20261029])
        nested = report["nested_aggregate"][0]
        self.assertEqual(nested["cohort_count"], 3)
        self.assertEqual(nested["request_seed_counts"], [20, 20, 20])
        self.assertTrue(nested["uncertainty"]["small_cohort_warning"])
        for cohort in nested["cohorts"]:
            self.assertEqual(cohort["request_seeds"], list(range(20)))
            self.assertLess(cohort["metrics"]["baseline_full_plan_wait_hours"]["min"],
                            cohort["metrics"]["baseline_full_plan_wait_hours"]["max"])
        self.assertIn("dynamic_analysis.py", report["source_sha256"])
        self.assertTrue(all(len(v) == 64 for v in report["source_sha256"].values()))
        json.dumps(report, allow_nan=False)

    def test_crossed_optimizer_seeds_do_not_redraw_patients_or_request_seed(self):
        with patch("run_dynamic_experiments.catalog", tiny_catalog):
            report = run_suite(cohort_seeds=(42,), request_seeds=(0, 1), optimizer_seeds=(100, 101))
        self.assertEqual(len(report["runs"]), 4)
        inputs = [r["result"]["scenario"] for r in report["runs"]]
        for scenario in inputs:
            self.assertEqual(scenario["patients"], inputs[0]["patients"])
            self.assertEqual(scenario["beds"], inputs[0]["beds"])
        self.assertEqual([(s["tie_break_seed"], s["optimizer_seed"]) for s in inputs], [(0, 100), (0, 101), (1, 100), (1, 101)])
        self.assertEqual(len(report["nested_aggregate"]), 2)
        self.assertTrue(all(g["cohort_count"] == 1 for g in report["nested_aggregate"]))
        for group in report["nested_aggregate"]:
            self.assertIsNone(group["metrics"]["full_plan_wait_hours_saved"]["cohort_bootstrap_ci95"])

    def test_compact_retains_reconstructible_inputs_exact_plans_and_all_fairness(self):
        with patch("run_dynamic_experiments.catalog", tiny_catalog):
            report = run_suite((42,), request_seeds=(3,), compact=True)
        record = report["runs"][0]
        template = report["cohort_inputs"][0]
        reconstructed = deepcopy(template["scenario_template"])
        reconstructed.update(tie_break_seed=3, optimizer_seed=20261029)
        self.assertEqual(_hash_json(reconstructed), record["input_sha256"])
        self.assertNotIn("scenario", record["result"])
        self.assertNotIn("snapshots", record["result"]["baseline"])
        self.assertEqual(len(record["result"]["baseline"]["patients"]), 2)
        self.assertEqual(len(record["result"]["fairness"]["patients"]["rows"]), 2)
        self.assertIn("solver", record["result"]["ccn"])
        self.assertEqual(len(record["summary"]["hospital_fairness"]), 2)
        self.assertEqual(len(report["nested_aggregate"][0]["cohorts"][0]["hospital_metrics"]), 2)

    def test_experiment_filter_applies_before_preference_sensitivity(self):
        with patch("run_dynamic_experiments.catalog", tiny_catalog):
            report = run_suite((42,), request_seeds=(0,), experiment_ids=("tiny",),
                               preference_variants=("supplied", "ordinary_first", "shuffled"),
                               preference_seed=17, compact=True)
        self.assertEqual(report["experiment_ids"], ["tiny"])
        self.assertEqual([r["summary"]["experiment_id"] for r in report["runs"]],
                         ["tiny", "tiny__ordinary_first", "tiny__shuffled"])
        self.assertTrue(all(r["summary"]["base_experiment_id"] == "tiny" for r in report["runs"]))
        self.assertEqual([r["preference_seed"] for r in report["runs"]], [None, None, 17])
        self.assertEqual(len(report["nested_aggregate"]), 3)

    def test_metadata_distinguishes_catalog_design_from_actual_crossed_inputs(self):
        def declared_catalog(seed):
            values = tiny_catalog(seed)
            values[0]["design"].update(seed=seed, held_constant=["Patient records and tie-break seed"])
            return values
        with patch("run_dynamic_experiments.catalog", declared_catalog):
            report = run_suite((42,), request_seeds=(3, 4), optimizer_seeds=(7,))
        provenance = report["cohort_inputs"][0]
        self.assertEqual(provenance["catalog_design"]["seed"], 42)
        self.assertIn("Original catalog", provenance["catalog_design_scope"])
        actual = provenance["actual_matrix_design"]
        self.assertNotIn("seed", actual)
        self.assertEqual(actual["cohort_seed"], 42)
        self.assertEqual(actual["actual_request_seeds"], [3, 4])
        self.assertEqual(actual["actual_optimizer_seeds"], [7])
        self.assertEqual(actual["held_constant"], ["Patient records"])
        for record in report["runs"]:
            executed = record["experiment"]["design"]
            self.assertEqual(executed["actual_request_seed"], record["seeds"]["request_seed"])
            self.assertEqual(executed["actual_optimizer_seed"], 7)
            self.assertNotIn("tie-break seed", " ".join(executed["held_constant"]))

    def test_cohort_confidence_interval_does_not_pseudoreplicate_request_draws(self):
        rows = [row(42, i, 10) for i in range(20)] + [row(84, 0, 100)]
        nested = aggregate_nested(rows)[0]
        metric = nested["metrics"]["full_plan_wait_hours_saved"]
        self.assertEqual(nested["paired_run_count"], 21)
        self.assertEqual(nested["cohort_count"], 2)
        self.assertEqual(metric["cohort_means"], [10, 100])
        self.assertEqual(metric["mean_of_cohort_means"], 55)
        self.assertEqual(metric["cohort_bootstrap_ci95"], [10, 100])
        repeated = aggregate_nested([row(42, 0, 10), row(84, 0, 100)])[0]
        self.assertEqual(metric, repeated["metrics"]["full_plan_wait_hours_saved"])

    def test_undefined_draw_invalidates_its_cohort_and_across_cohort_mean(self):
        nested = aggregate_nested([row(42, 0, 5), row(42, 1, None), row(84, 0, -2)])[0]
        first = nested["cohorts"][0]["metrics"]["full_plan_wait_hours_saved"]
        self.assertEqual(first["defined_run_count"], 1)
        self.assertFalse(first["complete"])
        self.assertIsNone(first["mean"])
        metric = nested["metrics"]["full_plan_wait_hours_saved"]
        self.assertEqual(metric["defined_cohort_count"], 1)
        self.assertIsNone(metric["mean_of_cohort_means"])
        self.assertIsNone(metric["cohort_bootstrap_ci95"])
        self.assertEqual(nested["cohorts"][1]["negative_savings_run_count"], 1)

    def test_duplicate_replicate_is_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_nested([row(42, 0, 5), row(42, 0, 5)])

    def test_seed_alias_conflict_and_unknown_experiment_are_rejected(self):
        with self.assertRaises(ValueError):
            run_suite(seeds=(42,), cohort_seeds=(42,))
        with patch("run_dynamic_experiments.catalog", tiny_catalog), patch("run_dynamic_experiments.compare") as simulate:
            with self.assertRaises(ValueError):
                run_suite((42,), request_seeds=(0,), experiment_ids=("unknown",))
            simulate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
