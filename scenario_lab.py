"""Serve a local scenario lab using the existing hospital placement models."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import errno
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import socket
from typing import Any
import webbrowser

import phase1
import phase1_workflow
import phase2
import dynamic_model


ROOT = Path(__file__).resolve().parent
MAX_PATIENTS = 50
MAX_BEDS = 100
MAX_FACILITIES = 12
MAX_BODY_BYTES = 1024 * 1024
MAX_NUMBER = 1_000_000_000
MODES = ("workflow", "allocation", "clinical")
EXAMPLE_FILES = {
    "workflow": "phase1_workflow_examples.json",
    "allocation": "phase1_examples.json",
    "clinical": "phase2_examples.json",
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/dynamic": ("dynamic.html", "text/html; charset=utf-8"),
    "/dynamic/": ("dynamic.html", "text/html; charset=utf-8"),
    "/dynamic.js": ("dynamic.js", "text/javascript; charset=utf-8"),
    "/dynamic.css": ("dynamic.css", "text/css; charset=utf-8"),
}
ASSUMPTIONS = {
    "workflow": [
        "Beds are unlimited, all SNFs are compatible, and the first referral is accepted.",
        "Patients independently start at their ready time; there are no shared-worker queues.",
        "Contact, review, and confirmation durations are explicit scenario inputs.",
        "Both workflows choose the nearest SNF; travel starts immediately after acceptance.",
        "The objective is total placement delay, the sum of T2 minus T1.",
    ],
    "allocation": [
        "Beds have static capacity and all patients are compatible with all SNFs.",
        "Greedy allocation follows patient input order and nearest available SNF.",
        "Global allocation maximizes placements, then minimizes total travel time.",
        "This allocation model does not calculate referral or placement delay.",
    ],
    "clinical": [
        "Compatibility uses room gender and required clinical capabilities.",
        "Both strategies process patients in input order with a static bed inventory.",
        "Sequential referral can incur the configured delay for each rejected bed.",
        "Compatibility and reservation use the existing heuristic to preserve later patients' options; it is not a global optimizer.",
        "Mean delay describes placed patients only; the total-delay objective is unavailable if anyone is unplaced.",
    ],
}


def _validate_json(value: Any) -> None:
    """Reject non-JSON values and numbers that could overflow model arithmetic."""
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 100_000:
            raise ValueError("Scenario JSON is too complex")
        if item is None or isinstance(item, (str, bool)):
            continue
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Scenario numbers must be finite")
            if abs(item) > MAX_NUMBER:
                raise ValueError(f"Scenario numbers must be between {-MAX_NUMBER} and {MAX_NUMBER}")
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Scenario object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        else:
            raise ValueError("Scenario must contain only JSON values")


def _validate_limits(mode: str, data: dict[str, Any]) -> None:
    for field, limit in (
        ("patients", MAX_PATIENTS),
        ("hospitals", MAX_FACILITIES),
        ("snfs", MAX_FACILITIES),
        ("beds", MAX_BEDS),
    ):
        rows = data.get(field)
        if isinstance(rows, list) and len(rows) > limit:
            raise ValueError(f"The scenario lab supports at most {limit} {field}")
    if mode == "allocation" and isinstance(data.get("snfs"), list):
        capacities = [
            row.get("available_beds")
            for row in data["snfs"]
            if isinstance(row, dict)
        ]
        if all(type(capacity) is int and capacity >= 0 for capacity in capacities):
            if sum(capacities) > MAX_BEDS:
                raise ValueError(f"The scenario lab supports at most {MAX_BEDS} beds in total")


def _scenario_model(mode: str, data: Any) -> Any:
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("mode must be workflow, allocation, or clinical")
    if not isinstance(data, dict):
        raise ValueError("scenario must be an object")
    _validate_json(data)
    _validate_limits(mode, data)
    validators = {
        "workflow": phase1_workflow.WorkflowScenario.from_dict,
        "allocation": phase1.Scenario.from_dict,
        "clinical": phase2.Scenario.from_dict,
    }
    try:
        return validators[mode](data)
    except (TypeError, KeyError, OverflowError, RecursionError) as error:
        raise ValueError("Invalid scenario fields; check IDs, references, and numeric values") from error


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def examples() -> dict[str, list[dict[str, Any]]]:
    """Return the repository's native example scenario objects, grouped by mode."""
    result = {}
    for mode, filename in EXAMPLE_FILES.items():
        document = json.loads(
            (ROOT / "scenarios" / filename).read_text(encoding="utf-8-sig"),
            parse_constant=_reject_constant,
        )
        records = document["scenarios"]
        for record in records:
            _scenario_model(mode, record)
        result[mode] = records
    return result


