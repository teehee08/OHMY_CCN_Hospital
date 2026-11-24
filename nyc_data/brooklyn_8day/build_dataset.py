"""Build a model-ready Brooklyn scenario (staffing + urgency) from Brooklyn_Hospital_SNF_Data.xlsx.

Real (from the workbook): hospital occupancy, hospital-to-SNF daily flow, patient age / sex /
severity / payer mix by hospital, SNF location, corrected vacant beds, ventilator and dialysis
beds, SNF RN/LPN/CNA hours per resident-day (PBJ), case-mix nursing need, SNF length of stay.
Simulated where no public data exists: ready hour within the window, home location, referral
list, mobility score, authorization expiry, ventilator/dialysis need, shift timing.

Usage: python build_dataset.py --xlsx Brooklyn_Hospital_SNF_Data.xlsx --days 8 --scale 3 --seed 11 --out brooklyn_8d.json
"""
import argparse, json, math, random, re
import openpyxl

CENTER = (40.645, -73.950)
SHORT = {"001305": "Maimonides", "001304": "NYU_Langone_Brooklyn", "001306": "NYP_Brooklyn_Methodist",
         "001294": "South_Brooklyn_Health", "001286": "Brookdale", "001293": "Maimonides_Midwood",
         "001301": "Kings_County", "001324": "Mount_Sinai_Brooklyn", "001288": "Brooklyn_Hospital_Ctr",
         "001320": "SUNY_Downstate", "001309": "Interfaith", "001318": "Wyckoff_Heights", "001692": "Woodhull"}
SEV_FACTOR = {"Minor": 0.85, "Moderate": 0.95, "Major": 1.05, "Extreme": 1.25}
RISK = {"Minor": "low", "Moderate": "low", "Major": "medium", "Extreme": "high"}
AGE = {"0-17": (18, 18), "18-29": (18, 29), "30-49": (30, 49), "50-69": (50, 69), "70 or Older": (70, 98)}
ROLES = ("RN", "LPN", "CNA")


def xy(lat, lon):
    return (round((lon - CENTER[1]) * 111.320 * math.cos(math.radians(CENTER[0])), 2),
            round((lat - CENTER[0]) * 110.574, 2))


def pick(rnd, weights):
    items = [(k, v) for k, v in weights.items() if v > 0]
    r = rnd.random() * sum(v for _, v in items)
    for k, v in items:
        r -= v
        if r < 0:
            return k
    return items[-1][0]


def read_sheet(wb, name):
    rows = list(wb[name].iter_rows(values_only=True))
    head = [str(h) for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:] if r[0] not in (None, "TOTAL") and r[1] is not None]


