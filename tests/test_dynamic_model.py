"""Behavioral checks for dynamic placement, horizon accounting, and scheduling."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from dynamic_model import Scenario, compare


ROOT = Path(__file__).parents[1]


def bed(bed_id, snf_id="S1", gender="any"):
    return {"id": bed_id, "snf_id": snf_id, "gender": gender}


def patient(patient_id, ready=0, stay=2, eligible=("S1",), gender="female", hospital="A"):
    return {
        "id": patient_id,
        "hospital_id": hospital,
        "gender": gender,
        "ready_hour": ready,
        "length_of_stay_hours": stay,
        "eligible_snf_ids": list(eligible),
    }


def scenario_data(patients, beds=None, horizon=24, name="dynamic_regression"):
    beds = [bed("b1")] if beds is None else beds
    return {
        "schema_version": 1,
        # Keep legacy FCFS regression cases explicit as the default policy evolves.
        "baseline_policy": "fcfs",
        "name": name,
        "horizon_hours": horizon,
        "hospitals": [{"id": "A"}, {"id": "B"}],
        "snfs": [{"id": snf_id} for snf_id in dict.fromkeys(item["snf_id"] for item in beds)],
        "beds": deepcopy(beds),
        "patients": deepcopy(patients),
    }


def patients_by_id(result):
    return {item["patient_id"]: item for item in result["patients"]}


class DynamicSchedulingTests(unittest.TestCase):
    def test_default_five_patient_case_has_hand_checked_waits(self):
        document = json.loads((ROOT / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8-sig"))
        data = next(row for row in document["scenarios"] if len(row["patients"]) == 5)
        data["baseline_policy"] = "fcfs"
        report = compare(data)
        baseline = patients_by_id(report["baseline"])
        ccn = patients_by_id(report["ccn"])

        self.assertEqual(set(baseline), {"P1", "P2", "P3", "P4", "P5"})
        self.assertEqual({key: value["wait_hours"] for key, value in baseline.items()},
                         {"P1": 0, "P2": 24, "P3": 0, "P4": 0, "P5": 28})
        self.assertEqual({key: value["admission_hour"] for key, value in baseline.items()},
                         {"P1": 10, "P2": 34, "P3": 15, "P4": 33, "P5": 63})
        self.assertEqual({key: value["wait_hours"] for key, value in ccn.items()},
                         {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0})
        self.assertEqual(ccn["P3"]["snf_id"], "SNF3")
        self.assertEqual(report["baseline"]["summary"]["full_plan_wait_hours"], 52)
        self.assertEqual(report["ccn"]["summary"]["full_plan_wait_hours"], 0)
        self.assertEqual(report["baseline"]["summary"]["blocked_bed_hours"], 52)
        self.assertEqual(report["ccn"]["summary"]["blocked_bed_hours"], 0)
        self.assertEqual(report["baseline"]["summary"]["snf_occupied_bed_hours"], 143)
        self.assertEqual(report["ccn"]["summary"]["snf_occupied_bed_hours"], 168)
        self.assertEqual(report["baseline"]["summary"]["completed_count"], 3)
        self.assertEqual(report["ccn"]["summary"]["completed_count"], 5)

    def test_fifo_uses_readiness_not_json_order_and_los_starts_at_admission(self):
        data = scenario_data([
            patient("late", ready=5, stay=7),
            patient("first", ready=1, stay=5),
            patient("second", ready=2, stay=2),
        ], horizon=20)
        result = patients_by_id(compare(data)["baseline"])

        self.assertEqual((result["first"]["admission_hour"], result["first"]["release_hour"]), (1, 6))
        self.assertEqual((result["second"]["admission_hour"], result["second"]["release_hour"]), (6, 8))
        self.assertEqual((result["late"]["admission_hour"], result["late"]["release_hour"]), (8, 15))
        self.assertEqual(result["second"]["wait_hours"], 4)
        self.assertEqual(result["late"]["wait_hours"], 3)

    def test_same_hour_release_is_reused_and_horizon_admission_has_zero_area(self):
        data = scenario_data([
            patient("first", ready=0, stay=4),
            patient("second", ready=4, stay=2),
        ], horizon=4)
        report = compare(data)
        for strategy in ("baseline", "ccn"):
            with self.subTest(strategy=strategy):
                result = report[strategy]
                records = patients_by_id(result)
                self.assertEqual(records["first"]["status"], "completed")
                self.assertEqual(records["second"]["admission_hour"], 4)
                self.assertIsNone(records["second"]["release_hour"])
                self.assertEqual(records["second"]["planned_release_hour"], 6)
                self.assertEqual(records["second"]["status"], "in_snf")
                self.assertEqual(records["first"]["bed_id"], records["second"]["bed_id"])
                self.assertEqual(result["summary"]["placed_count"], 2)
                self.assertEqual(result["summary"]["completed_count"], 1)
                self.assertEqual(result["summary"]["snf_occupied_bed_hours"], 4)
                self.assertEqual(result["summary"]["blocked_bed_hours"], 0)
                self.assertEqual(result["summary"]["snf_utilization"], 1)
                at_boundary = [item["type"] for item in result["events"]
                               if item["time_hour"] == 4 and item["type"] in ("admit", "release")]
                self.assertLess(at_boundary.index("release"), at_boundary.index("admit"))
                final = result["snapshots"][-1]
                self.assertEqual(final["time_hour"], 4)
                self.assertEqual(final["occupied_count"], 1)
                self.assertEqual(final["completed_count"], 1)

    def test_horizon_censors_waiting_but_preserves_the_eventual_plan(self):
        data = scenario_data([
            patient("first", ready=0, stay=10),
            patient("waiting", ready=1, stay=5),
        ], horizon=3)
        result = compare(data)["baseline"]
        waiting = patients_by_id(result)["waiting"]

        self.assertEqual(waiting["status"], "waiting")
        self.assertIsNone(waiting["admission_hour"])
        self.assertIsNone(waiting["release_hour"])
        self.assertEqual(waiting["planned_admission_hour"], 10)
        self.assertEqual(waiting["planned_release_hour"], 15)
        self.assertEqual(waiting["wait_hours"], 9)
        self.assertEqual(waiting["wait_in_window_hours"], 2)
        self.assertEqual(result["summary"]["blocked_bed_hours"], 2)
        self.assertEqual(result["summary"]["mean_observed_wait_hours"], 1)
        self.assertEqual(result["summary"]["mean_observed_wait_hours"],
                         result["summary"]["blocked_bed_hours"] / result["summary"]["patient_count"])
        self.assertEqual(result["summary"]["mean_wait_hours"], 0)
        self.assertEqual(result["summary"]["full_plan_wait_hours"], 9)
        self.assertEqual(result["summary"]["waiting_count"], 1)
        self.assertEqual(result["summary"]["placed_count"], 1)
        self.assertEqual(result["summary"]["snf_occupied_bed_hours"], 3)
        self.assertTrue(all(0 <= item["time_hour"] <= 3 for item in result["events"]))

    def test_offline_ccn_can_wait_for_known_future_demand_to_reduce_total_wait(self):
        # Long-first incurs 9 hours; short-first, after one idle hour, incurs 2.
        data = scenario_data([
            patient("long", ready=0, stay=10),
            patient("short", ready=1, stay=1),
        ], horizon=20)
        report = compare(data)
        baseline = patients_by_id(report["baseline"])
        ccn = patients_by_id(report["ccn"])

        self.assertEqual(baseline["long"]["admission_hour"], 0)
        self.assertEqual(baseline["short"]["admission_hour"], 10)
        self.assertEqual(ccn["short"]["admission_hour"], 1)
        self.assertEqual(ccn["long"]["admission_hour"], 2)
        self.assertEqual(report["baseline"]["summary"]["full_plan_wait_hours"], 9)
        self.assertEqual(report["ccn"]["summary"]["full_plan_wait_hours"], 2)
        self.assertEqual(report["ccn"]["solver"]["status"], "optimal")

    def test_search_budget_exhaustion_returns_a_feasible_nonworsening_incumbent(self):
        data = scenario_data([
            patient("long", ready=0, stay=10),
            patient("short", ready=1, stay=1),
        ], horizon=20)
        with patch("dynamic_model.SEARCH_NODE_LIMIT", 0):
            report = compare(data)

        ccn = report["ccn"]
        self.assertEqual(ccn["solver"]["status"], "search_limited")
        self.assertEqual(ccn["solver"]["nodes"], 0)
        self.assertGreater(ccn["summary"]["full_plan_wait_hours"], 0)
        self.assertLessEqual(ccn["summary"]["full_plan_wait_hours"],
                             report["baseline"]["summary"]["full_plan_wait_hours"])
        self.assertEqual(ccn["summary"]["placed_count"], 2)
        self.assertEqual(ccn["summary"]["structurally_unplaceable_patient_ids"], [])
        self.assertEqual(ccn["solver"]["objective_hours"], ccn["summary"]["full_plan_wait_hours"])
        self._assert_conservation(data, ccn)

    def test_unready_future_demand_does_not_change_earlier_baseline_decisions(self):
        future_ready = 6
        data = scenario_data([
            patient("future", ready=future_ready, stay=1, eligible=("S2",)),
            patient("flexible", ready=0, stay=8, eligible=("S2", "S1")),
            patient("restricted", ready=1, stay=2, eligible=("S2",)),
            patient("other_hospital", ready=2, stay=1, eligible=("S1",), hospital="B"),
        ], beds=[bed("one", "S1"), bed("two", "S2")], horizon=20)
        without_future = deepcopy(data)
        without_future["patients"] = [item for item in data["patients"] if item["ready_hour"] < future_ready]
        full = compare(data)["baseline"]
        reduced = compare(without_future)["baseline"]

        def early_decisions(result):
            return sorted((item["patient_id"], item["bed_id"], item["snf_id"],
                           item["planned_admission_hour"], item["planned_release_hour"])
                          for item in result["patients"]
                          if item["planned_admission_hour"] is not None
                          and item["planned_admission_hour"] < future_ready)

        self.assertEqual(early_decisions(full), early_decisions(reduced))
        self.assertEqual([item[0] for item in early_decisions(full)], ["flexible", "other_hospital"])
        self.assertEqual(patients_by_id(full)["flexible"]["snf_id"], "S2")

    def test_eligibility_and_gender_are_both_required_and_unplaceable_patients_wait(self):
        data = scenario_data([
            patient("wrong_gender_1", eligible=("S1",), gender="male"),
            patient("wrong_gender_3", eligible=("S3",), gender="female"),
            patient("male_compatible", eligible=("S1", "S2"), gender="male"),
            patient("female_compatible", eligible=("S1",)),
        ], beds=[bed("female", "S1", "female"), bed("flex", "S2"), bed("male", "S3", "male")], horizon=5)
        report = compare(data)
        for strategy in ("baseline", "ccn"):
            with self.subTest(strategy=strategy):
                result = report[strategy]
                records = patients_by_id(result)
                self.assertEqual(records["male_compatible"]["snf_id"], "S2")
                self.assertEqual(records["female_compatible"]["snf_id"], "S1")
                self.assertEqual(set(result["summary"]["structurally_unplaceable_patient_ids"]),
                                 {"wrong_gender_1", "wrong_gender_3"})
                for patient_id in ("wrong_gender_1", "wrong_gender_3"):
                    item = records[patient_id]
                    self.assertEqual(item["status"], "waiting")
                    self.assertIsNone(item["planned_admission_hour"])
                    self.assertIsNone(item["planned_release_hour"])
                    self.assertIsNone(item["wait_hours"])
                    self.assertEqual(item["wait_in_window_hours"], 5)
                self.assertEqual(result["summary"]["blocked_bed_hours"], 10)
                self.assertEqual(result["summary"]["waiting_count"], 2)
                self.assertEqual(result["summary"]["snf_occupied_bed_hours"], 4)

    def test_no_gender_compatible_bed_does_not_create_a_phantom_placement(self):
        data = scenario_data([patient("blocked", ready=1, gender="male")],
                             beds=[bed("female", gender="female")], horizon=6)
        report = compare(data)
        for strategy in ("baseline", "ccn"):
            with self.subTest(strategy=strategy):
                result = report[strategy]
                self.assertEqual(result["summary"]["placed_count"], 0)
                self.assertEqual(result["summary"]["waiting_count"], 1)
                self.assertEqual(result["summary"]["completed_count"], 0)
                self.assertEqual(result["summary"]["blocked_bed_hours"], 5)
                self.assertEqual(result["summary"]["snf_occupied_bed_hours"], 0)
                self.assertFalse(any(item["type"] == "admit" for item in result["events"]))

    def test_interval_and_population_conservation_for_both_policies(self):
        data = scenario_data([
            patient("flexible", ready=0, stay=6, eligible=("S2", "S1")),
            patient("restricted", ready=1, stay=4, eligible=("S2",), gender="male", hospital="B"),
            patient("later", ready=2, stay=2, eligible=("S1", "S2"), gender="male"),
            patient("unplaceable", ready=3, eligible=("S1",), gender="male"),
            patient("last", ready=7, stay=3, eligible=("S2",), hospital="B"),
        ], beds=[bed("female", "S1", "female"), bed("shared", "S2")], horizon=8)
        report = compare(data)
        for strategy in ("baseline", "ccn"):
            with self.subTest(strategy=strategy):
                self._assert_conservation(data, report[strategy])

    def _assert_conservation(self, data, result):
        horizon = data["horizon_hours"]
        inputs = {item["id"]: item for item in data["patients"]}
        beds = {item["id"]: item for item in data["beds"]}
        records = patients_by_id(result)
        self.assertEqual(set(inputs), set(records))
        intervals = {bed_id: [] for bed_id in beds}
        expected_blocked = 0
        expected_occupied = 0
        for patient_id, item in records.items():
            original = inputs[patient_id]
            start = item["planned_admission_hour"]
            end = item["planned_release_hour"]
            waiting_end = horizon if start is None else min(start, horizon)
            wait_in_window = max(0, waiting_end - original["ready_hour"])
            self.assertAlmostEqual(item["wait_in_window_hours"], wait_in_window)
            expected_blocked += wait_in_window
            if start is None:
                self.assertIsNone(end)
                continue
            self.assertGreaterEqual(start, original["ready_hour"])
            self.assertAlmostEqual(end - start, original["length_of_stay_hours"])
            chosen = beds[item["bed_id"]]
            self.assertEqual(item["snf_id"], chosen["snf_id"])
            self.assertIn(chosen["snf_id"], original["eligible_snf_ids"])
            self.assertIn(chosen["gender"], ("any", original["gender"]))
            self.assertAlmostEqual(item["wait_hours"], start - original["ready_hour"])
            intervals[item["bed_id"]].append((start, end))
            expected_occupied += max(0, min(end, horizon) - min(start, horizon))

        for bed_id, occupied in intervals.items():
            ordered = sorted(occupied)
            for previous, following in zip(ordered, ordered[1:]):
                self.assertLessEqual(previous[1], following[0], msg=f"Overlapping stays on {bed_id}")
        self.assertAlmostEqual(result["summary"]["blocked_bed_hours"], expected_blocked)
        self.assertAlmostEqual(result["summary"]["snf_occupied_bed_hours"], expected_occupied)
        self.assertAlmostEqual(result["summary"]["snf_utilization"], expected_occupied / (horizon * len(beds)))

        times = [item["time_hour"] for item in result["snapshots"]]
        self.assertEqual(times, sorted(set(times)))
        self.assertEqual(times[0], 0)
        self.assertEqual(times[-1], horizon)
        for snapshot in result["snapshots"]:
            time = snapshot["time_hour"]
            ready = {key for key, value in inputs.items() if value["ready_hour"] <= time}
            occupied = {key for key, value in records.items()
                        if value["planned_admission_hour"] is not None
                        and value["planned_admission_hour"] <= time < value["planned_release_hour"]}
            completed = {key for key, value in records.items()
                         if value["planned_release_hour"] is not None and value["planned_release_hour"] <= time}
            waiting = ready - occupied - completed
            self.assertEqual(snapshot["waiting_count"], len(waiting))
            self.assertEqual(snapshot["occupied_count"], len(occupied))
            self.assertEqual(snapshot["completed_count"], len(completed))
            self.assertEqual(set(snapshot["waiting_patient_ids"]), waiting)
            self.assertEqual(sum(snapshot["hospital_waiting"].values()), len(waiting))
            self.assertEqual(sum(snapshot["snf_occupied"].values()), len(occupied))
            self.assertEqual(len(ready), snapshot["waiting_count"] + snapshot["occupied_count"] + snapshot["completed_count"])

    def test_results_are_finite_json_and_runs_do_not_share_mutable_state(self):
        data = scenario_data([patient("P", ready=1, stay=3)], horizon=5)
        original = deepcopy(data)
        scenario = Scenario.from_dict(data)
        first = compare(scenario)
        json.dumps(first, allow_nan=False)
        self.assertEqual(data, original)
        ccn_before = deepcopy(first["ccn"])
        first["baseline"]["patients"][0]["status"] = "modified_by_consumer"
        self.assertEqual(first["ccn"], ccn_before)
        second = compare(scenario)
        self.assertEqual(second, compare(original))
        self.assertNotEqual(second["baseline"]["patients"][0]["status"], "modified_by_consumer")
        self.assertEqual(data, original)

    def test_ccn_eventual_wait_is_not_worse_on_small_competing_cases(self):
        cases = [
            scenario_data([patient("a", stay=9), patient("b", stay=1), patient("c", ready=2, stay=2)]),
            scenario_data([patient("a", stay=3, eligible=("S1", "S2")),
                           patient("b", ready=1, stay=2, eligible=("S1",), gender="male")],
                          beds=[bed("flex", "S1"), bed("female", "S2", "female")]),
            scenario_data([patient("a", ready=2, stay=1), patient("b", ready=0, stay=8),
                           patient("c", ready=4, stay=2)], horizon=6),
        ]
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                report = compare(data)
                self.assertLessEqual(report["ccn"]["summary"]["full_plan_wait_hours"],
                                     report["baseline"]["summary"]["full_plan_wait_hours"] + 1e-9)
                self._assert_conservation(data, report["baseline"])
                self._assert_conservation(data, report["ccn"])


class DynamicValidationTests(unittest.TestCase):
    def test_invalid_times_are_rejected_before_simulation(self):
        for field, values in (
            ("ready_hour", (-1, 25, math.nan, math.inf, "1", True)),
            ("length_of_stay_hours", (0, -1, math.nan, math.inf, "2", True)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    data = scenario_data([patient("P")])
                    data["patients"][0][field] = value
                    with self.assertRaises(ValueError):
                        Scenario.from_dict(data)
        for value in (-1, math.nan, math.inf, "24", True):
            with self.subTest(horizon=value):
                data = scenario_data([patient("P")])
                data["horizon_hours"] = value
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)

    def test_invalid_references_genders_and_duplicate_ids_are_rejected(self):
        mutations = (
            lambda data: data["patients"][0].update(hospital_id="unknown"),
            lambda data: data["patients"][0].update(eligible_snf_ids=["unknown"]),
            lambda data: data["patients"][0].update(eligible_snf_ids="S1"),
            lambda data: data["patients"][0].update(gender="unknown"),
            lambda data: data["beds"][0].update(gender="unknown"),
            lambda data: data["beds"][0].update(snf_id="unknown"),
            lambda data: data["patients"].append(deepcopy(data["patients"][0])),
            lambda data: data["beds"].append(deepcopy(data["beds"][0])),
            lambda data: data.update(schema_version=2),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                data = scenario_data([patient("P")])
                mutate(data)
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)


if __name__ == "__main__":
    unittest.main()
