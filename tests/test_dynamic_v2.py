"""Behavioral checks for hospital-local planning and constrained dynamic flow."""

from copy import deepcopy
import json
import unittest

from dynamic_model import Scenario, compare


def make_patient(identifier, *, hospital="A", ready=0, stay=2, eligible=("S1",),
                 gender="female", insurance="public", care=()):
    return {"id": identifier, "hospital_id": hospital, "gender": gender,
            "ready_hour": ready, "length_of_stay_hours": stay,
            "eligible_snf_ids": list(eligible), "insurance_type": insurance,
            "required_capabilities": list(care)}


def make_bed(identifier, snf="S1", *, gender="any", care=()):
    return {"id": identifier, "snf_id": snf, "gender": gender,
            "capabilities": list(care)}


def make_scenario(patients, *, beds=None, snfs=None, horizon=24, policy="local", seed=42):
    beds = [make_bed("b1")] if beds is None else beds
    if snfs is None:
        snfs = [{"id": value, "accepted_insurances": ["*"]}
                for value in dict.fromkeys(bed["snf_id"] for bed in beds)]
    return {"schema_version": 1, "name": "Constrained planning regression",
            "baseline_policy": policy, "tie_break_seed": seed,
            "horizon_hours": horizon,
            "hospitals": [{"id": value} for value in dict.fromkeys(p["hospital_id"] for p in patients)],
            "snfs": deepcopy(snfs), "beds": deepcopy(beds), "patients": deepcopy(patients)}


def by_patient(result):
    return {row["patient_id"]: row for row in result["patients"]}


