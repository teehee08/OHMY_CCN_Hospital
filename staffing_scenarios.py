"""Reproducible, explicitly synthetic staffing inputs for the V4 demo."""
from pathlib import Path
import json
from copy import deepcopy


CASE_SPECS = {
    "base": ("Baseline", 1, 6),
    "stress_36": ("Moderate pressure", 2, 3),
    "stress_72": ("High pressure", 4, 2),
    "stress_144": ("Extreme pressure", 8, 2),
}


def example():
    return json.loads((Path(__file__).parent / "scenarios" / "staffing_v4.json").read_text(encoding="utf-8"))


def case(case_id: str):
    """Repeat the same 18-person cohort while holding staff supply fixed."""
    if case_id not in CASE_SPECS:
        raise ValueError("Unknown staffing scenario")
    label, repeats, beds_per_snf = CASE_SPECS[case_id]
    case = example()
    original = deepcopy(case["patients"])
    for repeat in range(1, repeats):
        suffix = "stress" if repeat == 1 else f"copy{repeat + 1}"
        for patient in original:
            extra = deepcopy(patient)
            extra["id"] = f"{patient['id']}_{suffix}"
            case["patients"].append(extra)
    case["beds"] = [bed for bed in case["beds"] if int(bed["id"].rsplit("_", 1)[-1]) <= beds_per_snf]
    case["name"] = ("Synthetic staffing stress: 36 patients / 6 free beds" if case_id == "stress_36"
                    else f"Synthetic staffing {label.lower()}: {len(case['patients'])} patients / {len(case['beds'])} free beds")
    return case


def stress():
    """Preserve the original 36-patient stress endpoint for old clients."""
    return case("stress_36")


def catalog():
    template = example()
    roster = {role: sum(shift["staff_count"][role] for shift in template["staffing"]["shifts"])
              for role in ("RN", "LPN", "CNA")}
    rows = []
    for case_id, (label, repeats, beds_per_snf) in CASE_SPECS.items():
        patients = len(template["patients"]) * repeats
        beds = sum(int(bed["id"].rsplit("_", 1)[-1]) <= beds_per_snf for bed in template["beds"])
        rows.append({"id": case_id, "label": label, "patient_count": patients,
                     "free_bed_count": beds, "free_beds_per_snf": beds_per_snf,
                     "patients_per_free_bed": round(patients / beds, 1)})
    return {"cases": rows, "shared_roster": roster,
            "note": "All cases reuse the same ready times, care needs, and daily staff roster."}
