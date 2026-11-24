"""Daily parameters from the offline (full-information CCN) plan, for a later bed-reservation optimisation."""
import json, math
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

scen = json.load(open("brooklyn_8d_staffing_urgency.json"))          # staffing version: care hours and staff supply
res = json.load(open("offline_run_result.json"))  # output of dynamic_model.compare on brooklyn_8d_urgency.json
DAYS = 16                                  # 8-day arrival window + 8 days follow-up
ROLES = ("RN", "LPN", "CNA")
beds_by_snf = defaultdict(int)
for b in scen["beds"]:
    beds_by_snf[b["snf_id"]] += 1
supply = {r["snf_id"]: r["direct_care_hours"] for r in scen["staffing"]["shifts"]}
care = {p["id"]: p["care_hours_per_day"] for p in scen["patients"]}
names = {}
import openpyxl
wbsrc = openpyxl.load_workbook("Brooklyn_Hospital_SNF_Data.xlsx", data_only=True)
for row in wbsrc["SNFs"].iter_rows(min_row=2, values_only=True):
    if row[0] and row[0] != "TOTAL":
        names[str(row[0])] = row[1]


def present(p, t):
    return p["admission_hour"] is not None and p["admission_hour"] <= t < p["release_hour"]


def daily(plan):
    snf_rows, sys_rows = [], []
    for d in range(DAYS):
        t_end = 24 * (d + 1) - 1e-6
        tot_free = tot_beds = 0
        for sid in sorted(beds_by_snf):
            pts = [p for p in plan if p["snf_id"] == sid]
            occ = [p for p in pts if present(p, t_end)]
            adm = sum(1 for p in pts if 24 * d <= p["admission_hour"] < 24 * (d + 1))
            dis = sum(1 for p in pts if 24 * d <= p["release_hour"] < 24 * (d + 1))
            peak = max(sum(1 for p in pts if present(p, 24 * d + h)) for h in range(24))
            used = {r: sum(care[p["patient_id"]][r] for p in occ) for r in ROLES}
            util = {r: used[r] / supply[sid][r] if supply[sid][r] else 0 for r in ROLES}
            free = beds_by_snf[sid] - len(occ)
            tot_free += free; tot_beds += beds_by_snf[sid]
            snf_rows.append([d, sid, names.get(sid, sid), beds_by_snf[sid], adm, dis, len(occ), peak, free,
                             round(used["RN"], 2), round(used["LPN"], 2), round(used["CNA"], 2),
                             round(util["RN"], 3), round(util["LPN"], 3), round(util["CNA"], 3),
                             int(any(u > 1 + 1e-9 for u in util.values()))])
        ready = [p for p in plan if 24 * d <= p["ready_hour"] < 24 * (d + 1)]
        waiting = [p for p in plan if p["ready_hour"] <= t_end and not (p["admission_hour"] is not None and p["admission_hour"] <= t_end)]
        admitted = sum(1 for p in plan if 24 * d <= p["admission_hour"] < 24 * (d + 1))
        over = sum(1 for r in snf_rows if r[0] == d and r[15])
        sys_rows.append([d, len(ready), admitted, len(waiting), tot_beds, tot_free, over])
    return snf_rows, sys_rows


off_snf, off_sys = daily(res["ccn"]["patients"])
base_snf, base_sys = daily(res["baseline"]["patients"])

# per-SNF parameters over the 8-day arrival window (days 0-7)
params = []
for sid in sorted(beds_by_snf):
    rows = [r for r in off_snf if r[1] == sid and r[0] < 8]
    allrows = [r for r in off_snf if r[1] == sid]
    params.append([sid, names.get(sid, sid), beds_by_snf[sid], sum(r[4] for r in rows), max(r[6] for r in rows),
                   min(r[8] for r in rows), round(sum(r[8] for r in rows) / len(rows), 2), min(r[8] for r in allrows),
                   round(max(r[12] for r in allrows), 3), round(max(r[13] for r in allrows), 3), round(max(r[14] for r in allrows), 3),
                   sum(r[15] for r in allrows),
                   supply[sid]["RN"], supply[sid]["LPN"], supply[sid]["CNA"]])

hosp_rows = []
for d in range(DAYS):
    t_end = 24 * (d + 1) - 1e-6
    for h in sorted({p["hospital_id"] for p in res["ccn"]["patients"]}):
        for label, plan in (("Offline CCN", res["ccn"]["patients"]), ("Baseline", res["baseline"]["patients"])):
            hp = [p for p in plan if p["hospital_id"] == h]
            hosp_rows.append([d, h, label, sum(1 for p in hp if 24 * d <= p["ready_hour"] < 24 * (d + 1)),
                              sum(1 for p in hp if 24 * d <= p["admission_hour"] < 24 * (d + 1)),
                              sum(1 for p in hp if p["ready_hour"] <= t_end and p["admission_hour"] > t_end)])

# ---------------- workbook ----------------
F = Font(name="Arial", size=10); B = Font(name="Arial", size=10, bold=True); H = PatternFill("solid", start_color="DDEBF7")
wb = Workbook()


