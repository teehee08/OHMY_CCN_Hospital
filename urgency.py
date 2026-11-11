"""Optional urgency-weighted objective. All defaults are illustrative assumptions.

When a scenario supplies an ``urgency`` block, both policies minimize

    sum_i u_i * C(wait_i) + lambda * sum_i (1 - Home_i)

instead of plain total waiting, where

    u_i = h_hospital(i) * (1 + sum_k beta_k * f_k,i)
    C(w) = (exp(alpha * w) - 1) / alpha      (C(w) = w when alpha = 0)

Patient factors f_k,i are scaled to 0-1:

* clinical_risk: clinician flag low / medium / high -> 0 / 0.5 / 1
* decline_risk: supplied 0-1 value, or derived from age and AM-PAC 6-Clicks
  mobility (raw 6-24): 0.5 * (age - 65) / 30 + 0.5 * (24 - mobility) / 18
* authorization: 1 - (authorization_expiry_hour - ready_hour) / window
* hard_to_place: 1 - compatible beds / largest compatible-bed count

The hospital multiplier h rises linearly from 1 to 1 + kappa as occupancy
moves from ``pressure_start`` to ``pressure_full`` (or as ED boarding reaches
``ed_boarding_full``). Waiting cost grows exponentially with time already
waited (``wait_growth_per_day``). Distance uses straight-line kilometres
between a patient's home and the SNF; Home = 1 - min(d, max) / max.

Hard constraints (eligibility, gender, insurance, care capabilities and
staffing) are unchanged: urgency only reorders feasible placements.
"""
import math

DEFAULTS = {
    "beta": {"clinical_risk": 1.0, "decline_risk": 0.5, "authorization": 0.5, "hard_to_place": 0.5},
    "wait_growth_per_day": 0.10,
    "hospital_pressure_weight": 0.5,
    "pressure_start": 0.85,
    "pressure_full": 0.95,
    "ed_boarding_full": 10,
    "authorization_window_hours": 72,
    "distance_weight_hours": 6.0,
    "max_distance_km": 30.0,
}
FACTORS = ("clinical_risk", "decline_risk", "authorization", "hard_to_place")
RISK_LEVELS = {"low": 0.0, "medium": 0.5, "high": 1.0}
PATIENT_FIELDS = ("clinical_risk", "age", "mobility_score", "decline_risk",
                  "authorization_expiry_hour", "home_x_km", "home_y_km")