class HospitalLocalPlanningTests(unittest.TestCase):
    def test_default_policy_and_backward_compatible_constraint_defaults(self):
        data = make_scenario([make_patient("P")])
        del data["baseline_policy"]
        del data["tie_break_seed"]
        del data["patients"][0]["insurance_type"]
        del data["patients"][0]["required_capabilities"]
        del data["beds"][0]["capabilities"]
        del data["snfs"][0]["accepted_insurances"]
        canonical = Scenario.from_dict(data).to_dict()
        self.assertEqual(canonical["baseline_policy"], "local")
        self.assertEqual(canonical["tie_break_seed"], 42)
        self.assertEqual(canonical["patients"][0]["insurance_type"], "unspecified")
        self.assertEqual(canonical["patients"][0]["required_capabilities"], [])
        self.assertEqual(canonical["beds"][0]["capabilities"], [])
        self.assertEqual(canonical["snfs"][0]["accepted_insurances"], ["*"])
        self.assertEqual(compare(data)["baseline"]["summary"]["placed_count"], 1)

    def test_single_hospital_allocates_flexible_patient_away_from_restricted_patient(self):
        data = make_scenario([
            make_patient("flexible", stay=8, eligible=("S1", "S2")),
            make_patient("restricted", stay=3, eligible=("S1",)),
        ], beds=[make_bed("one"), make_bed("two", "S2")])
        report = compare(data)
        baseline = by_patient(report["baseline"])
        self.assertEqual(baseline["flexible"]["snf_id"], "S2")
        self.assertEqual(baseline["restricted"]["snf_id"], "S1")
        self.assertEqual(report["baseline"]["summary"]["full_plan_wait_hours"], 0)
        self.assertEqual(report["ccn"]["summary"]["full_plan_wait_hours"], 0)
        data["baseline_policy"] = "fcfs"
        self.assertGreater(compare(data)["baseline"]["summary"]["full_plan_wait_hours"], 0)

    def test_single_hospital_anticipates_own_future_short_stay(self):
        data = make_scenario([
            make_patient("long", ready=0, stay=10),
            make_patient("short", ready=1, stay=1),
        ], horizon=20)
        report = compare(data)
        local = by_patient(report["baseline"])
        self.assertEqual(local["short"]["admission_hour"], 1)
        self.assertEqual(local["long"]["admission_hour"], 2)
        self.assertEqual(report["baseline"]["summary"]["full_plan_wait_hours"], 2)
        self.assertEqual(report["ccn"]["summary"]["full_plan_wait_hours"], 2)
        data["baseline_policy"] = "fcfs"
        self.assertEqual(compare(data)["baseline"]["summary"]["full_plan_wait_hours"], 9)

    def test_other_hospitals_unready_future_cannot_change_baseline_prefix(self):
        data = make_scenario([
            make_patient("flexible", ready=0, stay=8, eligible=("S2", "S1")),
            make_patient("other_future", hospital="B", ready=6, stay=1, eligible=("S2",)),
        ], beds=[make_bed("one", "S1"), make_bed("two", "S2")])
        reduced = deepcopy(data)
        reduced["patients"] = reduced["patients"][:1]
        full = compare(data)
        without_future = compare(reduced)

        def prefix(result):
            return [(row["time_hour"], row["type"], row["patient_id"], row.get("bed_id"))
                    for row in result["events"]
                    if row["time_hour"] < 6 and row["type"] in ("admit", "release")]

        self.assertEqual(prefix(full["baseline"]), prefix(without_future["baseline"]))
        self.assertEqual(by_patient(full["baseline"])["flexible"]["snf_id"], "S2")
        self.assertEqual(full["ccn"]["summary"]["full_plan_wait_hours"], 0)
        self.assertGreater(full["baseline"]["summary"]["full_plan_wait_hours"], 0)

    def test_leftover_patient_keeps_original_ready_time_across_new_arrivals(self):
        data = make_scenario([
            make_patient("already_in_snf", hospital="B", ready=0, stay=10),
            make_patient("leftover", ready=1, stay=2),
            make_patient("new_arrival", ready=3, stay=1),
        ], horizon=8)
        result = compare(data)["baseline"]
        records = by_patient(result)
        self.assertEqual(records["leftover"]["ready_hour"], 1)
        self.assertEqual(records["leftover"]["wait_in_window_hours"], 10)
        self.assertEqual(records["new_arrival"]["wait_in_window_hours"], 7)
        self.assertEqual(result["summary"]["blocked_bed_hours"], 17)
        before_release = next(row for row in reversed(result["snapshots"]) if row["time_hour"] <= 8)
        self.assertEqual(set(before_release["waiting_patient_ids"]), {"leftover", "new_arrival"})
        self.assertEqual(before_release["hospital_waiting"]["A"], 2)
        final = result["snapshots"][-1]
        self.assertEqual(final["waiting_patient_ids"], [])
        self.assertEqual(final["completed_count"], 3)
        self.assertEqual(records["leftover"]["ready_hour"], 1)
        self.assertEqual(records["leftover"]["wait_hours"],
                         records["leftover"]["planned_admission_hour"] - 1)

    def test_seeded_same_hour_competition_is_repeatable_and_does_not_overlap(self):
        data = make_scenario([
            make_patient("A_request", hospital="A", stay=3),
            make_patient("B_request", hospital="B", stay=4),
            make_patient("C_request", hospital="C", stay=2),
        ], seed=17)
        original = deepcopy(data)
        first = compare(data)
        second = compare(data)
        self.assertEqual(data, original)
        for policy in ("baseline", "ccn"):
            self.assertEqual(first[policy]["patients"], second[policy]["patients"])
            self.assertEqual(first[policy]["events"], second[policy]["events"])
            records = first[policy]["patients"]
            intervals = sorted((row["planned_admission_hour"], row["planned_release_hour"])
                               for row in records)
            for earlier, later in zip(intervals, intervals[1:]):
                self.assertLessEqual(earlier[1], later[0])
            json.dumps(first[policy], allow_nan=False)
        admissions = [row for row in first["baseline"]["events"] if row["type"] == "admit"]
        self.assertEqual(len(admissions), 3)
        self.assertEqual([row["request_order"] for row in admissions],
                         sorted(row["request_order"] for row in admissions))
        self.assertTrue(all(row["request_hour"] <= row["time_hour"] for row in admissions))

    def test_small_ccn_eventual_objective_does_not_worsen_local_incumbent(self):
        cases = [
            make_scenario([make_patient("long", stay=9), make_patient("short", hospital="B", ready=1, stay=1)]),
            make_scenario([make_patient("flex", eligible=("S1", "S2"), stay=5),
                           make_patient("restricted", hospital="B", ready=2, eligible=("S1",), stay=3)],
                          beds=[make_bed("one"), make_bed("two", "S2")]),
            make_scenario([make_patient("A1", ready=0, stay=4), make_patient("A2", ready=2, stay=1),
                           make_patient("B1", hospital="B", ready=1, stay=3)]),
        ]
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                report = compare(data)
                self.assertLessEqual(report["ccn"]["summary"]["full_plan_wait_hours"],
                                     report["baseline"]["summary"]["full_plan_wait_hours"] + 1e-9)

    def test_rejected_hospital_replans_to_an_alternative_bed_at_the_same_hour(self):
        data = make_scenario([
            make_patient("flexible", hospital="A", eligible=("S1", "S2"), stay=8),
            make_patient("restricted", hospital="B", eligible=("S1",), stay=3),
        ], beds=[make_bed("one"), make_bed("two", "S2")], seed=42)
        result = compare(data)["baseline"]
        records = by_patient(result)
        self.assertEqual(records["restricted"]["bed_id"], "one")
        self.assertEqual(records["flexible"]["bed_id"], "two")
        self.assertEqual(records["restricted"]["admission_hour"], 0)
        self.assertEqual(records["flexible"]["admission_hour"], 0)
        requests = [row for row in result["events"] if row["type"] == "request"]
        self.assertEqual([row["request_order"] for row in requests], sorted(row["request_order"] for row in requests))
        rejected = [row for row in requests if not row["accepted"]]
        self.assertEqual(len(rejected), 1)
        self.assertEqual((rejected[0]["patient_id"], rejected[0]["bed_id"]), ("flexible", "one"))
        self.assertTrue(any(row["type"] == "request_rejected" for row in result["events"]))
        accepted = {row["request_order"]: row for row in requests if row["accepted"]}
        for admission in (row for row in result["events"] if row["type"] == "admit"):
            request = accepted[admission["request_order"]]
            self.assertEqual(admission["patient_id"], request["patient_id"])
            self.assertEqual(admission["bed_id"], request["bed_id"])
            self.assertEqual(admission["time_hour"], request["time_hour"])


