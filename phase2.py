"""Simulate Phase 2 clinical and demographic bed matching experiments."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any


def _nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return result


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(value)


def _records(data: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError(f"{field} must be a list of objects")
    ids = [item.get("id") for item in data]
    if any(not isinstance(item_id, str) or not item_id.strip() for item_id in ids):
        raise ValueError(f"Each {field} entry needs a nonempty string id")
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate id in {field}")
    return data


@dataclass(frozen=True)
class Bed:
    id: str
    snf_id: str
    room_id: str
    room_gender: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class Patient:
    id: str
    hospital_id: str
    gender: str
    required_capabilities: tuple[str, ...]
    ready_time_minutes: float


@dataclass(frozen=True)
class Scenario:
    name: str
    hospitals: tuple[str, ...]
    snfs: tuple[str, ...]
    beds: tuple[Bed, ...]
    patients: tuple[Patient, ...]
    travel_time_minutes: dict[str, dict[str, float]]
    rejection_delay_minutes: float
    acceptance_delay_minutes: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        if not isinstance(data, dict):
            raise ValueError("Each scenario must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Scenario name must be a nonempty string")

        hospital_rows = _records(data.get("hospitals"), "hospitals")
        snf_rows = _records(data.get("snfs"), "snfs")
        bed_rows = _records(data.get("beds"), "beds")
        patient_rows = _records(data.get("patients"), "patients")
        hospitals = tuple(row["id"] for row in hospital_rows)
        snfs = tuple(row["id"] for row in snf_rows)
        hospital_ids, snf_ids = set(hospitals), set(snfs)
        beds: list[Bed] = []
        for row in bed_rows:
            if row.get("snf_id") not in snf_ids:
                raise ValueError(f"Bed {row['id']}: unknown snf_id")
            room_gender = row.get("room_gender")
            if room_gender not in ("male", "female", "any"):
                raise ValueError(f"Bed {row['id']}: room_gender must be male, female, or any")
            room_id = row.get("room_id")
            if not isinstance(room_id, str) or not room_id.strip():
                raise ValueError(f"Bed {row['id']}: room_id must be a nonempty string")
            beds.append(
                Bed(
                    row["id"],
                    row["snf_id"],
                    room_id,
                    room_gender,
                    _string_list(row.get("capabilities", []), f"Bed {row['id']} capabilities"),
                )
            )

        patients: list[Patient] = []
        for row in patient_rows:
            if row.get("hospital_id") not in hospital_ids:
                raise ValueError(f"Patient {row['id']}: unknown hospital_id")
            gender = row.get("gender")
            if gender not in ("male", "female"):
                raise ValueError(f"Patient {row['id']}: gender must be male or female")
            patients.append(
                Patient(
                    row["id"],
                    row["hospital_id"],
                    gender,
                    _string_list(
                        row.get("required_capabilities", []),
                        f"Patient {row['id']} required_capabilities",
                    ),
                    _nonnegative(row.get("ready_time_minutes"), "ready_time_minutes"),
                )
            )

        raw_times = data.get("travel_time_minutes")
        if not isinstance(raw_times, dict) or set(raw_times) != hospital_ids:
            raise ValueError("travel_time_minutes must have one row per hospital id")
        travel: dict[str, dict[str, float]] = {}
        for hospital_id in hospitals:
            row = raw_times[hospital_id]
            if not isinstance(row, dict) or set(row) != snf_ids:
                raise ValueError(f"Hospital {hospital_id}: provide one travel time per SNF")
            travel[hospital_id] = {
                snf_id: _nonnegative(row[snf_id], "travel time")
                for snf_id in snfs
            }

        return cls(
            name,
            hospitals,
            snfs,
            tuple(beds),
            tuple(patients),
            travel,
            _nonnegative(data.get("rejection_delay_minutes", 120), "rejection_delay_minutes"),
            _nonnegative(data.get("acceptance_delay_minutes", 0), "acceptance_delay_minutes"),
        )


def _compatible(patient: Patient, bed: Bed) -> bool:
    return (bed.room_gender in ("any", patient.gender)) and set(
        patient.required_capabilities
    ).issubset(bed.capabilities)


@dataclass(frozen=True)
class Assignment:
    patient_id: str
    hospital_id: str
    bed_id: str
    snf_id: str
    t1_minutes: float
    t2_minutes: float
    placement_delay_minutes: float
    rejection_count: int
    travel_time_minutes: float


@dataclass(frozen=True)
class MatchingResult:
    strategy: str
    assignments: tuple[Assignment, ...]
    unplaced_patient_ids: tuple[str, ...]
    total_placement_delay_minutes: float

    @property
    def placed_count(self) -> int:
        return len(self.assignments)

    @property
    def objective_value_minutes(self) -> float | None:
        """Return Σ(T2-T1), or None when the scenario leaves patients blocked."""
        if self.unplaced_patient_ids:
            return None
        return self.total_placement_delay_minutes

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "assignments": [asdict(item) for item in self.assignments],
            "unplaced_patient_ids": list(self.unplaced_patient_ids),
            "placed_count": self.placed_count,
            "unplaced_count": len(self.unplaced_patient_ids),
            "total_placement_delay_minutes": self.total_placement_delay_minutes,
            "objective_value_minutes": self.objective_value_minutes,
        }


def _choose(candidates: list[Bed], scenario: Scenario, patient: Patient) -> Bed:
    order = {bed.id: index for index, bed in enumerate(scenario.beds)}
    return min(
        candidates,
        key=lambda bed: (
            scenario.travel_time_minutes[patient.hospital_id][bed.snf_id],
            order[bed.id],
        ),
    )


def _result(
    scenario: Scenario,
    strategy: str,
    assignments: list[Assignment],
    unplaced: list[str],
) -> MatchingResult:
    return MatchingResult(
        strategy,
        tuple(assignments),
        tuple(unplaced),
        math.fsum(item.placement_delay_minutes for item in assignments),
    )


def baseline_match(scenario: Scenario) -> MatchingResult:
    """Try visible beds in travel/input order; incompatible referrals are rejected."""
    available = list(scenario.beds)
    assignments: list[Assignment] = []
    unplaced: list[str] = []
    for patient in scenario.patients:
        rejected = 0
        attempted: set[str] = set()
        while available:
            candidates = [bed for bed in available if bed.id not in attempted]
            if not candidates:
                unplaced.append(patient.id)
                break
            bed = _choose(candidates, scenario, patient)
            if _compatible(patient, bed):
                available.remove(bed)
                t2 = patient.ready_time_minutes + (
                    rejected * scenario.rejection_delay_minutes
                ) + scenario.acceptance_delay_minutes
                assignments.append(
                    Assignment(
                        patient.id,
                        patient.hospital_id,
                        bed.id,
                        bed.snf_id,
                        patient.ready_time_minutes,
                        t2,
                        t2 - patient.ready_time_minutes,
                        rejected,
                        scenario.travel_time_minutes[patient.hospital_id][bed.snf_id],
                    )
                )
                break
            attempted.add(bed.id)
            rejected += 1
        else:
            unplaced.append(patient.id)
    return _result(scenario, "baseline", assignments, unplaced)


def ccn_match(scenario: Scenario) -> MatchingResult:
    """Filter incompatible beds and preserve scarce beds for hard dependencies."""
    available = list(scenario.beds)
    assignments: list[Assignment] = []
    unplaced: list[str] = []
    for index, patient in enumerate(scenario.patients):
        candidates = [bed for bed in available if _compatible(patient, bed)]
        if not candidates:
            unplaced.append(patient.id)
            continue
        remaining_patients = scenario.patients[index + 1 :]
        safe: list[Bed] = []
        for bed in candidates:
            remaining_beds = [item for item in available if item.id != bed.id]
            if all(
                any(_compatible(other, item) for item in remaining_beds)
                for other in remaining_patients
                if other.required_capabilities
            ):
                safe.append(bed)
        if not safe:
            safe = candidates
        bed = min(
            safe,
            key=lambda item: (
                len(item.capabilities) if not patient.required_capabilities else 0,
                scenario.travel_time_minutes[patient.hospital_id][item.snf_id],
                scenario.beds.index(item),
            ),
        )
        available.remove(bed)
        t2 = patient.ready_time_minutes + scenario.acceptance_delay_minutes
        assignments.append(
            Assignment(
                patient.id,
                patient.hospital_id,
                bed.id,
                bed.snf_id,
                patient.ready_time_minutes,
                t2,
                t2 - patient.ready_time_minutes,
                0,
                scenario.travel_time_minutes[patient.hospital_id][bed.snf_id],
            )
        )
    return _result(scenario, "ccn", assignments, unplaced)


def compare(scenario: Scenario) -> dict[str, Any]:
    baseline = baseline_match(scenario)
    ccn = ccn_match(scenario)
    return {
        "scenario": scenario.name,
        "baseline": baseline.to_dict(),
        "ccn": ccn.to_dict(),
        "objective_delta_minutes": (
            baseline.objective_value_minutes - ccn.objective_value_minutes
            if baseline.objective_value_minutes is not None
            and ccn.objective_value_minutes is not None
            else None
        ),
    }


def load_scenarios(path: Path) -> list[Scenario]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Input must contain schema_version 1")
    rows = data.get("scenarios")
    if not isinstance(rows, list):
        raise ValueError("scenarios must be a list")
    scenarios = [Scenario.from_dict(row) for row in rows]
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("Duplicate scenario name")
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("scenarios/phase2_examples.json"))
    parser.add_argument("--scenario")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scenarios = load_scenarios(args.input)
    if args.scenario:
        scenarios = [scenario for scenario in scenarios if scenario.name == args.scenario]
        if not scenarios:
            raise SystemExit(f"Unknown scenario: {args.scenario}")
    result = {"schema_version": 1, "results": [compare(scenario) for scenario in scenarios]}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
