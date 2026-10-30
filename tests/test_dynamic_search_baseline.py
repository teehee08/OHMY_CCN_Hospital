"""Independent search, rational local bed ties, and objective-priority checks."""

from copy import deepcopy
from dataclasses import replace
import itertools
import math
import unittest

from dynamic_experiments import catalog
from dynamic_model import (Booking, CCN_SEARCH_SEED, Scenario, _capacity_objective,
                           _ccn, _local_baseline, _objective, _options, _schedule,
                           compare, validate_plan)
from dynamic_scenarios import generate


def patient(identifier, *, hospital="A", ready=0, stay=2, care=(), gender="female"):
    return {"id": identifier, "hospital_id": hospital, "gender": gender,
            "ready_hour": ready, "length_of_stay_hours": stay,
            "eligible_snf_ids": ["special", "ordinary"],
            "required_capabilities": list(care)}


def scenario(patients):
    return {"name": "Capacity-preserving local planning", "arrival_span_hours": 24,
            "hospitals": [{"id": "A"}, {"id": "B"}],
            "snfs": [{"id": "special"}, {"id": "ordinary"}],
            "beds": [{"id": "special-bed", "snf_id": "special", "gender": "any",
                      "capabilities": ["oxygen"]},
                     {"id": "ordinary-bed", "snf_id": "ordinary", "gender": "female",
                      "capabilities": []}],
            "patients": patients}


def independent_local_oracle(data):
    """Enumerate assignments and bed sequences, then score directly from inputs."""
    patients, beds = data["patients"], data["beds"]
    choices = [[b for b, bed in enumerate(beds)
                if bed["snf_id"] in p["eligible_snf_ids"]
                and bed["gender"] in ("any", p["gender"])
                and set(p["required_capabilities"]) <= set(bed["capabilities"])]
               for p in patients]
    best = (math.inf, math.inf, math.inf)
    for assignment in itertools.product(*choices):
        groups = [[i for i, b in enumerate(assignment) if b == bed] for bed in range(len(beds))]
        for orders in itertools.product(*(itertools.permutations(group) for group in groups)):
            waiting = 0
            for order in orders:
                free = 0
                for i in order:
                    p = patients[i]
                    start = max(p["ready_hour"], free)
                    waiting += start - p["ready_hour"]
                    free = start + p["length_of_stay_hours"]
            unused = sum(len(set(beds[b]["capabilities"]) - set(p["required_capabilities"]))
                         * p["length_of_stay_hours"] for p, b in zip(patients, assignment))
            flexible = sum(p["length_of_stay_hours"] for p, b in zip(patients, assignment)
                           if beds[b]["gender"] == "any")
            best = min(best, (waiting, unused, flexible))
    return best


class IndependentGlobalSearchTests(unittest.TestCase):
    def test_optimizer_seed_is_validated_defaulted_and_exported(self):
        data = scenario([patient("p")])
        self.assertEqual(Scenario.from_dict(data).optimizer_seed, CCN_SEARCH_SEED)
        for value in (0, 2**32 - 1):
            data["optimizer_seed"] = value
            canonical = Scenario.from_dict(data).to_dict()
            self.assertEqual(canonical["optimizer_seed"], value)
            self.assertEqual(compare(canonical)["ccn"]["solver"]["seed"], value)
        for value in (True, -1, 2**32, 1.5, "42", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "optimizer_seed"):
                Scenario.from_dict({**data, "optimizer_seed": value})

    def test_twenty_arbitration_seeds_do_not_change_fixed_medium_global_search(self):
        data = next(row["scenario"] for row in catalog(42) if row["id"] == "load_medium")
        search_objectives = set()
        baseline_objectives = set()
        global_assignments = set()
        for seed in range(20):
            with self.subTest(seed=seed):
                result = compare({**data, "tie_break_seed": seed})
                solver = result["ccn"]["solver"]
                self.assertTrue(solver["search_independent_of_baseline"])
                self.assertEqual(solver["seed"], CCN_SEARCH_SEED)
                self.assertEqual(solver["candidate_budget"], 1000)
                self.assertEqual(solver["candidates_evaluated"], 1000)
                search_objectives.add(solver["search_objective_hours"])
                baseline_objectives.add(result["baseline"]["summary"]["full_plan_wait_hours"])
                self.assertLessEqual(solver["objective_hours"], solver["baseline_objective_hours"])
                if not solver["baseline_fallback_used"]:
                    global_assignments.add(tuple((row["patient_id"], row["bed_id"], row["admission_hour"])
                                                 for row in result["ccn"]["patients"]))
        self.assertGreater(len(baseline_objectives), 1)
        self.assertEqual(len(search_objectives), 1)
        self.assertEqual(len(global_assignments), 1)

    def test_different_feasible_fallbacks_do_not_change_exploration_and_best_is_retained(self):
        s = Scenario.from_dict(next(row["scenario"] for row in catalog(42) if row["id"] == "load_medium"))
        choices = _options(s)
        ordinary, _, _ = _local_baseline(s, choices)
        delayed = {i: Booking(b.bed, b.start + 100, b.end + 100) for i, b in ordinary.items()}
        validate_plan(s, delayed)
        first, first_meta = _ccn(s, choices, ordinary)
        second, second_meta = _ccn(s, choices, delayed)
        self.assertEqual(first, second)
        self.assertEqual(first_meta["search_objective_hours"], second_meta["search_objective_hours"])
        # An independently found stronger feasible plan must remain available
        # as a fallback without changing the default-seed exploration path.
        stronger, _ = _ccn(replace(s, optimizer_seed=2), choices, ordinary)
        self.assertLess(_objective(s, stronger), first_meta["search_objective_hours"])
        retained, retained_meta = _ccn(s, choices, stronger)
        self.assertEqual(retained_meta["search_objective_hours"], first_meta["search_objective_hours"])
        self.assertTrue(retained_meta["baseline_fallback_used"])
        self.assertEqual(retained_meta["selected_source"], "baseline_fallback")
        self.assertEqual(retained, stronger)
        validate_plan(s, retained)


