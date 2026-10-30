/* Pure complete-cohort reporting semantics shared by browser and Node checks. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.OHMYDynamicMetrics = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const nonnegative = value => finite(value) && value >= 0 ? value : null;
  function fullCohortWait(result) {
    const summary = result?.summary || {}, total = nonnegative(summary.full_plan_wait_hours);
    if (total === null || summary.structurally_unplaceable_patient_ids?.length || result.solver?.full_cohort_objective_defined === false) return null;
    if (result.patients?.some(patient => patient.planned_admission_hour === null || patient.wait_hours === null)) return null;
    return total;
  }
  function observationBounds(data) {
    const source = data.observation || {}, patients = data.scenario?.patients || [], ready = patients.map(p => p.ready_hour).filter(finite);
    const start = finite(source.start_hour) ? source.start_hour : ready.length ? Math.min(...ready) : 0;
    const rows = [data.baseline, data.ccn].flatMap(result => result?.patients || []);
    const releases = rows.map(p => p.planned_release_hour).filter(finite);
    const known = [...ready, ...rows.flatMap(p => [p.planned_admission_hour, p.planned_release_hour]).filter(finite), start];
    const unresolved = Array.isArray(source.unresolved_patient_ids) ? [...source.unresolved_patient_ids] : [...new Set(rows.filter(p => p.planned_admission_hour === null).map(p => p.patient_id))];
    const complete = typeof source.complete === "boolean" ? source.complete : unresolved.length === 0 && fullCohortWait(data.baseline) !== null && fullCohortWait(data.ccn) !== null;
    const end = complete ? finite(source.end_hour) ? source.end_hour : releases.length ? Math.max(start, ...releases) : start : null;
    const playbackEnd = finite(source.playback_end_hour) ? Math.max(start, source.playback_end_hour) : end !== null ? end : Math.max(...known);
    return { mode: "complete_cohort", start_hour: start, end_hour: end, duration_hours: end === null ? null : Math.max(0, end - start), complete, playback_end_hour: playbackEnd, unresolved_patient_ids: unresolved };
  }
  function comparison(data) {
    const incomplete = data.observation?.complete === false || data.objective?.defined === false;
    const baseline = incomplete ? null : fullCohortWait(data.baseline), ccn = incomplete ? null : fullCohortWait(data.ccn), defined = baseline !== null && ccn !== null;
    const savedHours = defined ? baseline - ccn : null;
    const savedPercent = defined && baseline > 0 ? savedHours / baseline * 100 : null;
    const observedBaseline = nonnegative(data.baseline?.summary?.blocked_bed_hours), observedCcn = nonnegative(data.ccn?.summary?.blocked_bed_hours), bounds = observationBounds(data);
    const observed = { baseline: observedBaseline, ccn: observedCcn, saved_bed_hours: observedBaseline !== null && observedCcn !== null ? observedBaseline - observedCcn : null, start_hour: bounds.start_hour, end_hour: bounds.end_hour, playback_end_hour: bounds.playback_end_hour, complete: bounds.complete };
    return {
      full: { baseline, ccn, saved_hours: savedHours, saved_percent: savedPercent, defined, percentage_reason: !defined ? "undefined_full_cohort" : baseline === 0 ? "zero_baseline" : null },
      observed,
      // Retained only for callers reading older exports; the UI uses full/observed.
      window: { ...observed, end_hour: data.observation ? bounds.end_hour : data.scenario?.horizon_hours ?? bounds.end_hour }
    };
  }
  function completion(data) {
    const shared = observationBounds(data), result = { shared, defined: true };
    for (const key of ["baseline", "ccn"]) {
      const policy = data[key], rows = policy?.patients || [], cohortCount = data.scenario?.patients?.length ?? policy?.summary?.patient_count;
      const releases = rows.map(p => p.planned_release_hour).filter(finite);
      let end = finite(policy?.summary?.last_planned_release_hour) ? policy.summary.last_planned_release_hour : releases.length ? Math.max(...releases) : cohortCount === 0 ? shared.start_hour : null;
      const defined = shared.complete && fullCohortWait(policy) !== null && end !== null;
      if (!defined) end = null;
      result[key] = { end_hour: end, duration_hours: end === null ? null : Math.max(0, end - shared.start_hour), defined };
      result.defined = result.defined && defined;
    }
    result.saved_hours = result.defined ? result.baseline.duration_hours - result.ccn.duration_hours : null;
    return result;
  }
  function patientAt(patient, hour, boundsOrEnd) {
    const bounds = typeof boundsOrEnd === "number" ? { start_hour: 0, playback_end_hour: boundsOrEnd } : boundsOrEnd || {};
    const start = finite(bounds.start_hour) ? bounds.start_hour : 0, end = finite(bounds.playback_end_hour) ? Math.max(start, bounds.playback_end_hour) : finite(bounds.end_hour) ? Math.max(start, bounds.end_hour) : start;
    const time = Math.min(Math.max(finite(hour) ? hour : start, start), end);
    const admission = finite(patient.admission_hour) && patient.admission_hour <= end ? patient.admission_hour : null;
    const release = finite(patient.release_hour) && patient.release_hour <= end ? patient.release_hour : null;
    let status = "not_ready";
    if (time >= patient.ready_hour) status = admission === null || time < admission ? "waiting" : release !== null && time >= release ? "completed" : "in_snf";
    const throughEnd = nonnegative(patient.wait_through_playback_end_hours ?? patient.wait_in_window_hours);
    return {
      time_hour: time,
      status,
      waiting_so_far_hours: Math.max(0, Math.min(time, admission ?? time) - patient.ready_hour),
      full_planned_wait_hours: finite(patient.planned_admission_hour) ? nonnegative(patient.wait_hours) : null,
      wait_through_playback_end_hours: throughEnd,
      wait_in_window_hours: throughEnd,
      observed_admission_hour: admission,
      observed_release_hour: release,
      planned_admission_hour: finite(patient.planned_admission_hour) ? patient.planned_admission_hour : null,
      planned_release_hour: finite(patient.planned_release_hour) ? patient.planned_release_hour : null
    };
  }
  function planExtent(data) {
    const bounds = observationBounds(data), result = { ...bounds, last_admission_hour: null, last_release_hour: null };
    for (const key of ["baseline", "ccn"]) for (const patient of data[key]?.patients || []) {
      if (finite(patient.planned_admission_hour)) result.last_admission_hour = Math.max(result.last_admission_hour ?? 0, patient.planned_admission_hour);
      if (finite(patient.planned_release_hour)) result.last_release_hour = Math.max(result.last_release_hour ?? 0, patient.planned_release_hour);
    }
    return result;
  }
  function fairness(data) {
    // Current runs provide the canonical report. Derivation keeps older exported
    // runs readable without treating an incomplete compatible subset as a cohort.
    if (data.fairness) return data.fairness;
    const policies = ["baseline", "ccn"], indexes = {}, invalid = { baseline: [], ccn: [] };
    for (const key of policies) {
      indexes[key] = new Map();
      for (const row of data[key]?.patients || []) {
        if (indexes[key].has(row.patient_id)) invalid[key].push(row.patient_id);
        indexes[key].set(row.patient_id, row);
      }
    }
    const cohort = data.scenario?.patients || [...new Set(policies.flatMap(key => [...indexes[key].keys()]))].map(id => ({ id, hospital_id: indexes.baseline.get(id)?.hospital_id ?? indexes.ccn.get(id)?.hospital_id }));
    const expectedIds = new Set(cohort.map(patient => patient.id));
    for (const key of policies) for (const id of indexes[key].keys()) if (!expectedIds.has(id)) invalid[key].push(id);
    const tolerance = 1e-7, classify = delta => delta < -tolerance ? "improved" : delta > tolerance ? "worsened" : "unchanged";
    const rows = cohort.map(patient => {
      const waits = {};
      for (const key of policies) {
        const row = indexes[key].get(patient.id);
        waits[key] = row && finite(row.planned_admission_hour ?? row.admission_hour) ? nonnegative(row.wait_hours) : null;
        if (waits[key] === null || row?.hospital_id !== patient.hospital_id || invalid[key].includes(patient.id)) { invalid[key].push(patient.id); waits[key] = null; }
      }
      const delta = waits.baseline === null || waits.ccn === null ? null : waits.ccn - waits.baseline;
      return { patient_id: patient.id, hospital_id: patient.hospital_id, baseline_wait_hours: waits.baseline, ccn_wait_hours: waits.ccn, delta_wait_hours: delta, status: delta === null ? "undefined" : classify(delta) };
    });
    for (const key of policies) invalid[key] = [...new Set(invalid[key])];
    const defined = expectedIds.size === cohort.length && !invalid.baseline.length && !invalid.ccn.length && comparison(data).full.defined;
    function counts(population, available) {
      return Object.fromEntries(["improved", "worsened", "unchanged"].map(status => [`${status}_count`, available ? population.filter(row => row.status === status).length : null]));
    }
    const hospitalIds = data.scenario?.hospitals?.map(hospital => hospital.id) || [...new Set(rows.map(row => row.hospital_id))];
    const hospitals = hospitalIds.map(hospitalId => {
      const patients = rows.filter(row => row.hospital_id === hospitalId), complete = patients.every(row => row.status !== "undefined"), count = patients.length;
      const total = key => complete ? patients.reduce((sum, row) => sum + row[`${key}_wait_hours`], 0) : null;
      const b = total("baseline"), c = total("ccn"), delta = complete ? c - b : null, patientCounts = counts(patients, complete);
      return { hospital_id: hospitalId, patient_count: count, defined: complete, baseline_total_wait_hours: b, ccn_total_wait_hours: c, delta_total_wait_hours: delta, baseline_mean_wait_hours: complete && count ? b / count : null, ccn_mean_wait_hours: complete && count ? c / count : null, delta_mean_wait_hours: complete && count ? delta / count : null, improved_patient_count: patientCounts.improved_count, worsened_patient_count: patientCounts.worsened_count, unchanged_patient_count: patientCounts.unchanged_count, status: !count ? "empty" : complete ? classify(delta) : "undefined" };
    });
    function stats(key) {
      const values = rows.map(row => row[`${key}_wait_hours`]).sort((a, b) => a - b), n = values.length;
      const total = defined ? values.reduce((sum, value) => sum + value, 0) : null, position = (n - 1) * 0.9, low = Math.floor(position), high = Math.ceil(position);
      const means = hospitals.filter(row => row.patient_count).map(row => row[`${key}_mean_wait_hours`]);
      return { total_wait_hours: total, mean_wait_hours: defined && n ? total / n : null, p90_wait_hours: defined && n ? values[low] + (values[high] - values[low]) * (position - low) : null, max_wait_hours: defined && n ? values[n - 1] : null, worst_hospital_mean_wait_hours: defined && means.length ? Math.max(...means) : null };
    }
    const patientCounts = counts(rows, defined), patients = { ...patientCounts };
    for (const status of ["improved", "worsened", "unchanged"]) patients[`${status}_fraction`] = defined && rows.length ? patientCounts[`${status}_count`] / rows.length : null;
    patients.max_increase_wait_hours = defined && rows.length ? Math.max(0, ...rows.map(row => row.delta_wait_hours)) : null;
    patients.rows = rows;
    return { schema_version: 1, defined, reason: defined ? null : "undefined_complete_cohort", patient_count: rows.length, undefined_patient_ids: invalid, delta_definition: "CCN minus baseline; a positive waiting delta means worse under CCN.", comparison_tolerance_hours: tolerance, baseline: stats("baseline"), ccn: stats("ccn"), patients, hospitals, hospital_comparison: counts(hospitals.filter(row => row.patient_count), defined) };
  }
  function inputIdentity(scenario) {
    function stable(value) {
      if (Array.isArray(value)) return value.map(stable);
      if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
      return value;
    }
    const { tie_break_seed, optimizer_seed, ...inputs } = scenario;
    return JSON.stringify(stable(inputs));
  }
  return { fullCohortWait, comparison, observationBounds, completion, patientAt, planExtent, fairness, inputIdentity };
});
