"""Behavioral checks for Phase 2 clinical attribute matching."""

import json
from pathlib import Path
import tempfile
import unittest

from phase2 import Scenario, baseline_match, ccn_match, compare, load_scenarios


ROOT = Path(__file__).parents[1]


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
