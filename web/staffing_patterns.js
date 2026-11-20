(function (root, factory) {
  const patterns = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = patterns;
  root.OHMYStaffingPatterns = patterns;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const roles = ["RN", "LPN", "CNA"];
  const baseHours = { RN: 5, LPN: 6, CNA: 6 };
  const names = ["full", "shortage", "night", "uneven"];

  function hours(pattern, shift, role, firstSnf) {
    const scale = pattern === "shortage" ? 0.6
      : pattern === "night" && role === "RN" && shift.start_hour === 0 ? 0.3
        : pattern === "uneven" && shift.snf_id === firstSnf ? 0.4 : 1;
    return Number((Math.min(baseHours[role], shift.end_hour - shift.start_hour) * scale).toFixed(6));
  }

  function apply(scenario, pattern) {
    if (!names.includes(pattern)) throw new Error("Choose a staffing pattern.");
    if (!scenario.staffing?.shifts?.length || !scenario.staffing.shifts.every(shift => shift.staff_count && shift.direct_care_hours_per_staff_per_day)) {
      throw new Error("This draft has legacy shift-hour staffing. Load a headcount-based scenario to use the staffing patterns.");
    }
    const firstSnf = scenario.snfs[0]?.id;
    for (const shift of scenario.staffing.shifts) {
      for (const role of roles) shift.direct_care_hours_per_staff_per_day[role] = hours(pattern, shift, role, firstSnf);
      shift.direct_care_hours = Object.fromEntries(roles.map(role => [role, shift.staff_count[role] * shift.direct_care_hours_per_staff_per_day[role]]));
    }
    return scenario;
  }

  function detect(scenario) {
    if (!scenario.staffing?.shifts?.length || !scenario.staffing.shifts.every(shift => shift.staff_count && shift.direct_care_hours_per_staff_per_day)) return "custom";
    const firstSnf = scenario.snfs[0]?.id;
    return names.find(pattern => scenario.staffing.shifts.every(shift => roles.every(role =>
      Math.abs(shift.direct_care_hours_per_staff_per_day[role] - hours(pattern, shift, role, firstSnf)) < 1e-7))) || "custom";
  }

  return { apply, detect };
});
