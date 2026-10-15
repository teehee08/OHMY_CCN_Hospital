"""Behavioral checks for the static hospital-to-SNF allocation experiment."""

from copy import deepcopy
import json
from pathlib import Path
import random
import tempfile
import unittest

from phase1 import Scenario, global_allocate, greedy_allocate, load_scenarios


def scenario_data(name="example", beds=(1, 1), patient_order=("a", "b")):
    return {
        "name": name,
        "hospitals": [{"id": "A"}, {"id": "B"}],
        "snfs": [
            {"id": "X", "available_beds": beds[0]},
            {"id": "Y", "available_beds": beds[1]},
        ],
        "patients": [
            {"id": patient_id, "hospital_id": patient_id.upper()}
            for patient_id in patient_order
        ],
        "travel_time_minutes": {"A": {"X": 4, "Y": 5}, "B": {"X": 5, "Y": 14}},
    }


def exhaustive_optimum(data):
    """Enumerate all feasible assignments independently of the solver."""
    patients = data["patients"]
    remaining = {snf["id"]: snf["available_beds"] for snf in data["snfs"]}
    costs = data["travel_time_minutes"]
    best = (0, 0.0)

    def visit(index, placed, total):
        nonlocal best
        if index == len(patients):
            if placed > best[0] or (placed == best[0] and total < best[1]):
                best = (placed, total)
            return
        visit(index + 1, placed, total)
        hospital = patients[index]["hospital_id"]
        for snf_id in remaining:
            if remaining[snf_id] > 0:
                remaining[snf_id] -= 1
                visit(index + 1, placed + 1, total + costs[hospital][snf_id])
                remaining[snf_id] += 1

    visit(0, 0, 0.0)
    return best


class AllocationTests(unittest.TestCase):
    def assert_feasible(self, data, result):
        patients = {patient["id"]: patient["hospital_id"] for patient in data["patients"]}
        capacity = {snf["id"]: snf["available_beds"] for snf in data["snfs"]}
        used = {snf_id: 0 for snf_id in capacity}
        assigned = []
        total = 0.0
        for assignment in result.assignments:
            self.assertIn(assignment.patient_id, patients)
            self.assertEqual(assignment.hospital_id, patients[assignment.patient_id])
            self.assertIn(assignment.snf_id, capacity)
            expected_cost = data["travel_time_minutes"][assignment.hospital_id][assignment.snf_id]
            self.assertAlmostEqual(assignment.travel_time_minutes, expected_cost)
            used[assignment.snf_id] += 1
            assigned.append(assignment.patient_id)
            total += expected_cost
        self.assertEqual(len(assigned), len(set(assigned)))
        unplaced = list(result.unplaced_patient_ids)
        self.assertEqual(len(unplaced), len(set(unplaced)))
        self.assertFalse(set(assigned) & set(unplaced))
        self.assertEqual(set(assigned) | set(unplaced), set(patients))
        for snf_id, count in used.items():
            self.assertLessEqual(count, capacity[snf_id])
        self.assertEqual(result.placed_count, len(assigned))
        self.assertAlmostEqual(result.total_travel_time_minutes, total)

    def test_documented_allocation_examples(self):
        cases = [
            ("ample_capacity", (2, 2), ("a", "b"), 9, 9),
            ("a_first", (1, 1), ("a", "b"), 18, 10),
            ("b_first", (1, 1), ("b", "a"), 10, 10),
        ]
        for name, beds, order, greedy_cost, global_cost in cases:
            with self.subTest(name=name):
                data = scenario_data(name, beds, order)
                scenario = Scenario.from_dict(data)
                for allocator, expected in [(greedy_allocate, greedy_cost), (global_allocate, global_cost)]:
                    result = allocator(scenario)
                    self.assert_feasible(data, result)
                    self.assertEqual(result.placed_count, 2)
                    self.assertAlmostEqual(result.total_travel_time_minutes, expected)

    def test_global_can_reassign_multiple_earlier_choices(self):
        data = {
            "name": "rerouting_chain",
            "hospitals": [{"id": hospital} for hospital in ("A", "B", "C")],
            "snfs": [{"id": snf, "available_beds": 1} for snf in ("X", "Y", "Z")],
            "patients": [
                {"id": "p" + hospital, "hospital_id": hospital} for hospital in ("A", "B", "C")
            ],
            "travel_time_minutes": {
                "A": {"X": 1, "Y": 2, "Z": 100},
                "B": {"X": 100, "Y": 1, "Z": 2},
                "C": {"X": 2, "Y": 100, "Z": 100},
            },
        }
        result = global_allocate(Scenario.from_dict(data))
        self.assert_feasible(data, result)
        self.assertEqual(result.total_travel_time_minutes, 6)
        self.assertEqual(
            {assignment.patient_id: assignment.snf_id for assignment in result.assignments},
            {"pA": "Y", "pB": "Z", "pC": "X"},
        )

    def test_zero_capacity(self):
        data = scenario_data(beds=(0, 0))
        for allocator in (greedy_allocate, global_allocate):
            with self.subTest(allocator=allocator.__name__):
                result = allocator(Scenario.from_dict(data))
                self.assert_feasible(data, result)
                self.assertEqual(result.placed_count, 0)
                self.assertEqual(result.total_travel_time_minutes, 0)

    def test_capacity_shortage_selects_minimum_cost_patient_subset(self):
        data = scenario_data()
        data["hospitals"].append({"id": "C"})
        data["patients"].insert(0, {"id": "c", "hospital_id": "C"})
        data["travel_time_minutes"] = {
            "A": {"X": 1, "Y": 2},
            "B": {"X": 2, "Y": 3},
            "C": {"X": 20, "Y": 30},
        }
        scenario = Scenario.from_dict(data)
        greedy_result = greedy_allocate(scenario)
        global_result = global_allocate(scenario)
        self.assert_feasible(data, greedy_result)
        self.assert_feasible(data, global_result)
        self.assertEqual(greedy_result.placed_count, 2)
        self.assertEqual(global_result.placed_count, 2)
        self.assertEqual(global_result.total_travel_time_minutes, 4)
        self.assertEqual(set(global_result.unplaced_patient_ids), {"c"})

    def test_placement_count_has_priority_over_travel_cost(self):
        data = scenario_data(beds=(1, 0), patient_order=("a",))
        data["travel_time_minutes"]["A"]["X"] = 1_000_000
        result = global_allocate(Scenario.from_dict(data))
        self.assert_feasible(data, result)
        self.assertEqual(result.placed_count, 1)
        self.assertEqual(result.total_travel_time_minutes, 1_000_000)

    def test_equal_costs_and_zero_costs_are_valid(self):
        data = scenario_data()
        data["travel_time_minutes"] = {"A": {"X": 0, "Y": 0}, "B": {"X": 0, "Y": 0}}
        for allocator in (greedy_allocate, global_allocate):
            with self.subTest(allocator=allocator.__name__):
                result = allocator(Scenario.from_dict(data))
                self.assert_feasible(data, result)
                self.assertEqual(result.placed_count, 2)
                self.assertEqual(result.total_travel_time_minutes, 0)

    def test_empty_system(self):
        data = {"name": "empty", "hospitals": [], "snfs": [], "patients": [], "travel_time_minutes": {}}
        for allocator in (greedy_allocate, global_allocate):
            with self.subTest(allocator=allocator.__name__):
                result = allocator(Scenario.from_dict(data))
                self.assert_feasible(data, result)
                self.assertEqual(result.placed_count, 0)

    def test_global_matches_exhaustive_search_on_small_random_instances(self):
        rng = random.Random(20261014)
        for case in range(64):
            hospitals = ["H" + str(index) for index in range(rng.randint(1, 3))]
            snfs = ["S" + str(index) for index in range(rng.randint(1, 3))]
            denominator = 4 if case % 2 == 0 else 10
            data = {
                "name": "random_" + str(case),
                "hospitals": [{"id": hospital} for hospital in hospitals],
                "snfs": [{"id": snf, "available_beds": rng.randint(0, 2)} for snf in snfs],
                "patients": [
                    {"id": "p" + str(index), "hospital_id": rng.choice(hospitals)}
                    for index in range(rng.randint(0, 5))
                ],
                "travel_time_minutes": {
                    hospital: {snf: rng.randint(0, 120) / denominator for snf in snfs} for hospital in hospitals
                },
            }
            with self.subTest(case=case):
                before = deepcopy(data)
                scenario = Scenario.from_dict(data)
                result = global_allocate(scenario)
                expected_count, expected_cost = exhaustive_optimum(data)
                self.assert_feasible(data, result)
                self.assertEqual(result.placed_count, expected_count)
                self.assertAlmostEqual(result.total_travel_time_minutes, expected_cost)
                # Reusing an input must not inherit bed consumption from a previous run.
                repeated = global_allocate(scenario)
                self.assert_feasible(data, repeated)
                self.assertEqual(repeated.placed_count, expected_count)
                self.assertAlmostEqual(repeated.total_travel_time_minutes, expected_cost)
                self.assertEqual(data, before)


