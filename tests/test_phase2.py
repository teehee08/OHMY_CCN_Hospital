"""Behavioral checks for Phase 2 clinical attribute matching."""

import json
from pathlib import Path
import tempfile
import unittest

from phase2 import Scenario, baseline_match, ccn_match, compare, load_scenarios


ROOT = Path(__file__).parents[1]


def scenario_data(beds, patients=()):
    snf_ids = list(dict.fromkeys(bed["snf_id"] for bed in beds))
    return {
        "name": "regression",
        "hospitals": [{"id": "h"}],
        "snfs": [{"id": snf_id} for snf_id in snf_ids],
        "beds": beds,
        "patients": list(patients),
        "travel_time_minutes": {"h": {snf_id: 10 for snf_id in snf_ids}},
    }


def bed_data(bed_id, gender="any", capabilities=(), snf_id="s", room_id=None):
    return {
        "id": bed_id,
        "snf_id": snf_id,
        "room_id": room_id or bed_id,
        "room_gender": gender,
        "capabilities": list(capabilities),
    }


def patient_data(patient_id, gender="male", capabilities=()):
    return {
        "id": patient_id,
        "hospital_id": "h",
        "gender": gender,
        "required_capabilities": list(capabilities),
        "ready_time_minutes": 0,
    }


class Phase2Tests(unittest.TestCase):
    def test_ghost_bed_is_rejected_by_baseline_and_filtered_by_ccn(self):
        scenario = load_scenarios(ROOT / "scenarios" / "phase2_examples.json")[0]
        baseline = baseline_match(scenario)
        ccn = ccn_match(scenario)

        self.assertEqual(baseline.assignments[0].bed_id, "room_102_bed_female")
        self.assertEqual(baseline.assignments[0].rejection_count, 1)
        self.assertEqual(baseline.assignments[0].placement_delay_minutes, 120)
        self.assertEqual(ccn.assignments[0].bed_id, "room_102_bed_female")
        self.assertEqual(ccn.assignments[0].placement_delay_minutes, 0)
        self.assertEqual(compare(scenario)["objective_delta_minutes"], 120)

    def test_ccn_reserves_bariatric_bed_for_hard_dependency(self):
        scenario = load_scenarios(ROOT / "scenarios" / "phase2_examples.json")[1]
        baseline = baseline_match(scenario)
        ccn = ccn_match(scenario)

        self.assertEqual(baseline.assignments[0].bed_id, "bariatric_bed")
        self.assertEqual(baseline.unplaced_patient_ids, ("bariatric_patient",))
        self.assertIsNone(baseline.objective_value_minutes)
        self.assertEqual(
            {item.patient_id: item.bed_id for item in ccn.assignments},
            {"standard_patient": "standard_bed", "bariatric_patient": "bariatric_bed"},
        )
        self.assertEqual(ccn.objective_value_minutes, 0)

    def test_ccn_reserves_gender_compatible_bed_without_required_capabilities(self):
        data = scenario_data(
            [bed_data("flex"), bed_data("male_only", gender="male")],
            [patient_data("male"), patient_data("female", gender="female")],
        )

        result = ccn_match(Scenario.from_dict(data))

        self.assertEqual(
            {item.patient_id: item.bed_id for item in result.assignments},
            {"male": "male_only", "female": "flex"},
        )
        self.assertEqual(result.unplaced_patient_ids, ())
        self.assertEqual(result.placed_count, 2)
        self.assertEqual(result.objective_value_minutes, 0)

    def test_ccn_keeps_useful_reservation_when_future_patient_is_infeasible(self):
        data = scenario_data(
            [
                bed_data("combined", capabilities=("iv", "bariatric")),
                bed_data("iv_only", capabilities=("iv",)),
            ],
            [
                patient_data("iv_patient", capabilities=("iv",)),
                patient_data("bariatric_patient", capabilities=("bariatric",)),
                patient_data("ventilator_patient", capabilities=("ventilator",)),
            ],
        )

        result = ccn_match(Scenario.from_dict(data))

        self.assertEqual(
            {item.patient_id: item.bed_id for item in result.assignments},
            {"iv_patient": "iv_only", "bariatric_patient": "combined"},
        )
        self.assertEqual(result.unplaced_patient_ids, ("ventilator_patient",))
        self.assertEqual(result.placed_count, 2)
        self.assertIsNone(result.objective_value_minutes)

    def test_ccn_candidate_priority_when_all_future_patients_are_infeasible(self):
        cases = [
            (
                "standard_patient_preserves_specialized_capacity",
                [
                    bed_data("specialized", capabilities=("iv",), snf_id="near"),
                    bed_data("standard", snf_id="far"),
                ],
                patient_data("current"),
                {"near": 1, "far": 20},
                "standard",
            ),
            (
                "required_capability_patient_uses_nearest_bed",
                [
                    bed_data("far_bed", capabilities=("iv",), snf_id="far"),
                    bed_data("near_bed", capabilities=("iv",), snf_id="near"),
                ],
                patient_data("current", capabilities=("iv",)),
                {"near": 1, "far": 20},
                "near_bed",
            ),
            (
                "equal_candidates_follow_input_order",
                [bed_data("first"), bed_data("second")],
                patient_data("current"),
                {"s": 10},
                "first",
            ),
        ]
        for name, beds, current_patient, travel_times, expected_bed in cases:
            with self.subTest(name=name):
                data = scenario_data(
                    beds,
                    [
                        current_patient,
                        patient_data("blocked", capabilities=("ventilator",)),
                    ],
                )
                data["travel_time_minutes"]["h"] = travel_times

                result = ccn_match(Scenario.from_dict(data))

                self.assertEqual(
                    [(item.patient_id, item.bed_id) for item in result.assignments],
                    [("current", expected_bed)],
                )
                self.assertEqual(result.unplaced_patient_ids, ("blocked",))

    def test_conflicting_genders_in_one_room_are_rejected(self):
        for first_gender in ("male", "female", "any"):
            for second_gender in ("male", "female", "any"):
                if first_gender == second_gender:
                    continue
                with self.subTest(first=first_gender, second=second_gender):
                    data = scenario_data([
                        bed_data("first", gender=first_gender, room_id="shared"),
                        bed_data("second", gender=second_gender, room_id="shared"),
                    ])

                    with self.assertRaises(ValueError):
                        Scenario.from_dict(data)

    def test_multiple_beds_can_share_consistent_room_gender(self):
        for gender in ("male", "female", "any"):
            with self.subTest(gender=gender):
                data = scenario_data([
                    bed_data("first", gender=gender, room_id="shared"),
                    bed_data(
                        "second", gender=gender, capabilities=("iv",), room_id="shared"
                    ),
                ])

                scenario = Scenario.from_dict(data)

                self.assertEqual(len(scenario.beds), 2)
                self.assertEqual([bed.room_gender for bed in scenario.beds], [gender, gender])

    def test_room_ids_are_scoped_to_each_snf(self):
        data = scenario_data([
            bed_data("first", gender="male", snf_id="one", room_id="shared"),
            bed_data("second", gender="female", snf_id="two", room_id="shared"),
        ])

        scenario = Scenario.from_dict(data)

        self.assertEqual(
            [(bed.snf_id, bed.room_id, bed.room_gender) for bed in scenario.beds],
            [("one", "shared", "male"), ("two", "shared", "female")],
        )

    def test_round_trip_output_is_json_serializable(self):
        scenarios = load_scenarios(ROOT / "scenarios" / "phase2_examples.json")
        rendered = json.dumps([compare(scenario) for scenario in scenarios])
        self.assertIn("ghost_bed_gender_mismatch", rendered)

    def test_invalid_gender_and_capability_inputs_are_rejected(self):
        data = {
            "name": "invalid",
            "hospitals": [{"id": "h"}],
            "snfs": [{"id": "s"}],
            "beds": [{
                "id": "b", "snf_id": "s", "room_id": "r",
                "room_gender": "unknown", "capabilities": []
            }],
            "patients": [],
            "travel_time_minutes": {"h": {"s": 1}},
        }
        with self.assertRaises(ValueError):
            Scenario.from_dict(data)


if __name__ == "__main__":
    unittest.main()
