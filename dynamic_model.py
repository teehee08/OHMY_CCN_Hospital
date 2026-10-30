"""Hospital-local rolling placement and coordinated hospital-to-SNF scheduling.

No external dependencies. Run ``python dynamic_model.py`` for the bundled case.
The CCN objective is total waiting for the supplied finite cohort. Both policies
are observed from the earliest readiness through their final SNF releases.
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

from dynamic_analysis import fairness_report


MAX_PATIENTS = 500
MAX_BEDS = 500
MAX_FACILITIES = 40
MAX_HOURS = 1_000_000
SEARCH_NODE_LIMIT = 30_000
SEARCH_SECONDS = 2.0
EXACT_PATIENT_LIMIT = 8
CCN_SEARCH_SEED = 20261029
CCN_CANDIDATE_LIMIT = 1000
CCN_INSERTION_WORK_BUDGET = 4_000_000


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
    # Legacy storage name: arrival-generation metadata, never a simulation cutoff.
    horizon_hours: float
    hospitals: tuple[str, ...]
    snfs: tuple[str, ...]
    beds: tuple[Bed, ...]
    patients: tuple[Patient, ...]
    accepted_insurances: tuple[tuple[str, tuple[str, ...]], ...] = ()
    tie_break_seed: int = 42
    baseline_policy: str = "local"
    optimizer_seed: int = CCN_SEARCH_SEED

    @property
    def arrival_span_hours(self) -> float:
        return self.horizon_hours

    @classmethod
    def from_dict(cls, data: Any) -> "Scenario":
        if not isinstance(data, dict):
            raise ValueError("Scenario must be an object")
        if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
            raise ValueError("schema_version must be 1")
        name = _identifier(data.get("name", "Custom dynamic scenario"), "name")
        absent = object()
        arrival_span = data.get("arrival_span_hours", data.get("horizon_hours", absent))
        hospitals = tuple(row["id"] for row in _rows(data, "hospitals", MAX_FACILITIES))
        snf_rows = _rows(data, "snfs", MAX_FACILITIES)
        snfs = tuple(row["id"] for row in snf_rows)
        accepted_insurances = tuple((row["id"], _strings(row.get("accepted_insurances", ["*"]),
                                    f"SNF {row['id']} accepted_insurances")) for row in snf_rows)
        seed = data.get("tie_break_seed", 42)
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("tie_break_seed must be an integer between 0 and 4294967295")
        optimizer_seed = data.get("optimizer_seed", CCN_SEARCH_SEED)
        if type(optimizer_seed) is not int or not 0 <= optimizer_seed <= 2**32 - 1:
            raise ValueError("optimizer_seed must be an integer between 0 and 4294967295")
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
            stay = _number(row.get("length_of_stay_hours"), f"Patient {row['id']} length_of_stay_hours", positive=True)
            insurance = _identifier(row.get("insurance_type", "unspecified"), f"Patient {row['id']} insurance_type")
            care = _strings(row.get("required_capabilities", []), f"Patient {row['id']} required_capabilities")
            patients.append(Patient(row["id"], row["hospital_id"], row["gender"], ready, stay,
                                    tuple(eligible), insurance, care))
        if arrival_span is absent:
            arrival_span = max((p.ready_hour for p in patients), default=0.0) or 1.0
        horizon = _number(arrival_span, "arrival_span_hours", positive=True)
        return cls(name, horizon, hospitals, snfs, tuple(beds), tuple(patients),
                   accepted_insurances, seed, policy, optimizer_seed)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "name": self.name, "arrival_span_hours": self.arrival_span_hours,
            "horizon_hours": self.arrival_span_hours,
            "tie_break_seed": self.tie_break_seed, "baseline_policy": self.baseline_policy,
            "optimizer_seed": self.optimizer_seed,
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


def _capacity_cost(patient: Patient, bed: Bed) -> tuple[float, float]:
    """Unused care capabilities and flexible-gender capacity occupied in bed-hours.

    This uses only bed attributes and this patient's requirements. It never
    measures other hospitals' demand. It is a secondary preference, not an
    eligibility restriction or a reason to increase the primary waiting cost.
    """
    extra = len(set(bed.capabilities) - set(patient.required_capabilities))
    return (round(extra * patient.length_of_stay_hours, 9),
            patient.length_of_stay_hours if bed.gender == "any" else 0.0)


def _capacity_objective(s: Scenario, plan: dict[int, Booking]) -> tuple[float, float]:
    costs = [_capacity_cost(s.patients[i], s.beds[booking.bed]) for i, booking in plan.items()]
    return tuple(round(sum(cost[column] for cost in costs), 9) for column in (0, 1))


def _schedule(s: Scenario, options: list[tuple[int, ...]], indices: list[int], *, now: float = 0,
              occupied: list[list[tuple[float, float]]] | None = None,
              incumbent: dict[int, Booking] | None = None, deadline: float | None = None,
              node_limit: int = SEARCH_NODE_LIMIT,
              preserve_capacity: bool = False) -> tuple[dict[int, Booking], dict]:
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
    best_capacity = _capacity_objective(s, best) if best is not None else (math.inf, math.inf)
    theoretical_lower = round(sum(max(0, now - s.patients[i].ready_hour) for i in feasible), 9)
    capacity_costs = {(i, b): _capacity_cost(s.patients[i], s.beds[b])
                      for i in feasible for b in options[i]} if preserve_capacity else {}
    minimum_capacity = {i: min(capacity_costs[i, b] for b in options[i]) for i in feasible} if preserve_capacity else {}
    theoretical_capacity = tuple(round(sum(value[c] for value in minimum_capacity.values()), 9)
                                 for c in (0, 1)) if preserve_capacity else (0.0, 0.0)

    def certificate() -> bool:
        return best_cost == theoretical_lower and (not preserve_capacity or best_capacity == theoretical_capacity)

    def improves(cost: float, capacity: tuple[float, float]) -> bool:
        return cost < best_cost or (preserve_capacity and cost == best_cost and capacity < best_capacity)
    orders = [
        lambda i: (s.patients[i].ready_hour, i),
        lambda i: (len(options[i]), s.patients[i].ready_hour, s.patients[i].length_of_stay_hours, i),
        lambda i: (s.patients[i].length_of_stay_hours, s.patients[i].ready_hour, len(options[i]), i),
        lambda i: (len(options[i]), s.patients[i].length_of_stay_hours, s.patients[i].ready_hour, i),
        lambda i: (s.patients[i].ready_hour, s.patients[i].length_of_stay_hours, len(options[i]), i),
        lambda i: (-s.patients[i].length_of_stay_hours, len(options[i]), s.patients[i].ready_hour, i),
    ]
    constructions = [(key, use_capacity_tie) for key in orders
                     for use_capacity_tie in ((False, True) if preserve_capacity else (False,))]
    evaluated_constructions = 0
    for key, use_capacity_tie in constructions:
        if certificate():
            break
        calendars = [list(intervals) for intervals in base]
        plan: dict[int, Booking] = {}
        for i in sorted(feasible, key=key):
            p = s.patients[i]
            earliest = max(p.ready_hour, now)
            start, bed = math.inf, -1
            bed_capacity = (math.inf, math.inf)
            for b in options[i]:
                candidate = _gap(p, calendars[b], now)
                capacity = capacity_costs[i, b] if use_capacity_tie else (0.0, 0.0)
                if candidate < start or (candidate == start and capacity < bed_capacity):
                    start, bed, bed_capacity = candidate, b, capacity
                    if start == earliest and not use_capacity_tie:
                        break
            end = round(start + p.length_of_stay_hours, 9)
            bisect.insort(calendars[bed], (start, end))
            plan[i] = Booking(bed, start, end)
        cost = _objective(s, plan)
        evaluated_constructions += 1
        capacity = _capacity_objective(s, plan) if preserve_capacity else (0.0, 0.0)
        if improves(cost, capacity):
            best, best_cost = plan, cost
            best_capacity = capacity
    best = best or {}
    metadata = {"status": "heuristic", "nodes": 0, "objective_hours": best_cost,
                "optimized_patient_count": len(feasible),
                "construction_count": evaluated_constructions,
                "construction_limit": len(constructions)}
    if preserve_capacity:
        metadata.update(secondary_objective="preserve_unused_care_then_flexible_gender_bed_hours",
                        unused_care_capability_bed_hours=best_capacity[0],
                        flexible_gender_bed_hours=best_capacity[1])
    if certificate():
        metadata["status"] = "optimal"
        return best, metadata
    if len(feasible) > EXACT_PATIENT_LIMIT:
        return best, metadata
    deadline = time.monotonic() + SEARCH_SECONDS if deadline is None else deadline
    calendars = [list(intervals) for intervals in base]
    current: dict[int, Booking] = {}
    nodes = 0
    limited = False

    def search(remaining: tuple[int, ...], cost_so_far: float,
               capacity_so_far: tuple[float, float] = (0.0, 0.0)) -> None:
        nonlocal nodes, limited, best, best_cost, best_capacity
        if certificate():
            return
        if nodes >= node_limit or time.monotonic() >= deadline:
            limited = True
            return
        nodes += 1
        if not remaining:
            if improves(round(cost_so_far, 9), capacity_so_far):
                best, best_cost = dict(current), round(cost_so_far, 9)
                best_capacity = capacity_so_far
            return
        choices = {}
        lower = cost_so_far
        for i in remaining:
            p = s.patients[i]
            choices[i] = sorted((_gap(p, calendars[b], now),
                                 capacity_costs[i, b] if preserve_capacity else (0.0, 0.0), rank, b)
                                for rank, b in enumerate(options[i]))
            lower += choices[i][0][0] - p.ready_hour
        capacity_lower = tuple(round(capacity_so_far[c] + sum(minimum_capacity[i][c] for i in remaining), 9)
                               for c in (0, 1)) if preserve_capacity else (0.0, 0.0)
        if round(lower, 9) > best_cost or (round(lower, 9) == best_cost and
                                         (not preserve_capacity or capacity_lower >= best_capacity)):
            return
        for i in sorted(remaining, key=orders[1]):
            p = s.patients[i]
            rest = tuple(j for j in remaining if j != i)
            # Interchangeable empty/same-calendar beds add no distinct branch.
            equivalent = set()
            for start, capacity, _, bed in choices[i]:
                signature = (tuple(calendars[bed]), tuple(j for j in rest if bed in options[j]))
                if preserve_capacity:
                    signature += (capacity, tuple((j, capacity_costs[j, bed]) for j in rest if bed in options[j]))
                if signature in equivalent:
                    continue
                equivalent.add(signature)
                new_cost = round(cost_so_far + start - p.ready_hour, 9)
                if new_cost > best_cost or (new_cost == best_cost and not preserve_capacity):
                    continue
                end = round(start + p.length_of_stay_hours, 9)
                interval = (start, end)
                bisect.insort(calendars[bed], interval)
                current[i] = Booking(bed, start, end)
                new_capacity = tuple(round(capacity_so_far[c] + capacity[c], 9) for c in (0, 1))
                search(rest, new_cost, new_capacity)
                del current[i]
                calendars[bed].remove(interval)
                if limited or certificate():
                    return

    search(tuple(feasible), 0.0)
    metadata.update(status="search_limited" if limited else "optimal", nodes=nodes, objective_hours=best_cost)
    if preserve_capacity:
        metadata.update(unused_care_capability_bed_hours=best_capacity[0], flexible_gender_bed_hours=best_capacity[1])
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
    construction_count = 0
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
                                   deadline=local_deadline, node_limit=max(0, SEARCH_NODE_LIMIT - total_nodes),
                                   preserve_capacity=True)
            plans[hospital] = plan
            counts[info["status"]] += 1
            total_nodes += info["nodes"]
            construction_count += info["construction_count"]
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
                    "subproblem_status_counts": counts, "optimized_patient_count": len(actual),
                    "construction_count": construction_count, "construction_limit_per_subproblem": 12,
                    "secondary_objective": "preserve_unused_care_then_flexible_gender_bed_hours",
                    "secondary_priority": "only_among_equal_total_wait_plans",
                    "capacity_information_scope": "bed_attributes_and_own_patients_only",
                    "unused_care_capability_bed_hours": _capacity_objective(s, actual)[0],
                    "flexible_gender_bed_hours": _capacity_objective(s, actual)[1]}, logs


def validate_plan(s: Scenario, plan: dict[int, Booking]) -> None:
    """Check a complete eventual plan, excluding only incompatible patients.

    This validates the final schedule rather than treating planned times as
    observed admissions. It has no observation-horizon cutoff. Public callers
    receive ``None`` on success and ``ValueError`` for an invalid plan.
    """
    if not isinstance(plan, dict):
        raise ValueError("A placement plan must be a dictionary of patient indices to bookings")
    expected = {i for i, choices in enumerate(_options(s)) if choices}
    if any(type(i) is not int for i in plan) or set(plan) != expected:
        raise ValueError("A placement plan must contain every compatible patient exactly once")
    insurance = dict(s.accepted_insurances)
    intervals: list[list[tuple[float, float, int]]] = [[] for _ in s.beds]
    for i, booking in plan.items():
        if not isinstance(booking, Booking) or type(booking.bed) is not int or not 0 <= booking.bed < len(s.beds):
            raise ValueError(f"Patient {s.patients[i].id} has an invalid bed booking")
        p, bed = s.patients[i], s.beds[booking.bed]
        if any(isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at)
               for at in (booking.start, booking.end)):
            raise ValueError(f"Patient {p.id} has a nonfinite booking time")
        if booking.start < p.ready_hour or booking.end != round(booking.start + p.length_of_stay_hours, 9):
            raise ValueError(f"Patient {p.id} booking violates readiness or length of stay")
        accepted = insurance.get(bed.snf_id, ("*",))
        if (bed.snf_id not in p.eligible_snf_ids or bed.gender not in (p.gender, "any")
                or ("*" not in accepted and p.insurance_type not in accepted)
                or not set(p.required_capabilities).issubset(bed.capabilities)):
            raise ValueError(f"Patient {p.id} booking violates bed compatibility")
        intervals[booking.bed].append((booking.start, booking.end, i))
    for bed, rows in zip(s.beds, intervals):
        rows.sort()
        if any(previous[1] > following[0] for previous, following in zip(rows, rows[1:])):
            raise ValueError(f"Overlapping bookings on bed {bed.id}")


def _ccn(s: Scenario, options: list[tuple[int, ...]], baseline: dict[int, Booking]) -> tuple[dict[int, Booking], dict]:
    """Search independently, then retain the better global or baseline plan.

    The baseline is a final feasible fallback, never an exploration threshold
    or a mutation parent. Holding the clinical/resource input and optimizer
    seed fixed therefore holds the global search fixed across arbitration
    seeds. Small cases use bounded exact search; larger cases use the same
    deterministic amount of multistart/mutation work as before.
    """
    validate_plan(s, baseline)
    plan, metadata = _schedule(s, options, list(range(len(s.patients))),
                               deadline=time.monotonic() + SEARCH_SECONDS, node_limit=SEARCH_NODE_LIMIT)
    feasible = [i for i, choices in enumerate(options) if choices]
    initial_cost = _objective(s, plan)
    initial_status = metadata["status"]
    metadata.update(scope="all_hospitals", excluded_patient_ids=[p.id for i, p in enumerate(s.patients) if not options[i]],
                    method="bounded_exact" if len(feasible) <= EXACT_PATIENT_LIMIT else "seeded_multistart_insertion",
                    seed=s.optimizer_seed, candidates_evaluated=0, candidate_budget=0,
                    initial_objective_hours=initial_cost, initial_status=initial_status,
                    initial_method=("bounded_exact_with_insertion_incumbents" if len(feasible) <= EXACT_PATIENT_LIMIT
                                    else "six_global_orders"),
                    baseline_objective_hours=_objective(s, baseline),
                    search_independent_of_baseline=True,
                    lower_bound_hours=initial_cost if initial_status == "optimal" else 0.0,
                    lower_bound_source="exact_or_zero_wait_certificate" if initial_status == "optimal"
                                       else "nonnegative_waiting")

    def retain_best(explored: dict[int, Booking]) -> tuple[dict[int, Booking], dict]:
        search_cost = _objective(s, explored)
        fallback = _objective(s, baseline) < search_cost
        selected = dict(baseline) if fallback else explored
        selected_cost = _objective(s, selected)
        metadata.update(search_objective_hours=search_cost, objective_hours=selected_cost,
                        search_status=metadata["status"], baseline_fallback_used=fallback,
                        selected_source="baseline_fallback" if fallback else "independent_global_search")
        if selected_cost == 0:
            metadata.update(status="optimal", lower_bound_hours=0.0, lower_bound_source="zero_wait_certificate")
        validate_plan(s, selected)
        return selected, metadata

    if len(feasible) <= EXACT_PATIENT_LIMIT or initial_cost == 0:
        return retain_best(plan)

    # Limit candidate work using only input size, never elapsed wall time. The
    # occupancy factor also bounds costly calendar scans for crowded few-bed
    # cases. Default 96-patient inputs receive all 1,000 candidates.
    occupancy_factor = max(1, math.ceil(len(feasible) / max(1, len(s.beds))))
    estimated_work = max(1, sum(len(options[i]) for i in feasible) * occupancy_factor)
    budget = min(CCN_CANDIDATE_LIMIT, max(1, CCN_INSERTION_WORK_BUDGET // estimated_work))
    rng = random.Random(s.optimizer_seed)
    best_cost = initial_cost
    best_order = None
    evaluated = 0
    for trial in range(budget):
        if best_order and trial % 3:
            order = best_order[:]
            for _ in range(rng.randrange(1, 9)):
                left, right = rng.sample(range(len(order)), 2)
                order[left], order[right] = order[right], order[left]
        else:
            jitter = rng.choice((0, 4, 12, 24, 48, 96, 168))
            duration = rng.choice((-2, -1, -.25, 0, .25, 1, 2))
            constraint = rng.choice((-48, -24, 0, 24, 48))
            order = sorted(feasible, key=lambda i:
                           s.patients[i].ready_hour + duration * s.patients[i].length_of_stay_hours
                           + constraint * math.log2(len(options[i])) + rng.uniform(-jitter, jitter))
        calendars: list[list[tuple[float, float]]] = [[] for _ in s.beds]
        candidate: dict[int, Booking] = {}
        random_bed_ties = trial % 5 == 0
        for i in order:
            p = s.patients[i]
            earliest, ties = math.inf, []
            for b in options[i]:
                start = _gap(p, calendars[b])
                if start < earliest:
                    earliest, ties = start, [b]
                elif start == earliest:
                    ties.append(b)
                if not random_bed_ties and earliest == p.ready_hour:
                    break
            bed = rng.choice(ties) if random_bed_ties else ties[0]
            end = round(earliest + p.length_of_stay_hours, 9)
            bisect.insort(calendars[bed], (earliest, end))
            candidate[i] = Booking(bed, earliest, end)
        evaluated += 1
        cost = _objective(s, candidate)
        if cost < best_cost:
            plan, best_cost, best_order = candidate, cost, order
        if best_cost == 0:
            break
    metadata.update(status="optimal" if best_cost == 0 else "heuristic", objective_hours=best_cost,
                    candidates_evaluated=evaluated, candidate_budget=budget)
    if best_cost == 0:
        metadata["lower_bound_source"] = "zero_wait_certificate"
    return retain_best(plan)


def _observation(s: Scenario, plans: tuple[dict[int, Booking], ...]) -> dict:
    """One complete-cohort timeline, shared by every compared policy.

    A structurally incompatible patient has no finite completion time. We still
    expose all known events for playback, without declaring that finite preview
    to be a completed observation or using it to censor the full objective.
    """
    start = min((p.ready_hour for p in s.patients), default=0.0)
    unresolved = [p.id for i, p in enumerate(s.patients) if any(i not in plan for plan in plans)]
    playback_end = max((b.end for plan in plans for b in plan.values()), default=start)
    playback_end = max(playback_end, max((p.ready_hour for p in s.patients), default=0.0))
    complete = not unresolved
    end = playback_end if complete else None
    return {"mode": "complete_cohort", "start_hour": start, "end_hour": end,
            "duration_hours": round(end - start, 9) if end is not None else None,
            "complete": complete, "playback_end_hour": playback_end,
            "unresolved_patient_ids": unresolved}


def _result(s: Scenario, plan: dict[int, Booking], strategy: str, solver: dict | None,
            requests: list[dict] | None = None, *, observation: dict | None = None) -> dict:
    observation = dict(observation) if observation is not None else _observation(s, (plan,))
    observation["unresolved_patient_ids"] = list(observation["unresolved_patient_ids"])
    playback_end = observation["playback_end_hour"]
    patients = []
    events = []
    if requests is None:
        requests = [_request_event(s, i, booking, booking.start, order)
                    for order, (i, booking) in enumerate(sorted(plan.items(), key=lambda row: (row[1].start, row[0])), 1)]
    accepted_requests = {event["patient_id"]: event for event in requests if event["type"] == "request" and event["accepted"]}
    events.extend(event.copy() for event in requests)
    for i, p in enumerate(s.patients):
        booking = plan.get(i)
        bed = s.beds[booking.bed] if booking else None
        start, end = (booking.start, booking.end) if booking else (None, None)
        placed = start is not None
        completed = end is not None
        observed_wait = round((start if placed else playback_end) - p.ready_hour, 9)
        patients.append({
            "patient_id": p.id, "hospital_id": p.hospital_id, "gender": p.gender,
            "ready_hour": p.ready_hour, "length_of_stay_hours": p.length_of_stay_hours,
            "insurance_type": p.insurance_type, "required_capabilities": list(p.required_capabilities),
            "eligible_snf_ids": list(p.eligible_snf_ids), "bed_id": bed.id if bed else None,
            "snf_id": bed.snf_id if bed else None,
            "admission_hour": start if placed else None, "release_hour": end if completed else None,
            "planned_admission_hour": start, "planned_release_hour": end,
            "wait_hours": round(start - p.ready_hour, 9) if booking else None,
            "wait_through_playback_end_hours": observed_wait,
            "wait_in_window_hours": observed_wait,
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
    for now in sorted({observation["start_hour"], playback_end, *(event["time_hour"] for event in events)}):
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
    occupied_hours = round(sum(b.end - b.start for b in plan.values()), 9)
    impossible = [p.id for i, p in enumerate(s.patients) if i not in plan]
    known_wait = _objective(s, plan)
    full_wait = None if impossible else known_wait
    observed_wait = round(sum(p["wait_through_playback_end_hours"] for p in patients), 9)
    admitted_mean = round(known_wait / len(admitted), 9) if admitted else None
    last_release = max((b.end for b in plan.values()), default=None)
    complete_duration = observation["duration_hours"]
    summary = {
        "patient_count": len(patients), "placed_count": len(admitted),
        "waiting_count": len(patients) - len(admitted),
        "completed_count": sum(p["status"] == "completed" for p in patients),
        "blocked_bed_hours": full_wait,
        "known_placeable_wait_hours": known_wait,
        "observed_wait_to_playback_end_hours": observed_wait,
        "mean_observed_wait_hours": round(observed_wait / len(patients), 9) if patients else None,
        "mean_observed_wait_time_scope": "through_playback_end_hour",
        "mean_wait_hours": admitted_mean,
        "mean_admitted_wait_hours": admitted_mean,
        "mean_admitted_population": "compatible_patients_only" if impossible else "all_patients",
        "full_plan_wait_hours": full_wait,
        "mean_full_plan_wait_hours": round(full_wait / len(patients), 9) if full_wait is not None and patients else None,
        "planned_patient_count": len(plan),
        "last_planned_admission_hour": max((b.start for b in plan.values()), default=None),
        "last_planned_release_hour": last_release,
        "time_to_completion_hours": None if impossible else round((last_release if last_release is not None else observation["start_hour"]) - observation["start_hour"], 9),
        "snf_occupied_bed_hours": occupied_hours,
        "snf_utilization": occupied_hours / (complete_duration * len(s.beds))
                           if observation["complete"] and complete_duration > 0 and s.beds else None,
        "total_beds": len(s.beds), "structurally_unplaceable_patient_ids": impossible,
        "request_count": sum(event["type"] == "request" for event in events),
        "rejected_request_count": sum(event["type"] == "request_rejected" for event in events),
    }
    if solver is not None:
        solver = {**solver, "objective_population": "compatible_patients_only" if impossible else "all_patients",
                  "full_cohort_objective_defined": not impossible,
                  "excluded_patient_ids": impossible}
    return {"label": ("Hospital-local optimization" if s.baseline_policy == "local" else "Legacy ready-only FCFS") if strategy == "baseline" else "CCN · full foresight",
            "strategy": strategy, "patients": patients, "events": events, "snapshots": snapshots,
            "summary": summary, "solver": solver, "observation": observation}


ASSUMPTIONS = [
    "Hour 0 is Monday 00:00. All SNF beds start empty. A fixed input cohort is followed from its earliest ready time through the later of the two policies' final SNF releases. arrival_span_hours and its legacy horizon_hours alias describe input-generation metadata, never a simulation cutoff or a readiness limit.",
    "Hospital-local baseline knows its own current patients and future ready times and stays, plus shared actual bed occupancy and release times. It targets its own eventual total waiting without seeing other hospitals' future demand. Among equal-wait plans it preserves unused care capabilities, then flexible-gender beds, measured in occupied bed-hours using only bed attributes and own patients. Six patient orders retain both original preference and capacity-preserving bed ties; bounded exact search additionally covers small local cohorts. Tentative future plans do not reserve SNF beds; conflicting plans are revised after actual admissions.",
    "CCN knows all hospitals' supplied future ready times, eligibility and SNF stay durations exactly. It targets eventual total waiting jointly. Global exploration uses six insertion orders followed by seeded multistart/mutation work independently of the baseline; the baseline is compared only as a final feasible fallback. optimizer_seed controls that independent search, while tie_break_seed controls hospital request arbitration. The two policies use different search effort, so gains do not isolate information alone. Only a proven bound or exhausted exact search is labeled optimal.",
    "Qualified SNF requests are processed first come, first served. Equal-hour inter-hospital requests use the scenario's reproducible seeded tie break, preserving each hospital's planned priority. Rejected hospitals replan at the same hour, with no artificial retry delay. Requests are submitted only for ready patients at their planned admission time.",
    "Both policies know current compatibility and beds. Admission, hospital departure and confirmation happen together; referral, review, authorization and transport durations are zero.",
    "A bed is reused immediately at admission plus length of stay. SNF eligibility, fixed per-bed gender, accepted insurance and all required bed care capabilities are hard constraints for both policies. An insurance wildcard (*) accepts every plan. Insurance authorization duration is not modeled. SNF list order breaks remaining bed ties after the local baseline's capacity-preservation preference; it does not override a lower total-wait plan.",
    "Hospital blocked bed-hours equal total ready-to-admission waiting for the complete cohort; they are not total hospital occupancy. The full-cohort mean divides that same total by every supplied patient. SNF utilization uses a common earliest-ready-to-final-release duration for both policies, so it is equal when both place the same cohort with the same stays.",
    "Every known admission and release is included in event logs and playback. If any patient has no compatible bed, the cohort cannot finish: end time, complete-cohort total/mean waiting and utilization are undefined. Playback still shows every known event, and observed_wait_to_playback_end_hours reports only the finite accumulated waiting to that last known event. Legacy window-named patient fields are aliases of this finite playback quantity, not a seven-day cutoff.",
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
    validate_plan(s, baseline_plan)
    observation = _observation(s, (baseline_plan, ccn_plan))
    baseline = _result(s, baseline_plan, "baseline", baseline_solver, requests, observation=observation)
    ccn = _result(s, ccn_plan, "ccn", solver, observation=observation)
    before, after = baseline["summary"], ccn["summary"]
    full_defined = before["full_plan_wait_hours"] is not None and after["full_plan_wait_hours"] is not None
    full_saved = round(before["full_plan_wait_hours"] - after["full_plan_wait_hours"], 9) if full_defined else None
    completion_defined = before["time_to_completion_hours"] is not None and after["time_to_completion_hours"] is not None
    completion_saved = round(before["time_to_completion_hours"] - after["time_to_completion_hours"], 9) if completion_defined else None
    assumptions = list(ASSUMPTIONS)
    if s.baseline_policy == "fcfs":
        assumptions[1] = "Legacy FCFS baseline sees only already-ready patients, uses ready-time/input-order priority, and takes its first compatible free bed; hospital-local foresight is disabled."
        assumptions[3] = "Legacy baseline resolves equal-ready ties by patient input order for regression compatibility; seeded inter-hospital arbitration applies to the hospital-local baseline. Qualified requests are served without artificial processing delays."
    return {"scenario": s.to_dict(), "baseline": baseline, "ccn": ccn, "observation": observation,
            "fairness": fairness_report(s.to_dict(), baseline, ccn),
            "objective": {"id": "full_cohort_total_wait", "direction": "minimize", "unit": "patient_hours",
                          "time_scope": "through_eventual_admission", "includes_all_supplied_patients": True,
                          "defined": full_defined},
            "comparison": {"blocked_bed_hours_saved": full_saved,
                           "blocked_bed_hours_reduction_percent": round(100 * full_saved / before["blocked_bed_hours"], 9) if full_defined and before["blocked_bed_hours"] > 0 else None,
                           "placement_gain": after["placed_count"] - before["placed_count"],
                           "full_plan_wait_hours_saved": full_saved,
                           "full_plan_wait_reduction_percent": round(100 * full_saved / before["full_plan_wait_hours"], 9) if full_defined and before["full_plan_wait_hours"] > 0 else None,
                           "time_to_completion_hours_saved": completion_saved,
                           "time_to_completion_reduction_percent": round(100 * completion_saved / before["time_to_completion_hours"], 9) if completion_defined and before["time_to_completion_hours"] > 0 else None},
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