def _normalized(mode: str, label: str, result: Any) -> dict[str, Any]:
    if mode == "workflow":
        assignments = [item.to_dict() for item in result.placements]
        unplaced = []
        strategy = result.mode
        total_delay = result.total_placement_delay_minutes
        mean_delay = result.mean_placement_delay_minutes
        objective = total_delay
    else:
        assignments = [asdict(item) for item in result.assignments]
        unplaced = list(result.unplaced_patient_ids)
        strategy = result.strategy
        total_delay = None if mode == "allocation" else result.total_placement_delay_minutes
        mean_delay = total_delay / result.placed_count if total_delay is not None and result.placed_count else None
        objective = None if mode == "allocation" else result.objective_value_minutes
    return {
        "label": label,
        "strategy": strategy,
        "placed_count": result.placed_count,
        "unplaced_count": len(unplaced),
        "unplaced_patient_ids": unplaced,
        "total_travel_time_minutes": math.fsum(item["travel_time_minutes"] for item in assignments),
        "total_placement_delay_minutes": total_delay,
        "mean_placement_delay_minutes": mean_delay,
        "objective_value_minutes": objective,
        "assignments": assignments,
    }


def run_experiment(mode: str, data: Any) -> dict[str, Any]:
    """Validate native input and compare the two existing strategies for a mode."""
    scenario = _scenario_model(mode, data)
    if mode == "workflow":
        baseline = _normalized(mode, "Manual workflow", phase1_workflow.simulate_workflow(scenario, "manual"))
        ccn = _normalized(mode, "Digital workflow", phase1_workflow.simulate_workflow(scenario, "digital"))
    elif mode == "allocation":
        baseline = _normalized(mode, "Greedy nearest available", phase1.greedy_allocate(scenario))
        ccn = _normalized(mode, "Global allocation", phase1.global_allocate(scenario))
    else:
        baseline = _normalized(mode, "Sequential referral", phase2.baseline_match(scenario))
        ccn = _normalized(mode, "Compatibility + reservation", phase2.ccn_match(scenario))
    same_patients = {item["patient_id"] for item in baseline["assignments"]} == {
        item["patient_id"] for item in ccn["assignments"]
    }
    delay_known = baseline["objective_value_minutes"] is not None and ccn["objective_value_minutes"] is not None
    response = {
        "mode": mode,
        "scenario": data,
        "baseline": baseline,
        "ccn": ccn,
        "comparison": {
            "placement_gain": ccn["placed_count"] - baseline["placed_count"],
            "same_placed_patients": same_patients,
            "travel_reduction_minutes": baseline["total_travel_time_minutes"] - ccn["total_travel_time_minutes"] if same_patients else None,
            "delay_reduction_minutes": baseline["objective_value_minutes"] - ccn["objective_value_minutes"] if delay_known else None,
        },
        "assumptions": list(ASSUMPTIONS[mode]),
    }
    # Keep both the direct API and the HTTP response strictly finite JSON.
    json.dumps(response, allow_nan=False)
    return response


