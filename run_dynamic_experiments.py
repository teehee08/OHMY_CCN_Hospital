"""Run controlled experiments with separate, predeclared random streams.

Standard library only. Patient cohorts, SNF request arbitration and global search
use separate seeds. JSON retains exact inputs, plans and fairness; CSV contains
one row per crossed trial. No seed is selected for its outcome.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
import random
import platform

from dynamic_analysis import fairness_report
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
    observation = result["observation"]
    start_hour = min((p["ready_hour"] for p in patients.values()), default=0)
    playback_end = observation["playback_end_hour"]
    duration = observation["duration_hours"]
    if observation["start_hour"] != start_hour or playback_end < max((p["ready_hour"] for p in patients.values()), default=0):
        raise ValueError("Observation must cover the complete supplied cohort")
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
                    or row["admission_hour"] is not None or row["release_hour"] is not None
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
            if row["admission_hour"] != start or row["release_hour"] != end or end > playback_end:
                raise ValueError("Complete-cohort observation lost an admission or release")
            calendars[b["id"]].append((start, end))
            total_wait += start - p["ready_hour"]
            occupied_hours += end - start
            admitted += 1
            completed += 1
        expected_wait = (playback_end if start is None else start) - p["ready_hour"]
        if not math.isclose(row["wait_in_window_hours"], expected_wait, abs_tol=1e-7):
            raise ValueError("Patient playback waiting is inconsistent")
        if not math.isclose(row["wait_through_playback_end_hours"], expected_wait, abs_tol=1e-7):
            raise ValueError("Patient complete observation waiting is inconsistent")
        window_wait += expected_wait
    for intervals in calendars.values():
        intervals.sort()
        if any(a[1] > b[0] for a, b in zip(intervals, intervals[1:])):
            raise ValueError("Overlapping SNF bed bookings")
    summary = result["summary"]
    for key, expected in (("observed_wait_to_playback_end_hours", window_wait), ("snf_occupied_bed_hours", occupied_hours),
                          ("placed_count", admitted), ("completed_count", completed),
                          ("waiting_count", len(patients) - admitted)):
        if not math.isclose(summary[key], expected, abs_tol=1e-6):
            raise ValueError(f"Inconsistent summary: {key}")
    if impossible:
        if summary["full_plan_wait_hours"] is not None:
            raise ValueError("Unplaceable patients must leave full-population objective undefined")
        if summary["blocked_bed_hours"] is not None or observation["complete"] or observation["end_hour"] is not None or duration is not None:
            raise ValueError("An unresolved cohort cannot have a finite complete observation")
    elif not math.isclose(summary["full_plan_wait_hours"], total_wait, abs_tol=1e-6):
        raise ValueError("Inconsistent eventual objective")
    elif (not observation["complete"] or observation["end_hour"] != playback_end
          or isinstance(duration, bool) or not isinstance(duration, (int, float))
          or not math.isfinite(duration)
          or not math.isclose(duration, playback_end - start_hour, rel_tol=0, abs_tol=1e-7)
          or not math.isclose(summary["blocked_bed_hours"], total_wait, abs_tol=1e-6)):
        raise ValueError("Full waiting and full blocked bed-hours must share one complete interval")
    def check_optional(key, expected):
        value = summary[key]
        if expected is None:
            if value is not None:
                raise ValueError(f"Undefined summary must remain null: {key}")
        elif (isinstance(value, bool) or not isinstance(value, (int, float))
              or not math.isfinite(value) or not math.isclose(value, expected, abs_tol=1e-6)):
            raise ValueError(f"Inconsistent summary: {key}")

    admitted_wait = sum(row["wait_hours"] for row in rows if row["admission_hour"] is not None)
    check_optional("mean_wait_hours", admitted_wait / admitted if admitted else None)
    check_optional("mean_observed_wait_hours", window_wait / len(rows) if rows else None)
    check_optional("snf_utilization", occupied_hours / (duration * len(beds)) if beds and duration and not impossible else None)
    check_optional("time_to_completion_hours", max((row["planned_release_hour"] for row in rows), default=start_hour) - start_hour if not impossible else None)
    if "mean_admitted_wait_hours" in summary:
        check_optional("mean_admitted_wait_hours", admitted_wait / admitted if admitted else None)
    if "mean_full_plan_wait_hours" in summary:
        check_optional("mean_full_plan_wait_hours", total_wait / len(rows) if rows and not impossible else None)
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
    if result["snapshots"][0]["time_hour"] != start_hour or result["snapshots"][-1]["time_hour"] != playback_end:
        raise ValueError("Snapshots do not cover the common observation interval")
    for kind, column in (("ready", "ready_hour"), ("admit", "planned_admission_hour"), ("release", "planned_release_hour")):
        expected = {(row["patient_id"], row[column]) for row in rows if row[column] is not None}
        actual = {(event["patient_id"], event["time_hour"]) for event in result["events"] if event["type"] == kind}
        if actual != expected:
            raise ValueError(f"Missing complete-cohort events: {kind}")
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


def summarize(experiment: dict, seed: int, result: dict, seconds: float, *,
              request_seed=None, optimizer_seed=None, preference_variant="supplied", preference_seed=0) -> dict:
    s = result["scenario"]
    before, after = result["baseline"]["summary"], result["ccn"]["summary"]
    fairness = result.get("fairness") or fairness_report(s, result["baseline"], result["ccn"])
    row = {"experiment_id": experiment["id"], "cohort_id": experiment["cohort_id"], "seed": seed,
           "cohort_seed": seed,
           "request_seed": s.get("tie_break_seed", 42) if request_seed is None else request_seed,
           "optimizer_seed": s.get("optimizer_seed", 20261029) if optimizer_seed is None else optimizer_seed,
           "preference_variant": preference_variant,
           "preference_seed": preference_seed,
           "base_experiment_id": experiment.get("base_experiment_id", experiment["id"]),
           "patients": len(s["patients"]), "hospitals": len(s["hospitals"]), "snfs": len(s["snfs"]),
           "beds": len(s["beds"]), "arrival_span_hours": s["arrival_span_hours"],
           "observation_start_hour": result["observation"]["start_hour"],
           "observation_end_hour": result["observation"]["end_hour"],
           "observation_duration_hours": result["observation"]["duration_hours"],
           "offered_load": sum(p["length_of_stay_hours"] for p in s["patients"]) / (len(s["beds"]) * s["arrival_span_hours"]) if s["beds"] else None,
           "runtime_seconds": round(seconds, 6)}
    for policy in ("baseline", "ccn"):
        summary, patient_rows = result[policy]["summary"], result[policy]["patients"]
        for key in ("full_plan_wait_hours", "blocked_bed_hours", "placed_count", "waiting_count", "snf_utilization", "time_to_completion_hours"):
            row[f"{policy}_{key}"] = summary[key]
        row[f"{policy}_eventually_placeable_patient_count"] = sum(p["wait_hours"] is not None for p in patient_rows)
        row[f"{policy}_structurally_unplaceable_patient_count"] = sum(p["wait_hours"] is None for p in patient_rows)
        for output, field in (("mean_eventual_wait_hours", "mean_wait_hours"),
                              ("p90_eventual_wait_hours", "p90_wait_hours"),
                              ("max_eventual_wait_hours", "max_wait_hours"),
                              ("worst_hospital_mean_wait_hours", "worst_hospital_mean_wait_hours")):
            row[f"{policy}_{output}"] = fairness[policy][field]
    for key in ("full_plan_wait_hours", "blocked_bed_hours"):
        saved = before[key] - after[key] if before[key] is not None and after[key] is not None else None
        row[f"{key}_saved"] = saved
        row[f"{key}_saved_percent"] = _percent(saved, before[key]) if saved is not None else None
    before_completion, after_completion = before["time_to_completion_hours"], after["time_to_completion_hours"]
    row["time_to_completion_hours_saved"] = before_completion - after_completion if before_completion is not None and after_completion is not None else None
    row["solver_status"] = result["ccn"]["solver"]["status"]
    row["solver_initial_objective_hours"] = result["ccn"]["solver"].get("initial_objective_hours")
    row["solver_candidates_evaluated"] = result["ccn"]["solver"].get("candidates_evaluated")
    row["fairness_defined"] = fairness["defined"]
    for status in ("improved", "worsened", "unchanged"):
        row[f"patients_{status}_count"] = fairness["patients"][f"{status}_count"]
        row[f"patients_{status}_fraction"] = fairness["patients"][f"{status}_fraction"]
        row[f"hospitals_{status}_count"] = fairness["hospital_comparison"][f"{status}_count"]
    row["max_patient_wait_increase_hours"] = fairness["patients"]["max_increase_wait_hours"]
    for field in ("p90_wait_hours", "max_wait_hours", "worst_hospital_mean_wait_hours"):
        a, b = fairness["baseline"][field], fairness["ccn"][field]
        row[f"{field}_change"] = b - a if a is not None and b is not None else None
    row["hospital_fairness"] = fairness["hospitals"]
    return row


def aggregate(rows: list[dict]) -> list[dict]:
    """Legacy descriptive row pooling; use aggregate_nested for cohort inference."""
    result = []
    for experiment_id in dict.fromkeys(row["experiment_id"] for row in rows):
        group = [row for row in rows if row["experiment_id"] == experiment_id]
        item = {"experiment_id": experiment_id, "seed_count": len(group), "seeds": [row["seed"] for row in group],
                "paired_run_count": len(group),
                "aggregation_unit": "paired run, descriptive only; seed_count is a legacy run-count alias, not an independent-cohort count",
                "cohort_count": len({row.get("cohort_seed", row["seed"]) for row in group})}
        for field in ("baseline_full_plan_wait_hours", "ccn_full_plan_wait_hours", "full_plan_wait_hours_saved",
                      "baseline_blocked_bed_hours", "ccn_blocked_bed_hours", "blocked_bed_hours_saved",
                      "baseline_placed_count", "ccn_placed_count", "baseline_time_to_completion_hours",
                      "ccn_time_to_completion_hours", "time_to_completion_hours_saved", "runtime_seconds"):
            values = [row[field] for row in group if row[field] is not None]
            item[field + "_defined_seed_count"] = len(values)
            # Never silently turn a full-seed cohort into a selected subset of
            # seeds by dropping undefined eventual objectives.
            all_defined = len(values) == len(group)
            item[field + "_mean"] = statistics.mean(values) if values and all_defined else None
            item[field + "_stdev"] = (statistics.stdev(values) if len(values) > 1 else 0) if values and all_defined else None
        for field in ("full_plan_wait_hours", "blocked_bed_hours"):
            if any(row[f"baseline_{field}"] is None or row[f"{field}_saved"] is None for row in group):
                item[field + "_pooled_saved_percent"] = None
            else:
                before = sum(row[f"baseline_{field}"] for row in group)
                saved = sum(row[f"{field}_saved"] for row in group)
                item[field + "_pooled_saved_percent"] = _percent(saved, before)
        result.append(item)
    return result


DEFAULT_COHORT_SEEDS = (42, 84, 126)
DEFAULT_REQUEST_SEEDS = tuple(range(20))
DEFAULT_OPTIMIZER_SEEDS = (20261029,)
BOOTSTRAP_SEED = 20261030
BOOTSTRAP_REPLICATES = 2000
PREFERENCE_VARIANTS = ("supplied", "ordinary_first", "shuffled")

# Declared before inspecting any result. Every field uses the same complete-case
# rule: one undefined run invalidates its complete group mean, rather than being
# silently removed. Replicate trials within one cohort are not independent cohorts.
METRIC_FIELDS = (
    "baseline_full_plan_wait_hours", "ccn_full_plan_wait_hours", "full_plan_wait_hours_saved",
    "full_plan_wait_hours_saved_percent", "baseline_blocked_bed_hours", "ccn_blocked_bed_hours",
    "blocked_bed_hours_saved", "baseline_placed_count", "ccn_placed_count",
    "baseline_time_to_completion_hours", "ccn_time_to_completion_hours", "time_to_completion_hours_saved",
    "baseline_mean_eventual_wait_hours", "ccn_mean_eventual_wait_hours",
    "baseline_p90_eventual_wait_hours", "ccn_p90_eventual_wait_hours",
    "baseline_max_eventual_wait_hours", "ccn_max_eventual_wait_hours",
    "baseline_worst_hospital_mean_wait_hours", "ccn_worst_hospital_mean_wait_hours",
    "patients_improved_count", "patients_worsened_count", "patients_unchanged_count",
    "patients_improved_fraction", "patients_worsened_fraction", "patients_unchanged_fraction",
    "hospitals_improved_count", "hospitals_worsened_count", "hospitals_unchanged_count",
    "max_patient_wait_increase_hours", "p90_wait_hours_change", "max_wait_hours_change",
    "worst_hospital_mean_wait_hours_change", "runtime_seconds",
)


def _seeds(values, label):
    values = tuple(values)
    if not values or any(type(seed) is not int or not 0 <= seed <= 2**32 - 1 for seed in values) or len(set(values)) != len(values):
        raise ValueError(f"{label}: supply distinct integer seeds between 0 and 4294967295")
    return values


def _hash_json(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


SOURCE_FILES = ("dynamic_model.py", "dynamic_experiments.py", "dynamic_scenarios.py", "dynamic_analysis.py", "run_dynamic_experiments.py")


def _source_hashes():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _matrix_design(experiment, cohort_seed, request_seeds, optimizer_seeds):
    """Describe actual crossed inputs rather than a catalog's historical seed."""
    design = deepcopy(experiment.get("design") or {})
    catalog_seed = design.pop("seed", cohort_seed)
    held = []
    for text in design.get("held_constant", []):
        # Catalog controls share a seed when comparing capacity/ownership. This
        # batch additionally crosses independent request and optimizer streams.
        for phrase in (" and tie-break seed", ", tie-break seed", " and tie_break_seed", ", tie_break_seed"):
            text = text.replace(phrase, "")
        held.append(text)
    design.update(
        catalog_cohort_seed=catalog_seed, cohort_seed=cohort_seed,
        held_constant=held,
        held_constant_scope="Patient/resource controls hold within a cohort and preference variant; request and optimizer seeds are separately crossed. A baseline/CCN pair always shares the exact same supplied input.",
        actual_request_seeds=list(request_seeds), actual_optimizer_seeds=list(optimizer_seeds),
        actual_seed_policy="cohort_seed generates patients; tie_break_seed is independently assigned from request_seeds; optimizer_seed independently controls CCN exploration. Catalog seed never substitutes for either execution stream.",
        preference_variant=experiment.get("preference_variant", "supplied"),
        preference_seed=experiment.get("preference_seed"),
    )
    if len(request_seeds) == 1:
        design["actual_request_seed"] = request_seeds[0]
    if len(optimizer_seeds) == 1:
        design["actual_optimizer_seed"] = optimizer_seeds[0]
    design["comparison_note"] = design.get("comparison_note", "") + (
        " In this batch, 'seed' in the original catalog denotes cohort generation only. "
        "Request arbitration and optimizer seeds are crossed independently and are held equal only within each paired baseline/CCN run."
    )
    return design