class InputValidationTests(unittest.TestCase):
    def test_invalid_capacities(self):
        for beds in (-1, 1.5, True):
            with self.subTest(beds=beds):
                data = scenario_data()
                data["snfs"][0]["available_beds"] = beds
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)

    def test_duplicate_ids(self):
        for collection in ("hospitals", "snfs", "patients"):
            with self.subTest(collection=collection):
                data = scenario_data()
                data[collection].append(deepcopy(data[collection][0]))
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)

    def test_unknown_patient_hospital(self):
        data = scenario_data()
        data["patients"][0]["hospital_id"] = "unknown"
        with self.assertRaises(ValueError):
            Scenario.from_dict(data)

    def test_travel_matrix_must_cover_every_hospital_snf_pair(self):
        for missing_row in (False, True):
            with self.subTest(missing_row=missing_row):
                data = scenario_data()
                if missing_row:
                    del data["travel_time_minutes"]["A"]
                else:
                    del data["travel_time_minutes"]["A"]["X"]
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)

    def test_invalid_travel_times(self):
        for cost in (-1, float("inf"), float("nan"), True, "5"):
            with self.subTest(cost=cost):
                data = scenario_data()
                data["travel_time_minutes"]["A"]["X"] = cost
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)

    def test_load_versioned_scenario_file(self):
        payload = {"schema_version": 1, "scenarios": [scenario_data("first"), scenario_data("second", (2, 2))]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenarios.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            scenarios = load_scenarios(path)
        self.assertEqual([scenario.name for scenario in scenarios], ["first", "second"])
        self.assertEqual(global_allocate(scenarios[0]).total_travel_time_minutes, 10)
        self.assertEqual(global_allocate(scenarios[1]).total_travel_time_minutes, 9)


if __name__ == "__main__":
    unittest.main()
