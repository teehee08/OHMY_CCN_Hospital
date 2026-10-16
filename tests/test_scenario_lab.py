"""Integration checks for the scenario lab's native-model adapters and JSON API."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import call, patch

import phase1
import phase1_workflow
import phase2
import scenario_lab


ROOT = Path(__file__).parents[1]
EXAMPLE_FILES = {
    "workflow": "phase1_workflow_examples.json",
    "allocation": "phase1_examples.json",
    "clinical": "phase2_examples.json",
}


def example(mode, name):
    return next(row for row in scenario_lab.examples()[mode] if row["name"] == name)


def json_value(value):
    """Compare the native dataclasses' tuple exports with their JSON form."""
    return json.loads(json.dumps(value, allow_nan=False))


class ScenarioLabAdapterTests(unittest.TestCase):
    def assert_normalized(self, result, patient_count):
        self.assertIn("comparison", result)
        for side in ("baseline", "ccn"):
            with self.subTest(side=side):
                metrics = result[side]
                self.assertEqual(metrics["placed_count"], len(metrics["assignments"]))
                self.assertEqual(metrics["unplaced_count"], len(metrics["unplaced_patient_ids"]))
                self.assertEqual(metrics["placed_count"] + metrics["unplaced_count"], patient_count)
                self.assertEqual(
                    metrics["total_travel_time_minutes"],
                    sum(row["travel_time_minutes"] for row in metrics["assignments"]),
                )
                self.assertTrue({
                    "total_placement_delay_minutes",
                    "mean_placement_delay_minutes",
                    "objective_value_minutes",
                }.issubset(metrics))
        self.assertEqual(json_value(result), result)

    def test_examples_preserve_the_native_scenario_documents(self):
        actual = scenario_lab.examples()
        self.assertEqual(set(actual), set(EXAMPLE_FILES))
        for mode, filename in EXAMPLE_FILES.items():
            with self.subTest(mode=mode):
                native = json.loads((ROOT / "scenarios" / filename).read_text(encoding="utf-8"))
                self.assertEqual(actual[mode], native["scenarios"])
                self.assertTrue(actual[mode])

    def test_examples_return_independent_editable_data(self):
        first = scenario_lab.examples()
        original = deepcopy(first)
        for rows in first.values():
            rows[0]["patients"][0]["id"] = "edited_patient"
            rows[0]["name"] = "edited_example"
        self.assertEqual(scenario_lab.examples(), original)

    def test_all_examples_match_the_existing_models(self):
        for mode, rows in scenario_lab.examples().items():
            for data in rows:
                with self.subTest(mode=mode, scenario=data["name"]):
                    actual = scenario_lab.run_experiment(mode, data)
                    self.assert_normalized(actual, len(data["patients"]))
                    if mode == "workflow":
                        native_scenario = phase1_workflow.WorkflowScenario.from_dict(data)
                        native_results = (
                            phase1_workflow.simulate_workflow(native_scenario, "manual"),
                            phase1_workflow.simulate_workflow(native_scenario, "digital"),
                        )
                    elif mode == "allocation":
                        native_scenario = phase1.Scenario.from_dict(data)
                        native_results = (
                            phase1.greedy_allocate(native_scenario),
                            phase1.global_allocate(native_scenario),
                        )
                    else:
                        native_scenario = phase2.Scenario.from_dict(data)
                        native_results = (
                            phase2.baseline_match(native_scenario),
                            phase2.ccn_match(native_scenario),
                        )
                    for side, native in zip(("baseline", "ccn"), native_results):
                        metrics = actual[side]
                        expected = json_value(native.to_dict())
                        self.assertEqual(metrics["placed_count"], expected["placed_count"])
                        assignment_key = "placements" if mode == "workflow" else "assignments"
                        self.assertEqual(metrics["assignments"], expected[assignment_key])
                        self.assertEqual(metrics["unplaced_patient_ids"], expected.get("unplaced_patient_ids", []))
                        if mode == "allocation":
                            for field in (
                                "total_placement_delay_minutes",
                                "mean_placement_delay_minutes",
                                "objective_value_minutes",
                            ):
                                self.assertIsNone(metrics[field])
                        else:
                            total = expected["total_placement_delay_minutes"]
                            self.assertEqual(metrics["total_placement_delay_minutes"], total)
                            mean = total / expected["placed_count"] if expected["placed_count"] else None
                            self.assertEqual(metrics["mean_placement_delay_minutes"], mean)
                            self.assertEqual(metrics["objective_value_minutes"], expected.get("objective_value_minutes", total))

    def test_shorter_workflow_reports_confirmation_delay_separately_from_travel(self):
        data = example("workflow", "shorter_digital_processing")
        result = scenario_lab.run_experiment("workflow", data)
        self.assertEqual(result["baseline"]["mean_placement_delay_minutes"], 120)
        self.assertEqual(result["ccn"]["mean_placement_delay_minutes"], 10)
        self.assertEqual(result["baseline"]["objective_value_minutes"], 240)
        self.assertEqual(result["ccn"]["objective_value_minutes"], 20)
        self.assertEqual(result["comparison"]["delay_reduction_minutes"], 220)
        self.assertEqual(result["comparison"]["travel_reduction_minutes"], 0)
        self.assertEqual(result["comparison"]["placement_gain"], 0)
        self.assertTrue(result["comparison"]["same_placed_patients"])
        for side in ("baseline", "ccn"):
            self.assertEqual(result[side]["total_travel_time_minutes"], 9)
            for row in result[side]["assignments"]:
                self.assertEqual(row["arrival_time_minutes"], row["t2_minutes"] + row["travel_time_minutes"])
                self.assertEqual(row["placement_delay_minutes"], row["t2_minutes"] - row["t1_minutes"])

    def test_slower_digital_assumptions_can_produce_a_negative_delay_reduction(self):
        data = example("workflow", "longer_digital_processing")
        result = scenario_lab.run_experiment("workflow", data)
        self.assertEqual(result["baseline"]["mean_placement_delay_minutes"], 20)
        self.assertEqual(result["ccn"]["mean_placement_delay_minutes"], 30)
        self.assertEqual(result["comparison"]["delay_reduction_minutes"], -20)

    def test_limited_allocation_reports_travel_improvement_without_delay_metrics(self):
        data = example("allocation", "limited_beds_a_first")
        result = scenario_lab.run_experiment("allocation", data)
        self.assertEqual(result["baseline"]["placed_count"], 2)
        self.assertEqual(result["ccn"]["placed_count"], 2)
        self.assertEqual(result["baseline"]["total_travel_time_minutes"], 18)
        self.assertEqual(result["ccn"]["total_travel_time_minutes"], 10)
        self.assertEqual(result["comparison"]["travel_reduction_minutes"], 8)
        self.assertIsNone(result["comparison"]["delay_reduction_minutes"])

    def test_ghost_bed_preserves_rejection_details_and_delay_reduction(self):
        data = example("clinical", "ghost_bed_gender_mismatch")
        result = scenario_lab.run_experiment("clinical", data)
        self.assertEqual(result["baseline"]["objective_value_minutes"], 120)
        self.assertEqual(result["ccn"]["objective_value_minutes"], 0)
        self.assertEqual(result["baseline"]["assignments"][0]["rejection_count"], 1)
        self.assertEqual(result["ccn"]["assignments"][0]["rejection_count"], 0)
        self.assertEqual(result["comparison"]["delay_reduction_minutes"], 120)
        self.assertEqual(result["comparison"]["travel_reduction_minutes"], 0)

    def test_bariatric_reservation_reports_blocked_population_and_placement_gain(self):
        data = example("clinical", "hard_dependency_bariatric_reservation")
        result = scenario_lab.run_experiment("clinical", data)
        self.assertEqual(result["baseline"]["placed_count"], 1)
        self.assertEqual(result["baseline"]["unplaced_patient_ids"], ["bariatric_patient"])
        self.assertEqual(result["baseline"]["total_placement_delay_minutes"], 0)
        self.assertIsNone(result["baseline"]["objective_value_minutes"])
        self.assertEqual(result["ccn"]["placed_count"], 2)
        self.assertEqual(result["ccn"]["objective_value_minutes"], 0)
        self.assertEqual(result["comparison"]["placement_gain"], 1)
        self.assertFalse(result["comparison"]["same_placed_patients"])
        self.assertIsNone(result["comparison"]["travel_reduction_minutes"])
        self.assertIsNone(result["comparison"]["delay_reduction_minutes"])

    def test_equal_placement_counts_do_not_imply_the_same_patients(self):
        data = {
            "name": "different_placed_patients",
            "hospitals": [{"id": "far"}, {"id": "near"}],
            "snfs": [{"id": "s", "available_beds": 1}],
            "patients": [
                {"id": "first_far", "hospital_id": "far"},
                {"id": "second_near", "hospital_id": "near"},
            ],
            "travel_time_minutes": {"far": {"s": 100}, "near": {"s": 1}},
        }
        result = scenario_lab.run_experiment("allocation", data)
        self.assertEqual(result["baseline"]["assignments"][0]["patient_id"], "first_far")
        self.assertEqual(result["ccn"]["assignments"][0]["patient_id"], "second_near")
        self.assertEqual(result["comparison"]["placement_gain"], 0)
        self.assertFalse(result["comparison"]["same_placed_patients"])
        self.assertIsNone(result["comparison"]["travel_reduction_minutes"])

    def test_changed_inputs_are_used_and_runs_do_not_mutate_the_scenario(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = deepcopy(rows[0])
                for row in data["travel_time_minutes"].values():
                    for snf_id in row:
                        row[snf_id] = 37
                before = deepcopy(data)
                first = scenario_lab.run_experiment(mode, data)
                second = scenario_lab.run_experiment(mode, data)
                self.assertEqual(first, second)
                self.assertEqual(data, before)
                for side in ("baseline", "ccn"):
                    self.assertEqual(first[side]["total_travel_time_minutes"], 37 * first[side]["placed_count"])

    def test_empty_patient_lists_have_defined_normalized_results(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = deepcopy(rows[0])
                data["patients"] = []
                result = scenario_lab.run_experiment(mode, data)
                self.assert_normalized(result, 0)
                for side in ("baseline", "ccn"):
                    self.assertEqual(result[side]["unplaced_patient_ids"], [])
                    self.assertEqual(result[side]["total_travel_time_minutes"], 0)
                    self.assertIsNone(result[side]["mean_placement_delay_minutes"])
                self.assertTrue(result["comparison"]["same_placed_patients"])
                self.assertEqual(result["comparison"]["travel_reduction_minutes"], 0)

    def test_unknown_modes_and_nonobject_scenarios_are_rejected(self):
        valid = scenario_lab.examples()["allocation"][0]
        for mode in ("unknown", "", None, [], True):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    scenario_lab.run_experiment(mode, valid)
        for data in (None, [], "scenario", 1):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    scenario_lab.run_experiment("allocation", data)

    def test_native_reference_errors_are_rejected_in_every_mode(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = deepcopy(rows[0])
                data["patients"][0]["hospital_id"] = "missing_hospital"
                with self.assertRaises(ValueError):
                    scenario_lab.run_experiment(mode, data)
        data = scenario_lab.examples()["clinical"][0]
        data["beds"][0]["snf_id"] = "missing_snf"
        with self.assertRaises(ValueError):
            scenario_lab.run_experiment("clinical", data)

    def test_scenarios_over_the_patient_limit_are_rejected(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = deepcopy(rows[0])
                patient = data["patients"][0]
                data["patients"] = [dict(patient, id=f"patient_{index}") for index in range(51)]
                with self.assertRaises(ValueError):
                    scenario_lab.run_experiment(mode, data)

    def test_scenarios_over_the_bed_or_capacity_limit_are_rejected(self):
        data = example("allocation", "limited_beds_a_first")
        data["snfs"][0]["available_beds"] = 51
        data["snfs"][1]["available_beds"] = 50
        with self.assertRaises(ValueError):
            scenario_lab.run_experiment("allocation", data)
        data = scenario_lab.examples()["clinical"][0]
        bed = data["beds"][0]
        data["beds"] = [dict(bed, id=f"bed_{index}") for index in range(101)]
        with self.assertRaises(ValueError):
            scenario_lab.run_experiment("clinical", data)

    def test_scenarios_over_the_facility_limits_are_rejected(self):
        for mode, rows in scenario_lab.examples().items():
            for field in ("hospitals", "snfs"):
                with self.subTest(mode=mode, field=field):
                    data = deepcopy(rows[0])
                    while len(data[field]) < 13:
                        row = {"id": f"extra_{field}_{len(data[field])}"}
                        if mode == "allocation" and field == "snfs":
                            row["available_beds"] = 0
                        data[field].append(row)
                    data["travel_time_minutes"] = {
                        hospital["id"]: {snf["id"]: 1 for snf in data["snfs"]}
                        for hospital in data["hospitals"]
                    }
                    with self.assertRaises(ValueError):
                        scenario_lab.run_experiment(mode, data)

    def test_patient_limit_itself_is_accepted(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = deepcopy(rows[0])
                patient = data["patients"][0]
                data["patients"] = [dict(patient, id=f"patient_{index}") for index in range(50)]
                result = scenario_lab.run_experiment(mode, data)
                self.assert_normalized(result, 50)

    def test_nonfinite_negative_and_excessive_minutes_are_rejected(self):
        for mode, rows in scenario_lab.examples().items():
            for invalid in (float("nan"), float("inf"), -1, True, 1_000_000_001):
                with self.subTest(mode=mode, value=invalid):
                    data = deepcopy(rows[0])
                    hospital_id = data["hospitals"][0]["id"]
                    snf_id = data["snfs"][0]["id"]
                    data["travel_time_minutes"][hospital_id][snf_id] = invalid
                    with self.assertRaises(ValueError):
                        scenario_lab.run_experiment(mode, data)


class ScenarioLabHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = scenario_lab.create_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            request_headers.update(headers)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, response.getheader("Content-Type"), payload
        finally:
            connection.close()

    def request_json(self, method, path, value):
        status, content_type, payload = self.request(method, path, json.dumps(value).encode("utf-8"))
        self.assertTrue(content_type.startswith("application/json"))
        return status, json.loads(payload)

    def test_examples_endpoint_returns_native_editable_scenarios(self):
        status, content_type, payload = self.request("GET", "/api/examples")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertEqual(json.loads(payload), scenario_lab.examples())

    def test_run_endpoint_matches_the_python_adapter_in_every_mode(self):
        for mode, rows in scenario_lab.examples().items():
            with self.subTest(mode=mode):
                data = rows[-1]
                status, result = self.request_json("POST", "/api/run", {"mode": mode, "scenario": data})
                self.assertEqual(status, 200)
                self.assertEqual(result, scenario_lab.run_experiment(mode, data))

    def test_malformed_json_returns_a_json_400_and_server_remains_usable(self):
        status, content_type, payload = self.request("POST", "/api/run", b'{"mode":')
        self.assertEqual(status, 400)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertTrue(json.loads(payload).get("error"))
        status, _, _ = self.request("GET", "/api/examples")
        self.assertEqual(status, 200)

    def test_invalid_request_shapes_and_model_input_return_json_400(self):
        valid = scenario_lab.examples()["workflow"][0]
        invalid_reference = deepcopy(valid)
        invalid_reference["patients"][0]["hospital_id"] = "missing_hospital"
        requests = (
            None,
            [],
            {},
            {"mode": "unknown", "scenario": valid},
            {"mode": "workflow", "scenario": invalid_reference},
        )
        for value in requests:
            with self.subTest(value=value):
                status, result = self.request_json("POST", "/api/run", value)
                self.assertEqual(status, 400)
                self.assertTrue(result.get("error"))

    def test_unknown_api_routes_return_json_404(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, content_type, payload = self.request(method, "/api/missing", b"{}" if method == "POST" else None)
                self.assertEqual(status, 404)
                self.assertTrue(content_type.startswith("application/json"))
                self.assertTrue(json.loads(payload).get("error"))

    def test_invalid_encoding_and_nonstandard_numbers_return_json_400(self):
        for body in (b"\xff", b'{"mode": "workflow", "scenario": {"invalid": NaN}}'):
            with self.subTest(body=body):
                status, content_type, payload = self.request("POST", "/api/run", body)
                self.assertEqual(status, 400)
                self.assertTrue(content_type.startswith("application/json"))
                self.assertTrue(json.loads(payload).get("error"))

    def test_oversized_declared_body_is_rejected_before_reading_it(self):
        status, content_type, payload = self.request(
            "POST", "/api/run", b"{}", {"Content-Length": str(1024 * 1024 + 1)}
        )
        self.assertEqual(status, 400)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertTrue(json.loads(payload).get("error"))

    def test_json_content_type_and_local_origin_are_required(self):
        data = scenario_lab.examples()["workflow"][0]
        body = json.dumps({"mode": "workflow", "scenario": data}).encode("utf-8")
        rejected_headers = (
            {"Content-Type": "text/plain"},
            {"Origin": "https://unrelated.example"},
            {"Host": "unrelated.example"},
        )
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                status, content_type, payload = self.request("POST", "/api/run", body, headers)
                self.assertEqual(status, 400)
                self.assertTrue(content_type.startswith("application/json"))
                self.assertTrue(json.loads(payload).get("error"))
        port = self.server.server_address[1]
        status, _, _ = self.request("POST", "/api/run", body, {"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(status, 200)

    def test_repeated_open_reuses_the_running_lab_and_returns(self):
        port = self.server.server_address[1]
        url = f"http://127.0.0.1:{port}/"
        output = io.StringIO()
        with (
            patch("sys.argv", ["scenario_lab.py", "--port", str(port), "--open"]),
            patch.object(scenario_lab.webbrowser, "open") as open_browser,
            patch.object(ThreadingHTTPServer, "serve_forever", side_effect=AssertionError("Occupied-port launch must not start another server")),
            redirect_stdout(output),
        ):
            for _ in range(2):
                self.assertIsNone(scenario_lab.main())
        self.assertEqual(open_browser.call_args_list, [call(url), call(url)])
        self.assertEqual(output.getvalue().count(url), 2)

    def test_open_refuses_an_unrelated_http_service_with_similar_json(self):
        body = json.dumps(scenario_lab.examples()).encode("utf-8")

        class UnrelatedHandler(BaseHTTPRequestHandler):
            server_version = "UnrelatedService"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        unrelated = ThreadingHTTPServer(("127.0.0.1", 0), UnrelatedHandler)
        worker = threading.Thread(target=unrelated.serve_forever, daemon=True)
        worker.start()
        try:
            port = unrelated.server_address[1]
            with (
                patch("sys.argv", ["scenario_lab.py", "--port", str(port), "--open"]),
                patch.object(scenario_lab.webbrowser, "open") as open_browser,
                patch.object(ThreadingHTTPServer, "serve_forever", side_effect=AssertionError("Occupied-port launch must not start another server")),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    scenario_lab.main()
                self.assertEqual(raised.exception.code, 2)
                open_browser.assert_not_called()
        finally:
            unrelated.shutdown()
            worker.join(timeout=5)
            unrelated.server_close()

    def test_occupied_port_without_open_remains_a_cli_error(self):
        port = self.server.server_address[1]
        with (
            patch("sys.argv", ["scenario_lab.py", "--port", str(port)]),
            patch.object(scenario_lab.webbrowser, "open") as open_browser,
            patch.object(ThreadingHTTPServer, "serve_forever", side_effect=AssertionError("Occupied-port launch must not start another server")),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                scenario_lab.main()
            self.assertEqual(raised.exception.code, 2)
            open_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
