"""Optional V4 resource constraints. No clinical ratios are implied.

Daily repeating shifts specify staff count and per-person direct-care hours/day,
or legacy direct-care hours per role per shift. Patient hours/day are spread
uniformly across occupied hours; resources cannot be banked or substituted
between roles. Intervals are half-open, including partial shifts.
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
        entry = {"snf_id": row["snf_id"], "start_hour": left, "end_hour": right}
        if "staff_count" in row or "direct_care_hours_per_staff_per_day" in row:
            counts = quantities(row.get("staff_count"), "staff_count")
            if any(type(count) is not int for count in counts.values()):
                raise ValueError("staff_count values must be nonnegative integers")
            hours = quantities(row.get("direct_care_hours_per_staff_per_day"),
                               "direct_care_hours_per_staff_per_day")
            if any(hours[role] > right - left for role in ROLES):
                raise ValueError("Per-staff daily direct-care hours cannot exceed the shift duration")
            entry.update(staff_count=counts, direct_care_hours_per_staff_per_day=hours,
                         direct_care_hours={role: counts[role] * hours[role] for role in ROLES})
        else:
            entry["direct_care_hours"] = quantities(row.get("direct_care_hours"), "direct_care_hours")
        normalized.append(entry)
    for snf in snfs:
        shifts = sorted((r for r in normalized if r["snf_id"] == snf), key=lambda r: r["start_hour"])
        if not shifts or shifts[0]["start_hour"] != 0 or shifts[-1]["end_hour"] != 24 or any(a["end_hour"] != b["start_hour"] for a, b in zip(shifts, shifts[1:])):
            raise ValueError(f"Staffing shifts for {snf} must cover 0–24 with no gaps or overlaps")
    return {"schedule_type": "daily_repeating", "data_source": str(value.get("data_source", "unspecified"))[:80], "shifts": normalized}


_CACHE = {}


def _prepared(s):
    """Per-scenario lookups: shift rows by SNF and patient demand per clock hour."""
    key = id(s)
    cached = _CACHE.get(key)
    if cached is None or cached[0] is not s:
        rows = {}
        for row in s.staffing["shifts"]:
            rows.setdefault(row["snf_id"], []).append(row)
        demand = [tuple(dict(p.care_hours_per_day)[role] / 24 for role in ROLES) for p in s.patients]
        # An SNF can never run short when every one of its beds, filled by the most
        # demanding patient, still fits within its lowest hourly supply: skip its checks.
        beds = {}
        for bed in s.beds:
            beds[bed.snf_id] = beds.get(bed.snf_id, 0) + 1
        peak = [max((d[k] for d in demand), default=0.0) for k in range(len(ROLES))]
        slack = set()
        for snf, snf_rows in rows.items():
            lowest = [min(r["direct_care_hours"][role] / (r["end_hour"] - r["start_hour"]) for r in snf_rows)
                      for role in ROLES]
            if all(beds.get(snf, 0) * peak[k] <= lowest[k] + 1e-9 for k in range(len(ROLES))):
                slack.add(snf)
        cached = (s, rows, demand, slack)
        _CACHE.clear()
        _CACHE[key] = cached
    return cached[1], cached[2]


def never_short(s, snf):
    _prepared(s)
    return snf in _CACHE[id(s)][3]


def supply(s, snf, hour):
    rows, _ = _prepared(s)
    phase = hour % 24
    row = next(r for r in rows.get(snf, ()) if r["start_hour"] <= phase < r["end_hour"])
    return {role: row["direct_care_hours"][role] / (row["end_hour"] - row["start_hour"]) for role in ROLES}


def segments(s, snf, start, end, plan):
    """Yield (left, right, available, used) for each constant piece of [start, end).

    Same pieces and values as a direct recomputation, but bookings are swept once
    instead of being re-summed for every piece.
    """
    rows, demand = _prepared(s)
    snf_rows = rows.get(snf, ())
    boundaries = {start, end}
    if len(snf_rows) > 1:  # a single 0–24 shift has the same supply every hour: no daily cut points needed
        for day in range(math.floor(start / 24), math.floor(end / 24) + 1):
            for row in snf_rows:
                for t in (day * 24 + row["start_hour"], day * 24 + row["end_hour"]):
                    if start < t < end:
                        boundaries.add(t)
    relevant = [(i, b) for i, b in plan.items() if s.beds[b.bed].snf_id == snf and b.start < end and start < b.end]
    for _, b in relevant:
        if start < b.start < end:
            boundaries.add(b.start)
        if start < b.end < end:
            boundaries.add(b.end)
    points = sorted(boundaries)
    for left, right in zip(points, points[1:]):
        mid = (left + right) / 2
        active = [demand[i] for i, b in relevant if b.start <= mid < b.end]
        used = {role: sum(d[k] for d in active) for k, role in enumerate(ROLES)}
        phase = mid % 24
        row = next(r for r in snf_rows if r["start_hour"] <= phase < r["end_hour"])
        width = row["end_hour"] - row["start_hour"]
        yield left, right, {role: row["direct_care_hours"][role] / width for role in ROLES}, used


def violation(s, patient, bed, start, plan):
    if not s.staffing or never_short(s, s.beds[bed].snf_id):
        return None
    demand = dict(patient.care_hours_per_day)
    need = {r: demand[r] / 24 for r in ROLES}
    end = round(start + patient.length_of_stay_hours, 9)
    for left, right, available, used in segments(s, s.beds[bed].snf_id, start, end, plan):
        if any(used[r] + need[r] > available[r] + 1e-9 for r in ROLES):
            return right
    return None


def earliest(s, patient, bed, intervals, plan, now=0):
    from dynamic_model import _gap
    start = max(now, patient.ready_hour)
    if not s.staffing or never_short(s, s.beds[bed].snf_id):
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