def _distribution(values):
    defined = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    complete = bool(values) and len(defined) == len(values)
    return {"run_count": len(values), "defined_run_count": len(defined), "complete": complete,
            "mean": statistics.mean(defined) if complete else None,
            "stdev": (statistics.stdev(defined) if len(defined) > 1 else 0.0) if complete else None,
            "min": min(defined) if complete else None,
            "p05": _quantile(defined, .05) if complete else None,
            "median": _quantile(defined, .5) if complete else None,
            "p95": _quantile(defined, .95) if complete else None,
            "max": max(defined) if complete else None}


def _fairness_outcomes(rows):
    return {"undefined_run_count": sum(not r.get("fairness_defined", False) for r in rows),
            "runs_with_worsened_patients": sum(r.get("patients_worsened_count") is not None and r["patients_worsened_count"] > 0 for r in rows),
            "runs_with_worsened_hospitals": sum(r.get("hospitals_worsened_count") is not None and r["hospitals_worsened_count"] > 0 for r in rows),
            "runs_with_p90_increase": sum(r.get("p90_wait_hours_change") is not None and r["p90_wait_hours_change"] > 1e-7 for r in rows),
            "runs_with_max_wait_increase": sum(r.get("max_wait_hours_change") is not None and r["max_wait_hours_change"] > 1e-7 for r in rows)}


