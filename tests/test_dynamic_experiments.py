"""Controlled cohorts must isolate the declared factor and stay feasible."""

from collections import Counter
from copy import deepcopy
import json
import unittest

import dynamic_experiments as experiments
from dynamic_model import Scenario, compare


EXPERIMENT_IDS = ["load_light", "load_medium", "load_heavy", "competition_within", "competition_cross"]


def options(scenario, patient, *, ignore_insurance=False, ignore_care=False):
    """Independent constraint check on public JSON, without model private APIs."""
    contracts = {snf["id"]: snf["accepted_insurances"] for snf in scenario["snfs"]}
    return [bed for bed in scenario["beds"]
            if bed["snf_id"] in patient["eligible_snf_ids"]
            and bed["gender"] in ("any", patient["gender"])
            and (ignore_insurance or "*" in contracts[bed["snf_id"]]
                 or patient["insurance_type"] in contracts[bed["snf_id"]])
            and (ignore_care or set(patient["required_capabilities"]).issubset(bed["capabilities"]))]


def type_counts(scenario):
    return Counter((bed["snf_id"], bed["gender"], tuple(bed["capabilities"])) for bed in scenario["beds"])


class DynamicExperimentTests(unittest.TestCase):
    def test_catalog_contract_and_metadata_for_every_prespecified_seed(self):
        self.assertEqual(experiments.DEFAULT_SEEDS, (42, 84, 126))
        for seed in experiments.DEFAULT_SEEDS:
            rows = experiments.catalog(seed)
            with self.subTest(seed=seed):
                self.assertEqual([row["id"] for row in rows], EXPERIMENT_IDS)
                json.dumps(rows, allow_nan=False)
                for row in rows:
                    self.assertTrue({"id", "title", "description", "changed_factor", "cohort_id", "scenario", "design"}.issubset(row))
                    scenario, design = row["scenario"], row["design"]
                    self.assertEqual(Scenario.from_dict(scenario).to_dict(), scenario)
                    self.assertEqual(scenario["baseline_policy"], "local")
                    self.assertEqual(scenario["tie_break_seed"], seed)
                    self.assertEqual(design["seed"], seed)
                    self.assertEqual(design["patient_count"], len(scenario["patients"]))
                    self.assertEqual(design["bed_count"], len(scenario["beds"]))
                    offered = sum(patient["length_of_stay_hours"] for patient in scenario["patients"])
                    capacity = len(scenario["beds"]) * scenario["horizon_hours"]
                    self.assertEqual(design["total_offered_snf_hours"], offered)
                    self.assertEqual(design["nominal_capacity_bed_hours"], capacity)
                    self.assertEqual(design["offered_load"], offered / capacity)
                    self.assertFalse(design["empirically_calibrated"])
                    self.assertTrue(design["held_constant"])
                    self.assertTrue(design["comparison_note"])

    def test_load_changes_capacity_without_regenerating_any_patient(self):
        for seed in experiments.DEFAULT_SEEDS:
            rows = experiments.catalog(seed)[:3]
            light, medium, heavy = [row["scenario"] for row in rows]
            with self.subTest(seed=seed):
                self.assertEqual(len(light["patients"]), 96)
                self.assertEqual([len(row["beds"]) for row in (light, medium, heavy)], [48, 24, 12])
                self.assertEqual(sum(patient["length_of_stay_hours"] for patient in light["patients"]), 3660)
                for scenario in (light, medium, heavy):
                    self.assertEqual(scenario["patients"], heavy["patients"])
                    self.assertEqual(scenario["horizon_hours"], 168)
                    self.assertEqual(len(scenario["hospitals"]), 4)
                    self.assertEqual(len(scenario["snfs"]), 6)
                    reduced = {key: value for key, value in scenario.items() if key not in ("name", "beds")}
                    self.assertEqual(reduced, {key: value for key, value in heavy.items() if key not in ("name", "beds")})
                self.assertEqual(len({row["cohort_id"] for row in rows}), 1)
                self.assertLess(rows[0]["design"]["offered_load"], rows[1]["design"]["offered_load"])
                self.assertLess(rows[1]["design"]["offered_load"], rows[2]["design"]["offered_load"])
                self.assertLess(rows[0]["design"]["offered_load"], 0.5)
                self.assertGreater(rows[2]["design"]["offered_load"], 1)

    def test_capacity_copies_preserve_the_complete_clinical_bed_mix(self):
        for seed in experiments.DEFAULT_SEEDS:
            rows = experiments.catalog(seed)[:3]
            heavy = rows[2]["scenario"]
            base_counts = type_counts(heavy)
            for row in rows:
                with self.subTest(seed=seed, experiment=row["id"]):
                    scenario = row["scenario"]
                    multiplier = row["design"]["capacity_multiplier"]
                    self.assertEqual(type_counts(scenario), Counter({key: value * multiplier for key, value in base_counts.items()}))
                    bed_by_id = {bed["id"]: bed for bed in scenario["beds"]}
                    self.assertEqual(len(bed_by_id), len(scenario["beds"]))
                    for bed in heavy["beds"]:
                        self.assertEqual(bed_by_id[bed["id"]], bed)

    def test_competition_pair_changes_only_hospital_assignment(self):
        for seed in experiments.DEFAULT_SEEDS:
            within_row, cross_row = experiments.catalog(seed)[3:]
            within, cross = within_row["scenario"], cross_row["scenario"]
            with self.subTest(seed=seed):
                self.assertEqual(len(within["patients"]), 24)
                self.assertEqual(within_row["cohort_id"], cross_row["cohort_id"])
                self.assertEqual({p["hospital_id"] for p in within["patients"]}, {"Hospital_A"})
                self.assertEqual(Counter(p["hospital_id"] for p in cross["patients"]),
                                 Counter({hospital: 6 for hospital in experiments.HOSPITAL_IDS}))
                for original, reassigned in zip(within["patients"], cross["patients"]):
                    self.assertEqual({key: value for key, value in original.items() if key != "hospital_id"},
                                     {key: value for key, value in reassigned.items() if key != "hospital_id"})
                self.assertEqual({key: value for key, value in within.items() if key not in ("name", "patients")},
                                 {key: value for key, value in cross.items() if key not in ("name", "patients")})
                self.assertEqual(within_row["design"]["active_hospital_count"], 1)
                self.assertEqual(cross_row["design"]["active_hospital_count"], 4)

    def test_every_patient_has_a_structurally_feasible_bed_at_every_capacity(self):
        for seed in experiments.DEFAULT_SEEDS:
            for row in experiments.catalog(seed):
                with self.subTest(seed=seed, experiment=row["id"]):
                    for patient in row["scenario"]["patients"]:
                        self.assertTrue(options(row["scenario"], patient), msg=patient["id"])

    def test_competition_has_a_real_flexible_externality_and_independent_hard_constraints(self):
        for seed in experiments.DEFAULT_SEEDS:
            row = experiments.catalog(seed)[4]
            scenario = row["scenario"]
            patients = {patient["id"]: patient for patient in scenario["patients"]}
            roles = row["design"]["roles"]
            with self.subTest(seed=seed):
                self.assertEqual({role: len(ids) for role, ids in roles.items()},
                                 {"flexible": 8, "scarce_dependent": 8, "background": 8})
                for flexible_id, scarce_id in zip(roles["flexible"], roles["scarce_dependent"]):
                    flexible, scarce = patients[flexible_id], patients[scarce_id]
                    self.assertEqual({bed["snf_id"] for bed in options(scenario, flexible)}, {"SNF1", "SNF2"})
                    self.assertEqual({bed["snf_id"] for bed in options(scenario, scarce)}, {"SNF1"})
                    self.assertEqual({bed["snf_id"] for bed in options(scenario, scarce, ignore_insurance=True)}, {"SNF1", "SNF3"})
                    self.assertEqual({bed["snf_id"] for bed in options(scenario, scarce, ignore_care=True)}, {"SNF1", "SNF2"})
                    self.assertEqual(scarce["ready_hour"] - flexible["ready_hour"], 1)
                    self.assertNotEqual(flexible["hospital_id"], scarce["hospital_id"])

    def test_seed_reproducibility_and_editable_results_do_not_share_state(self):
        original = experiments.catalog(42)
        self.assertEqual(original, experiments.catalog(42))
        self.assertEqual(original, experiments.build_experiments(42))
        other = experiments.catalog(84)
        self.assertNotEqual(original[0]["scenario"]["patients"], other[0]["scenario"]["patients"])
        mutated = deepcopy(original)
        mutated[0]["scenario"]["patients"][0]["insurance_type"] = "consumer_edit"
        self.assertEqual(mutated[1:], original[1:])
        self.assertEqual(experiments.catalog(42), original)
        fresh = experiments.catalog(42)
        fresh[0]["scenario"]["patients"][0]["insurance_type"] = "direct_edit"
        self.assertEqual(fresh[1:], original[1:])
        self.assertEqual(experiments.catalog(42), original)

    def test_standard_competition_batch_runs_without_modifying_inputs_or_weakening_baseline(self):
        # Check all prespecified seeds, not a selected favorable result. No gain is asserted.
        for seed in experiments.DEFAULT_SEEDS:
            for row in experiments.catalog(seed)[3:]:
                with self.subTest(seed=seed, experiment=row["id"]):
                    before = deepcopy(row["scenario"])
                    report = compare(row["scenario"])
                    self.assertEqual(row["scenario"], before)
                    self.assertEqual(report["scenario"]["baseline_policy"], "local")
                    for policy in ("baseline", "ccn"):
                        self.assertEqual(report[policy]["summary"]["patient_count"], 24)
                        self.assertEqual(report[policy]["summary"]["structurally_unplaceable_patient_ids"], [])
                    json.dumps(report, allow_nan=False)

    def test_seed_validation(self):
        for seed in (-1, 2**32, True, False, 42.0, "42", None):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    experiments.catalog(seed)


if __name__ == "__main__":
    unittest.main()