class RationalHospitalBaselineTests(unittest.TestCase):
    def test_ordinary_patient_preserves_special_bed_despite_snf_input_preference(self):
        data = scenario([patient("ordinary", stay=8),
                         patient("other-hospital", hospital="B", ready=1, stay=3, care=("oxygen",))])
        result = compare(data)
        ordinary = result["baseline"]["patients"][0]
        self.assertEqual(ordinary["bed_id"], "ordinary-bed")
        self.assertEqual(result["baseline"]["summary"]["full_plan_wait_hours"], 0)
        solver = result["baseline"]["solver"]
        self.assertEqual(solver["capacity_information_scope"], "bed_attributes_and_own_patients_only")
        self.assertEqual(solver["secondary_priority"], "only_among_equal_total_wait_plans")

    def test_same_care_uses_gender_specific_bed_before_flexible_gender_bed(self):
        data = scenario([patient("ordinary")])
        data["beds"][0]["capabilities"] = []
        result = compare(data)
        self.assertEqual(result["baseline"]["patients"][0]["bed_id"], "ordinary-bed")

    def test_private_future_demand_cannot_change_other_hospitals_local_plan(self):
        data = scenario([patient("own", ready=0, stay=8),
                         patient("private", hospital="B", ready=20, stay=5, care=("oxygen",))])
        reduced = deepcopy(data)
        reduced["patients"] = reduced["patients"][:1]
        changed = deepcopy(data)
        changed["patients"][1].update(ready_hour=100, length_of_stay_hours=100,
                                      required_capabilities=[], gender="male")
        plans = []
        for variant in (data, reduced, changed):
            s = Scenario.from_dict(variant)
            choices = _options(s)
            plan, _ = _schedule(s, choices, [0], preserve_capacity=True)
            actual, _, _ = _local_baseline(s, choices)
            self.assertEqual(actual[0], plan[0])
            plans.append(plan)
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[0], plans[2])

    def test_capacity_preference_never_replaces_a_better_primary_objective(self):
        data = scenario([patient("one", stay=8), patient("two", stay=8)])
        for p in data["patients"]:
            p["hospital_id"] = "A"
        report = compare(data)
        self.assertEqual(report["baseline"]["summary"]["full_plan_wait_hours"], 0)
        self.assertEqual({row["bed_id"] for row in report["baseline"]["patients"]},
                         {"ordinary-bed", "special-bed"})

    def test_small_local_lexicographic_objective_matches_independent_oracle(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                patients = [patient(str(i), ready=(seed + i) % 3,
                                    stay=1 + (seed * 3 + i * 2) % 5,
                                    care=("oxygen",) if (seed + i) % 3 == 0 else ()) for i in range(4)]
                data = scenario(patients)
                s = Scenario.from_dict(data)
                plan, metadata = _schedule(s, _options(s), list(range(4)), preserve_capacity=True)
                self.assertEqual(metadata["status"], "optimal")
                self.assertEqual((_objective(s, plan), *_capacity_objective(s, plan)),
                                 independent_local_oracle(data))
                self.assertEqual(compare(data)["ccn"]["summary"]["full_plan_wait_hours"],
                                 independent_local_oracle(data)[0])

    def test_large_local_candidates_retain_the_original_six_primary_plans(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                data = generate({"patient_count": 16, "hospital_count": 1,
                                 "snf_count": 3, "bed_count": 5,
                                 "arrival_span_hours": 24, "seed": seed})
                s = Scenario.from_dict(data)
                choices = _options(s)
                original, original_info = _schedule(s, choices, list(range(len(s.patients))))
                rational, rational_info = _schedule(s, choices, list(range(len(s.patients))), preserve_capacity=True)
                self.assertLessEqual(_objective(s, rational), _objective(s, original))
                self.assertEqual(original_info["construction_limit"], 6)
                self.assertEqual(rational_info["construction_limit"], 12)


if __name__ == "__main__":
    unittest.main()