HOSPITAL_METRIC_FIELDS = ("baseline_total_wait_hours", "ccn_total_wait_hours", "delta_total_wait_hours",
                          "baseline_mean_wait_hours", "ccn_mean_wait_hours", "delta_mean_wait_hours",
                          "improved_patient_count", "worsened_patient_count", "unchanged_patient_count")


def _hospital_distributions(rows):
    identifiers = list(dict.fromkeys(h["hospital_id"] for r in rows for h in r.get("hospital_fairness", [])))
    result = []
    for identifier in identifiers:
        hospitals = [next((h for h in r.get("hospital_fairness", []) if h["hospital_id"] == identifier), None) for r in rows]
        result.append({"hospital_id": identifier,
                       "patient_counts": [h["patient_count"] if h is not None else None for h in hospitals],
                       "metrics": {field: _distribution([h[field] if h is not None else None for h in hospitals]) for field in HOSPITAL_METRIC_FIELDS}})
    return result


def aggregate_nested(rows: list[dict]) -> list[dict]:
    """Keep request draws nested in cohorts; CI resamples cohort means only.

    Optimizer seeds and preference variants are reported as separate strata.
    The bootstrap describes these synthetic sampled cohorts, not clinical effects.
    With only three default cohorts its nominal 95% interval is highly unstable.
    """
    groups = {}
    for row in rows:
        key = (row["experiment_id"], row.get("preference_variant", "supplied"), row.get("optimizer_seed", 20261029))
        groups.setdefault(key, []).append(row)
    results = []
    for (experiment_id, preference_variant, optimizer_seed), group in groups.items():
        cohorts = {}
        for row in group:
            cohorts.setdefault(row.get("cohort_seed", row["seed"]), []).append(row)
        cohort_summaries = []
        for cohort_seed, draws in cohorts.items():
            request_seeds = [r.get("request_seed", r["seed"]) for r in draws]
            if len(set(request_seeds)) != len(request_seeds):
                raise ValueError("Duplicate request replicate within the same cohort/optimizer/preference stratum")
            cohort_summaries.append({
                "cohort_seed": cohort_seed, "cohort_id": draws[0]["cohort_id"],
                "request_seeds": request_seeds, "paired_run_count": len(draws),
                "metrics": {field: _distribution([r.get(field) for r in draws]) for field in METRIC_FIELDS},
                "hospital_metrics": _hospital_distributions(draws),
                "fairness_outcomes": _fairness_outcomes(draws),
                "zero_savings_run_count": sum(r.get("full_plan_wait_hours_saved") == 0 for r in draws),
                "negative_savings_run_count": sum(r.get("full_plan_wait_hours_saved") is not None and r["full_plan_wait_hours_saved"] < 0 for r in draws),
            })
        count = len(cohort_summaries)
        rng = random.Random(BOOTSTRAP_SEED)
        resamples = [tuple(rng.randrange(count) for _ in range(count)) for _ in range(BOOTSTRAP_REPLICATES)] if count >= 2 else []
        metrics = {}
        for field in METRIC_FIELDS:
            means = [c["metrics"][field]["mean"] for c in cohort_summaries]
            complete = all(v is not None for v in means)
            bootstrapped = [statistics.mean(means[i] for i in draw) for draw in resamples] if complete else []
            metrics[field] = {
                "cohort_means": means, "defined_cohort_count": sum(v is not None for v in means),
                "mean_of_cohort_means": statistics.mean(means) if complete else None,
                "between_cohort_stdev": statistics.stdev(means) if complete and count > 1 else None,
                "cohort_bootstrap_ci95": [_quantile(bootstrapped, .025), _quantile(bootstrapped, .975)] if bootstrapped else None,
            }
        before = metrics["baseline_full_plan_wait_hours"]["mean_of_cohort_means"]
        saved = metrics["full_plan_wait_hours_saved"]["mean_of_cohort_means"]
        results.append({"experiment_id": experiment_id, "preference_variant": preference_variant,
                        "optimizer_seed": optimizer_seed, "cohort_count": count, "paired_run_count": len(group),
                        "request_seed_counts": [c["paired_run_count"] for c in cohort_summaries],
                        "cohorts": cohort_summaries, "metrics": metrics,
                        "fairness_outcomes": _fairness_outcomes(group),
                        "equal_cohort_pooled_saved_percent": _percent(saved, before) if saved is not None and before is not None else None,
                        "uncertainty": {
                            "unit": "cohort (request draws are nested replicates, not independent cohorts)",
                            "method": "percentile bootstrap of equally weighted cohort means",
                            "bootstrap_seed": BOOTSTRAP_SEED, "bootstrap_replicates": BOOTSTRAP_REPLICATES,
                            "nominal_level": .95,
                            "small_cohort_warning": "Fewer than ten independent synthetic cohorts: bootstrap coverage and endpoints are unstable; do not interpret as a precise clinical effect interval." if count < 10 else None,
                            "single_cohort_warning": "A between-cohort confidence interval cannot be estimated from one cohort." if count < 2 else None,
                        }})
    return results


