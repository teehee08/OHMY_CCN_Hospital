"""HTTP integration checks for the separate dynamic lab and legacy isolation."""

import http.client
import json
from pathlib import Path
import threading
import unittest

import dynamic_model
import scenario_lab


ROOT = Path(__file__).parents[1]


def default_scenario():
    document = json.loads((ROOT / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8-sig"))
    return document["scenarios"][0]


def reject_nonfinite(value):
    raise ValueError(f"Nonfinite number in response: {value}")


class DynamicLabHttpTests(unittest.TestCase):
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
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def request_json(self, method, path, value=None, *, headers=None):
        body = None if method in ("GET", "HEAD") else json.dumps(value).encode("utf-8")
        status, response_headers, payload = self.request(method, path, body, headers)
        self.assertTrue(response_headers["Content-Type"].startswith("application/json"))
        result = json.loads(payload, parse_constant=reject_nonfinite)
        json.dumps(result, allow_nan=False)
        return status, result

    def test_dynamic_examples_are_editable_inputs_with_explicit_limits(self):
        status, document = self.request_json("GET", "/api/dynamic/examples")
        self.assertEqual(status, 200)
        self.assertEqual(set(document), {"scenarios", "limits"})
        source = json.loads((ROOT / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(document["scenarios"], source["scenarios"])
        self.assertEqual(document["limits"], {"max_patients": 50, "max_beds": 100, "max_facilities": 12})
        document["scenarios"][0]["patients"][0]["id"] = "consumer_edit"
        _, second = self.request_json("GET", "/api/dynamic/examples")
        self.assertEqual(second["scenarios"], source["scenarios"])

    def test_dynamic_run_returns_native_finite_results_and_expected_default_effect(self):
        data = default_scenario()
        status, result = self.request_json("POST", "/api/dynamic/run", {"scenario": data})
        self.assertEqual(status, 200)
        self.assertEqual(result, json.loads(json.dumps(dynamic_model.compare(data), allow_nan=False)))
        self.assertEqual(result["baseline"]["summary"]["blocked_bed_hours"], 52)
        self.assertEqual(result["ccn"]["summary"]["blocked_bed_hours"], 0)
        self.assertTrue(result["assumptions"])

    def test_original_examples_keep_exactly_three_legacy_modes(self):
        status, result = self.request_json("GET", "/api/examples")
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"workflow", "allocation", "clinical"})
        self.assertEqual(result, scenario_lab.examples())
        for mode, filename in (
            ("workflow", "phase1_workflow_examples.json"),
            ("allocation", "phase1_examples.json"),
            ("clinical", "phase2_examples.json"),
        ):
            native = json.loads((ROOT / "scenarios" / filename).read_text(encoding="utf-8-sig"))
            self.assertEqual(result[mode], native["scenarios"])

    def test_dynamic_assets_are_served_with_their_content_types_and_support_head(self):
        for path, content_type in (
            ("/dynamic", "text/html"),
            ("/dynamic.js", "text/javascript"),
            ("/dynamic.css", "text/css"),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                status, _, head_body = self.request("HEAD", path)
                self.assertEqual(status, 200)
                self.assertEqual(head_body, b"")

    def test_invalid_shapes_references_and_times_return_json_400(self):
        invalid_reference = default_scenario()
        invalid_reference["patients"][0]["hospital_id"] = "unknown_hospital"
        invalid_stay = default_scenario()
        invalid_stay["patients"][0]["length_of_stay_hours"] = 0
        invalid_ready = default_scenario()
        invalid_ready["patients"][0]["ready_hour"] = invalid_ready["horizon_hours"] + 1
        for value in (None, [], {}, {"scenario": None}, {"scenario": []},
                      {"scenario": invalid_reference}, {"scenario": invalid_stay}, {"scenario": invalid_ready}):
            with self.subTest(value=value):
                status, result = self.request_json("POST", "/api/dynamic/run", value)
                self.assertEqual(status, 400)
                self.assertTrue(result.get("error"))

    def test_malformed_json_encoding_and_nonfinite_numbers_do_not_break_server(self):
        for body in (b'{"scenario":', b"\xff", b'{"scenario":{"horizon_hours":NaN}}'):
            with self.subTest(body=body):
                status, headers, payload = self.request("POST", "/api/dynamic/run", body)
                self.assertEqual(status, 400)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                self.assertTrue(json.loads(payload, parse_constant=reject_nonfinite).get("error"))
        status, result = self.request_json("POST", "/api/dynamic/run", {"scenario": default_scenario()})
        self.assertEqual(status, 200)
        self.assertEqual(result["ccn"]["summary"]["patient_count"], 5)

    def test_dynamic_routes_apply_content_type_host_and_origin_protections(self):
        body = json.dumps({"scenario": default_scenario()}).encode("utf-8")
        for headers in (
            {"Content-Type": "text/plain"},
            {"Host": "unrelated.example"},
            {"Origin": "https://unrelated.example"},
        ):
            with self.subTest(headers=headers):
                status, response_headers, payload = self.request("POST", "/api/dynamic/run", body, headers)
                self.assertEqual(status, 400)
                self.assertTrue(response_headers["Content-Type"].startswith("application/json"))
                self.assertTrue(json.loads(payload).get("error"))
        for path in ("/api/dynamic/examples", "/dynamic", "/dynamic.js", "/dynamic.css"):
            with self.subTest(get_path=path):
                status, _, _ = self.request("GET", path, headers={"Host": "unrelated.example"})
                self.assertEqual(status, 400)
        port = self.server.server_address[1]
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(valid_host=host):
                status, result = self.request_json(
                    "POST", "/api/dynamic/run", {"scenario": default_scenario()},
                    headers={"Host": f"{host}:{port}", "Origin": f"http://{host}:{port}"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(result["baseline"]["summary"]["patient_count"], 5)

    def test_oversized_declared_body_is_rejected_without_waiting_for_body(self):
        # Send headers only: rejecting the size must not require receiving it.
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest("POST", "/api/dynamic/run")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(1024 * 1024 + 1))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertTrue(json.loads(response.read()).get("error"))
        finally:
            connection.close()

    def test_dynamic_unknown_routes_and_nonpublic_files_are_not_served(self):
        for path in ("/api/dynamic/missing", "/dynamic/../dynamic_model.py", "/dynamic_model.py"):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertTrue(json.loads(payload).get("error"))
        status, _, payload = self.request("POST", "/api/dynamic/missing")
        self.assertEqual(status, 404)
        self.assertTrue(json.loads(payload).get("error"))


if __name__ == "__main__":
    unittest.main()