def sheet(title, headers, rows, widths, fmt=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font = B; c.fill = H; c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = F
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for col, f in (fmt or {}).items():
        for c in ws[col][1:]:
            c.number_format = f
    ws.freeze_panes = "C2"; ws.row_dimensions[1].height = 44
    return ws


ws = wb.active; ws.title = "Summary"
b, c = res["baseline"]["summary"], res["ccn"]["summary"]
lines = [
    ["Brooklyn 8-day offline run (full-information CCN vs hospital-local baseline)"], [],
    ["Scenario", scen["name"]],
    ["Hospitals / SNFs / beds / patients", f'{len(scen["hospitals"])} / {len(scen["snfs"])} / {len(scen["beds"])} / {len(scen["patients"])}'],
    ["Arrival window", "8 days (hours 0–192); daily tables cover days 0–15"], [],
    ["Measure", "Baseline", "Offline CCN"],
    ["Total waiting (h)", b["full_plan_wait_hours"], c["full_plan_wait_hours"]],
    ["Mean waiting per patient (h)", round(b["mean_full_plan_wait_hours"], 2), round(c["mean_full_plan_wait_hours"], 2)],
    ["Rejected SNF requests", b["rejected_request_count"], c["rejected_request_count"]],
    ["Urgency-weighted cost (objective)", round(b["urgency_weighted_cost"], 1), round(c["urgency_weighted_cost"], 1)],
    ["High-urgency mean wait (h)", round(b["high_urgency_mean_wait_hours"], 2), round(c["high_urgency_mean_wait_hours"], 2)],
    ["Mean home-to-SNF distance (km)", round(b["mean_distance_km"], 2), round(c["mean_distance_km"], 2)],
    ["Last admission (hour)", b["last_planned_admission_hour"], c["last_planned_admission_hour"]],
    ["SNF-days where nursing demand > supply (post-hoc check)", sum(r[15] for r in base_snf), sum(r[15] for r in off_snf)], [],
    ["Notes"],
    ["1. Offline = CCN with full knowledge of all 8 days of patients (dynamic_model.compare, ccn plan). Beds, eligibility and urgency are enforced in the run."],
    ["2. Nursing staffing is NOT enforced in the run (the repo algorithm does not finish with staffing on this size); demand vs supply is checked afterwards per SNF-day."],
    ["3. 'Free beds' = modelled vacant beds not occupied by this cohort at the end of the day; this is the candidate reserve level for the later optimisation model."],
    ["4. Real data: hospital occupancy, hospital→SNF flows and patient mix (SPARCS 2024), SNF vacant beds, ventilator/dialysis beds, PBJ staffing, case-mix care need, SNF LOS. Simulated: ready times, home location, referral lists, mobility, authorization expiry, ventilator/dialysis need."],
]
for l in lines:
    ws.append(l)
for row in ws.iter_rows():
    for cell in row:
        cell.font = F
ws["A1"].font = Font(name="Arial", size=12, bold=True)
for cell in ws[7]:
    cell.font = B; cell.fill = H
ws["A17"].font = B
ws.column_dimensions["A"].width = 52; ws.column_dimensions["B"].width = 40; ws.column_dimensions["C"].width = 14

sheet("SNF_Parameters", ["CCN", "SNF", "Modelled vacant beds", "Admissions days 0–7", "Max occupied days 0–7",
                         "Min free beds days 0–7 (safe reserve)", "Mean free beds days 0–7", "Min free beds days 0–15",
                         "Peak RN utilisation", "Peak LPN utilisation", "Peak CNA utilisation", "SNF-days staffing over 100%",
                         "RN hours/day available", "LPN hours/day available", "CNA hours/day available"], params,
      [9, 40, 10, 11, 11, 14, 11, 12, 10, 10, 10, 11, 10, 10, 10], {"I": "0%", "J": "0%", "K": "0%"})
sheet("SNF_Daily_Offline", ["Day", "CCN", "SNF", "Modelled beds", "Admissions", "Discharges", "Occupied (end of day)", "Peak occupied",
                            "Free beds (end of day)", "RN hours used", "LPN hours used", "CNA hours used",
                            "RN utilisation", "LPN utilisation", "CNA utilisation", "Staffing over 100% (1=yes)"], off_snf,
      [6, 9, 40, 9, 10, 10, 11, 10, 11, 10, 10, 10, 10, 10, 10, 11], {"M": "0%", "N": "0%", "O": "0%"})
sheet("System_Daily", ["Day", "Patients ready", "Admitted (offline)", "Waiting end of day (offline)", "Modelled beds",
                       "Free beds end of day (offline)", "SNFs over staffing (offline)", "Admitted (baseline)",
                       "Waiting end of day (baseline)", "Free beds end of day (baseline)", "SNFs over staffing (baseline)"],
      [o[:7] + [bs[2], bs[3], bs[5], bs[6]] for o, bs in zip(off_sys, base_sys)], [6, 10, 11, 12, 10, 12, 12, 11, 12, 12, 12])
sheet("Hospital_Daily", ["Day", "Hospital", "Policy", "Patients ready", "Admitted to SNF", "Waiting end of day"], hosp_rows, [6, 24, 12, 10, 10, 10])
plan_rows = [[p["patient_id"], p["hospital_id"], p["ready_hour"], p["admission_hour"], p["wait_hours"], p["snf_id"],
              names.get(p["snf_id"], p["snf_id"]), p["bed_id"], p["release_hour"], round(p.get("urgency_weight") or 0, 3),
              round(p.get("distance_km") or 0, 2)] for p in res["ccn"]["patients"]]
sheet("Offline_Plan", ["Patient", "Hospital", "Ready hour", "Admission hour", "Wait (h)", "CCN", "SNF", "Bed", "Release hour",
                       "Urgency weight u_i", "Distance (km)"], plan_rows, [8, 22, 9, 10, 8, 9, 38, 11, 10, 10, 10])
wb.save("Brooklyn_8day_offline_parameters.xlsx")
print("offline SNF-days over staffing", sum(r[15] for r in off_snf), "baseline", sum(r[15] for r in base_snf))
print("system daily offline", off_sys)
