"""Reproducible synthetic cohorts for the dynamic placement lab.

Insurance plans and care tags are scenario labels, not real provider contracts.
Patients have a compatible anchor bed when any beds exist. The configured
arrival span generates this finite cohort; it never stops the simulation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import random
from typing import Any


LIMITS = {"max_patients": 500, "max_beds": 500, "max_facilities": 40}
DEFAULTS = {"patient_count": 96, "hospital_count": 4, "snf_count": 6,
            "bed_count": 24, "horizon_hours": 168, "seed": 42}
PLANS = ("Plan_A", "Plan_B", "Plan_C")
CARE_TAGS = ("oxygen", "bariatric", "wound_care")
_UNSET = object()


def _integer(value: Any, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 0 and {maximum}")
    return value


def generate(params: Any = _UNSET) -> dict:
    if params is _UNSET:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Generator parameters must be an object")
    params = dict(params)
    if "arrival_span_hours" in params:
        if "horizon_hours" in params and params["horizon_hours"] != params["arrival_span_hours"]:
            raise ValueError("arrival_span_hours and legacy horizon_hours must agree when both are supplied")
        params["horizon_hours"] = params.pop("arrival_span_hours")
    unknown = set(params) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown generator parameter: " + sorted(unknown)[0])
    values = {**DEFAULTS, **params}
    count = _integer(values["patient_count"], "patient_count", LIMITS["max_patients"])
    hospital_count = _integer(values["hospital_count"], "hospital_count", LIMITS["max_facilities"])
    snf_count = _integer(values["snf_count"], "snf_count", LIMITS["max_facilities"])
    bed_count = _integer(values["bed_count"], "bed_count", LIMITS["max_beds"])
    seed = _integer(values["seed"], "seed", 2**32 - 1)
    horizon = values["horizon_hours"]
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError("arrival_span_hours must be a finite positive number")
    try:
        horizon = float(horizon)
    except OverflowError:
        raise ValueError("arrival_span_hours must be a finite positive number") from None
    if not math.isfinite(horizon) or not 0 < horizon <= 1_000_000:
        raise ValueError("arrival_span_hours must be greater than 0 and at most 1000000")
    horizon = round(float(horizon), 6)
    if horizon <= 0:
        raise ValueError("arrival_span_hours must be at least 0.000001")
    if count and not hospital_count:
        raise ValueError("At least one hospital is required when generating patients")
    if bed_count and not snf_count:
        raise ValueError("At least one SNF is required when generating beds")
    rng = random.Random(seed)
    hospitals = [{"id": f"Hospital_{chr(65 + i)}" if i < 26 else f"Hospital_{i + 1}"}
                 for i in range(hospital_count)]
    snfs = [{"id": f"SNF_{i + 1}",
             "accepted_insurances": list(PLANS) if i % 5 == 0 else [PLANS[i % 3], PLANS[(i + 1) % 3]]}
            for i in range(snf_count)]
    beds = []
    for i in range(bed_count):
        capabilities = ["rehabilitation"]
        if i % 3 != 2:
            capabilities.append(CARE_TAGS[(i // max(1, snf_count)) % len(CARE_TAGS)])
        if rng.random() < 0.15:
            capabilities.append(CARE_TAGS[(CARE_TAGS.index(capabilities[-1]) + 1) % 3] if len(capabilities) > 1 else "oxygen")
        beds.append({"id": f"B{i + 1:03}", "snf_id": snfs[i % snf_count]["id"],
                     "gender": ("any", "female", "male")[(i // max(1, snf_count) + i) % 3],
                     "capabilities": list(dict.fromkeys(capabilities))})
    snf_by_id = {snf["id"]: snf for snf in snfs}
    patients = []
    day_count = max(1, math.ceil(horizon / 24))
    for i in range(count):
        anchor = rng.choice(beds) if beds else None
        gender = anchor["gender"] if anchor and anchor["gender"] != "any" else rng.choice(("female", "male"))
        insurance = rng.choice(snf_by_id[anchor["snf_id"]]["accepted_insurances"]) if anchor else rng.choice(PLANS)
        required = ["rehabilitation"]
        if anchor:
            extras = [tag for tag in anchor["capabilities"] if tag != "rehabilitation"]
            if extras and rng.random() < 0.55:
                required.append(rng.choice(extras))
        eligible = [snf["id"] for snf in snfs]
        rng.shuffle(eligible)
        eligible = eligible[:rng.randint(1, min(4, len(eligible)))] if eligible else []
        if anchor and anchor["snf_id"] not in eligible:
            if len(eligible) >= 4:
                eligible.pop()
            eligible.append(anchor["snf_id"])
        rng.shuffle(eligible)
        ready = min(horizon, rng.randrange(day_count) * 24 + rng.choice((8, 9, 10, 11, 13, 14, 15, 16, 17)))
        patients.append({"id": f"P{i + 1:03}", "hospital_id": hospitals[i % hospital_count]["id"],
                         "gender": gender, "insurance_type": insurance, "required_capabilities": required,
                         "ready_hour": round(ready, 6),
                         "length_of_stay_hours": rng.choice((12, 24, 36, 48, 72)),
                         "eligible_snf_ids": eligible})
    return {"schema_version": 1,
            "name": f"{horizon / 24:g}-day arrivals · {hospital_count} hospitals · {count} patients",
            "arrival_span_hours": horizon, "horizon_hours": horizon,
            "tie_break_seed": seed, "baseline_policy": "local",
            "hospitals": hospitals, "snfs": snfs, "beds": beds, "patients": patients}


def examples() -> dict:
    legacy = json.loads((Path(__file__).parent / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8"))["scenarios"][0]
    legacy["baseline_policy"] = "fcfs"
    legacy["name"] = "Legacy FIFO reference · 5 patients"
    return {"scenarios": [generate(), generate({"patient_count": 240, "hospital_count": 8,
                                               "snf_count": 12, "bed_count": 72,
                                               "horizon_hours": 336, "seed": 84}), legacy],
            "limits": dict(LIMITS)}