def read_mix(wb):
    """Wide SNF_Patient_Mix blocks -> {attribute: {pfi: {value: count}}}."""
    mix, attr, cols = {}, None, None
    for r in wb["SNF_Patient_Mix"].iter_rows(values_only=True):
        if r[0] and " — " in str(r[0]):
            attr = str(r[0]).split(" — ")[1]; mix[attr] = {}; continue
        if r[0] == "Hospital":
            cols = [str(c).split("\n")[0] if c else None for c in r]; continue
        if attr and cols and r[1] and str(r[1]).isdigit():
            mix[attr][str(r[1])] = {cols[j]: r[j] for j in range(2, len(cols)) if cols[j] and cols[j] != "合计 Total" and isinstance(r[j], (int, float))}
    return mix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="Brooklyn_Hospital_SNF_Data.xlsx")
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--referrals", type=int, default=5)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--no-staffing", action="store_true")
    ap.add_argument("--out", default="brooklyn_8d.json")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    wb = openpyxl.load_workbook(a.xlsx, data_only=True)
    H = read_sheet(wb, "Hospitals")
    S = read_sheet(wb, "SNFs")
    mix = read_mix(wb)

    # ---- SNFs: vacant beds (1:scale), capabilities, residual staffing for the vacant beds ----
    total_census = sum(float(s["Avg daily census (PBJ 2026Q2)"]) for s in S)
    casemix = sum(float(s["Case-mix expected nurse hours per resident-day (care need)"]) * float(s["Avg daily census (PBJ 2026Q2)"]) for s in S) / total_census
    role_tot = {r: sum(float(s[k]) for s in S) for r, k in zip(ROLES, ("RN hours/day", "LPN hours/day", "CNA + aide hours/day"))}
    split = {r: role_tot[r] / sum(role_tot.values()) for r in ROLES}
    snfs, beds, shifts, info = [], [], [], {}
    for s in S:
        vac = int(s["Available beds (corrected) = MIN(reported, certified − min census)"] or 0)
        n = int(vac / a.scale + rnd.random())
        if n == 0:
            continue
        sid = str(s["CCN"])
        x, y = xy(float(s["Lat"]), float(s["Lon"]))
        snfs.append({"id": sid, "accepted_insurances": ["*"], "x_km": x, "y_km": y})
        vent = min(n, int((s["Ventilator beds available"] or 0) / a.scale + rnd.random()))
        dial = (s["Dialysis capacity (beds/dens)"] or 0) > 0
        for k in range(n):
            caps = ["rehabilitation"] + (["ventilator"] if k < vent else []) + (["dialysis"] if dial and k % 3 == 0 else [])
            beds.append({"id": f"{sid}_{k + 1}", "snf_id": sid, "gender": "any", "capabilities": caps})
        census = float(s["Avg daily census (PBJ 2026Q2)"])
        per_res = {"RN": float(s["RN hours/day"]) / census, "LPN": float(s["LPN hours/day"]) / census,
                   "CNA": float(s["CNA + aide hours/day"]) / census}
        supply = {r: round(per_res[r] * n, 3) for r in ROLES}
        if not a.no_staffing:
            shifts.append({"snf_id": sid, "start_hour": 0, "end_hour": 24, "direct_care_hours": supply})
        info[sid] = dict(name=s["SNF"], xy=(x, y), beds=n, caps=set(c for b in beds if b["snf_id"] == sid for c in b["capabilities"]),
                         supply=supply, los=float(s["Avg Medicare LOS (days)"] or 41))

    # ---- patients: SPARCS flows and mix; simulated timing, home, referrals, mobility, authorization ----
    span = a.days * 24
    hospitals, patients = [], []
    for h in H:
        pfi = str(h["PFI"])
        hid = SHORT.get(pfi, pfi)
        hospitals.append({"id": hid, "occupancy": round(float(h["Occupancy"]), 3)})
        hx, hy = xy(float(h["Lat"]), float(h["Lon"]))
        n = int(float(h["To SNF per day"]) * a.days / a.scale + rnd.random())
        for _ in range(n):
            sev = pick(rnd, mix["APR severity"][pfi])
            sex = pick(rnd, mix["Sex"][pfi]); sex = "female" if sex == "F" else "male"
            lo, hi = AGE[pick(rnd, mix["Age group"][pfi])]
            payer = pick(rnd, mix["Primary payer"][pfi])
            ins = payer if payer in ("Medicare", "Medicaid") else "Commercial"
            need = ["rehabilitation"]
            u = rnd.random()
            if u < 0.010: need.append("ventilator")
            elif u < 0.040: need.append("dialysis")
            care = {r: round(casemix * SEV_FACTOR[sev] * split[r], 3) for r in ROLES}
            home = (hx + rnd.gauss(0, 2.5), hy + rnd.gauss(0, 2.5))
            ok = [sid for sid, v in info.items() if set(need) <= v["caps"]
                  and (a.no_staffing or all(care[r] <= v["supply"][r] + 1e-9 for r in ROLES))]  # SNF screens out patients it can never staff
            ok.sort(key=lambda sid: math.dist(home, info[sid]["xy"]))
            k = max(1, min(len(ok), a.referrals + rnd.choice([-2, -1, 0, 0, 1, 2])))
            pool = ok[:k + 2]; rnd.shuffle(pool)
            chosen = sorted(pool[:k], key=lambda sid: math.dist(home, info[sid]["xy"]))
            age = rnd.randint(lo, hi)
            ready = round(rnd.uniform(0, span))
            los = max(7.0, min(100.0, rnd.lognormvariate(math.log(41.7) - 0.32, 0.8)))
            p = {"id": "", "hospital_id": hid, "gender": sex, "ready_hour": ready, "length_of_stay_hours": round(los * 24),
                 "eligible_snf_ids": chosen, "insurance_type": ins, "required_capabilities": need,
                 "clinical_risk": RISK[sev], "age": age,
                 "mobility_score": max(6, min(24, round(rnd.gauss(20 - 0.15 * (age - 65) - 3 * (SEV_FACTOR[sev] - 1) * 4, 3.5)))),
                 "home_x_km": round(home[0], 1), "home_y_km": round(home[1], 1)}
            if not a.no_staffing:
                p["care_hours_per_day"] = care
            if ins == "Commercial" or rnd.random() < 0.3:  # simulated: prior-authorization window
                p["authorization_expiry_hour"] = ready + rnd.choice([24, 48, 72])
            patients.append(p)
    patients.sort(key=lambda p: p["ready_hour"])
    for i, p in enumerate(patients):
        p["id"] = f"P{i + 1:03d}"
    scenario = {
        "schema_version": 1, "name": f"Brooklyn real data · {a.days} days · 1:{a.scale:g}" + ("" if a.no_staffing else " · staffing") + " · urgency",
        "arrival_span_hours": span, "tie_break_seed": 42, "optimizer_seed": 20261029, "baseline_policy": "local",
        "urgency": {"beta": {"clinical_risk": 1, "decline_risk": 0.5, "authorization": 0.5, "hard_to_place": 0.5},
                    "wait_growth_per_day": 0.1, "hospital_pressure_weight": 0.5, "pressure_start": 0.85, "pressure_full": 0.95,
                    "ed_boarding_full": 10, "authorization_window_hours": 72, "distance_weight_hours": 6, "max_distance_km": 30},
        "hospitals": hospitals, "snfs": snfs, "beds": beds, "patients": patients}
    if not a.no_staffing:
        scenario["staffing"] = {"schedule_type": "daily_repeating",
                                "data_source": "CMS PBJ 2026Q2 hours per resident-day x vacant beds",
                                "shifts": shifts}
    json.dump(scenario, open(a.out, "w"), indent=1)
    print(f"hospitals {len(hospitals)} snfs {len(snfs)} beds {len(beds)} patients {len(patients)} "
          f"casemix {casemix:.2f} split {', '.join(f'{r} {v:.2f}' for r, v in split.items())}")


if __name__ == "__main__":
    main()
