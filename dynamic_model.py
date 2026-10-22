"""Hourly hospital-to-SNF bed turnover with online and full-foresight policies.

No external dependencies. Run ``python dynamic_model.py`` for the bundled case.
The CCN objective is eventual total waiting for the supplied finite cohort;
observation-window metrics are separately censored at horizon_hours.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import heapq
import json
import math
from pathlib import Path
import time
from typing import Any


MAX_PATIENTS = 50
MAX_BEDS = 100
MAX_FACILITIES = 12
MAX_HOURS = 1_000_000
SEARCH_NODE_LIMIT = 30_000
SEARCH_SECONDS = 2.0


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(value) or value < 0 or value > MAX_HOURS:
        raise ValueError(f"{label} must be between 0 and {MAX_HOURS} hours")
    value = round(value, 9)
    if positive and value <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError(f"{label} must be a nonempty string of at most 80 characters")
    return value


def _rows(data: dict, key: str, limit: int) -> list[dict]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{key} must be a list of objects")
    if len(value) > limit:
        raise ValueError(f"At most {limit} {key} are supported")
    seen = set()
    for row in value:
        identifier = _identifier(row.get("id"), f"{key} id")
        if identifier in seen:
            raise ValueError(f"Duplicate {key} id: {identifier}")
        seen.add(identifier)
    return value


@dataclass(frozen=True)
class Bed:
    id: str
    snf_id: str
    gender: str


@dataclass(frozen=True)
class Patient:
    id: str
    hospital_id: str
    gender: str
    ready_hour: float
    length_of_stay_hours: float
    eligible_snf_ids: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    name: str
    horizon_hours: float
    hospitals: tuple[str, ...]
    snfs: tuple[str, ...]
    beds: tuple[Bed, ...]
    patients: tuple[Patient, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "Scenario":
        if not isinstance(data, dict):
            raise ValueError("Scenario must be an object")
        if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
            raise ValueError("schema_version must be 1")
        name = _identifier(data.get("name", "Custom dynamic scenario"), "name")
        horizon = _number(data.get("horizon_hours"), "horizon_hours", positive=True)
        hospitals = tuple(row["id"] for row in _rows(data, "hospitals", MAX_FACILITIES))
        snfs = tuple(row["id"] for row in _rows(data, "snfs", MAX_FACILITIES))
        beds = []
        for row in _rows(data, "beds", MAX_BEDS):
            if row.get("snf_id") not in snfs:
                raise ValueError(f"Bed {row['id']} references an unknown SNF")
            if row.get("gender") not in ("male", "female", "any"):
                raise ValueError(f"Bed {row['id']} gender must be male, female or any")
            beds.append(Bed(row["id"], row["snf_id"], row["gender"]))
        patients = []
        for row in _rows(data, "patients", MAX_PATIENTS):
            if row.get("hospital_id") not in hospitals:
                raise ValueError(f"Patient {row['id']} references an unknown hospital")
            if row.get("gender") not in ("male", "female"):
                raise ValueError(f"Patient {row['id']} gender must be male or female")
            eligible = row.get("eligible_snf_ids")
            if not isinstance(eligible, list) or any(not isinstance(snf, str) for snf in eligible):
                raise ValueError(f"Patient {row['id']} eligible_snf_ids must be a list of SNF IDs")
            if len(eligible) != len(set(eligible)) or any(snf not in snfs for snf in eligible):
                raise ValueError(f"Patient {row['id']} has duplicate or unknown eligible SNFs")
            ready = _number(row.get("ready_hour"), f"Patient {row['id']} ready_hour")
            if ready > horizon:
                raise ValueError(f"Patient {row['id']} ready_hour must not exceed the horizon")
            stay = _number(row.get("length_of_stay_hours"), f"Patient {row['id']} length_of_stay_hours", positive=True)
            patients.append(Patient(row["id"], row["hospital_id"], row["gender"], ready, stay, tuple(eligible)))
        return cls(name, horizon, hospitals, snfs, tuple(beds), tuple(patients))

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "name": self.name, "horizon_hours": self.horizon_hours,
            "hospitals": [{"id": value} for value in self.hospitals],
            "snfs": [{"id": value} for value in self.snfs],
            "beds": [{"id": b.id, "snf_id": b.snf_id, "gender": b.gender} for b in self.beds],
            "patients": [{"id": p.id, "hospital_id": p.hospital_id, "gender": p.gender,
                          "ready_hour": p.ready_hour, "length_of_stay_hours": p.length_of_stay_hours,
                          "eligible_snf_ids": list(p.eligible_snf_ids)} for p in self.patients],
        }


@dataclass(frozen=True)
class Booking:
    bed: int
    start: float
    end: float


def _options(scenario: Scenario) -> list[tuple[int, ...]]:
    return [tuple(b for snf in p.eligible_snf_ids for b, bed in enumerate(scenario.beds)
                  if bed.snf_id == snf and bed.gender in (p.gender, "any")) for p in scenario.patients]


def _baseline(s: Scenario, options: list[tuple[int, ...]]) -> dict[int, Booking]:
    """FCFS ready queue; choose first compatible free bed, skip blocked patients."""
    events = [(p.ready_hour, 1, i) for i, p in enumerate(s.patients)]
    heapq.heapify(events)
    free = set(range(len(s.beds)))
    pending: set[int] = set()
    plan: dict[int, Booking] = {}
    while events:
        now = events[0][0]
        while events and events[0][0] == now:
            _, kind, index = heapq.heappop(events)
            if kind == 0:
                free.add(index)
            else:
                pending.add(index)
        for i in sorted(pending, key=lambda j: (s.patients[j].ready_hour, j)):
            bed = next((b for b in options[i] if b in free), None)
            if bed is None:
                continue
            end = round(now + s.patients[i].length_of_stay_hours, 9)
            plan[i] = Booking(bed, now, end)
            free.remove(bed)
            pending.remove(i)
            heapq.heappush(events, (end, 0, bed))
    return plan


def _gap(patient: Patient, intervals: list[tuple[float, float]]) -> float:
    start = patient.ready_hour
    for left, right in intervals:
        if round(start + patient.length_of_stay_hours, 9) <= left:
            break
        if start < right:
            start = right
    return start


def _objective(s: Scenario, plan: dict[int, Booking]) -> float:
    return round(sum(booking.start - s.patients[i].ready_hour for i, booking in plan.items()), 9)


def _ccn(s: Scenario, options: list[tuple[int, ...]], baseline: dict[int, Booking]) -> tuple[dict[int, Booking], dict]:
    """Full-foresight interval scheduling, bounded exact search with an incumbent.

    Inserting at the earliest gap for every patient/bed ordering covers an optimal
    active schedule for the sum-of-wait objective. A zero-wait feasible plan is
    already globally optimal. Larger nonzero cases may hit the search budget.
    """
    feasible = [i for i in range(len(s.patients)) if options[i]]
    best = dict(baseline)
    best_cost = _objective(s, best)
    calendars: list[list[tuple[float, float]]] = [[] for _ in s.beds]
    heuristic: dict[int, Booking] = {}
    for i in sorted(feasible, key=lambda j: (len(options[j]), s.patients[j].ready_hour, j)):
        p = s.patients[i]
        rank, bed, start = min(((rank, b, _gap(p, calendars[b])) for rank, b in enumerate(options[i])),
                               key=lambda entry: (entry[2], entry[0]))
        end = round(start + p.length_of_stay_hours, 9)
        calendars[bed].append((start, end))
        calendars[bed].sort()
        heuristic[i] = Booking(bed, start, end)
    cost = _objective(s, heuristic)
    if cost < best_cost:
        best, best_cost = heuristic, cost
    nodes = 0
    limited = False
    deadline = time.monotonic() + SEARCH_SECONDS
    calendars = [[] for _ in s.beds]
    current: dict[int, Booking] = {}

    def search(remaining: tuple[int, ...], cost_so_far: float) -> None:
        nonlocal nodes, limited, best, best_cost
        if best_cost == 0:
            return
        if nodes >= SEARCH_NODE_LIMIT or time.monotonic() >= deadline:
            limited = True
            return
        nodes += 1
        if not remaining:
            if cost_so_far < best_cost:
                best, best_cost = dict(current), round(cost_so_far, 9)
            return
        choices = {}
        lower = cost_so_far
        for i in remaining:
            p = s.patients[i]
            choices[i] = sorted(((_gap(p, calendars[b]), rank, b) for rank, b in enumerate(options[i])))
            lower += choices[i][0][0] - p.ready_hour
        if round(lower, 9) >= best_cost:
            return
        for i in sorted(remaining, key=lambda j: (len(options[j]), s.patients[j].ready_hour, j)):
            p = s.patients[i]
            rest = tuple(j for j in remaining if j != i)
            for start, _, bed in choices[i]:
                new_cost = round(cost_so_far + start - p.ready_hour, 9)
                if new_cost >= best_cost:
                    continue
                end = round(start + p.length_of_stay_hours, 9)
                interval = (start, end)
                calendars[bed].append(interval)
                calendars[bed].sort()
                current[i] = Booking(bed, start, end)
                search(rest, new_cost)
                del current[i]
                calendars[bed].remove(interval)
                if limited or best_cost == 0:
                    return

    search(tuple(feasible), 0.0)
    return best, {"status": "search_limited" if limited else "optimal", "nodes": nodes,
                  "objective_hours": best_cost, "optimized_patient_count": len(feasible),
                  "excluded_patient_ids": [p.id for i, p in enumerate(s.patients) if not options[i]]}


def _result(s: Scenario, plan: dict[int, Booking], strategy: str, solver: dict | None) -> dict:
    horizon = s.horizon_hours
    patients = []
    events = []
    for i, p in enumerate(s.patients):
        booking = plan.get(i)
        bed = s.beds[booking.bed] if booking else None
        start, end = (booking.start, booking.end) if booking else (None, None)
        placed = start is not None and start <= horizon
        completed = end is not None and end <= horizon
        window_wait = round((min(start, horizon) if start is not None else horizon) - p.ready_hour, 9)
        patients.append({
            "patient_id": p.id, "hospital_id": p.hospital_id, "gender": p.gender,
            "ready_hour": p.ready_hour, "length_of_stay_hours": p.length_of_stay_hours,
            "eligible_snf_ids": list(p.eligible_snf_ids), "bed_id": bed.id if bed else None,
            "snf_id": bed.snf_id if bed else None,
            "admission_hour": start if placed else None, "release_hour": end if completed else None,
            "planned_admission_hour": start, "planned_release_hour": end,
            "wait_hours": round(start - p.ready_hour, 9) if booking else None,
            "wait_in_window_hours": window_wait,
            "status": "completed" if completed else "in_snf" if placed else "waiting",
        })

        def add_event(at: float, kind: str, detail: str) -> None:
            events.append({"time_hour": at, "type": kind, "patient_id": p.id,
                           "hospital_id": p.hospital_id, "snf_id": bed.snf_id if bed else None,
                           "bed_id": bed.id if bed else None, "detail": detail})

        add_event(p.ready_hour, "ready", "Ready to leave hospital; enters the placement queue.")
        if start is None or start > p.ready_hour:
            add_event(p.ready_hour, "wait", "No compatible bed exists." if booking is None else "Remains in hospital until the scheduled SNF admission.")
        if placed:
            add_event(start, "admit", "Leaves hospital and occupies the SNF bed.")
        if completed:
            add_event(end, "release", "Completes the SNF stay; the bed can be reused immediately.")
    priorities = {"release": 0, "ready": 1, "wait": 2, "admit": 3}
    order = {p.id: i for i, p in enumerate(s.patients)}
    events.sort(key=lambda event: (event["time_hour"], priorities[event["type"]], order[event["patient_id"]]))
    snapshots = []
    for now in sorted({0.0, horizon, *(event["time_hour"] for event in events)}):
        waiting = []
        occupants = {bed.id: None for bed in s.beds}
        hospital_waiting = {hospital: 0 for hospital in s.hospitals}
        snf_occupied = {snf: 0 for snf in s.snfs}
        complete_count = 0
        for i, p in enumerate(s.patients):
            booking = plan.get(i)
            if booking and booking.start <= now:
                if booking.end <= now:
                    complete_count += 1
                else:
                    bed = s.beds[booking.bed]
                    occupants[bed.id] = p.id
                    snf_occupied[bed.snf_id] += 1
            elif p.ready_hour <= now:
                waiting.append(p.id)
                hospital_waiting[p.hospital_id] += 1
        snapshots.append({"time_hour": now, "waiting_count": len(waiting),
                          "occupied_count": sum(snf_occupied.values()), "completed_count": complete_count,
                          "hospital_waiting": hospital_waiting, "snf_occupied": snf_occupied,
                          "bed_occupants": occupants, "waiting_patient_ids": waiting})
    admitted = [p for p in patients if p["admission_hour"] is not None]
    occupied_hours = round(sum(max(0.0, min(horizon, b.end) - b.start) for b in plan.values()), 9)
    impossible = [p.id for i, p in enumerate(s.patients) if i not in plan]
    summary = {
        "patient_count": len(patients), "placed_count": len(admitted),
        "waiting_count": len(patients) - len(admitted),
        "completed_count": sum(p["status"] == "completed" for p in patients),
        "blocked_bed_hours": round(sum(p["wait_in_window_hours"] for p in patients), 9),
        "mean_observed_wait_hours": round(sum(p["wait_in_window_hours"] for p in patients) / len(patients), 9) if patients else None,
        "mean_wait_hours": round(sum(p["wait_hours"] for p in admitted) / len(admitted), 9) if admitted else None,
        "full_plan_wait_hours": None if impossible else _objective(s, plan),
        "snf_occupied_bed_hours": occupied_hours,
        "snf_utilization": occupied_hours / (horizon * len(s.beds)) if s.beds else None,
        "total_beds": len(s.beds), "structurally_unplaceable_patient_ids": impossible,
    }
    return {"label": "Online baseline" if strategy == "baseline" else "CCN · full foresight",
            "strategy": strategy, "patients": patients, "events": events, "snapshots": snapshots,
            "summary": summary, "solver": solver}


ASSUMPTIONS = [
    "Hour 0 is Monday 00:00. All SNF beds start empty; all supplied patients are ready within the observation window.",
    "Baseline sees only patients already ready, uses first-ready order (input order for ties), and takes the first compatible free bed in the patient's ordered SNF list. A blocked patient does not block other compatible admissions.",
    "CCN knows all supplied future ready times, eligibility and SNF stay durations exactly. It minimizes eventual total waiting among patients with compatible beds; structurally impossible patients still contribute censored waiting. Larger cases may stop at a feasible search-limited plan.",
    "Both policies know current compatibility and beds. Admission, hospital departure and confirmation happen together; referral, review, authorization and transport durations are zero.",
    "A bed is reused immediately at admission plus length of stay. Gender is a fixed per-bed restriction; any accepts either gender. SNF eligibility is a hard constraint, and baseline uses its list order as preference.",
    "Hospital blocked bed-hours sum ready-to-departure waiting inside the observation window for this cohort only; they are not total hospital occupancy. Mean observed waiting includes all supplied patients with waiting censored at the horizon. Mean wait among admitted patients excludes those still waiting.",
    "Plans may extend past the observation horizon. Future scheduled admissions are not counted as observed occupancy. The optimized eventual-wait objective and censored observation-window waiting are reported separately.",
    "These synthetic scenarios illustrate coordination under stated information assumptions; their time savings are not calibrated real-world CCN effect estimates.",
]


def compare(data: dict | Scenario) -> dict:
    s = data if isinstance(data, Scenario) else Scenario.from_dict(data)
    options = _options(s)
    baseline_plan = _baseline(s, options)
    ccn_plan, solver = _ccn(s, options, baseline_plan)
    baseline = _result(s, baseline_plan, "baseline", None)
    ccn = _result(s, ccn_plan, "ccn", solver)
    before, after = baseline["summary"], ccn["summary"]
    return {"scenario": s.to_dict(), "baseline": baseline, "ccn": ccn,
            "comparison": {"blocked_bed_hours_saved": round(before["blocked_bed_hours"] - after["blocked_bed_hours"], 9),
                           "placement_gain": after["placed_count"] - before["placed_count"],
                           "full_plan_wait_hours_saved": None if before["full_plan_wait_hours"] is None else round(before["full_plan_wait_hours"] - after["full_plan_wait_hours"], 9)},
            "assumptions": list(ASSUMPTIONS)}


def examples() -> dict:
    document = json.loads((Path(__file__).parent / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8"))
    return {"scenarios": [Scenario.from_dict(row).to_dict() for row in document["scenarios"]],
            "limits": {"max_patients": MAX_PATIENTS, "max_beds": MAX_BEDS, "max_facilities": MAX_FACILITIES}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="One scenario JSON; defaults to the bundled example")
    parser.add_argument("--output", type=Path, help="Save the complete result as JSON")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8-sig")) if args.input else examples()["scenarios"][0]
    result = compare(data)
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