def _compact_result(result):
    """Keep exact plans and all fairness; omit only large event/snapshot playback."""
    compact = {key: deepcopy(value) for key, value in result.items() if key not in ("baseline", "ccn", "scenario")}
    for policy in ("baseline", "ccn"):
        compact[policy] = {key: deepcopy(value) for key, value in result[policy].items()
                           if key not in ("snapshots", "events", "requests")}
    return compact


def run_suite(seeds=None, *, cohort_seeds=None, request_seeds=DEFAULT_REQUEST_SEEDS,
              optimizer_seeds=DEFAULT_OPTIMIZER_SEEDS, experiment_ids=None,
              preference_variants=("supplied",), preference_seed=0, compact=False) -> dict:
    """Run a predeclared crossed cohort × request × optimizer experiment matrix.

    `seeds` is the legacy alias of cohort_seeds only. It never sets request or
    optimizer seeds. Each input template is frozen once per cohort/experiment/
    preference; only the two explicitly declared random-stream fields then vary.
    """
    if seeds is not None and cohort_seeds is not None:
        raise ValueError("Use either seeds (legacy cohort alias) or cohort_seeds, not both")
    cohort_seeds = _seeds(cohort_seeds if cohort_seeds is not None else seeds if seeds is not None else DEFAULT_COHORT_SEEDS, "cohort_seeds")
    request_seeds = _seeds(request_seeds, "request_seeds")
    optimizer_seeds = _seeds(optimizer_seeds, "optimizer_seeds")
    preference_variants = tuple(preference_variants)
    _seeds((preference_seed,), "preference_seed")
    if not preference_variants or len(set(preference_variants)) != len(preference_variants) or any(v not in PREFERENCE_VARIANTS for v in preference_variants):
        raise ValueError(f"Supply distinct preference variants from {PREFERENCE_VARIANTS}")
    experiment_ids = tuple(experiment_ids) if experiment_ids is not None else None
    if experiment_ids is not None and (not experiment_ids or len(set(experiment_ids)) != len(experiment_ids)):
        raise ValueError("experiment_ids must be nonempty and distinct")
    source_sha256 = _source_hashes()
    runs, rows, templates = [], [], []
    selected_ids = None
    for cohort_seed in cohort_seeds:
        experiments = catalog(cohort_seed)
        known = {e["id"] for e in experiments}
        if experiment_ids is not None and set(experiment_ids) - known:
            raise ValueError(f"Unknown experiment IDs: {sorted(set(experiment_ids) - known)}")
        experiments = [e for e in experiments if experiment_ids is None or e["id"] in experiment_ids]
        if not experiments:
            raise ValueError("No experiments selected")
        selected_ids = [e["id"] for e in experiments]
        for base_experiment in experiments:
            for preference_mode in preference_variants:
                from dynamic_experiments import preference_variant as build_preference_variant
                experiment = build_preference_variant(base_experiment, mode=preference_mode, seed=preference_seed)
                actual_preference_seed = experiment.get("preference_seed")
                template = deepcopy(experiment["scenario"])
                # A cohort generator's legacy tie seed is not an experimental
                # request seed. Explicit crossed streams replace it on every run.
                template.pop("tie_break_seed", None)
                template.pop("optimizer_seed", None)
                input_id = f"{cohort_seed}:{experiment['id']}:{preference_mode}:{actual_preference_seed}"
                templates.append({"input_id": input_id, "cohort_seed": cohort_seed,
                                  "experiment_id": experiment["id"], "cohort_id": experiment["cohort_id"],
                                  "base_experiment_id": base_experiment["id"],
                                  "preference_variant": preference_mode, "preference_seed": actual_preference_seed,
                                  "scenario_template": deepcopy(template), "template_sha256": _hash_json(template),
                                  "catalog_design": deepcopy(base_experiment.get("design")),
                                  "catalog_design_scope": "Original catalog design, preserved for provenance only. Its seed is the cohort seed and its tie-break held-constant statement describes paired catalog controls, not the crossed batch. Use actual_matrix_design for executed seed semantics.",
                                  "actual_matrix_design": _matrix_design(experiment, cohort_seed, request_seeds, optimizer_seeds),
                                  "actual_request_seeds": list(request_seeds), "actual_optimizer_seeds": list(optimizer_seeds),
                                  "reconstruction": "Copy scenario_template; assign tie_break_seed=request_seed and optimizer_seed=optimizer_seed from run.seeds."})
                for request_seed in request_seeds:
                    for optimizer_seed in optimizer_seeds:
                        scenario = deepcopy(template)
                        scenario.update(tie_break_seed=request_seed, optimizer_seed=optimizer_seed)
                        input_hash = _hash_json(scenario)
                        started = time.perf_counter()
                        result = compare(scenario)
                        seconds = time.perf_counter() - started
                        audits = {policy: audit_result(result["scenario"], result[policy]) for policy in ("baseline", "ccn")}
                        before, after = (result[policy]["summary"]["full_plan_wait_hours"] for policy in ("baseline", "ccn"))
                        if before is not None and after is not None and after > before + 1e-7:
                            raise ValueError("CCN worsened its feasible local incumbent")
                        if "fairness" not in result:
                            result["fairness"] = fairness_report(result["scenario"], result["baseline"], result["ccn"])
                        summary = summarize(experiment, cohort_seed, result, seconds, request_seed=request_seed,
                                            optimizer_seed=optimizer_seed, preference_variant=preference_mode,
                                            preference_seed=actual_preference_seed)
                        summary["input_id"] = input_id
                        summary["input_sha256"] = input_hash
                        rows.append(summary)
                        record = {"input_id": input_id, "input_sha256": input_hash,
                                  "seeds": {"cohort_seed": cohort_seed, "request_seed": request_seed,
                                            "optimizer_seed": optimizer_seed},
                                  "preference_variant": preference_mode, "preference_seed": actual_preference_seed, "summary": summary,
                                  "audits": audits, "result": _compact_result(result) if compact else result}
                        if not compact:
                            record["experiment"] = {**deepcopy(experiment), "scenario": deepcopy(scenario)}
                            record["experiment"]["design"] = _matrix_design(experiment, cohort_seed, (request_seed,), (optimizer_seed,))
                        runs.append(record)
    if _source_hashes() != source_sha256:
        raise ValueError("Experiment sources changed during this batch; rerun with frozen sources for valid provenance")
    return {"schema_version": 2, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "seeds": list(cohort_seeds), "cohort_seeds": list(cohort_seeds),
            "request_seeds": list(request_seeds), "optimizer_seeds": list(optimizer_seeds),
            "experiment_ids": selected_ids, "preference_variants": list(preference_variants), "preference_seed": preference_seed,
            "design": {"matrix": "cohort_seed × experiment_id × preference_variant × request_seed × optimizer_seed",
                       "seed_alias": "seeds and CLI --seeds select cohort generation only",
                       "request_seed_field": "tie_break_seed", "optimizer_seed_field": "optimizer_seed",
                       "independent_uncertainty_unit": "cohort; request and optimizer draws are nested sensitivity replicates",
                       "predeclared_all_runs_retained": True, "compact": bool(compact),
                       "compact_omissions": ["event and snapshot playback", "per-run duplicated input scenario"] if compact else [],
                       "preference_sensitivity": "Only ordered eligible_snf_ids change. Eligible sets, hospital ownership, insurance/care, readiness, LOS and beds remain fixed. This tests order sensitivity, not empirical patient choice."},
            "rows": rows, "aggregate": aggregate(rows), "nested_aggregate": aggregate_nested(rows),
            "cohort_inputs": templates, "runs": runs,
            "source_sha256": source_sha256,
            "runtime": {"python_version": platform.python_version(), "python_implementation": platform.python_implementation(),
                        "platform": platform.platform()},
            "interpretation": [
                "Every declared seed is retained, including zero savings, worse patient/hospital tails or longer completion duration. No favorable seed is selected.",
                "Request randomness is measured within a frozen cohort. It is not confounded with drawing another patient population. Optimizer seeds are separately declared and separately aggregated.",
                "Use nested_aggregate for inference. The legacy aggregate pools rows descriptively and provides no independent-cohort confidence interval.",
                "Each cohort mean receives equal weight in across-cohort estimates. Nominal 95% bootstrap intervals resample cohorts, never individual request draws; three cohorts provide very weak and unstable interval evidence.",
                "Full-cohort total waiting equals complete ready-to-departure hospital blocked bed-hours. Playback ends after both cohorts finish SNF stays.",
                "Offered load uses the arrival-generation span, not observation duration; it is neither actual utilization nor a steady-state estimate.",
                "With fixed SNF stays and a common complete interval, occupied SNF bed-hours and utilization are identical. Completion and waiting can differ.",
                "Information scope, coordination and solver effort differ between baseline and CCN. Their difference is not a pure information-value estimate.",
                "Heuristic plans are feasible upper bounds, not certified global optima. Synthetic mechanism and capacity stress tests do not estimate real-world clinical effects.",
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cohort_group = parser.add_mutually_exclusive_group()
    cohort_group.add_argument("--cohort-seeds", nargs="+", type=int, help="Frozen patient-generation seeds (default: 42 84 126)")
    cohort_group.add_argument("--seeds", nargs="+", type=int, help="Legacy alias of --cohort-seeds; does not set request seeds")
    parser.add_argument("--request-seeds", nargs="+", type=int, default=list(DEFAULT_REQUEST_SEEDS), help="SNF same-time arbitration seeds (default: 0 through 19)")
    parser.add_argument("--optimizer-seeds", nargs="+", type=int, default=list(DEFAULT_OPTIMIZER_SEEDS), help="Independent CCN search seeds")
    parser.add_argument("--experiment-ids", nargs="+", help="Run only these declared experiment IDs")
    parser.add_argument("--preference-variants", nargs="+", choices=PREFERENCE_VARIANTS, default=["supplied"])
    parser.add_argument("--preference-seed", type=int, default=0, help="Fixed preference-shuffle seed, independent of other random streams")
    parser.add_argument("--compact", action="store_true", help="Retain exact inputs, plans, fairness and seeds; omit large animation playback")
    parser.add_argument("--output", type=Path, help="Save input templates, paired results, audits and nested aggregates as JSON")
    parser.add_argument("--csv", type=Path, help="Save one summary row per complete crossed trial")
    args = parser.parse_args()
    try:
        report = run_suite(args.seeds, cohort_seeds=args.cohort_seeds, request_seeds=args.request_seeds,
                           optimizer_seeds=args.optimizer_seeds, experiment_ids=args.experiment_ids,
                           preference_variants=args.preference_variants, preference_seed=args.preference_seed, compact=args.compact)
    except ValueError as error:
        parser.error(str(error))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.csv:
        with args.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(report["rows"][0]))
            writer.writeheader()
            writer.writerows({key: json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                              if isinstance(value, (dict, list)) else value for key, value in row.items()}
                             for row in report["rows"])
    print(json.dumps({"cohort_seeds": report["cohort_seeds"], "request_seeds": report["request_seeds"],
                      "optimizer_seeds": report["optimizer_seeds"],
                      "summary": [{"experiment_id": x["experiment_id"], "preference_variant": x["preference_variant"],
                                   "optimizer_seed": x["optimizer_seed"], "cohort_count": x["cohort_count"],
                                   "paired_run_count": x["paired_run_count"],
                                   "savings": x["metrics"]["full_plan_wait_hours_saved"],
                                   "small_cohort_warning": x["uncertainty"]["small_cohort_warning"]}
                                  for x in report["nested_aggregate"]],
                      "all_audits_passed": True}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
