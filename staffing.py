"""Optional V4 resource constraints. No clinical ratios are implied.

Daily repeating shifts provide direct-care hours per role per shift. Patient
hours/day are spread uniformly across occupied hours; resources cannot be banked
or substituted between roles. Intervals are half-open, including partial shifts.
"""
import math

ROLES = ("RN", "LPN", "CNA")


def quantities(value, label):
    if not isinstance(value, dict) or set(value) != set(ROLES):
        raise ValueError(f"{label} must specify RN, LPN and CNA")
    for role, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1000000:
            raise ValueError(f"{label}.{role} must be a finite nonnegative number")
    return dict(value)


def parse(value, snfs):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schedule_type") != "daily_repeating":
        raise ValueError("staffing.schedule_type must be daily_repeating")
    rows = value.get("shifts")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 120:
        raise ValueError("staffing.shifts must contain 1 to 120 shifts")
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or row.get("snf_id") not in snfs:
            raise ValueError("Staffing shift references an unknown SNF")
        left, right = row.get("start_hour"), row.get("end_hour")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (left, right)) or not 0 <= left < right <= 24:
            raise ValueError("Daily shift bounds must satisfy 0 <= start < end <= 24")
        normalized.append({"snf_id": row["snf_id"], "start_hour": left, "end_hour": right,
                           "direct_care_hours": quantities(row.get("direct_care_hours"), "direct_care_hours")})
    for snf in snfs:
        shifts = sorted((r for r in normalized if r["snf_id"] == snf), key=lambda r: r["start_hour"])
        if not shifts or shifts[0]["start_hour"] != 0 or shifts[-1]["end_hour"] != 24 or any(a["end_hour"] != b["start_hour"] for a, b in zip(shifts, shifts[1:])):
            raise ValueError(f"Staffing shifts for {snf} must cover 0–24 with no gaps or overlaps")
    return {"schedule_type": "daily_repeating", "data_source": str(value.get("data_source", "unspecified"))[:80], "shifts": normalized}


def supply(s, snf, hour):
    phase = hour % 24
    row = next(r for r in s.staffing["shifts"] if r["snf_id"] == snf and r["start_hour"] <= phase < r["end_hour"])
    return {role: row["direct_care_hours"][role] / (row["end_hour"] - row["start_hour"]) for role in ROLES}


def segments(s, snf, start, end, plan):
    boundaries = {start, end}
    for day in range(math.floor(start / 24), math.floor(end / 24) + 1):
        for row in s.staffing["shifts"]:
            if row["snf_id"] == snf:
                boundaries.update(t for t in (day * 24 + row["start_hour"], day * 24 + row["end_hour"]) if start < t < end)
    relevant = [(i, b) for i, b in plan.items() if s.beds[b.bed].snf_id == snf and b.start < end and start < b.end]
    for _, b in relevant:
        boundaries.update(t for t in (b.start, b.end) if start < t < end)
    points = sorted(boundaries)
    for left, right in zip(points, points[1:]):
        mid = (left + right) / 2
        used = {role: sum(dict(s.patients[i].care_hours_per_day)[role] / 24 for i, b in relevant if b.start <= mid < b.end) for role in ROLES}
        yield left, right, supply(s, snf, mid), used


def violation(s, patient, bed, start, plan):
    if not s.staffing:
        return None
    demand = dict(patient.care_hours_per_day)
    end = round(start + patient.length_of_stay_hours, 9)
    for left, right, available, used in segments(s, s.beds[bed].snf_id, start, end, plan):
        if any(used[r] + demand[r] / 24 > available[r] + 1e-9 for r in ROLES):
            return right
    return None


def earliest(s, patient, bed, intervals, plan, now=0):
    from dynamic_model import _gap
    start = max(now, patient.ready_hour)
    if not s.staffing:
        return _gap(patient, intervals, start)
    # After the last booking, every daily phase has been considered within 24h.
    limit = max([start, *(b.end for b in plan.values()), *(right for _, right in intervals)]) + 24
    while start <= limit + 1e-9:
        start = _gap(patient, intervals, start)
        conflict_end = violation(s, patient, bed, start, plan)
        if conflict_end is None:
            return start
        start = conflict_end
    return math.inf


def validate(s, plan):
    if s.staffing:
        for i, booking in plan.items():
            others = {j: b for j, b in plan.items() if j != i}
            if violation(s, s.patients[i], booking.bed, booking.start, others) is not None:
                raise ValueError("Placement plan exceeds role-specific staffing capacity")


def audit(s, plan):
    if not s.staffing or not plan:
        return []
    start = min(b.start for b in plan.values())
    end = max(b.end for b in plan.values())
    rows = []
    for snf in s.snfs:
        for left, right, available, used in segments(s, snf, start, end, plan):
            rows.append({"snf_id": snf, "start_hour": left, "end_hour": right,
                         "available_hours_per_hour": available, "used_hours_per_hour": used})
    return rows
