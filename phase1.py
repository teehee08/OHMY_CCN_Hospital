"""Compare sequential nearest-bed allocation with joint static allocation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from fractions import Fraction
import json
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Hospital:
    id: str


@dataclass(frozen=True)
class SNF:
    id: str
    available_beds: int


@dataclass(frozen=True)
class Patient:
    id: str
    hospital_id: str


def _records(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    records = data.get(key)
    if not isinstance(records, list) or any(
        not isinstance(record, dict) for record in records
    ):
        raise ValueError(f"{key} must be a list of objects")
    ids = [record.get("id") for record in records]
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ValueError(f"Each {key} entry needs a nonempty string id")
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate id in {key}")
    return records


@dataclass(frozen=True)
class Scenario:
    name: str
    hospitals: tuple[Hospital, ...]
    snfs: tuple[SNF, ...]
    patients: tuple[Patient, ...]
    travel_time_minutes: dict[str, dict[str, float]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        """Validate the shared input format used by both allocation strategies."""
        if not isinstance(data, dict):
            raise ValueError("Each scenario must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Scenario name must be a nonempty string")
        hospital_rows = _records(data, "hospitals")
        snf_rows = _records(data, "snfs")
        patient_rows = _records(data, "patients")
        hospital_ids = {row["id"] for row in hospital_rows}
        snf_ids = {row["id"] for row in snf_rows}
        for row in snf_rows:
            beds = row.get("available_beds")
            if type(beds) is not int or beds < 0:
                raise ValueError(
                    f"SNF {row['id']}: available_beds must be a nonnegative integer"
                )
        for row in patient_rows:
            hospital_id = row.get("hospital_id")
            if not isinstance(hospital_id, str) or hospital_id not in hospital_ids:
                raise ValueError(f"Patient {row['id']}: unknown hospital_id")

        raw_times = data.get("travel_time_minutes")
        if not isinstance(raw_times, dict) or set(raw_times) != hospital_ids:
            raise ValueError("travel_time_minutes must have one row per hospital id")
        travel_times: dict[str, dict[str, float]] = {}
        for hospital in hospital_rows:
            hospital_id = hospital["id"]
            row = raw_times[hospital_id]
            if not isinstance(row, dict) or set(row) != snf_ids:
                raise ValueError(f"Hospital {hospital_id}: provide one travel time per SNF id")
            travel_times[hospital_id] = {}
            for snf_id, value in row.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("Travel times must be finite nonnegative numbers")
                try:
                    minutes = float(value)
                except OverflowError as error:
                    raise ValueError("Travel times must be finite nonnegative numbers") from error
                if not math.isfinite(minutes) or minutes < 0:
                    raise ValueError("Travel times must be finite nonnegative numbers")
                travel_times[hospital_id][snf_id] = minutes
        return cls(
            name=name,
            hospitals=tuple(Hospital(row["id"]) for row in hospital_rows),
            snfs=tuple(SNF(row["id"], row["available_beds"]) for row in snf_rows),
            patients=tuple(Patient(row["id"], row["hospital_id"]) for row in patient_rows),
            travel_time_minutes=travel_times,
        )


@dataclass(frozen=True)
class Assignment:
    patient_id: str
    hospital_id: str
    snf_id: str
    travel_time_minutes: float


@dataclass(frozen=True)
class AllocationResult:
    strategy: str
    assignments: tuple[Assignment, ...]
    unplaced_patient_ids: tuple[str, ...]
    total_travel_time_minutes: float

    @property
    def placed_count(self) -> int:
        return len(self.assignments)

    @property
    def mean_travel_time_minutes(self) -> float | None:
        if not self.assignments:
            return None
        return self.total_travel_time_minutes / self.placed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "placed_count": self.placed_count,
            "unplaced_count": len(self.unplaced_patient_ids),
            "mean_travel_time_minutes": self.mean_travel_time_minutes,
        }


def _result(
    scenario: Scenario, strategy: str, assignments: list[Assignment]
) -> AllocationResult:
    placed_ids = {assignment.patient_id for assignment in assignments}
    return AllocationResult(
        strategy=strategy,
        assignments=tuple(assignments),
        unplaced_patient_ids=tuple(
            patient.id for patient in scenario.patients if patient.id not in placed_ids
        ),
        total_travel_time_minutes=math.fsum(
            assignment.travel_time_minutes for assignment in assignments
        ),
    )


def greedy_allocate(scenario: Scenario) -> AllocationResult:
    """Process patients in input order; ties follow the SNF input order."""
    remaining = {snf.id: snf.available_beds for snf in scenario.snfs}
    assignments: list[Assignment] = []
    for patient in scenario.patients:
        available = [snf for snf in scenario.snfs if remaining[snf.id] > 0]
        if not available:
            continue
        times = scenario.travel_time_minutes[patient.hospital_id]
        selected = min(available, key=lambda snf: times[snf.id])
        remaining[selected.id] -= 1
        assignments.append(
            Assignment(patient.id, patient.hospital_id, selected.id, times[selected.id])
        )
    return _result(scenario, "greedy", assignments)


@dataclass
class _Edge:
    to: int
    reverse: int
    capacity: int
    cost: Fraction


def _add_edge(
    graph: list[list[_Edge]], source: int, target: int, capacity: int, cost: float
) -> _Edge:
    exact_cost = Fraction(cost)
    forward = _Edge(target, len(graph[target]), capacity, exact_cost)
    reverse = _Edge(source, len(graph[source]), 0, -exact_cost)
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def global_allocate(scenario: Scenario) -> AllocationResult:
    """Maximize placements, then minimize travel using min-cost max-flow.

    The graph is source -> patients -> SNFs -> sink. Reverse edges let a
    later augmentation revise earlier matches. Bellman-Ford handles their
    negative costs. Rational arithmetic prevents roundoff-induced negative
    cycles when travel times contain decimals. This implementation targets
    small experiments.
    """
    patient_count = len(scenario.patients)
    snf_count = len(scenario.snfs)
    source = 0
    first_patient = 1
    first_snf = first_patient + patient_count
    sink = first_snf + snf_count
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]
    choices: list[tuple[Patient, list[tuple[SNF, _Edge]]]] = []
    for index, patient in enumerate(scenario.patients):
        patient_node = first_patient + index
        _add_edge(graph, source, patient_node, 1, 0.0)
        patient_edges = []
        for snf_index, snf in enumerate(scenario.snfs):
            cost = scenario.travel_time_minutes[patient.hospital_id][snf.id]
            edge = _add_edge(graph, patient_node, first_snf + snf_index, 1, cost)
            patient_edges.append((snf, edge))
        choices.append((patient, patient_edges))
    for index, snf in enumerate(scenario.snfs):
        _add_edge(
            graph, first_snf + index, sink, min(snf.available_beds, patient_count), 0.0
        )

    while True:
        distances: list[Fraction | float] = [math.inf] * len(graph)
        parents: list[tuple[int, int] | None] = [None] * len(graph)
        distances[source] = Fraction(0)
        for _ in range(len(graph) - 1):
            changed = False
            for node, edges in enumerate(graph):
                if distances[node] == math.inf:
                    continue
                for index, edge in enumerate(edges):
                    candidate = distances[node] + edge.cost
                    if edge.capacity > 0 and candidate < distances[edge.to]:
                        distances[edge.to] = candidate
                        parents[edge.to] = (node, index)
                        changed = True
            if not changed:
                break
        if parents[sink] is None:
            break
        node = sink
        while node != source:
            parent = parents[node]
            if parent is None:
                raise RuntimeError("Incomplete augmenting path")
            previous, index = parent
            edge = graph[previous][index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = previous

    assignments = []
    for patient, patient_edges in choices:
        for snf, edge in patient_edges:
            if edge.capacity == 0:
                assignments.append(
                    Assignment(
                        patient.id,
                        patient.hospital_id,
                        snf.id,
                        scenario.travel_time_minutes[patient.hospital_id][snf.id],
                    )
                )
    return _result(scenario, "global", assignments)


def load_scenarios(path: Path) -> list[Scenario]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("Input must be an object with schema_version: 1")
    records = document.get("scenarios")
    if not isinstance(records, list) or not records:
        raise ValueError("scenarios must be a nonempty list")
    scenarios = [Scenario.from_dict(record) for record in records]
    names = [scenario.name for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("Scenario names must be unique")
    return scenarios


def compare(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario": asdict(scenario),
        "results": {
            "greedy": greedy_allocate(scenario).to_dict(),
            "global": global_allocate(scenario).to_dict(),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "scenarios" / "phase1_examples.json",
    )
    parser.add_argument("--scenario", help="Run only the scenario with this exact name")
    parser.add_argument(
        "--output", type=Path, help="Save the inputs and allocation results as JSON"
    )
    args = parser.parse_args()
    try:
        scenarios = load_scenarios(args.input)
        if args.scenario is not None:
            scenarios = [scenario for scenario in scenarios if scenario.name == args.scenario]
            if not scenarios:
                raise ValueError(f"Unknown scenario: {args.scenario}")
        reports = [compare(scenario) for scenario in scenarios]
        report = {"schema_version": 1, "model": "phase1_static", "scenarios": reports}
        if args.output is not None:
            protected_paths = {args.input.resolve(), Path(__file__).resolve()}
            if args.output.resolve() in protected_paths:
                raise ValueError("Output must differ from the input and program files")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for entry in reports:
        print(f"\n{entry['scenario']['name']}")
        for strategy, result in entry["results"].items():
            routes = ", ".join(
                f"{a['patient_id']}->{a['snf_id']} ({a['travel_time_minutes']:g} min)"
                for a in result["assignments"]
            ) or "none"
            unplaced = ", ".join(result["unplaced_patient_ids"]) or "none"
            print(
                f"  {strategy:6} placed={result['placed_count']} "
                f"total={result['total_travel_time_minutes']:g} min: "
                f"{routes}; unplaced={unplaced}"
            )
    if args.output is not None:
        print(f"\nSaved results to {args.output.resolve()}")


if __name__ == "__main__":
    main()
