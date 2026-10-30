"""Pure, full-cohort waiting and distribution comparisons.

No scheduling engine is imported here. Positive deltas always mean CCN waits
longer. Undefined patients remain visible and invalidate population summaries;
they are never silently removed to make either policy look better.
"""

from __future__ import annotations

import math
import statistics


WAIT_TOLERANCE_HOURS = 1e-7


def _finite_wait(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _quantile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def _status(delta):
    if delta is None:
        return "undefined"
    if abs(delta) <= WAIT_TOLERANCE_HOURS:
        return "unchanged"
    return "improved" if delta < 0 else "worsened"


def _distribution(values, defined):
    return {
        "total_wait_hours": math.fsum(values) if defined else None,
        "mean_wait_hours": statistics.mean(values) if defined and values else None,
        "p90_wait_hours": _quantile(values, .9) if defined else None,
        "max_wait_hours": max(values, default=None) if defined else None,
        "worst_hospital_mean_wait_hours": None,
    }


def fairness_report(scenario_dict: dict, baseline_result: dict, ccn_result: dict) -> dict:
    """Compare every supplied patient's eventual waiting under both policies.

    Result populations must match the scenario exactly; malformed populations
    raise ValueError. A legal unplaceable patient (wait_hours=None) makes all
    whole-population measures undefined. Fully observed individual hospitals
    remain explicitly labelled, so their rows cannot be mistaken for the whole
    population. Empty hospitals do not enter hospital means or improvement counts.
    """
    supplied = scenario_dict["patients"]
    ids = [p["id"] for p in supplied]
    if len(ids) != len(set(ids)):
        raise ValueError("Fairness inputs contain duplicate patient IDs")
    hospitals = [h["id"] for h in scenario_dict["hospitals"]]
    if len(hospitals) != len(set(hospitals)) or any(p["hospital_id"] not in hospitals for p in supplied):
        raise ValueError("Fairness inputs contain invalid hospital IDs")
    policy_rows = {}
    undefined_ids = {}
    for policy, result in (("baseline", baseline_result), ("ccn", ccn_result)):
        rows = result["patients"]
        row_ids = [p["patient_id"] for p in rows]
        if len(row_ids) != len(ids) or len(set(row_ids)) != len(row_ids) or set(row_ids) != set(ids):
            raise ValueError("Fairness result must retain the exact supplied patient population")
        policy_rows[policy] = {p["patient_id"]: p for p in rows}
        undefined_ids[policy] = [pid for pid in ids if not _finite_wait(policy_rows[policy][pid].get("wait_hours"))]
        for patient in supplied:
            row = policy_rows[policy][patient["id"]]
            if row.get("hospital_id") != patient["hospital_id"]:
                raise ValueError("Fairness result changed a patient's hospital")
    defined = not any(undefined_ids.values())
    pairs = []
    for patient in supplied:
        pid = patient["id"]
        before = policy_rows["baseline"][pid].get("wait_hours")
        after = policy_rows["ccn"][pid].get("wait_hours")
        before = before if _finite_wait(before) else None
        after = after if _finite_wait(after) else None
        delta = after - before if before is not None and after is not None else None
        pairs.append({"patient_id": pid, "hospital_id": patient["hospital_id"],
                      "baseline_wait_hours": before, "ccn_wait_hours": after,
                      "delta_wait_hours": delta, "status": _status(delta)})
    distributions = {
        policy: _distribution([p[f"{policy}_wait_hours"] for p in pairs], defined)
        for policy in ("baseline", "ccn")
    }
    patient_stats = {"rows": pairs}
    for status in ("improved", "worsened", "unchanged"):
        count = sum(p["status"] == status for p in pairs) if defined else None
        patient_stats[f"{status}_count"] = count
        patient_stats[f"{status}_fraction"] = count / len(pairs) if defined and pairs else None
    patient_stats["max_increase_wait_hours"] = max((max(0, p["delta_wait_hours"]) for p in pairs), default=None) if defined else None
    hospital_rows = []
    for hospital_id in hospitals:
        group = [p for p in pairs if p["hospital_id"] == hospital_id]
        complete = all(p["delta_wait_hours"] is not None for p in group)
        row = {"hospital_id": hospital_id, "patient_count": len(group), "defined": complete}
        for policy in ("baseline", "ccn"):
            values = [p[f"{policy}_wait_hours"] for p in group]
            row[f"{policy}_total_wait_hours"] = math.fsum(values) if complete else None
            row[f"{policy}_mean_wait_hours"] = statistics.mean(values) if complete and group else None
        row["delta_total_wait_hours"] = row["ccn_total_wait_hours"] - row["baseline_total_wait_hours"] if complete else None
        row["delta_mean_wait_hours"] = row["ccn_mean_wait_hours"] - row["baseline_mean_wait_hours"] if complete and group else None
        for status in ("improved", "worsened", "unchanged"):
            row[f"{status}_patient_count"] = sum(p["status"] == status for p in group) if complete else None
        row["status"] = "empty" if not group else _status(row["delta_total_wait_hours"])
        hospital_rows.append(row)
    if defined:
        for policy in distributions:
            distributions[policy]["worst_hospital_mean_wait_hours"] = max(
                (h[f"{policy}_mean_wait_hours"] for h in hospital_rows if h["patient_count"]), default=None)
    return {"schema_version": 1, "defined": defined,
            "reason": None if defined else "At least one supplied patient has undefined eventual waiting; full-cohort fairness is undefined.",
            "patient_count": len(pairs), "undefined_patient_ids": undefined_ids,
            "delta_definition": "CCN minus baseline; a positive waiting delta means worse under CCN.",
            "comparison_tolerance_hours": WAIT_TOLERANCE_HOURS,
            **distributions, "patients": patient_stats, "hospitals": hospital_rows,
            "hospital_comparison": {
                f"{status}_count": sum(h["status"] == status for h in hospital_rows) if defined else None
                for status in ("improved", "worsened", "unchanged")}}
