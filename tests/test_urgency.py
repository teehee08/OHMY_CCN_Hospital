import copy
import math
import unittest

import dynamic_model as model
import urgency


def two_patients_one_bed(**urgency_block):
    """Two hospitals each send one patient for the only bed at hour 0."""
    data = {
        "schema_version": 1, "name": "Two patients, one bed", "arrival_span_hours": 1,
        "hospitals": [{"id": "H1"}, {"id": "H2"}],
        "snfs": [{"id": "SNF_1", "accepted_insurances": ["*"]}],
        "beds": [{"id": "B1", "snf_id": "SNF_1", "gender": "any", "capabilities": []}],
        "patients": [
            {"id": "Stable", "hospital_id": "H1", "gender": "female", "ready_hour": 0,
             "length_of_stay_hours": 24, "eligible_snf_ids": ["SNF_1"], "clinical_risk": "low"},
            {"id": "Urgent", "hospital_id": "H2", "gender": "male", "ready_hour": 0,
             "length_of_stay_hours": 24, "eligible_snf_ids": ["SNF_1"], "clinical_risk": "high"},
        ],
    }
    if urgency_block is not None:
        data["urgency"] = urgency_block
    return data


def admissions(result, key):
    return {p["patient_id"]: p["admission_hour"] for p in result[key]["patients"]}


class UrgencyTests(unittest.TestCase):
    def test_absent_block_keeps_plain_objective(self):
        data = two_patients_one_bed()
        del data["urgency"]
        result = model.compare(data)
        self.assertEqual(result["objective"]["id"], "full_cohort_total_wait")
        self.assertNotIn("urgency", result)
        self.assertNotIn("urgency_weight", result["ccn"]["patients"][0])

    def test_urgent_patient_gets_the_single_bed_first(self):
        result = model.compare(two_patients_one_bed())
        self.assertEqual(admissions(result, "ccn"), {"Urgent": 0, "Stable": 24})
        self.assertEqual(result["objective"]["id"], "urgency_weighted_total_cost")
        weights = {p["patient_id"]: p["urgency_weight"] for p in result["ccn"]["patients"]}
        self.assertGreater(weights["Urgent"], weights["Stable"])
        # Plain total waiting is unchanged by who goes first; the weighted cost is not.
        self.assertEqual(result["ccn"]["summary"]["full_plan_wait_hours"], 24)

    def test_without_urgency_input_order_breaks_the_tie(self):
        data = two_patients_one_bed()
        for p in data["patients"]:
            p["clinical_risk"] = "low"
        result = model.compare(data)
        self.assertEqual(sorted(admissions(result, "ccn").values()), [0, 24])

    def test_hospital_pressure_multiplier(self):
        data = two_patients_one_bed()
        for p in data["patients"]:
            p["clinical_risk"] = "low"
        data["hospitals"][0]["occupancy"] = 0.97
        result = model.compare(data)
        stable = next(p for p in result["ccn"]["patients"] if p["patient_id"] == "Stable")
        self.assertAlmostEqual(stable["urgency_factors"]["hospital_multiplier"], 1.5)
        self.assertEqual(admissions(result, "ccn")["Stable"], 0)

    def test_long_wait_grows_exponentially(self):
        data = two_patients_one_bed(wait_growth_per_day=1.0)
        s = model.Scenario.from_dict(data)
        one_day, two_days = urgency.wait_cost(s, 0, 24), urgency.wait_cost(s, 0, 48)
        self.assertAlmostEqual(one_day, 24 * (math.e - 1))
        self.assertGreater(two_days - one_day, one_day)
        linear = model.Scenario.from_dict(two_patients_one_bed(wait_growth_per_day=0))
        self.assertEqual(urgency.wait_cost(linear, 0, 48), 48)

    def test_authorization_expiry_and_decline_factors(self):
        data = two_patients_one_bed()
        data["patients"][0].update(authorization_expiry_hour=18, age=95, mobility_score=6)
        s = model.Scenario.from_dict(data)
        factors = s.urgency_derived["components"][0]
        self.assertAlmostEqual(factors["authorization"], 0.75)
        self.assertAlmostEqual(factors["decline_risk"], 1.0)

    def test_distance_prefers_the_nearer_snf(self):
        data = two_patients_one_bed()
        data["snfs"] = [{"id": "Far", "accepted_insurances": ["*"], "x_km": 25, "y_km": 0},
                        {"id": "Near", "accepted_insurances": ["*"], "x_km": 2, "y_km": 0}]
        data["beds"] = [{"id": "F1", "snf_id": "Far", "gender": "any", "capabilities": []},
                        {"id": "N1", "snf_id": "Near", "gender": "any", "capabilities": []}]
        data["patients"] = [dict(data["patients"][0], eligible_snf_ids=["Far", "Near"], home_x_km=0, home_y_km=0)]
        result = model.compare(data)
        self.assertEqual(result["ccn"]["patients"][0]["snf_id"], "Near")
        self.assertAlmostEqual(result["ccn"]["patients"][0]["distance_km"], 2)

    def test_hard_to_place_factor(self):
        data = two_patients_one_bed()
        data["beds"].append({"id": "B2", "snf_id": "SNF_1", "gender": "female", "capabilities": []})
        s = model.Scenario.from_dict(data)
        stable, urgent = s.urgency_derived["components"]
        self.assertEqual((stable["compatible_beds"], urgent["compatible_beds"]), (2, 1))
        self.assertAlmostEqual(urgent["hard_to_place"], 0.5)

    def test_roundtrip_and_validation(self):
        data = two_patients_one_bed(beta={"clinical_risk": 2})
        data["hospitals"][1]["ed_boarding"] = 4
        s = model.Scenario.from_dict(data)
        again = model.Scenario.from_dict(s.to_dict())
        self.assertEqual(again, s)
        self.assertEqual(again.urgency_derived["weights"], s.urgency_derived["weights"])
        for mutate in (lambda d: d["patients"][0].update(clinical_risk="severe"),
                       lambda d: d["urgency"].update(beta={"unknown": 1}),
                       lambda d: d["urgency"].update(pressure_full=0.5),
                       lambda d: d["patients"][0].update(mobility_score=40)):
            bad = copy.deepcopy(data)
            mutate(bad)
            with self.assertRaises(ValueError):
                model.Scenario.from_dict(bad)

    def test_bundled_examples_run(self):
        import urgency_scenarios
        for scenario in urgency_scenarios.examples()["scenarios"]:
            result = model.compare(scenario)
            self.assertTrue(result["observation"]["complete"])
            self.assertLessEqual(result["ccn"]["summary"]["urgency_weighted_cost"],
                                 result["baseline"]["summary"]["urgency_weighted_cost"])


if __name__ == "__main__":
    unittest.main()
