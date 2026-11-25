import collections
import unittest

import dynamic_model as model
import dynamic_scenarios
import staffing


class RunnableBrooklynScenarioTests(unittest.TestCase):
    def test_reproducible_and_valid(self):
        first = dynamic_scenarios.nyc_scenario(days=3, referrals=5)
        self.assertEqual(first, dynamic_scenarios.nyc_scenario(days=3, referrals=5))
        self.assertEqual(len(first["patients"]), 165)
        self.assertEqual(len(first["beds"]), 549)
        self.assertEqual(model.Scenario.from_dict(first).to_dict()["patients"][0]["id"], first["patients"][0]["id"])

    def test_patients_use_real_mix_and_feasible_referrals(self):
        data = dynamic_scenarios.nyc_scenario(days=7, referrals=6)
        capabilities = collections.defaultdict(set)
        for bed in data["beds"]:
            capabilities[bed["snf_id"]].update(bed["capabilities"])
        for patient in data["patients"]:
            self.assertLessEqual(len(patient["eligible_snf_ids"]), 6)
            self.assertTrue(patient["eligible_snf_ids"])
            for snf in patient["eligible_snf_ids"]:
                self.assertTrue(set(patient["required_capabilities"]) <= capabilities[snf])
            self.assertIn(patient["clinical_risk"], ("low", "medium", "high"))
            self.assertGreaterEqual(patient["length_of_stay_hours"], 7 * 24)
        self.assertEqual({p["clinical_risk"] for p in data["patients"]}, {"low", "medium", "high"})

    def test_invalid_window_rejected(self):
        for kwargs in ({"days": 0}, {"days": 13}, {"referrals": 0}):
            with self.assertRaises(ValueError):
                dynamic_scenarios.nyc_scenario(**kwargs)

    def test_staffing_shortcut_only_skips_snfs_that_can_never_run_short(self):
        s = model.Scenario.from_dict(dynamic_scenarios.nyc_scenario(days=2))
        beds = collections.Counter(bed.snf_id for bed in s.beds)
        peak = {role: max(dict(p.care_hours_per_day)[role] for p in s.patients) / 24 for role in staffing.ROLES}
        for row in s.staffing["shifts"]:
            hourly = {role: row["direct_care_hours"][role] / (row["end_hour"] - row["start_hour"]) for role in staffing.ROLES}
            fits = all(beds[row["snf_id"]] * peak[role] <= hourly[role] + 1e-9 for role in staffing.ROLES)
            self.assertEqual(staffing.never_short(s, row["snf_id"]), fits)

    def test_small_window_runs_to_completion(self):
        result = model.compare(dynamic_scenarios.nyc_scenario(days=1))
        self.assertTrue(result["observation"]["complete"])
        self.assertLessEqual(result["ccn"]["summary"]["urgency_weighted_cost"],
                             result["baseline"]["summary"]["urgency_weighted_cost"])


if __name__ == "__main__":
    unittest.main()
