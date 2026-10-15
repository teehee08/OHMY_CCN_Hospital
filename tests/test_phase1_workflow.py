"""Behavioral checks for unlimited-bed referral processing timelines."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from phase1_workflow import (
    WorkflowScenario,
    load_workflow_scenarios,
    simulate_workflow,
)


def workflow_data(name="processing_example"):
    return {
        "name": name,
        "hospitals": [{"id": "A"}, {"id": "B"}],
        "snfs": [{"id": "X"}, {"id": "Y"}],
        "patients": [
            {"id": "patient_A", "hospital_id": "A", "ready_time_minutes": 0},
            {"id": "patient_B", "hospital_id": "B", "ready_time_minutes": 60},
            {"id": "patient_A_later", "hospital_id": "A", "ready_time_minutes": 90},
        ],
        "travel_time_minutes": {"A": {"X": 4, "Y": 5}, "B": {"X": 5, "Y": 14}},
        "workflows": {
            "manual": {"contact_minutes": 30, "review_minutes": 80, "confirmation_minutes": 10},
            "digital": {"contact_minutes": 2, "review_minutes": 7, "confirmation_minutes": 1},
        },
    }


def by_patient(result):
    return {placement.patient_id: placement for placement in result.placements}


class WorkflowTimelineTests(unittest.TestCase):
    def test_hand_calculated_manual_timelines_and_totals(self):
        result = simulate_workflow(WorkflowScenario.from_dict(workflow_data()), "manual")
        self.assertEqual(result.mode, "manual")
        expected = {
            "patient_A": ("A", 0, 0, 30, 110, 120, 4, 124),
            "patient_B": ("B", 60, 60, 90, 170, 180, 5, 185),
            "patient_A_later": ("A", 90, 90, 120, 200, 210, 4, 214),
        }
        placements = by_patient(result)
        self.assertEqual(set(placements), set(expected))
        for patient_id, values in expected.items():
            with self.subTest(patient=patient_id):
                placement = placements[patient_id]
                self.assertEqual(placement.snf_id, "X")
                self.assertEqual(
                    (
                        placement.hospital_id,
                        placement.t1_minutes,
                        placement.contact_started_minutes,
                        placement.contact_completed_minutes,
                        placement.review_completed_minutes,
                        placement.t2_minutes,
                        placement.travel_time_minutes,
                        placement.arrival_time_minutes,
                    ),
                    values,
                )
                self.assertEqual(placement.placement_delay_minutes, 120)
        self.assertEqual(result.total_placement_delay_minutes, 360)
        self.assertEqual(result.mean_placement_delay_minutes, 120)
        self.assertEqual(result.total_travel_time_minutes, 13)
        self.assertEqual(result.placed_count, 3)
        exported = result.to_dict()
        self.assertEqual(exported["placed_count"], 3)
        self.assertEqual(exported["total_placement_delay_minutes"], 360)
        self.assertEqual(exported["mean_placement_delay_minutes"], 120)
        self.assertEqual(exported["total_travel_time_minutes"], 13)
        self.assertEqual({placement["placement_delay_minutes"] for placement in exported["placements"]}, {120})

    def test_digital_has_its_own_processing_durations(self):
        result = simulate_workflow(WorkflowScenario.from_dict(workflow_data()), "digital")
        self.assertEqual(result.mode, "digital")
        placements = by_patient(result)
        self.assertEqual(placements["patient_A"].contact_completed_minutes, 2)
        self.assertEqual(placements["patient_A"].review_completed_minutes, 9)
        self.assertEqual(placements["patient_A"].t2_minutes, 10)
        self.assertEqual(placements["patient_A"].arrival_time_minutes, 14)
        self.assertEqual(placements["patient_B"].t2_minutes, 70)
        self.assertEqual(placements["patient_A_later"].t2_minutes, 100)
        self.assertEqual(result.total_placement_delay_minutes, 30)
        self.assertEqual(result.mean_placement_delay_minutes, 10)

    def test_shifting_readiness_shifts_timestamps_but_not_delays(self):
        original = workflow_data()
        shifted = deepcopy(original)
        shift = 100.25
        for patient in shifted["patients"]:
            patient["ready_time_minutes"] += shift
        absolute_fields = (
            "t1_minutes",
            "contact_started_minutes",
            "contact_completed_minutes",
            "review_completed_minutes",
            "t2_minutes",
            "arrival_time_minutes",
        )
        for mode in ("manual", "digital"):
            with self.subTest(mode=mode):
                before = simulate_workflow(WorkflowScenario.from_dict(original), mode)
                after = simulate_workflow(WorkflowScenario.from_dict(shifted), mode)
                before_patients, after_patients = by_patient(before), by_patient(after)
                for patient_id, placement in before_patients.items():
                    shifted_placement = after_patients[patient_id]
                    for field in absolute_fields:
                        self.assertAlmostEqual(getattr(shifted_placement, field), getattr(placement, field) + shift)
                    self.assertAlmostEqual(shifted_placement.placement_delay_minutes, placement.placement_delay_minutes)
                self.assertAlmostEqual(after.total_placement_delay_minutes, before.total_placement_delay_minutes)
                self.assertAlmostEqual(after.mean_placement_delay_minutes, before.mean_placement_delay_minutes)

    def test_travel_affects_arrival_but_not_acceptance(self):
        original = workflow_data()
        longer_trip = deepcopy(original)
        for row in longer_trip["travel_time_minutes"].values():
            for snf_id in row:
                row[snf_id] *= 10
        for mode in ("manual", "digital"):
            with self.subTest(mode=mode):
                before = simulate_workflow(WorkflowScenario.from_dict(original), mode)
                after = simulate_workflow(WorkflowScenario.from_dict(longer_trip), mode)
                before_patients, after_patients = by_patient(before), by_patient(after)
                for patient_id, placement in before_patients.items():
                    changed = after_patients[patient_id]
                    self.assertEqual(changed.snf_id, placement.snf_id)
                    self.assertEqual(changed.t2_minutes, placement.t2_minutes)
                    self.assertEqual(changed.placement_delay_minutes, placement.placement_delay_minutes)
                    self.assertAlmostEqual(changed.arrival_time_minutes - placement.arrival_time_minutes, 9 * placement.travel_time_minutes)
                self.assertEqual(after.total_placement_delay_minutes, before.total_placement_delay_minutes)
                self.assertEqual(after.total_travel_time_minutes, 130)

    def test_equal_processing_profiles_produce_equal_timelines(self):
        data = workflow_data()
        data["workflows"]["digital"] = deepcopy(data["workflows"]["manual"])
        scenario = WorkflowScenario.from_dict(data)
        manual = simulate_workflow(scenario, "manual")
        digital = simulate_workflow(scenario, "digital")
        self.assertEqual(manual.placements, digital.placements)
        self.assertEqual(manual.total_placement_delay_minutes, digital.total_placement_delay_minutes)

    def test_digital_can_be_slower_when_assumed_processing_is_slower(self):
        data = workflow_data()
        data["workflows"]["manual"], data["workflows"]["digital"] = data["workflows"]["digital"], data["workflows"]["manual"]
        scenario = WorkflowScenario.from_dict(data)
        self.assertEqual(simulate_workflow(scenario, "manual").mean_placement_delay_minutes, 10)
        self.assertEqual(simulate_workflow(scenario, "digital").mean_placement_delay_minutes, 120)

    def test_unlimited_beds_and_independent_processing(self):
        data = workflow_data()
        data["patients"] = [
            {"id": "patient_" + str(index), "hospital_id": "A", "ready_time_minutes": 17.5}
            for index in range(12)
        ]
        scenario = WorkflowScenario.from_dict(data)
        for mode, t2 in (("manual", 137.5), ("digital", 27.5)):
            with self.subTest(mode=mode):
                result = simulate_workflow(scenario, mode)
                self.assertEqual(len(result.placements), 12)
                self.assertEqual({placement.snf_id for placement in result.placements}, {"X"})
                self.assertEqual({placement.contact_started_minutes for placement in result.placements}, {17.5})
                self.assertEqual({placement.t2_minutes for placement in result.placements}, {t2})

    def test_nearest_snf_and_input_order_break_ties(self):
        data = workflow_data()
        data["travel_time_minutes"] = {"A": {"X": 4, "Y": 4}, "B": {"X": 5, "Y": 5}}
        for first_snf in ("X", "Y"):
            with self.subTest(first_snf=first_snf):
                data["snfs"] = [{"id": first_snf}, {"id": "Y" if first_snf == "X" else "X"}]
                result = simulate_workflow(WorkflowScenario.from_dict(data), "manual")
                self.assertEqual({placement.snf_id for placement in result.placements}, {first_snf})

    def test_zero_processing_and_travel_times(self):
        data = workflow_data()
        for profile in data["workflows"].values():
            for stage in profile:
                profile[stage] = 0
        data["travel_time_minutes"] = {"A": {"X": 0, "Y": 0}, "B": {"X": 0, "Y": 0}}
        for mode in ("manual", "digital"):
            with self.subTest(mode=mode):
                result = simulate_workflow(WorkflowScenario.from_dict(data), mode)
                self.assertEqual(result.total_placement_delay_minutes, 0)
                self.assertEqual(result.total_travel_time_minutes, 0)
                for placement in result.placements:
                    self.assertEqual(placement.t1_minutes, placement.t2_minutes)
                    self.assertEqual(placement.t2_minutes, placement.arrival_time_minutes)

    def test_empty_patient_set(self):
        data = workflow_data()
        data.update(hospitals=[], snfs=[], patients=[], travel_time_minutes={})
        for mode in ("manual", "digital"):
            with self.subTest(mode=mode):
                result = simulate_workflow(WorkflowScenario.from_dict(data), mode)
                self.assertEqual(len(result.placements), 0)
                self.assertEqual(result.total_placement_delay_minutes, 0)
                self.assertIsNone(result.mean_placement_delay_minutes)
                self.assertEqual(result.total_travel_time_minutes, 0)
                self.assertEqual(result.to_dict()["placed_count"], 0)
                self.assertIsNone(result.to_dict()["mean_placement_delay_minutes"])

    def test_reusing_scenario_does_not_change_input_or_previous_results(self):
        data = workflow_data()
        original = deepcopy(data)
        scenario = WorkflowScenario.from_dict(data)
        first = simulate_workflow(scenario, "manual")
        simulate_workflow(scenario, "digital")
        repeated = simulate_workflow(scenario, "manual")
        self.assertEqual(first.to_dict(), repeated.to_dict())
        roundtrip = WorkflowScenario.from_dict(scenario.to_dict())
        self.assertEqual(simulate_workflow(roundtrip, "manual").to_dict(), first.to_dict())
        self.assertEqual(data, original)


class WorkflowInputTests(unittest.TestCase):
    def test_invalid_ready_times(self):
        for value in (-1, float("inf"), float("nan"), True, "5"):
            with self.subTest(value=value):
                data = workflow_data()
                data["patients"][0]["ready_time_minutes"] = value
                with self.assertRaises(ValueError):
                    WorkflowScenario.from_dict(data)

    def test_invalid_processing_times(self):
        for mode in ("manual", "digital"):
            for stage in ("contact_minutes", "review_minutes", "confirmation_minutes"):
                for value in (-1, float("inf"), float("nan"), True, "5"):
                    with self.subTest(mode=mode, stage=stage, value=value):
                        data = workflow_data()
                        data["workflows"][mode][stage] = value
                        with self.assertRaises(ValueError):
                            WorkflowScenario.from_dict(data)

    def test_both_complete_workflow_profiles_are_required(self):
        for mode in ("manual", "digital"):
            for missing in ("profile", "contact_minutes", "review_minutes", "confirmation_minutes"):
                with self.subTest(mode=mode, missing=missing):
                    data = workflow_data()
                    if missing == "profile":
                        del data["workflows"][mode]
                    else:
                        del data["workflows"][mode][missing]
                    with self.assertRaises(ValueError):
                        WorkflowScenario.from_dict(data)

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            simulate_workflow(WorkflowScenario.from_dict(workflow_data()), "unknown")

    def test_duplicate_ids(self):
        for collection in ("hospitals", "snfs", "patients"):
            with self.subTest(collection=collection):
                data = workflow_data()
                data[collection].append(deepcopy(data[collection][0]))
                with self.assertRaises(ValueError):
                    WorkflowScenario.from_dict(data)

    def test_patient_must_reference_existing_hospital(self):
        data = workflow_data()
        data["patients"][0]["hospital_id"] = "unknown"
        with self.assertRaises(ValueError):
            WorkflowScenario.from_dict(data)

    def test_nonempty_patient_set_requires_a_destination(self):
        data = workflow_data()
        data["snfs"] = []
        data["travel_time_minutes"] = {"A": {}, "B": {}}
        with self.assertRaises(ValueError):
            WorkflowScenario.from_dict(data)

    def test_complete_finite_nonnegative_travel_matrix_required(self):
        invalid = []
        missing_row = workflow_data()
        del missing_row["travel_time_minutes"]["A"]
        invalid.append(missing_row)
        missing_entry = workflow_data()
        del missing_entry["travel_time_minutes"]["A"]["X"]
        invalid.append(missing_entry)
        for value in (-1, float("inf"), float("nan"), True, "5"):
            data = workflow_data()
            data["travel_time_minutes"]["A"]["X"] = value
            invalid.append(data)
        for index, data in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    WorkflowScenario.from_dict(data)

    def test_versioned_scenario_loader(self):
        payload = {"schema_version": 1, "scenarios": [workflow_data("first"), workflow_data("second")]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            scenarios = load_workflow_scenarios(path)
        self.assertEqual([scenario.name for scenario in scenarios], ["first", "second"])
        self.assertEqual(simulate_workflow(scenarios[0], "manual").mean_placement_delay_minutes, 120)


if __name__ == "__main__":
    unittest.main()