class DynamicClinicalConstraintTests(unittest.TestCase):
    def test_insurance_care_gender_and_eligibility_all_apply_to_both_policies(self):
        snfs = [
            {"id": "S1", "accepted_insurances": ["public"]},
            {"id": "S2", "accepted_insurances": ["commercial"]},
            {"id": "S3", "accepted_insurances": ["*"]},
        ]
        data = make_scenario([
            make_patient("public_dialysis", eligible=("S2", "S1"), care=("dialysis",)),
            make_patient("commercial", insurance="commercial", eligible=("S1", "S2")),
            make_patient("wildcard", insurance="rare_plan", eligible=("S3",)),
            make_patient("wrong_insurance", insurance="rare_plan", eligible=("S1",)),
            make_patient("missing_care", eligible=("S1",), care=("dialysis", "ventilator")),
            make_patient("wrong_gender", eligible=("S1",), gender="male"),
            make_patient("no_eligible", eligible=()),
        ], beds=[make_bed("public_bed", gender="female", care=("dialysis",)),
                 make_bed("commercial_bed", "S2"), make_bed("wildcard_bed", "S3")],
           snfs=snfs, horizon=10)
        report = compare(data)
        for policy, result in ((key, report[key]) for key in ("baseline", "ccn")):
            with self.subTest(policy=policy):
                records = by_patient(result)
                self.assertEqual(records["public_dialysis"]["snf_id"], "S1")
                self.assertEqual(records["commercial"]["snf_id"], "S2")
                self.assertEqual(records["wildcard"]["snf_id"], "S3")
                blocked = {"wrong_insurance", "missing_care", "wrong_gender", "no_eligible"}
                self.assertEqual(set(result["summary"]["structurally_unplaceable_patient_ids"]), blocked)
                self.assertIsNone(result["summary"]["blocked_bed_hours"])
                self.assertIsNone(result["summary"]["full_plan_wait_hours"])
                self.assertIsNone(result["summary"]["time_to_completion_hours"])
                self.assertIsNone(result["summary"]["snf_utilization"])
                self.assertEqual(result["summary"]["observed_wait_to_playback_end_hours"], 8)
                self.assertFalse(report["observation"]["complete"])
                self.assertIsNone(report["observation"]["end_hour"])
                self.assertEqual(set(report["observation"]["unresolved_patient_ids"]), blocked)
                self.assertEqual(report["observation"]["playback_end_hour"], 2)
                for identifier in blocked:
                    self.assertIsNone(records[identifier]["planned_admission_hour"])
                    self.assertIsNone(records[identifier]["wait_hours"])
                    self.assertEqual(records[identifier]["wait_in_window_hours"], 2)

    def test_capabilities_are_required_as_a_set_on_the_same_bed(self):
        data = make_scenario([
            make_patient("combined", care=("dialysis", "ventilator")),
        ], beds=[make_bed("dialysis_only", care=("dialysis",)),
                 make_bed("ventilator_only", care=("ventilator",))])
        report = compare(data)
        for policy in ("baseline", "ccn"):
            self.assertEqual(report[policy]["summary"]["placed_count"], 0)
        data["beds"].append(make_bed("both", care=("dialysis", "ventilator")))
        report = compare(data)
        for policy in ("baseline", "ccn"):
            self.assertEqual(by_patient(report[policy])["combined"]["bed_id"], "both")

    def test_new_fields_reject_bad_shapes_and_invalid_policy(self):
        mutations = (
            lambda d: d.update(baseline_policy="unknown"),
            lambda d: d.update(tie_break_seed=True),
            lambda d: d.update(tie_break_seed=1.5),
            lambda d: d["patients"][0].update(insurance_type=[]),
            lambda d: d["patients"][0].update(insurance_type=""),
            lambda d: d["patients"][0].update(required_capabilities="dialysis"),
            lambda d: d["patients"][0].update(required_capabilities=[None]),
            lambda d: d["beds"][0].update(capabilities="dialysis"),
            lambda d: d["snfs"][0].update(accepted_insurances="public"),
            lambda d: d["snfs"][0].update(accepted_insurances=[None]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                data = make_scenario([make_patient("P")])
                mutate(data)
                with self.assertRaises(ValueError):
                    Scenario.from_dict(data)


class GeneratedDynamicScenarioTests(unittest.TestCase):
    def test_generated_population_is_repeatable_and_all_constraints_have_a_destination(self):
        from dynamic_scenarios import generate

        params = {"patient_count": 96, "hospital_count": 4, "snf_count": 6,
                  "bed_count": 24, "horizon_hours": 168, "seed": 42}
        data = generate(params)
        self.assertEqual(data, generate(params))
        self.assertNotEqual(data["patients"], generate({**params, "seed": 43})["patients"])
        canonical = Scenario.from_dict(data).to_dict()
        self.assertEqual(len(canonical["patients"]), 96)
        self.assertEqual(len(canonical["hospitals"]), 4)
        self.assertEqual(len(canonical["snfs"]), 6)
        self.assertEqual(len(canonical["beds"]), 24)
        self.assertEqual(canonical["horizon_hours"], 168)
        self.assertEqual(canonical["arrival_span_hours"], 168)
        self.assertGreater(max(row["ready_hour"] for row in canonical["patients"]), 72)
        self.assertGreater(len({row["insurance_type"] for row in canonical["patients"]}), 1)
        self.assertTrue(any(row["required_capabilities"] for row in canonical["patients"]))
        snfs = {row["id"]: row for row in canonical["snfs"]}
        for patient in canonical["patients"]:
            compatible = [bed for bed in canonical["beds"]
                          if bed["snf_id"] in patient["eligible_snf_ids"]
                          and bed["gender"] in (patient["gender"], "any")
                          and ("*" in snfs[bed["snf_id"]]["accepted_insurances"]
                               or patient["insurance_type"] in snfs[bed["snf_id"]]["accepted_insurances"])
                          and set(patient["required_capabilities"]) <= set(bed["capabilities"])]
            self.assertTrue(compatible, patient["id"])
        json.dumps(data, allow_nan=False)

    def test_large_example_crosses_old_limits_without_losing_patients(self):
        from dynamic_scenarios import generate

        data = generate({"patient_count": 240, "hospital_count": 8, "snf_count": 12,
                         "bed_count": 72, "horizon_hours": 336, "seed": 84})
        scenario = Scenario.from_dict(data)
        self.assertEqual(len(scenario.patients), 240)
        self.assertEqual(len(scenario.beds), 72)
        self.assertEqual(scenario.horizon_hours, 336)
        self.assertEqual(len({row["id"] for row in data["patients"]}), 240)

    def test_empty_populations_complete_at_zero_but_no_bed_cohorts_cannot_complete(self):
        from dynamic_scenarios import generate

        empty = generate({"patient_count": 0, "hospital_count": 0,
                          "snf_count": 0, "bed_count": 0, "horizon_hours": 24})
        empty_result = compare(empty)
        self.assertTrue(empty_result["observation"]["complete"])
        self.assertEqual(empty_result["observation"]["duration_hours"], 0)
        for policy in ("baseline", "ccn"):
            self.assertEqual(empty_result[policy]["summary"]["patient_count"], 0)
            self.assertEqual(empty_result[policy]["summary"]["blocked_bed_hours"], 0)
            self.assertEqual(empty_result[policy]["summary"]["time_to_completion_hours"], 0)
            self.assertIsNone(empty_result[policy]["summary"]["snf_utilization"])
        blocked = generate({"patient_count": 3, "hospital_count": 1,
                            "snf_count": 0, "bed_count": 0, "horizon_hours": 24})
        result = compare(blocked)
        last_ready = max(row["ready_hour"] for row in blocked["patients"])
        expected_partial_wait = sum(last_ready - row["ready_hour"] for row in blocked["patients"])
        self.assertFalse(result["observation"]["complete"])
        self.assertIsNone(result["observation"]["end_hour"])
        self.assertEqual(result["observation"]["playback_end_hour"], last_ready)
        for policy in ("baseline", "ccn"):
            self.assertEqual(result[policy]["summary"]["placed_count"], 0)
            self.assertIsNone(result[policy]["summary"]["blocked_bed_hours"])
            self.assertIsNone(result[policy]["summary"]["full_plan_wait_hours"])
            self.assertIsNone(result[policy]["summary"]["time_to_completion_hours"])
            self.assertIsNone(result[policy]["summary"]["snf_utilization"])
            self.assertAlmostEqual(result[policy]["summary"]["observed_wait_to_playback_end_hours"], expected_partial_wait)

    def test_generator_rejects_bad_counts_seed_and_inconsistent_empty_facilities(self):
        from dynamic_scenarios import generate

        invalid = [None, [], {"unknown": 1}, {"patient_count": -1}, {"patient_count": 501},
                   {"patient_count": True}, {"patient_count": 2.5}, {"hospital_count": 41},
                   {"snf_count": 41}, {"bed_count": 501}, {"bed_count": "2"},
                   {"horizon_hours": 0}, {"horizon_hours": float("inf")},
                   {"seed": -1}, {"seed": 2 ** 32}, {"seed": True}, {"seed": 1.5},
                   {"patient_count": 1, "hospital_count": 0}, {"snf_count": 0, "bed_count": 1}]
        for params in invalid:
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    generate(params)


if __name__ == "__main__":
    unittest.main()