HOSPITAL_FIELDS = ("occupancy", "ed_boarding")
SNF_FIELDS = ("x_km", "y_km")
MAX_EXPONENT = 50.0


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _num(value, label, low=0.0, high=1_000_000.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a finite number from {low:g} to {high:g}")
    return float(value)


def _optional(row, key, label, low=0.0, high=1_000_000.0):
    value = row.get(key)
    return None if value is None else _num(value, f"{label} {key}", low, high)


def parse(data, hospital_rows, snf_rows, patient_rows):
    """Normalize the urgency block and per-entity urgency inputs, or None."""
    block = data.get("urgency")
    if block is None or block is False:
        return None
    if not isinstance(block, dict):
        raise ValueError("urgency must be an object")
    params = {key: value for key, value in DEFAULTS.items() if key != "beta"}
    for key in params:
        if key in block:
            params[key] = _num(block[key], f"urgency.{key}")
    if params["pressure_full"] <= params["pressure_start"]:
        raise ValueError("urgency.pressure_full must exceed pressure_start")
    if params["authorization_window_hours"] <= 0 or params["max_distance_km"] <= 0 or params["ed_boarding_full"] <= 0:
        raise ValueError("urgency windows and maximum distance must be positive")
    beta = dict(DEFAULTS["beta"])
    supplied = block.get("beta", {})
    if not isinstance(supplied, dict) or any(key not in FACTORS for key in supplied):
        raise ValueError(f"urgency.beta may only contain {', '.join(FACTORS)}")
    for key, value in supplied.items():
        beta[key] = _num(value, f"urgency.beta.{key}", 0.0, 100.0)
    params["beta"] = beta
    hospitals = {}
    for row in hospital_rows:
        hospitals[row["id"]] = {"occupancy": _optional(row, "occupancy", f"Hospital {row['id']}", 0.0, 2.0),
                                "ed_boarding": _optional(row, "ed_boarding", f"Hospital {row['id']}")}
    snfs = {}
    for row in snf_rows:
        snfs[row["id"]] = {key: _optional(row, key, f"SNF {row['id']}", -100_000.0, 100_000.0) for key in SNF_FIELDS}
    patients = {}
    for row in patient_rows:
        label = f"Patient {row['id']}"
        risk = row.get("clinical_risk", "low")
        if risk not in RISK_LEVELS:
            raise ValueError(f"{label} clinical_risk must be low, medium or high")
        patients[row["id"]] = {
            "clinical_risk": risk,
            "age": _optional(row, "age", label, 0.0, 120.0),
            "mobility_score": _optional(row, "mobility_score", label, 6.0, 24.0),
            "decline_risk": _optional(row, "decline_risk", label, 0.0, 1.0),
            "authorization_expiry_hour": _optional(row, "authorization_expiry_hour", label),
            "home_x_km": _optional(row, "home_x_km", label, -100_000.0, 100_000.0),
            "home_y_km": _optional(row, "home_y_km", label, -100_000.0, 100_000.0),
        }
    return {"enabled": True, "params": params, "hospitals": hospitals, "snfs": snfs, "patients": patients}


def block(urgency):
    """Scenario-level JSON block (entity fields are written on their rows)."""
    return {**{k: v for k, v in urgency["params"].items() if k != "beta"}, "beta": dict(urgency["params"]["beta"])}


def entity_fields(urgency, kind, entity_id):
    values = urgency[kind].get(entity_id, {})
    return {key: value for key, value in values.items() if value is not None}


def hospital_multiplier(params, attrs):
    span = params["pressure_full"] - params["pressure_start"]
    pressure = 0.0
    if attrs.get("occupancy") is not None:
        pressure = _clamp((attrs["occupancy"] - params["pressure_start"]) / span)
    if attrs.get("ed_boarding") is not None:
        pressure = max(pressure, _clamp(attrs["ed_boarding"] / params["ed_boarding_full"]))
    return 1.0 + params["hospital_pressure_weight"] * pressure, pressure


def derive(urgency, patients, beds, accepted_insurances):
    """Per-patient weights, factor values and per-SNF distance penalties."""
    params = urgency["params"]
    insurance = dict(accepted_insurances)
    compatible = []
    for p in patients:
        compatible.append(sum(1 for bed in beds if bed.snf_id in p.eligible_snf_ids
                              and bed.gender in (p.gender, "any")
                              and ("*" in insurance.get(bed.snf_id, ("*",)) or p.insurance_type in insurance.get(bed.snf_id, ("*",)))
                              and set(p.required_capabilities).issubset(bed.capabilities)))
    largest = max(compatible, default=0)
    weights, components, distances = [], [], []
    for p, count in zip(patients, compatible):
        attrs = urgency["patients"].get(p.id, {})
        f = {"clinical_risk": RISK_LEVELS[attrs.get("clinical_risk") or "low"]}
        if attrs.get("decline_risk") is not None:
            f["decline_risk"] = attrs["decline_risk"]
        elif attrs.get("age") is not None or attrs.get("mobility_score") is not None:
            age = 0.0 if attrs.get("age") is None else _clamp((attrs["age"] - 65) / 30)
            mobility = 0.0 if attrs.get("mobility_score") is None else _clamp((24 - attrs["mobility_score"]) / 18)
            f["decline_risk"] = 0.5 * age + 0.5 * mobility
        else:
            f["decline_risk"] = 0.0
        expiry = attrs.get("authorization_expiry_hour")
        f["authorization"] = 0.0 if expiry is None else _clamp(1 - (expiry - p.ready_hour) / params["authorization_window_hours"])
        f["hard_to_place"] = 0.0 if largest == 0 or count == 0 else _clamp(1 - count / largest)
        h, pressure = hospital_multiplier(params, urgency["hospitals"].get(p.hospital_id, {}))
        base = 1 + sum(params["beta"][k] * f[k] for k in FACTORS)
        weight = round(h * base, 9)
        weights.append(weight)
        components.append({**{k: round(v, 6) for k, v in f.items()}, "hospital_multiplier": round(h, 6),
                           "hospital_pressure": round(pressure, 6), "compatible_beds": count, "urgency_weight": weight})
        row = {}
        hx, hy = attrs.get("home_x_km"), attrs.get("home_y_km")
        for snf, loc in urgency["snfs"].items():
            if hx is None or hy is None or loc.get("x_km") is None or loc.get("y_km") is None:
                continue
            km = math.hypot(hx - loc["x_km"], hy - loc["y_km"])
            home = 1 - min(km, params["max_distance_km"]) / params["max_distance_km"]
            row[snf] = (round(params["distance_weight_hours"] * (1 - home), 9), round(km, 6))
        distances.append(row)
    return {"weights": tuple(weights), "components": tuple(components), "distances": tuple(distances),
            "alpha": params["wait_growth_per_day"] / 24.0}


def wait_cost(s, i, wait):
    if not s.urgency:
        return wait
    derived = s.urgency_derived
    alpha = derived["alpha"]
    if alpha <= 0:
        growth = wait
    else:
        growth = (math.exp(min(alpha * wait, MAX_EXPONENT)) - 1) / alpha
    return derived["weights"][i] * growth


def distance_cost(s, i, bed):
    if not s.urgency:
        return 0.0
    entry = s.urgency_derived["distances"][i].get(s.beds[bed].snf_id)
    return entry[0] if entry else 0.0


def placement_cost(s, i, bed, start):
    return wait_cost(s, i, start - s.patients[i].ready_hour) + distance_cost(s, i, bed)


def min_distance_cost(s, i, options):
    if not s.urgency or not options:
        return 0.0
    return min(distance_cost(s, i, b) for b in options)


def distance_km(s, i, bed):
    if not s.urgency:
        return None
    entry = s.urgency_derived["distances"][i].get(s.beds[bed].snf_id)
    return entry[1] if entry else None
