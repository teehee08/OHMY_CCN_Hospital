"""Reproducible, explicitly synthetic urgency inputs for the demo.

All clinical-risk flags, ages, mobility scores, authorization dates, hospital
pressure values and coordinates are illustrative assumptions, not measured data.
"""
import random

DEFAULT_URGENCY = {
    "beta": {"clinical_risk": 1.0, "decline_risk": 0.5, "authorization": 0.5, "hard_to_place": 0.5},
    "wait_growth_per_day": 0.10, "hospital_pressure_weight": 0.5,
    "pressure_start": 0.85, "pressure_full": 0.95, "ed_boarding_full": 10,
    "authorization_window_hours": 72, "distance_weight_hours": 6.0, "max_distance_km": 30.0,
}


def two_patients_one_bed():
    return {
        "schema_version": 1, "name": "Urgency: two patients, one bed", "arrival_span_hours": 1,
        "tie_break_seed": 42, "optimizer_seed": 20261029, "baseline_policy": "local",
        "urgency": dict(DEFAULT_URGENCY),
        "hospitals": [{"id": "H1", "occupancy": 0.80}, {"id": "H2", "occupancy": 0.80}],
        "snfs": [{"id": "SNF_1", "accepted_insurances": ["*"], "x_km": 0, "y_km": 0}],
        "beds": [{"id": "B1", "snf_id": "SNF_1", "gender": "any", "capabilities": []}],
        "patients": [
            {"id": "Stable", "hospital_id": "H1", "gender": "female", "ready_hour": 0, "length_of_stay_hours": 48,
             "eligible_snf_ids": ["SNF_1"], "insurance_type": "Medicare", "required_capabilities": [],
             "clinical_risk": "low", "age": 70, "mobility_score": 20, "home_x_km": 3, "home_y_km": 4},
            {"id": "Urgent", "hospital_id": "H2", "gender": "male", "ready_hour": 0, "length_of_stay_hours": 48,
             "eligible_snf_ids": ["SNF_1"], "insurance_type": "Medicare", "required_capabilities": [],
             "clinical_risk": "high", "age": 88, "mobility_score": 9, "authorization_expiry_hour": 24,
             "home_x_km": 3, "home_y_km": 4},
        ],
    }


def regional_cohort(patient_count=60, seed=7):
    rnd = random.Random(seed)
    hospitals = [{"id": "H1", "occupancy": 0.80, "ed_boarding": 0},
                 {"id": "H2", "occupancy": 0.88, "ed_boarding": 2},
                 {"id": "H3", "occupancy": 0.96, "ed_boarding": 6},
                 {"id": "H4", "occupancy": 0.99, "ed_boarding": 12}]
    snfs = [{"id": "SNF_North", "accepted_insurances": ["*"], "x_km": 0, "y_km": 12},
            {"id": "SNF_East", "accepted_insurances": ["Medicare", "Plan_A", "Plan_B"], "x_km": 12, "y_km": 0},
            {"id": "SNF_South", "accepted_insurances": ["Medicare", "Medicaid", "Plan_A"], "x_km": 0, "y_km": -12},
            {"id": "SNF_West", "accepted_insurances": ["*"], "x_km": -12, "y_km": 0}]
    beds = []
    for index, snf in enumerate(snfs):
        for k in range(7):
            capabilities = ["rehabilitation"] + (["oxygen"] if k < 2 else []) + (["bariatric"] if index == 0 and k == 0 else [])
            gender = "any" if k < 3 else ("female" if k % 2 else "male")
            beds.append({"id": f"{snf['id'][4]}{k + 1}", "snf_id": snf["id"], "gender": gender, "capabilities": capabilities})
    patients = []
    for i in range(patient_count):
        insurance = rnd.choice(["Medicare", "Medicare", "Medicaid", "Plan_A", "Plan_B"])
        roll = rnd.random()
        needs = ["rehabilitation"] + (["oxygen"] if roll < 0.2 else []) + (["bariatric"] if 0.2 <= roll < 0.25 else [])
        gender = rnd.choice(["female", "male"])
        eligible = [snf["id"] for snf in snfs
                    if ("*" in snf["accepted_insurances"] or insurance in snf["accepted_insurances"])
                    and any(b["snf_id"] == snf["id"] and b["gender"] in ("any", gender) and set(needs) <= set(b["capabilities"]) for b in beds)]
        rnd.shuffle(eligible)
        ready = round(rnd.uniform(0, 96))
        patient = {"id": f"P{i + 1:02d}", "hospital_id": rnd.choice(hospitals)["id"], "gender": gender,
                   "ready_hour": ready, "length_of_stay_hours": rnd.choice([48, 72, 96, 120, 144]),
                   "eligible_snf_ids": eligible[:rnd.choice([1, 2, 2, 3, 4])], "insurance_type": insurance,
                   "required_capabilities": needs,
                   "clinical_risk": rnd.choices(["low", "medium", "high"], [0.55, 0.3, 0.15])[0],
                   "age": rnd.randint(65, 95), "mobility_score": rnd.randint(8, 24),
                   "home_x_km": round(rnd.uniform(-15, 15), 1), "home_y_km": round(rnd.uniform(-15, 15), 1)}
        if rnd.random() < 0.4:
            patient["authorization_expiry_hour"] = ready + rnd.choice([12, 24, 48, 72])
        patients.append(patient)
    return {"schema_version": 1, "name": f"Urgency: regional cohort ({patient_count} patients)",
            "arrival_span_hours": 96, "tie_break_seed": 42, "optimizer_seed": 20261029, "baseline_policy": "local",
            "urgency": dict(DEFAULT_URGENCY), "hospitals": hospitals, "snfs": snfs, "beds": beds, "patients": patients}


def examples():
    return {"scenarios": [two_patients_one_bed(), regional_cohort()], "defaults": dict(DEFAULT_URGENCY)}