class ScenarioLabHandler(BaseHTTPRequestHandler):
    """Serve only the lab's public assets and JSON endpoints."""

    protocol_version = "HTTP/1.1"
    server_version = "ScenarioLab"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str, *, head: bool = False) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, *, head: bool = False) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", head=head)

    def _error(self, status: int, message: str, *, head: bool = False) -> None:
        self._json(status, {"error": message}, head=head)

    def _same_origin(self) -> bool:
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed_hosts.update(("127.0.0.1", "localhost"))
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in allowed_hosts:
            self._error(400, "Use the local scenario lab URL")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0].lower() != f"http://{hosts[0].lower()}"):
            self._error(400, "Only same-origin requests are accepted")
            return False
        return True

    def _get(self, *, head: bool = False) -> None:
        if not self._same_origin():
            return
        path = self.path.partition("?")[0]
        if path == "/api/examples":
            try:
                self._json(200, examples(), head=head)
            except (OSError, ValueError, KeyError):
                self._error(500, "The example scenarios could not be loaded", head=head)
        elif path == "/api/dynamic/examples":
            try:
                self._json(200, dynamic_model.examples(), head=head)
            except (OSError, ValueError, KeyError):
                self._error(500, "The dynamic examples could not be loaded", head=head)
        elif path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            try:
                content = (ROOT / "web" / filename).read_bytes()
            except OSError:
                self._error(404, "File not found", head=head)
                return
            self._send(200, content, content_type, head=head)
        else:
            self._error(404, "Route not found", head=head)

    def do_GET(self) -> None:
        self._get()

    def do_HEAD(self) -> None:
        self._get(head=True)

    def do_POST(self) -> None:
        if not self._same_origin():
            return
        path = self.path.partition("?")[0]
        if path not in ("/api/run", "/api/dynamic/run"):
            self._error(404, "Route not found")
            return
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
            self._error(400, "Content-Type must be application/json")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, "A valid Content-Length is required")
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self._error(400, "A valid Content-Length is required")
            return
        if not 0 < length <= MAX_BODY_BYTES:
            self._error(400, f"JSON body must contain 1 to {MAX_BODY_BYTES} bytes")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete JSON body")
            payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object")
            if path == "/api/dynamic/run":
                _validate_json(payload.get("scenario"))
                response = dynamic_model.compare(payload.get("scenario"))
            else:
                response = run_experiment(payload.get("mode"), payload.get("scenario"))
        except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Invalid JSON request or scenario"
            self._error(400, message)
            return
        except (TimeoutError, OSError):
            self._error(400, "Could not read the JSON request body")
            return
        except Exception:
            self._error(500, "The experiment could not be completed")
            return
        self._json(200, response)

    def do_OPTIONS(self) -> None:
        self._error(404, "Route not found")


class ScenarioLabServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits competing listeners on the same port.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port: int = 0) -> ThreadingHTTPServer:
    """Create a loopback-only server without starting its request loop."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    return ScenarioLabServer(("127.0.0.1", port), ScenarioLabHandler)


def _running_lab(port: int) -> bool:
    """Check an occupied local port before reusing it for the browser launcher."""
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/api/examples")
        response = connection.getresponse()
        if response.status != 200 or not response.getheader("Server", "").startswith("ScenarioLab "):
            return False
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            return False
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        return (
            isinstance(payload, dict)
            and set(payload) == set(MODES)
            and all(isinstance(payload[mode], list) for mode in MODES)
        )
    except (OSError, ValueError, HTTPException, RecursionError):
        return False
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    parser.add_argument("--open", action="store_true", help="Open the lab in your browser")
    args = parser.parse_args()
    try:
        server = create_server(args.port)
    except OSError as error:
        port_in_use = error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) == 10048
        if args.open and port_in_use and _running_lab(args.port):
            url = f"http://127.0.0.1:{args.port}/"
            print(f"Using running scenario lab: {url}", flush=True)
            webbrowser.open(url)
            return
        parser.error(str(error))
    except ValueError as error:
        parser.error(str(error))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Scenario lab: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
