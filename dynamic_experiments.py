"""Controlled synthetic experiments for capacity and cross-hospital coordination.

These are reproducible stress tests, not estimates calibrated to clinical data.
Capacity variants copy bed types without regenerating any patient. The competition
pair changes only hospital assignment, leaving physical demand and beds unchanged.
"""

from __future__ import annotations

from copy import deepcopy
import random
from typing import Any

from dynamic_model import Scenario


DEFAULT_SEEDS = (42, 84, 126)
HORIZON_HOURS = 168
PATIENT_COUNT = 96
HOSPITAL_IDS = ("Hospital_A", "Hospital_B", "Hospital_C", "Hospital_D")
SNF_IDS = tuple(f"SNF{i}" for i in range(1, 7))
LOAD_MULTIPLIERS = {"load_light": 4, "load_medium": 2, "load_heavy": 1}


def _seed(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**32 - 1:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    return value


def _network() -> tuple[list[dict], list[dict]]:
    hospitals = [{"id": identifier} for identifier in HOSPITAL_IDS]
    plans = (("Plan_A", "Plan_B"), ("Plan_B", "Plan_C"), ("Plan_A", "Plan_C"))
    snfs = [{"id": identifier, "accepted_insurances": list(plans[i % 3])}
            for i, identifier in enumerate(SNF_IDS)]
    return hospitals, snfs


def _load_cohort(seed: int) -> tuple[dict, list[dict]]:
    """Build once per seed; all three capacity levels use these same patients."""
    rng = random.Random(seed)
    hospitals, snfs = _network()
    ordinary_genders = ("any", "male", "female", "any", "male", "female")
    specialty_genders = ("female", "any", "male", "any", "female", "any")
    specialties = ("oxygen", "bariatric", "wound_care", "oxygen", "bariatric", "wound_care")
    prototypes = []
    for i, identifier in enumerate(SNF_IDS):
        prototypes.extend([
            {"id": f"L{i + 1}_STANDARD", "snf_id": identifier,
             "gender": ordinary_genders[i], "capabilities": ["rehabilitation"]},
            {"id": f"L{i + 1}_SPECIAL", "snf_id": identifier,
             "gender": specialty_genders[i], "capabilities": ["rehabilitation", specialties[i]]},
        ])

    # 19 of each duration plus one 12-hour stay: 3,660 offered bed-hours.
    # Keeping this multiset fixed makes the aggregate load identical across seeds.
    stays = [12, 24, 36, 48, 72] * 19 + [12]
    anchors = list(range(len(prototypes))) * 8
    ready = [(i % 7) * 24 + (8, 10, 13, 16)[(i // 7) % 4] + rng.randrange(2)
             for i in range(PATIENT_COUNT)]
    hospital_assignments = list(HOSPITAL_IDS) * 24
    for values in (stays, anchors, ready, hospital_assignments):
        rng.shuffle(values)

    accepted = {snf["id"]: snf["accepted_insurances"] for snf in snfs}
    patients = []
    for i, (anchor_index, stay, ready_hour, hospital_id) in enumerate(
        zip(anchors, stays, ready, hospital_assignments), 1
    ):
        anchor = prototypes[anchor_index]
        eligible = rng.sample(list(SNF_IDS), rng.randint(2, 4))
        if anchor["snf_id"] not in eligible:
            eligible[-1] = anchor["snf_id"]
        rng.shuffle(eligible)
        required = ["rehabilitation"]
        if len(anchor["capabilities"]) > 1 and rng.random() < 0.75:
            required.append(anchor["capabilities"][1])
        gender = anchor["gender"] if anchor["gender"] != "any" else rng.choice(("female", "male"))
        patients.append({
            "id": f"P{i:03}", "hospital_id": hospital_id, "gender": gender,
            "ready_hour": ready_hour, "length_of_stay_hours": stay,
            "eligible_snf_ids": eligible,
            "insurance_type": rng.choice(accepted[anchor["snf_id"]]),
            "required_capabilities": required,
        })
    base = {"schema_version": 1, "horizon_hours": HORIZON_HOURS,
            "hospitals": hospitals, "snfs": snfs, "patients": patients,
            "tie_break_seed": seed, "baseline_policy": "local"}
    return base, prototypes


def _copies(prototypes: list[dict], multiplier: int) -> list[dict]:
    return [{**deepcopy(prototype), "id": f"{prototype['id']}_{copy_index:02}"}
            for prototype in prototypes for copy_index in range(1, multiplier + 1)]


def _design(scenario: dict, *, seed: int, family: str, control: str,
            variant: str, held_constant: list[str], comparison_note: str) -> dict:
    offered = sum(patient["length_of_stay_hours"] for patient in scenario["patients"])
    capacity = len(scenario["beds"]) * scenario["horizon_hours"]
    return {
        "family": family, "seed": seed, "control": control, "variant": variant,
        "role": "control" if variant == control else "variant",
        "patient_count": len(scenario["patients"]), "bed_count": len(scenario["beds"]),
        "hospital_count": len(scenario["hospitals"]),
        "active_hospital_count": len({patient["hospital_id"] for patient in scenario["patients"]}),
        "snf_count": len(scenario["snfs"]), "arrival_span_hours": scenario["arrival_span_hours"],
        "horizon_hours": scenario["arrival_span_hours"],
        "total_offered_snf_hours": offered, "nominal_capacity_bed_hours": capacity,
        "offered_load": offered / capacity,
        "offered_load_definition": "sum(patient SNF LOS) / (physical bed count * configured arrival-span hours); not the observation duration",
        "held_constant": held_constant, "comparison_note": comparison_note,
        "empirically_calibrated": False,
        "parameter_basis": "Synthetic stress-test durations, arrival waves, and compatibility labels; not fitted to observed hospital or SNF data.",
    }


def _load_experiments(seed: int) -> list[dict]:
    base, prototypes = _load_cohort(seed)
    titles = {"load_light": "Controlled load · light capacity pressure",
              "load_medium": "Controlled load · medium capacity pressure",
              "load_heavy": "Controlled load · heavy capacity pressure"}
    result = []
    for identifier, multiplier in LOAD_MULTIPLIERS.items():
        scenario = deepcopy(base)
        scenario.update(name=titles[identifier], beds=_copies(prototypes, multiplier))
        scenario = Scenario.from_dict(scenario).to_dict()
        design = _design(
            scenario, seed=seed, family="load", control="load_medium", variant=identifier,
            held_constant=["All 96 patient records including IDs, hospital, readiness, LOS, gender, insurance, care and SNF preferences",
                           "Hospital/SNF IDs, insurance contracts, arrival span, baseline policy and tie-break seed",
                           "Relative mix of clinical bed types"],
            comparison_note="Compare capacity variants within the same seed. Only the number of identical copies of every bed type changes. Offered load uses the configured ready-generation span, not the complete simulation duration or actual utilization. Observation continues until both schedules finish.",
        )
        design.update(base_bed_type_count=len(prototypes), capacity_multiplier=multiplier,
                      paired_experiment_ids=list(LOAD_MULTIPLIERS))
        result.append({"id": identifier, "title": titles[identifier],
                       "description": f"The same 96 patients and 12 clinical bed types, with {multiplier} identical copies per type ({len(scenario['beds'])} beds).",
                       "changed_factor": "Physical bed capacity only; patient cohort and bed-type proportions are fixed.",
                       "cohort_id": f"controlled_load_96_seed_{seed}",
                       "scenario": scenario, "design": design})
    return result


def _competition_cohort(seed: int) -> tuple[dict, dict[str, list[str]], dict[str, str]]:
    rng = random.Random(seed)
    snfs = [
        {"id": "SNF1", "accepted_insurances": ["Plan_A"]},
        {"id": "SNF2", "accepted_insurances": ["Plan_A"]},
        {"id": "SNF3", "accepted_insurances": ["Plan_B"]},
        {"id": "SNF4", "accepted_insurances": ["Plan_B"]},
        {"id": "SNF5", "accepted_insurances": ["Plan_C"]},
        {"id": "SNF6", "accepted_insurances": ["Plan_C"]},
    ]
    beds = [
        {"id": "SCARCE_BARIATRIC", "snf_id": "SNF1", "gender": "any", "capabilities": ["rehabilitation", "bariatric"]},
        {"id": "STANDARD_ALTERNATIVE", "snf_id": "SNF2", "gender": "any", "capabilities": ["rehabilitation"]},
        {"id": "OTHER_PLAN_BARIATRIC", "snf_id": "SNF3", "gender": "any", "capabilities": ["rehabilitation", "bariatric"]},
        {"id": "OXYGEN", "snf_id": "SNF4", "gender": "any", "capabilities": ["rehabilitation", "oxygen"]},
        {"id": "FEMALE_WOUND", "snf_id": "SNF5", "gender": "female", "capabilities": ["rehabilitation", "wound_care"]},
        {"id": "MALE_STANDARD", "snf_id": "SNF6", "gender": "male", "capabilities": ["rehabilitation"]},
    ]
    roles: dict[str, list[str]] = {"flexible": [], "scarce_dependent": [], "background": []}
    cross_assignment = {}
    patients = []
    background_types = (
        ("SNF3", "Plan_B", "any", "bariatric", ["SNF1", "SNF3"]),
        ("SNF4", "Plan_B", "any", "oxygen", ["SNF3", "SNF4"]),
        ("SNF5", "Plan_C", "female", "wound_care", ["SNF5", "SNF6"]),
        ("SNF6", "Plan_C", "male", None, ["SNF6"]),
    )
    for wave in range(8):
        ready = 8 + 18 * wave + rng.choice((-1, 0, 1))
        for role, prefix, extra, offset, hospital in (
            ("flexible", "Flex", None, 0, HOSPITAL_IDS[0 if wave % 2 == 0 else 2]),
            ("scarce_dependent", "Rare", "bariatric", 1, HOSPITAL_IDS[1 if wave % 2 == 0 else 3]),
        ):
            identifier = f"{prefix}_{wave + 1:02}"
            roles[role].append(identifier)
            cross_assignment[identifier] = hospital
            patients.append({"id": identifier, "hospital_id": HOSPITAL_IDS[0],
                             "gender": rng.choice(("female", "male")),
                             "ready_hour": ready + offset, "length_of_stay_hours": 12,
                             "eligible_snf_ids": ["SNF1", "SNF2", "SNF3"],
                             "insurance_type": "Plan_A",
                             "required_capabilities": ["rehabilitation"] + ([extra] if extra else [])})
        _, insurance, gender, extra, eligible = background_types[wave % 4]
        identifier = f"Background_{wave + 1:02}"
        roles["background"].append(identifier)
        cross_assignment[identifier] = HOSPITAL_IDS[wave % 4]
        patients.append({"id": identifier, "hospital_id": HOSPITAL_IDS[0],
                         "gender": rng.choice(("female", "male")) if gender == "any" else gender,
                         "ready_hour": ready + 2, "length_of_stay_hours": 12,
                         "eligible_snf_ids": list(eligible), "insurance_type": insurance,
                         "required_capabilities": ["rehabilitation"] + ([extra] if extra else [])})
    return ({"schema_version": 1, "horizon_hours": HORIZON_HOURS,
             "hospitals": [{"id": identifier} for identifier in HOSPITAL_IDS],
             "snfs": snfs, "beds": beds, "patients": patients,
             "tie_break_seed": seed, "baseline_policy": "local"}, roles, cross_assignment)


def _competition_experiments(seed: int) -> list[dict]:
    base, roles, cross_assignment = _competition_cohort(seed)
    result = []
    for identifier, title in (
        ("competition_within", "Scarce-bed competition · one hospital informed"),
        ("competition_cross", "Scarce-bed competition · demand split across hospitals"),
    ):
        scenario = deepcopy(base)
        scenario["name"] = title
        if identifier == "competition_cross":
            for patient in scenario["patients"]:
                patient["hospital_id"] = cross_assignment[patient["id"]]
        scenario = Scenario.from_dict(scenario).to_dict()
        design = _design(
            scenario, seed=seed, family="competition", control="competition_within", variant=identifier,
            held_constant=["Patient IDs, readiness, SNF LOS, gender, insurance, required care and SNF preferences",
                           "The complete physical bed pool and SNF insurance contracts",
                           "Four declared hospital IDs, arrival-span metadata, local baseline policy and tie-break seed"],
            comparison_note="Only patient hospital assignment changes: all demand belongs to Hospital_A in the control and is evenly split across four hospitals in the variant. The baseline keeps its hospital-local future knowledge and planning in both cases. Outcomes are measured, not guaranteed to favor CCN.",
        )
        design.update(paired_experiment_ids=["competition_within", "competition_cross"],
                      roles=deepcopy(roles), scarce_bed_id="SCARCE_BARIATRIC",
                      flexible_alternative_bed_id="STANDARD_ALTERNATIVE",
                      mechanism="Eight waves each contain a flexible patient followed one hour later by a bariatric patient. Flexible patients can use the scarce or standard bed; bariatric patients cannot use the standard bed, and their insurance excludes the other bariatric SNF. Within one hospital both needs are known together; across hospitals a flexible patient's use of the scarce bed can delay another hospital's patient.")
        result.append({"id": identifier, "title": title,
                       "description": "24 patients: eight flexible/scarce-dependent pairs and eight background patients. Insurance and care requirements independently exclude physical alternatives.",
                       "changed_factor": "Patient hospital assignment only; physical demand, readiness and beds are identical.",
                       "cohort_id": f"scarce_bed_24_seed_{seed}", "scenario": scenario, "design": design})
    return result


def catalog(seed: int = 42) -> list[dict]:
    """Return five controlled experiments for a prespecified seed.

    Use all DEFAULT_SEEDS (42, 84, 126) for the standard batch rather than selecting
    a seed after inspecting its measured CCN gain. Each call returns fresh objects.
    """
    seed = _seed(seed)
    return _load_experiments(seed) + _competition_experiments(seed)


def build_experiments(seed: int = 42) -> list[dict]:
    """Alias for callers that prefer an explicit builder name."""
    return catalog(seed)


PREFERENCE_VARIANTS = ("supplied", "ordinary_first", "shuffled")


def preference_variant(experiment: dict, mode: str = "supplied", seed: int = 0) -> dict:
    """Reorder preferences without changing any patient's compatible bed set.

    This is a declared sensitivity factor, not a way to regenerate demand or
    select a favorable baseline outcome. Ordinary-first uses only physical
    capabilities and contracts; it never counts other hospitals' patients.
    """
    if mode not in PREFERENCE_VARIANTS:
        raise ValueError(f"Unknown preference variant: {mode}")
    seed = _seed(seed)
    result = deepcopy(experiment)
    result["base_experiment_id"] = experiment["id"]
    result["preference_variant"] = mode
    result["preference_seed"] = seed if mode == "shuffled" else None
    scenario = result["scenario"]
    contracts = {snf["id"]: snf["accepted_insurances"] for snf in scenario["snfs"]}
    rng = random.Random(seed)
    changed = 0
    for patient in scenario["patients"]:
        before = patient["eligible_snf_ids"][:]
        if mode == "shuffled":
            rng.shuffle(patient["eligible_snf_ids"])
        elif mode == "ordinary_first":
            required = set(patient.get("required_capabilities", []))

            def capacity_cost(snf_id):
                accepted = contracts[snf_id]
                if "*" not in accepted and patient.get("insurance_type", "unspecified") not in accepted:
                    return (float("inf"), float("inf"))
                return min(((len(set(bed.get("capabilities", [])) - required), int(bed["gender"] == "any"))
                            for bed in scenario["beds"] if bed["snf_id"] == snf_id
                            and bed["gender"] in (patient["gender"], "any")
                            and required.issubset(bed.get("capabilities", []))),
                           default=(float("inf"), float("inf")))

            patient["eligible_snf_ids"].sort(key=capacity_cost)
        changed += before != patient["eligible_snf_ids"]
    design = result["design"]
    design.update(preference_variant=mode, preference_seed=result["preference_seed"],
                  preference_changed_patient_count=changed,
                  compatible_bed_sets_preserved=True)
    if mode != "supplied":
        result["id"] = f"{experiment['id']}__{mode}"
        result["title"] += f" · {mode.replace('_', ' ')} preferences"
        result["changed_factor"] += "; declared sensitivity: SNF preference order only"
        design["held_constant"] = [text.replace(" and SNF preferences", "") for text in design["held_constant"]]
        design["comparison_note"] += (
            " This preference sensitivity reorders SNF lists only; patients' compatible bed sets,"
            " ready times, stays, insurance, care, hospitals and physical capacity are unchanged."
        )
    return result
