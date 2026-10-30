"""Run every controlled dynamic experiment for predeclared seeds.

Standard library only. JSON retains exact inputs, plans and observations; CSV
contains one row per seed and experiment. No seed is selected for its outcome.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from dynamic_experiments import catalog
from dynamic_model import compare


def audit_result(scenario: dict, result: dict) -> dict:
    """Independently check output constraints and accounting, without solver helpers."""
    patients = {p["id"]: p for p in scenario["patients"]}
    beds = {b["id"]: b for b in scenario["beds"]}
    snfs = {s["id"]: s for s in scenario["snfs"]}
    rows = result["patients"]
    if len(rows) != len(patients) or {p["patient_id"] for p in rows} != set(patients):
        raise ValueError("Patient population was dropped or duplicated")
    horizon = scenario["horizon_hours"]
    calendars = {b: [] for b in beds}
    total_wait = window_wait = occupied_hours = 0.0
    admitted = completed = 0
    impossible = []

    def compatible(p, b):
        plans = snfs[b["snf_id"]].get("accepted_insurances", ["*"])
        return (b["snf_id"] in p["eligible_snf_ids"]
                and b["gender"] in (p["gender"], "any")
                and ("*" in plans or p.get("insurance_type", "unspecified") in plans)
                and set(p.get("required_capabilities", [])) <= set(b.get("capabilities", [])))

    for row in rows:
        p = patients[row["patient_id"]]
        if any(row[key] != p[key] for key in ("hospital_id", "ready_hour", "length_of_stay_hours")):
            raise ValueError("Reported patient attributes differ from submitted inputs")
        start, end = row["planned_admission_hour"], row["planned_release_hour"]
        if start is None:
            if (end is not None or row["bed_id"] is not None or row["wait_hours"] is not None
                    or any(compatible(p, b) for b in beds.values())):
                raise ValueError("Missing a feasible patient's eventual placement")
            impossible.append(p["id"])
        else:
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)):
                raise ValueError("Nonfinite booking")
            if start < p["ready_hour"] or not math.isclose(end - start, p["length_of_stay_hours"], abs_tol=1e-7):
                raise ValueError("Ready time or SNF stay violated")
            if not math.isclose(row["wait_hours"], start - p["ready_hour"], abs_tol=1e-7):
                raise ValueError("Patient eventual waiting is inconsistent")
            b = beds.get(row["bed_id"])
            if b is None or not compatible(p, b) or row["snf_id"] != b["snf_id"]:
                raise ValueError("Incompatible placement")
            if row["admission_hour"] != (start if start <= horizon else None):
                raise ValueError("Future admission counted as observed")
            if row["release_hour"] != (end if end <= horizon else None):
                raise ValueError("Future release counted as observed")
            calendars[b["id"]].append((start, end))
            total_wait += start - p["ready_hour"]
            occupied_hours += max(0, min(horizon, end) - start)
            admitted += start <= horizon
            completed += end <= horizon
        expected_wait = (horizon if start is None else min(horizon, start)) - p["ready_hour"]
        if not math.isclose(row["wait_in_window_hours"], expected_wait, abs_tol=1e-7):
            raise ValueError("Patient window waiting is inconsistent")
        window_wait += expected_wait
    for intervals in calendars.values():
        intervals.sort()
        if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
            raise ValueError("Overlapping SNF bed bookings")
    summary = result["summary"]
    for key, expected in (("blocked_bed_hours", window_wait), ("snf_occupied_bed_hours", occupied_hours),
                          ("placed_count", admitted), ("completed_count", completed),
                          ("waiting_count", len(patients) - admitted)):
        if not math.isclose(summary[key], expected, abs_tol=1e-6):
            raise ValueError(f"Inconsistent summary: {key}")
    if impossible:
        if summary["full_plan_wait_hours"] is not None:
            raise ValueError("Unplaceable patients must leave full-population objective undefined")
    elif not math.isclose(summary["full_plan_wait_hours"], total_wait, abs_tol=1e-6):
        raise ValueError("Inconsistent eventual objective")
    for snapshot in result["snapshots"]:
        now = snapshot["time_hour"]
        expected_waiting = {p["patient_id"] for p in rows if p["ready_hour"] <= now
                            and (p["planned_admission_hour"] is None or p["planned_admission_hour"] > now)}
        expected_occupants = {p["bed_id"]: p["patient_id"] for p in rows
                              if p["planned_admission_hour"] is not None
                              and p["planned_admission_hour"] <= now < p["planned_release_hour"]}
        expected_complete = sum(p["planned_release_hour"] is not None and p["planned_release_hour"] <= now for p in rows)
        if (set(snapshot["waiting_patient_ids"]) != expected_waiting
                or snapshot["waiting_count"] != len(expected_waiting)
                or snapshot["occupied_count"] != len(expected_occupants)
                or snapshot["completed_count"] != expected_complete
                or {b: p for b, p in snapshot["bed_occupants"].items() if p is not None} != expected_occupants):
            raise ValueError("Snapshot population or occupancy is inconsistent")
        for hospital in scenario["hospitals"]:
            expected = sum(patients[p]["hospital_id"] == hospital["id"] for p in expected_waiting)
            if snapshot["hospital_waiting"].get(hospital["id"]) != expected:
                raise ValueError("Hospital waiting count is inconsistent")
        for snf in scenario["snfs"]:
            expected = sum(beds[b]["snf_id"] == snf["id"] for b in expected_occupants)
            if snapshot["snf_occupied"].get(snf["id"]) != expected:
                raise ValueError("SNF occupancy count is inconsistent")
    return {"passed": True, "patients_checked": len(rows), "bookings_checked": len(rows) - len(impossible),
            "snapshots_checked": len(result["snapshots"]), "unplaceable_ids": impossible}


def _percent(saved, before):
    return 100 * saved / before if before else None


def _quantile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def summarize(experiment: dict, seed: int, result: dict, seconds: float) -> dict:
    s = result["scenario"]
    before, after = result["baseline"]["summary"], result["ccn"]["summary"]
    row = {"experiment_id": experiment["id"], "cohort_id": experiment["cohort_id"], "seed": seed,
           "patients": len(s["patients"]), "hospitals": len(s["hospitals"]), "snfs": len(s["snfs"]),
           "beds": len(s["beds"]), "horizon_hours": s["horizon_hours"],
           "offered_load": sum(p["length_of_stay_hours"] for p in s["patients"]) / (len(s["beds"]) * s["horizon_hours"]),
           "runtime_seconds": round(seconds, 6)}
    for policy in ("baseline", "ccn"):
        summary, patient_rows = result[policy]["summary"], result[policy]["patients"]
        for key in ("full_plan_wait_hours", "blocked_bed_hours", "placed_count", "waiting_count", "snf_utilization"):
            row[f"{policy}_{key}"] = summary[key]
        row[f"{policy}_p90_eventual_wait_hours"] = _quantile([p["wait_hours"] for p in patient_rows if p["wait_hours"] is not None], .9)
        row[f"{policy}_max_eventual_wait_hours"] = max((p["wait_hours"] for p in patient_rows if p["wait_hours"] is not None), default=None)
        hospital_means = [statistics.mean(p["wait_hours"] for p in patient_rows if p["hospital_id"] == hospital["id"] and p["wait_hours"] is not None)
                          for hospital in s["hospitals"] if any(p["hospital_id"] == hospital["id"] and p["wait_hours"] is not None for p in patient_rows)]
        row[f"{policy}_worst_hospital_mean_wait_hours"] = max(hospital_means, default=None)
    for key in ("full_plan_wait_hours", "blocked_bed_hours"):
        saved = before[key] - after[key] if before[key] is not None and after[key] is not None else None
        row[f"{key}_saved"] = saved
        row[f"{key}_saved_percent"] = _percent(saved, before[key]) if saved is not None else None
    row["solver_status"] = result["ccn"]["solver"]["status"]
    row["solver_initial_objective_hours"] = result["ccn"]["solver"].get("initial_objective_hours")
    row["solver_candidates_evaluated"] = result["ccn"]["solver"].get("candidates_evaluated")
    return row


def aggregate(rows: list[dict]) -> list[dict]:
    result = []
    for experiment_id in dict.fromkeys(row["experiment_id"] for row in rows):
        group = [row for row in rows if row["experiment_id"] == experiment_id]
        item = {"experiment_id": experiment_id, "seed_count": len(group), "seeds": [row["seed"] for row in group]}
        for field in ("baseline_full_plan_wait_hours", "ccn_full_plan_wait_hours", "full_plan_wait_hours_saved",
                      "baseline_blocked_bed_hours", "ccn_blocked_bed_hours", "blocked_bed_hours_saved",
                      "baseline_placed_count", "ccn_placed_count", "runtime_seconds"):
            values = [row[field] for row in group if row[field] is not None]
            item[field + "_mean"] = statistics.mean(values) if values else None
            item[field + "_stdev"] = statistics.stdev(values) if len(values) > 1 else 0 if values else None
        for field in ("full_plan_wait_hours", "blocked_bed_hours"):
            before = sum(row[f"baseline_{field}"] for row in group)
            saved = sum(row[f"{field}_saved"] for row in group)
            item[field + "_pooled_saved_percent"] = _percent(saved, before)
        result.append(item)
    return result


def run_suite(seeds=(42, 84, 126)) -> dict:
    if not seeds or any(type(seed) is not int or not 0 <= seed <= 2**32 - 1 for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Supply distinct integer seeds between 0 and 4294967295")
    runs, rows = [], []
    for seed in seeds:
        for experiment in catalog(seed):
            started = time.perf_counter()
            result = compare(experiment["scenario"])
            seconds = time.perf_counter() - started
            audits = {policy: audit_result(result["scenario"], result[policy]) for policy in ("baseline", "ccn")}
            if result["ccn"]["summary"]["full_plan_wait_hours"] > result["baseline"]["summary"]["full_plan_wait_hours"] + 1e-7:
                raise ValueError("CCN worsened its feasible local incumbent")
            summary = summarize(experiment, seed, result, seconds)
            rows.append(summary)
            runs.append({"experiment": experiment, "summary": summary, "audits": audits, "result": result})
    sources = ("dynamic_model.py", "dynamic_experiments.py", "dynamic_scenarios.py", "run_dynamic_experiments.py")
    return {"schema_version": 1, "seeds": list(seeds), "rows": rows, "aggregate": aggregate(rows), "runs": runs,
            "source_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in sources},
            "interpretation": [
                "Every declared seed is retained, including zero or negative observation-window savings.",
                "Eventual total waiting is optimized; horizon-censored blocked bed-hours, admissions and tail waits are separate outcomes.",
                "Offered load is sum of all SNF stays divided by nominal bed-hours; it is not observed utilization or a steady-state estimate.",
                "Within/cross-hospital controls isolate hospital ownership of the same demand; solver effort differs between baseline and CCN, so their difference is not a pure information-value estimate.",
                "Heuristic plans are feasible upper bounds, not certified global optima. These are synthetic mechanism and capacity stress tests, not real-world effect estimates.",
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 84, 126])
    parser.add_argument("--output", type=Path, help="Save exact inputs, full results, audits and aggregate JSON")
    parser.add_argument("--csv", type=Path, help="Save one summary row per seed and experiment")
    args = parser.parse_args()
    try:
        report = run_suite(args.seeds)
    except ValueError as error:
        parser.error(str(error))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.csv:
        with args.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(report["rows"][0]))
            writer.writeheader()
            writer.writerows(report["rows"])
    print(json.dumps({"seeds": report["seeds"], "aggregate": report["aggregate"],
                      "all_audits_passed": True}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
