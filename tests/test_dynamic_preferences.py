"""Preference sensitivity must preserve the physical scheduling problem."""

from copy import deepcopy
import json
import unittest

from dynamic_experiments import catalog, preference_variant


def compatible_beds(scenario, patient):
    contracts = {snf["id"]: snf["accepted_insurances"] for snf in scenario["snfs"]}
    return {bed["id"] for bed in scenario["beds"]
            if bed["snf_id"] in patient["eligible_snf_ids"]
            and bed["gender"] in (patient["gender"], "any")
            and ("*" in contracts[bed["snf_id"]] or patient["insurance_type"] in contracts[bed["snf_id"]])
            and set(patient["required_capabilities"]) <= set(bed["capabilities"])}


class PreferenceSensitivityTests(unittest.TestCase):
    def test_each_variant_preserves_every_physical_input_and_feasible_set(self):
        for experiment in catalog(42):
            original = deepcopy(experiment)
            for mode in ("supplied", "ordinary_first", "shuffled"):
                with self.subTest(experiment=experiment["id"], mode=mode):
                    result = preference_variant(experiment, mode, 19)
                    a, b = original["scenario"], result["scenario"]
                    self.assertEqual({k: v for k, v in a.items() if k != "patients"},
                                     {k: v for k, v in b.items() if k != "patients"})
                    for left, right in zip(a["patients"], b["patients"]):
                        self.assertEqual({k: v for k, v in left.items() if k != "eligible_snf_ids"},
                                         {k: v for k, v in right.items() if k != "eligible_snf_ids"})
                        self.assertEqual(set(left["eligible_snf_ids"]), set(right["eligible_snf_ids"]))
                        self.assertEqual(compatible_beds(a, left), compatible_beds(b, right))
                    self.assertEqual(result["cohort_id"], original["cohort_id"])
                    self.assertTrue(result["design"]["compatible_bed_sets_preserved"])
                    json.dumps(result, allow_nan=False)
            self.assertEqual(experiment, original)

    def test_ordinary_first_exercises_the_identified_competition_sensitivity(self):
        original = catalog(42)[4]
        variant = preference_variant(original, "ordinary_first")
        flexible = set(original["design"]["roles"]["flexible"])
        for patient in variant["scenario"]["patients"]:
            if patient["id"] in flexible:
                self.assertEqual(patient["eligible_snf_ids"][0], "SNF2")
        self.assertEqual(variant["id"], "competition_cross__ordinary_first")
        self.assertEqual(variant["base_experiment_id"], "competition_cross")
        self.assertGreater(variant["design"]["preference_changed_patient_count"], 0)

    def test_shuffle_has_its_own_seed_and_does_not_break_capacity_pairing(self):
        originals = catalog(84)[:3]
        first = [preference_variant(e, "shuffled", 5) for e in originals]
        second = [preference_variant(e, "shuffled", 5) for e in originals]
        self.assertEqual(first, second)
        self.assertEqual(first[0]["scenario"]["patients"], first[1]["scenario"]["patients"])
        self.assertEqual(first[1]["scenario"]["patients"], first[2]["scenario"]["patients"])
        self.assertNotEqual(first[0]["scenario"]["patients"],
                            preference_variant(originals[0], "shuffled", 6)["scenario"]["patients"])
        self.assertEqual(first[0]["scenario"]["tie_break_seed"], originals[0]["scenario"]["tie_break_seed"])

    def test_variants_return_independent_data_and_validate_seeds(self):
        original = catalog(42)[4]
        supplied = preference_variant(original)
        self.assertEqual(supplied["id"], original["id"])
        self.assertEqual(supplied["scenario"], original["scenario"])
        supplied["scenario"]["patients"][0]["eligible_snf_ids"].clear()
        self.assertTrue(original["scenario"]["patients"][0]["eligible_snf_ids"])
        with self.assertRaises(ValueError):
            preference_variant(original, "pick_a_winner")
        for bad in (-1, 2**32, True, "42"):
            with self.subTest(seed=bad), self.assertRaises(ValueError):
                preference_variant(original, "shuffled", bad)


if __name__ == "__main__":
    unittest.main()
