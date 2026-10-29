"""Hospital-local rolling placement and coordinated hospital-to-SNF scheduling.

No external dependencies. Run ``python dynamic_model.py`` for the bundled case.
The CCN objective is eventual total waiting for the supplied finite cohort;
observation-window metrics are separately censored at horizon_hours.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import bisect
import heapq
import json
import math
import random
from pathlib import Path
import time
from typing import Any


MAX_PATIENTS = 500
MAX_BEDS = 500
MAX_FACILITIES = 40
MAX_HOURS = 1_000_000
SEARCH_NODE_LIMIT = 30_000
SEARCH_SECONDS = 2.0
EXACT_PATIENT_LIMIT = 8


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


def _strings(value: Any, label: str, limit: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{label} must be a list of at most {limit} strings")
    result = tuple(_identifier(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result


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
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Patient:
    id: str
    hospital_id: str
    gender: str
    ready_hour: float
    length_of_stay_hours: float
    eligible_snf_ids: tuple[str, ...]
    insurance_type: str = "unspecified"
    required_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    horizon_hours: float
    hospitals: tuple[str, ...]
    snfs: tuple[str, ...]
    beds: tuple[Bed, ...]
    patients: tuple[Patient, ...]
    accepted_insurances: tuple[tuple[str, tuple[str, ...]], ...] = ()
    tie_break_seed: int = 42
    baseline_policy: str = "local"

    @classmethod
    def from_dict(cls, data: Any) -> "Scenario":
        if not isinstance(data, dict):
            raise ValueError("Scenario must be an object")
        if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
            raise ValueError("schema_version must be 1")
        name = _identifier(data.get("name", "Custom dynamic scenario"), "name")
        horizon = _number(data.get("horizon_hours"), "horizon_hours", positive=True)
        hospitals = tuple(row["id"] for row in _rows(data, "hospitals", MAX_FACILITIES))
        snf_rows = _rows(data, "snfs", MAX_FACILITIES)
        snfs = tuple(row["id"] for row in snf_rows)
        accepted_insurances = tuple((row["id"], _strings(row.get("accepted_insurances", ["*"]),
                                    f"SNF {row['id']} accepted_insurances")) for row in snf_rows)
        seed = data.get("tie_break_seed", 42)
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("tie_break_seed must be an integer between 0 and 4294967295")
        policy = data.get("baseline_policy", "local")
        if policy not in ("local", "fcfs"):
            raise ValueError("baseline_policy must be local or fcfs")
        beds = []
        for row in _rows(data, "beds", MAX_BEDS):
            if row.get("snf_id") not in snfs:
                raise ValueError(f"Bed {row['id']} references an unknown SNF")
            if row.get("gender") not in ("male", "female", "any"):
                raise ValueError(f"Bed {row['id']} gender must be male, female or any")
            beds.append(Bed(row["id"], row["snf_id"], row["gender"],
                            _strings(row.get("capabilities", []), f"Bed {row['id']} capabilities")))
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
            insurance = _identifier(row.get("insurance_type", "unspecified"), f"Patient {row['id']} insurance_type")
            care = _strings(row.get("required_capabilities", []), f"Patient {row['id']} required_capabilities")
            patients.append(Patient(row["id"], row["hospital_id"], row["gender"], ready, stay,
                                    tuple(eligible), insurance, care))
        return cls(name, horizon, hospitals, snfs, tuple(beds), tuple(patients),
                   accepted_insurances, seed, policy)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "name": self.name, "horizon_hours": self.horizon_hours,
            "tie_break_seed": self.tie_break_seed, "baseline_policy": self.baseline_policy,
            "hospitals": [{"id": value} for value in self.hospitals],
            "snfs": [{"id": value, "accepted_insurances": list(dict(self.accepted_insurances).get(value, ("*",)))} for value in self.snfs],
            "beds": [{"id": b.id, "snf_id": b.snf_id, "gender": b.gender,
                      "capabilities": list(b.capabilities)} for b in self.beds],
            "patients": [{"id": p.id, "hospital_id": p.hospital_id, "gender": p.gender,
                          "ready_hour": p.ready_hour, "length_of_stay_hours": p.length_of_stay_hours,
                          "eligible_snf_ids": list(p.eligible_snf_ids), "insurance_type": p.insurance_type,
                          "required_capabilities": list(p.required_capabilities)} for p in self.patients],
        }


@dataclass(frozen=True)
class Booking:
    bed: int
    start: float
    end: float


def _options(scenario: Scenario) -> list[tuple[int, ...]]:
    insurance = dict(scenario.accepted_insurances)
    return [tuple(b for snf in p.eligible_snf_ids for b, bed in enumerate(scenario.beds)
                  if bed.snf_id == snf and bed.gender in (p.gender, "any")
                  and ("*" in insurance.get(snf, ("*",)) or p.insurance_type in insurance.get(snf, ("*",)))
                  and set(p.required_capabilities).issubset(bed.capabilities)) for p in scenario.patients]


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


def _gap(patient: Patient, intervals: list[tuple[float, float]], now: float = 0) -> float:
    start = max(patient.ready_hour, now)
    for left, right in intervals:
        if round(start + patient.length_of_stay_hours, 9) <= left:
            break
        if start < right:
            start = right
    return start


def _objective(s: Scenario, plan: dict[int, Booking]) -> float:
    return round(sum(booking.start - s.patients[i].ready_hour for i, booking in plan.items()), 9)


def _schedule(s: Scenario, options: list[tuple[int, ...]], indices: list[int], *, now: float = 0,
              occupied: list[list[tuple[float, float]]] | None = None,
              incumbent: dict[int, Booking] | None = None, deadline: float | None = None,
              node_limit: int = SEARCH_NODE_LIMIT) -> tuple[dict[int, Booking], dict]:
    """Minimize sum of waiting on a fixed visible cohort, with honest bounds.

    Deterministic multi-order insertion supplies feasible plans at every size.
    Small instances additionally enumerate patient/bed insertion orders. All
    exact calls share the caller's deadline and node budget, rather than gaining
    a new time allowance whenever a rolling event occurs.
    """
    feasible = [i for i in indices if options[i]]
    base = occupied if occupied is not None else [[] for _ in s.beds]
    best = dict(incumbent) if incumbent is not None else None
    best_cost = _objective(s, best) if best is not None else math.inf
    theoretical_lower = round(sum(max(0, now - s.patients[i].ready_hour) for i in feasible), 9)
    orders = [
        lambda i: (s.patients[i].ready_hour, i),
        lambda i: (len(options[i]), s.patients[i].ready_hour, s.patients[i].length_of_stay_hours, i),
        lambda i: (s.patients[i].length_of_stay_hours, s.patients[i].ready_hour, len(options[i]), i),
        lambda i: (len(options[i]), s.patients[i].length_of_stay_hours, s.patients[i].ready_hour, i),
        lambda i: (s.patients[i].ready_hour, s.patients[i].length_of_stay_hours, len(options[i]), i),
        lambda i: (-s.patients[i].length_of_stay_hours, len(options[i]), s.patients[i].ready_hour, i),
    ]
    for key in orders:
        if best_cost == theoretical_lower:
            break
        calendars = [list(intervals) for intervals in base]
        plan: dict[int, Booking] = {}
        for i in sorted(feasible, key=key):
            p = s.patients[i]
            earliest = max(p.ready_hour, now)
            start, bed = math.inf, -1
            for b in options[i]:
                candidate = _gap(p, calendars[b], now)
                if candidate < start:
                    start, bed = candidate, b
                    if start == earliest:
                        break
            end = round(start + p.length_of_stay_hours, 9)
            bisect.insort(calendars[bed], (start, end))
            plan[i] = Booking(bed, start, end)
        cost = _objective(s, plan)
        if cost < best_cost:
            best, best_cost = plan, cost
    best = best or {}
    metadata = {"status": "heuristic", "nodes": 0, "objective_hours": best_cost,
                "optimized_patient_count": len(feasible)}
    if best_cost == theoretical_lower:
        metadata["status"] = "optimal"
        return best, metadata
    if len(feasible) > EXACT_PATIENT_LIMIT:
        return best, metadata
    deadline = time.monotonic() + SEARCH_SECONDS if deadline is None else deadline
    calendars = [list(intervals) for intervals in base]
    current: dict[int, Booking] = {}
    nodes = 0
    limited = False

    def search(remaining: tuple[int, ...], cost_so_far: float) -> None:
        nonlocal nodes, limited, best, best_cost
        if best_cost == theoretical_lower:
            return
        if nodes >= node_limit or time.monotonic() >= deadline:
            limited = True
            return
        nodes += 1
        if not remaining:
            if round(cost_so_far, 9) < best_cost:
                best, best_cost = dict(current), round(cost_so_far, 9)
            return
        choices = {}
        lower = cost_so_far
        for i in remaining:
            p = s.patients[i]
            choices[i] = sorted((_gap(p, calendars[b], now), rank, b) for rank, b in enumerate(options[i]))
            lower += choices[i][0][0] - p.ready_hour
        if round(lower, 9) >= best_cost:
            return
        for i in sorted(remaining, key=orders[1]):
            p = s.patients[i]
            rest = tuple(j for j in remaining if j != i)
            # Interchangeable empty/same-calendar beds add no distinct branch.
            equivalent = set()
            for start, _, bed in choices[i]:
                signature = (tuple(calendars[bed]), tuple(j for j in rest if bed in options[j]))
                if signature in equivalent:
                    continue
                equivalent.add(signature)
                new_cost = round(cost_so_far + start - p.ready_hour, 9)
                if new_cost >= best_cost:
                    continue
                end = round(start + p.length_of_stay_hours, 9)
                interval = (start, end)
                bisect.insort(calendars[bed], interval)
                current[i] = Booking(bed, start, end)
                search(rest, new_cost)
                del current[i]
                calendars[bed].remove(interval)
                if limited or best_cost == theoretical_lower:
                    return

    search(tuple(feasible), 0.0)
    metadata.update(status="search_limited" if limited else "optimal", nodes=nodes, objective_hours=best_cost)
    return best, metadata


def _request_event(s: Scenario, index: int, booking: Booking, at: float, order: int,
                   kind: str = "request", accepted: bool = True) -> dict:
    p, bed = s.patients[index], s.beds[booking.bed]
    return {"time_hour": at, "type": kind, "patient_id": p.id, "hospital_id": p.hospital_id,
            "snf_id": bed.snf_id, "bed_id": bed.id, "request_hour": at, "request_order": order,
            "accepted": accepted,
            "detail": "Qualified request accepted in arrival order." if accepted else
                      "Request rejected: no compatible free bed remains at this SNF after earlier requests; the hospital replans."}


def _local_baseline(s: Scenario, options: list[tuple[int, ...]]) -> tuple[dict[int, Booking], dict, list[dict]]:
    """Independent hospital plans; only accepted current requests reserve beds.

    Each hospital sees all its own remaining patients and shared actual bed
    occupancy. A tentative plan remains cached while feasible. Another
    hospital's admission invalidates it only if the new interval overlaps it;
    preserving a feasible optimal plan under tighter constraints is equivalent
    to resolving that same subproblem. Future plans never reserve capacity.
    """
    by_hospital = {h: [i for i, p in enumerate(s.patients) if p.hospital_id == h and options[i]] for h in s.hospitals}
    actual: dict[int, Booking] = {}
    active: dict[int, Booking] = {}
    plans: dict[str, dict[int, Booking]] = {}
    logs: list[dict] = []
    request_order = 0
    now = 0.0
    local_deadline = time.monotonic() + SEARCH_SECONDS
    total_nodes = 0
    counts = {"optimal": 0, "heuristic": 0, "search_limited": 0}
    replan_count = 0
    rng = random.Random(s.tie_break_seed)

    while len(actual) < sum(len(indices) for indices in by_hospital.values()):
        active = {b: booking for b, booking in active.items() if booking.end > now}
        occupied = [[] for _ in s.beds]
        for b, booking in active.items():
            occupied[b] = [(now, booking.end)]
        for hospital, indices in by_hospital.items():
            if hospital in plans:
                continue
            remaining = [i for i in indices if i not in actual]
            if not remaining:
                plans[hospital] = {}
                continue
            plan, info = _schedule(s, options, remaining, now=now, occupied=occupied,
                                   deadline=local_deadline, node_limit=max(0, SEARCH_NODE_LIMIT - total_nodes))
            plans[hospital] = plan
            counts[info["status"]] += 1
            total_nodes += info["nodes"]
            replan_count += 1

        proposals = {hospital: [(i, booking) for i, booking in plan.items() if booking.start <= now]
                     for hospital, plan in plans.items()}
        hospitals = [h for h, rows in proposals.items() if rows]
        if hospitals:
            # Draw only when a real inter-hospital equal-hour tie exists.
            if len(hospitals) > 1:
                rng.shuffle(hospitals)
            accepted: list[tuple[str, int, Booking]] = []
            for hospital in hospitals:
                # Dictionary insertion order is the local optimizer's priority.
                for i, proposed in proposals[hospital]:
                    request_order += 1
                    requested_snf = s.beds[proposed.bed].snf_id
                    # Requests go to an SNF. Its earlier qualified request gets
                    # another compatible bed there before a later request does.
                    choices = (proposed.bed, *(b for b in options[i] if b != proposed.bed and s.beds[b].snf_id == requested_snf))
                    destination = next((b for b in choices if b not in active), None)
                    success = destination is not None
                    booking = Booking(destination, now, round(now + s.patients[i].length_of_stay_hours, 9)) if success else proposed
                    request = _request_event(s, i, booking, now, request_order, accepted=success)
                    request["requested_bed_id"] = s.beds[proposed.bed].id
                    logs.append(request)
                    if not success:
                        logs.append(_request_event(s, i, proposed, now, request_order, "request_rejected", False))
                        continue
                    actual[i] = booking
                    active[booking.bed] = booking
                    accepted.append((hospital, i, booking))
                    del plans[hospital][i]
            if not accepted:
                raise RuntimeError("Local scheduling made no progress with a current proposal")
            # Replan every conflicting future/current request at this SAME hour,
            # permitting rejected patients to use other compatible free beds.
            invalidate = set()
            for hospital, plan in plans.items():
                for owner, _, booking in accepted:
                    if any(other.bed == booking.bed and other.start < booking.end and booking.start < other.end
                           for other in plan.values()):
                        invalidate.add(hospital)
                        break
            for hospital in invalidate:
                del plans[hospital]
            continue
        future = [booking.start for plan in plans.values() for booking in plan.values() if booking.start > now]
        future.extend(booking.end for booking in active.values() if booking.end > now)
        # A ready event reveals no new private information: it was already in
        # that hospital's plan, but is retained as an explicit rolling boundary.
        future.extend(p.ready_hour for i, p in enumerate(s.patients) if i not in actual and p.ready_hour > now)
        if not future:
            break
        now = min(future)
    status = "search_limited" if counts["search_limited"] else "heuristic" if counts["heuristic"] else "optimal"
    return actual, {"status": status, "scope": "hospital_local_subproblems", "nodes": total_nodes,
                    "objective_hours": _objective(s, actual), "replans": replan_count,
                    "subproblem_status_counts": counts, "optimized_patient_count": len(actual)}, logs


def _ccn(s: Scenario, options: list[tuple[int, ...]], baseline: dict[int, Booking]) -> tuple[dict[int, Booking], dict]:
    plan, metadata = _schedule(s, options, list(range(len(s.patients))), incumbent=baseline,
                               deadline=time.monotonic() + SEARCH_SECONDS, node_limit=SEARCH_NODE_LIMIT)
    metadata.update(scope="all_hospitals", excluded_patient_ids=[p.id for i, p in enumerate(s.patients) if not options[i]])
    return plan, metadata


def _result(s: Scenario, plan: dict[int, Booking], strategy: str, solver: dict | None,
            requests: list[dict] | None = None) -> dict:
    horizon = s.horizon_hours
    patients = []
    events = []
    if requests is None:
        requests = [_request_event(s, i, booking, booking.start, order)
                    for order, (i, booking) in enumerate(sorted(plan.items(), key=lambda row: (row[1].start, row[0])), 1)]
    accepted_requests = {event["patient_id"]: event for event in requests if event["type"] == "request" and event["accepted"]}
    events.extend(event.copy() for event in requests if event["time_hour"] <= horizon)
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
            "insurance_type": p.insurance_type, "required_capabilities": list(p.required_capabilities),
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
            request = accepted_requests[p.id]
            events[-1].update(request_hour=request["request_hour"], request_order=request["request_order"])
        if completed:
            add_event(end, "release", "Completes the SNF stay; the bed can be reused immediately.")
    priorities = {"release": 0, "ready": 1, "wait": 2, "request": 3, "request_rejected": 3, "admit": 3}
    request_suborder = {"request": 0, "request_rejected": 1, "admit": 2}
    order = {p.id: i for i, p in enumerate(s.patients)}
    events.sort(key=lambda event: (event["time_hour"], priorities[event["type"]],
                                  event.get("request_order", order[event["patient_id"]]), request_suborder.get(event["type"], 0)))
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
        "request_count": sum(event["type"] == "request" for event in events),
        "rejected_request_count": sum(event["type"] == "request_rejected" for event in events),
    }
    return {"label": ("Hospital-local optimization" if s.baseline_policy == "local" else "Legacy ready-only FCFS") if strategy == "baseline" else "CCN · full foresight",
            "strategy": strategy, "patients": patients, "events": events, "snapshots": snapshots,
            "summary": summary, "solver": solver}


ASSUMPTIONS = [
    "Hour 0 is Monday 00:00. All SNF beds start empty; all supplied patients are ready within the observation window.",
    "Hospital-local baseline knows its own current patients and future ready times and stays, plus shared actual bed occupancy and release times. It minimizes its own eventual total waiting, without seeing other hospitals' future demand. Tentative future plans do not reserve SNF beds; conflicting plans are revised after actual admissions.",
    "CCN knows all hospitals' supplied future ready times, eligibility and SNF stay durations exactly. It minimizes eventual total waiting jointly and retains the hospital-local schedule as a feasible incumbent. Larger cases use deterministic multi-order heuristics; only a proven bound or exhausted exact search is labeled optimal.",
    "Qualified SNF requests are processed first come, first served. Equal-hour inter-hospital requests use the scenario's reproducible seeded tie break, preserving each hospital's planned priority. Rejected hospitals replan at the same hour, with no artificial retry delay. Requests are submitted only for ready patients at their planned admission time.",
    "Both policies know current compatibility and beds. Admission, hospital departure and confirmation happen together; referral, review, authorization and transport durations are zero.",
    "A bed is reused immediately at admission plus length of stay. SNF eligibility, fixed per-bed gender, accepted insurance and all required bed care capabilities are hard constraints for both policies. An insurance wildcard (*) accepts every plan. Insurance authorization duration is not modeled. SNF list order breaks equal-wait bed choices.",
    "Hospital blocked bed-hours sum ready-to-departure waiting inside the observation window for this cohort only; they are not total hospital occupancy. Mean observed waiting includes all supplied patients with waiting censored at the horizon. Mean wait among admitted patients excludes those still waiting.",
    "Plans may extend past the observation horizon. Future scheduled admissions are not counted as observed occupancy. The optimized eventual-wait objective and censored observation-window waiting are reported separately.",
    "These synthetic scenarios illustrate coordination under stated information assumptions; their time savings are not calibrated real-world CCN effect estimates.",
]


def compare(data: dict | Scenario) -> dict:
    s = data if isinstance(data, Scenario) else Scenario.from_dict(data)
    options = _options(s)
    if s.baseline_policy == "fcfs":
        baseline_plan, baseline_solver, requests = _baseline(s, options), None, None
    else:
        baseline_plan, baseline_solver, requests = _local_baseline(s, options)
    ccn_plan, solver = _ccn(s, options, baseline_plan)
    baseline = _result(s, baseline_plan, "baseline", baseline_solver, requests)
    ccn = _result(s, ccn_plan, "ccn", solver)
    before, after = baseline["summary"], ccn["summary"]
    assumptions = list(ASSUMPTIONS)
    if s.baseline_policy == "fcfs":
        assumptions[1] = "Legacy FCFS baseline sees only already-ready patients, uses ready-time/input-order priority, and takes its first compatible free bed; hospital-local foresight is disabled."
        assumptions[3] = "Legacy baseline resolves equal-ready ties by patient input order for regression compatibility; seeded inter-hospital arbitration applies to the hospital-local baseline. Qualified requests are served without artificial processing delays."
    return {"scenario": s.to_dict(), "baseline": baseline, "ccn": ccn,
            "comparison": {"blocked_bed_hours_saved": round(before["blocked_bed_hours"] - after["blocked_bed_hours"], 9),
                           "placement_gain": after["placed_count"] - before["placed_count"],
                           "full_plan_wait_hours_saved": None if before["full_plan_wait_hours"] is None else round(before["full_plan_wait_hours"] - after["full_plan_wait_hours"], 9)},
            "assumptions": assumptions}


def examples() -> dict:
    from dynamic_scenarios import examples as generated_examples
    document = generated_examples()
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
