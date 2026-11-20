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


LIMITS = {"max_patients": 1000, "max_beds": 600, "max_facilities": 40}
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


def _sheet_rows(workbook, sheet_name: str) -> tuple[list[str], list[list[Any]]]:
    worksheet = workbook[sheet_name]
    rows = list(worksheet.iter_rows(values_only=True))
    headers = [str(value) if value is not None else "" for value in rows[0]]
    return headers, [list(row) for row in rows[1:] if any(value is not None for value in row)]


def _header_index(headers: list[str], prefix: str) -> int:
    for index, header in enumerate(headers):
        if header.startswith(prefix):
            return index
    raise KeyError(prefix)


def _header_role_shifts(headers: list[str], role: str) -> int:
    for index, header in enumerate(headers):
        if header.startswith(role) and "shifts/day" in header:
            return index
    raise KeyError(f"{role} shifts/day")


def _nyc_scenario() -> dict:
    """Build the Brooklyn workbook as an editable dynamic scenario."""
    try:
        from openpyxl import load_workbook
    except ImportError as error:
        raise RuntimeError("Real NYC data requires the openpyxl package") from error

    workbook_path = Path(__file__).parent / "nyc_data" / "Brooklyn_Hospital_SNF_Data.xlsx"
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)

    hospital_headers, hospital_rows = _sheet_rows(workbook, "Hospitals")
    hospital_index = {name: index for index, name in enumerate(hospital_headers)}
    hospitals = []
    hospital_count_targets = []
    for row in hospital_rows:
        pfi = row[hospital_index["PFI"]]
        name = row[hospital_index["Hospital"]]
        if pfi in (None, "TOTAL") or name is None:
            continue
        occupancy = row[hospital_index["Occupancy"]]
        hospitals.append({
            "id": str(pfi),
            "name": str(name),
            "lat": float(row[hospital_index["Lat"]]),
            "lon": float(row[hospital_index["Lon"]]),
            "occupancy": round(float(occupancy), 6),
        })
        hospital_count_targets.append(float(row[hospital_index["To SNF per day"]]))

    snf_headers, snf_rows = _sheet_rows(workbook, "SNFs")
    snf_index = {name: index for index, name in enumerate(snf_headers)}
    corrected_beds_index = _header_index(snf_headers, "Available beds (corrected)")
    rn_shifts_index = _header_role_shifts(snf_headers, "RN")
    lpn_shifts_index = _header_role_shifts(snf_headers, "LPN")
    cna_shifts_index = _header_role_shifts(snf_headers, "CNA")
    snfs = []
    snf_stats = {}
    for row in snf_rows:
        ccn = row[snf_index["CCN"]]
        name = row[snf_index["SNF"]]
        if ccn is None or name is None:
            continue
        ccn = str(ccn)
        corrected_beds = max(0, int(round(float(row[corrected_beds_index] or 0))))
        rn_hours = float(row[snf_index["RN hours/day"]] or 0)
        lpn_hours = float(row[snf_index["LPN hours/day"]] or 0)
        cna_hours = float(row[snf_index["CNA + aide hours/day"]] or 0)
        avg_residents = float(row[snf_index["Avg residents/day"]] or 0)
        snf = {
            "id": ccn,
            "name": str(name),
            "lat": float(row[snf_index["Lat"]]),
            "lon": float(row[snf_index["Lon"]]),
            "available_beds": corrected_beds,
            "accepted_insurances": ["*"],
        }
        snfs.append(snf)
        snf_stats[ccn] = {
            "available_beds": corrected_beds,
            "rn_hours": rn_hours,
            "lpn_hours": lpn_hours,
            "cna_hours": cna_hours,
            "avg_residents": avg_residents,
            "rn_shifts": int(round(float(row[rn_shifts_index] or 0))),
            "lpn_shifts": int(round(float(row[lpn_shifts_index] or 0))),
            "cna_shifts": int(round(float(row[cna_shifts_index] or 0))),
            "avg_los_days": float(row[snf_index["Avg Medicare LOS (days)"]] or 41),
            "ventilator_beds": int(round(float(row[snf_index["Ventilator beds available"]] or 0))),
            "dialysis_beds": int(round(float(row[snf_index["Dialysis capacity (beds/dens)"]] or 0))),
        }

    distance_headers, distance_rows = _sheet_rows(workbook, "Distance_km")
    distance_matrix = {}
    for hospital, row in zip(hospitals, distance_rows):
        hospital_id = hospital["id"]
        distance_matrix[hospital_id] = {}
        for index, snf in enumerate(snfs, start=1):
            if len(row) > index and row[index] is not None:
                distance_matrix[hospital_id][snf["id"]] = float(row[index])

    los_headers, los_rows = _sheet_rows(workbook, "LOS_Before_SNF")
    median_los_days = 9.0
    los_los_index = los_headers.index("Hospital length of stay (days), patients discharged to SNF") if "Hospital length of stay (days), patients discharged to SNF" in los_headers else 0
    cumulative = 0
    for row in los_rows:
        patient_count = row[1] if len(row) > 1 else 0
        if isinstance(patient_count, (int, float)):
            cumulative += patient_count
            if cumulative >= 20250 / 2:
                if isinstance(row[los_los_index], (int, float)):
                    median_los_days = float(row[los_los_index])
                break

    total_residents = sum(stats["avg_residents"] for stats in snf_stats.values())
    care_demand = {
        "RN": round(sum(stats["rn_hours"] for stats in snf_stats.values()) / total_residents, 6) if total_residents else 0,
        "LPN": round(sum(stats["lpn_hours"] for stats in snf_stats.values()) / total_residents, 6) if total_residents else 0,
        "CNA": round(sum(stats["cna_hours"] for stats in snf_stats.values()) / total_residents, 6) if total_residents else 0,
    }

    daily_target = int(round(sum(hospital_count_targets)))
    base = [int(math.floor(value)) for value in hospital_count_targets]
    remainder = daily_target - sum(base)
    fractions = sorted(
        range(len(hospital_count_targets)),
        key=lambda index: (hospital_count_targets[index] - base[index], -index),
        reverse=True,
    )
    for index in fractions[:remainder]:
        base[index] += 1
    target_counts = base

    origin_lat = sum(hospital["lat"] for hospital in hospitals) / len(hospitals) if hospitals else 40.65
    origin_lon = sum(hospital["lon"] for hospital in hospitals) / len(hospitals) if hospitals else -73.95
    origin_lon_radians = math.radians(origin_lat)

    def projected(lat: float, lon: float) -> tuple[float, float]:
        x_km = (lon - origin_lon) * 111.32 * math.cos(origin_lon_radians)
        y_km = (lat - origin_lat) * 110.574
        return round(x_km, 6), round(y_km, 6)

    beds = []
    for snf in snfs:
        stats = snf_stats[snf["id"]]
        for bed_index in range(stats["available_beds"]):
            capabilities = ["general"]
            if bed_index < stats["ventilator_beds"]:
                capabilities.append("ventilator")
            if bed_index < stats["dialysis_beds"]:
                capabilities.append("dialysis")
            beds.append({
                "id": f"B_{snf['id']}_{bed_index + 1}",
                "snf_id": snf["id"],
                "gender": "any",
                "capabilities": capabilities,
            })

    patients = []
    patient_number = 1
    reconstruction_days = 12
    for day in range(reconstruction_days):
        for hospital, count in zip(hospitals, base):
            hospital_distances = distance_matrix.get(hospital["id"], {})
            eligible = sorted(hospital_distances, key=lambda snf_id: hospital_distances[snf_id])[:3]
            hx, hy = projected(hospital["lat"], hospital["lon"])
            for offset in range(count):
                ready = round(day * 24 + 24 * (offset + 1) / max(count, 1) - 24 / max(count, 1), 6)
                assigned_snf = eligible[(offset + day) % len(eligible)] if eligible else (snfs[0]["id"] if snfs else None)
                los_days = snf_stats[assigned_snf]["avg_los_days"] if assigned_snf else 41
                patients.append({
                    "id": f"NYC_{patient_number:03}",
                    "hospital_id": hospital["id"],
                    "gender": "male" if patient_number % 2 else "female",
                    "ready_hour": ready,
                    "length_of_stay_hours": round(los_days * 24, 6),
                    "eligible_snf_ids": eligible,
                    "insurance_type": "*",
                    "required_capabilities": [],
                    "care_hours_per_day": care_demand,
                    "home_x_km": hx,
                    "home_y_km": hy,
                })
                patient_number += 1

    staffing_shifts = []
    for snf in snfs:
        stats = snf_stats[snf["id"]]
        staffing_shifts.append({
            "snf_id": snf["id"],
            "start_hour": 0,
            "end_hour": 24,
            "staff_count": {
                "RN": stats["rn_shifts"],
                "LPN": stats["lpn_shifts"],
                "CNA": stats["cna_shifts"],
            },
            "direct_care_hours_per_staff_per_day": {"RN": 8, "LPN": 8, "CNA": 8},
        })

    total_beds = len(beds)
    workbook.close()
    return {
        "schema_version": 1,
        "name": f"Brooklyn 12-day reconstruction · {len(patients)} patients · {total_beds} available SNF beds",
        "arrival_span_hours": 288,
        "horizon_hours": 288,
        "tie_break_seed": 42,
        "baseline_policy": "local",
        "hospitals": [{
            "id": hospital["id"],
            "name": hospital["name"],
            "occupancy": hospital["occupancy"],
            "x_km": projected(hospital["lat"], hospital["lon"])[0],
            "y_km": projected(hospital["lat"], hospital["lon"])[1],
        } for hospital in hospitals],
        "snfs": [{
            "id": snf["id"],
            "name": snf["name"],
            "accepted_insurances": ["*"],
            "x_km": projected(snf["lat"], snf["lon"])[0],
            "y_km": projected(snf["lat"], snf["lon"])[1],
        } for snf in snfs],
        "beds": beds,
        "patients": patients,
        "staffing": {
            "schedule_type": "daily_repeating",
            "data_source": "CMS PBJ 2026 Q2",
            "shifts": staffing_shifts,
        },
        "urgency": {
            "distance_weight_hours": 6.0,
            "max_distance_km": 30.0,
        },
        "data_notes": {
            "simulation_window": "12 reconstructed days; arrivals repeat the 2024 annual-average SNF referral rate.",
            "hospital_inflow_outflow": "Daily inflow/outflow is not available. Starting hospital pressure uses 2026-11-18 occupancy, and arrivals use 2024 steady-state SNF referral averages.",
            "snf_to_hospital": "55 patients/day are allocated across hospitals using 2024 hospital-level SNF discharge totals divided by 366.",
            "pre_snf_los_median_days": median_los_days,
            "snf_los": "Patient SNF length of stay uses facility Medicare average LOS.",
            "distance": "Patients use the workbook's straight-line hospital-SNF matrix.",
            "staffing": "Staff headcounts use rounded RN/LPN/CNA 8-hour shift equivalents from CMS PBJ; per-patient demand is a facility-weighted Brooklyn average.",
            "bed_availability": "Available beds use the workbook's corrected census values.",
        },
    }


def examples() -> dict:
    legacy = json.loads((Path(__file__).parent / "scenarios" / "dynamic_examples.json").read_text(encoding="utf-8"))["scenarios"][0]
    legacy["baseline_policy"] = "fcfs"
    legacy["name"] = "Legacy FIFO reference · 5 patients"
    return {"scenarios": [generate(), generate({"patient_count": 240, "hospital_count": 8,
                                               "snf_count": 12, "bed_count": 72,
                                               "horizon_hours": 336, "seed": 84}), legacy, _nyc_scenario()],
            "limits": dict(LIMITS)}
