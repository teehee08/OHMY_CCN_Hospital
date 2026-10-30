"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const metrics = require("../web/dynamic_metrics.js");

function policy(full, window, patients = []) {
  return { summary: { full_plan_wait_hours: full, blocked_bed_hours: window,
                     mean_wait_hours: 0, structurally_unplaceable_patient_ids: [] }, patients };
}
function medium() {
  return {scenario: {arrival_span_hours: 168}, observation: {start_hour: 8, end_hour: 248,
    playback_end_hour: 248, duration_hours: 240, complete: true, unresolved_patient_ids: []},
    baseline: policy(657, 657), ccn: policy(490, 490)};
}
test("primary metric and blocked bed-hours use the same complete cohort", () => {
  const result = metrics.comparison(medium());
  assert.equal(result.full.baseline, 657);
  assert.equal(result.full.ccn, 490);
  assert.equal(result.full.saved_hours, 167);
  assert.ok(Math.abs(result.full.saved_percent - 100 * 167 / 657) < 1e-9);
  assert.equal(result.observed.saved_bed_hours, 167);
  assert.equal(result.observed.start_hour, 8);
  assert.equal(result.observed.end_hour, 248);
});
test("less waiting can have a later final release", () => {
  const data = {scenario: {patients: [{ready_hour: 0}, {ready_hour: 1}]},
    observation: {start_hour: 0, end_hour: 12, playback_end_hour: 12, complete: true, unresolved_patient_ids: []},
    baseline: policy(9, 9, [{planned_admission_hour: 10, planned_release_hour: 11}]),
    ccn: policy(2, 2, [{planned_admission_hour: 2, planned_release_hour: 12}])};
  const result = metrics.comparison(data);
  assert.equal(result.full.saved_hours, 7);
  assert.ok(result.full.saved_percent > 77);
  assert.equal(metrics.completion(data).saved_hours, -1);
});
test("zero baseline has defined absolute saving but undefined percentage", () => {
  const result = metrics.comparison({baseline: policy(0, 0), ccn: policy(0, 0)});
  assert.equal(result.full.saved_hours, 0);
  assert.equal(result.full.saved_percent, null);
  assert.equal(result.full.percentage_reason, "zero_baseline");
});
test("worsening is represented as negative full-cohort saving", () => {
  const result = metrics.comparison({baseline: policy(2, 0), ccn: policy(5, 0)});
  assert.equal(result.full.saved_hours, -3);
  assert.equal(result.full.saved_percent, -150);
});
test("unplaceable patients cannot be silently excluded even if partial objective is zero", () => {
  const result = medium();
  result.ccn.summary.full_plan_wait_hours = null;
  result.ccn.summary.structurally_unplaceable_patient_ids = ["blocked"];
  result.ccn.solver = {objective_hours: 0, status: "optimal"};
  assert.equal(metrics.fullCohortWait(result.ccn), null);
  assert.equal(metrics.comparison(result).full.saved_hours, null);
  assert.equal(metrics.comparison(result).full.saved_percent, null);
  result.ccn.summary.full_plan_wait_hours = 0;
  assert.equal(metrics.fullCohortWait(result.ccn), null);
});
test("legacy numeric playback bounds remain readable without changing total waiting", () => {
  const patient = {ready_hour: 0, admission_hour: null, release_hour: null,
                   planned_admission_hour: 20, planned_release_hour: 30, wait_hours: 20, wait_in_window_hours: 10};
  const row = policy(20, 10, [patient]);
  assert.equal(metrics.fullCohortWait(row), 20);
  for (const hour of [0, 5, 10, 30]) {
    const state = metrics.patientAt(patient, hour, 10);
    assert.equal(state.status, "waiting");
    assert.equal(state.observed_admission_hour, null);
    assert.equal(state.full_planned_wait_hours, 20);
    assert.equal(state.waiting_so_far_hours, Math.min(hour, 10));
  }
  const before = JSON.stringify(row);
  metrics.patientAt(patient, 8, 10);
  assert.equal(JSON.stringify(row), before);
});
test("ready, admission and release boundaries use consistent actual states", () => {
  const patient = {ready_hour: 2, admission_hour: 4, release_hour: 7,
                   planned_admission_hour: 4, planned_release_hour: 7, wait_hours: 2, wait_in_window_hours: 2};
  assert.equal(metrics.patientAt(patient, 1, 10).status, "not_ready");
  assert.equal(metrics.patientAt(patient, 2, 10).status, "waiting");
  assert.equal(metrics.patientAt(patient, 4, 10).status, "in_snf");
  assert.equal(metrics.patientAt(patient, 7, 10).status, "completed");
  assert.equal(metrics.patientAt(patient, 9, 10).waiting_so_far_hours, 2);
});
test("full-plan extent continues through all releases despite old arrival-span metadata", () => {
  const data = {scenario: {horizon_hours: 10}, baseline: policy(20, 10, [
    {planned_admission_hour: 20, planned_release_hour: 30},
    {planned_admission_hour: 0, planned_release_hour: 12}]), ccn: policy(2, 2, [
    {planned_admission_hour: 2, planned_release_hour: 8}])};
  const extent = metrics.planExtent(data);
  assert.equal(extent.last_admission_hour, 20);
  assert.equal(extent.last_release_hour, 30);
  assert.equal(extent.start_hour, 0);
  assert.equal(extent.end_hour, 30);
  assert.equal(extent.playback_end_hour, 30);
  assert.equal(extent.complete, true);
});
test("nonzero observation starts at earliest ready and includes a late release", () => {
  const patient = {ready_hour: 24, admission_hour: 24, release_hour: 36,
    planned_admission_hour: 24, planned_release_hour: 36, wait_hours: 0, wait_through_playback_end_hours: 0};
  const data = {scenario: {horizon_hours: 1, patients: [patient]}, baseline: policy(0, 0, [patient]), ccn: policy(0, 0, [patient])};
  const bounds = metrics.observationBounds(data);
  assert.equal(bounds.start_hour, 24);
  assert.equal(bounds.end_hour, 36);
  assert.equal(bounds.duration_hours, 12);
  assert.equal(metrics.patientAt(patient, 0, bounds).time_hour, 24);
  assert.equal(metrics.patientAt(patient, 24, bounds).status, "in_snf");
  assert.equal(metrics.patientAt(patient, 36, bounds).status, "completed");
  assert.equal(metrics.completion(data).baseline.duration_hours, 12);
});
test("incomplete cohort never treats the last known event as full completion", () => {
  const blocked = {patient_id: "blocked", ready_hour: 20, admission_hour: null, release_hour: null,
    planned_admission_hour: null, planned_release_hour: null, wait_hours: null, wait_through_playback_end_hours: 0};
  const data = {scenario: {patients: [blocked]}, baseline: policy(null, null, [blocked]), ccn: policy(null, null, [blocked]),
    observation: {start_hour: 20, end_hour: null, playback_end_hour: 20, duration_hours: null,
      complete: false, unresolved_patient_ids: ["blocked"]}};
  const bounds = metrics.observationBounds(data);
  assert.equal(bounds.end_hour, null);
  assert.equal(bounds.duration_hours, null);
  assert.equal(metrics.completion(data).defined, false);
  assert.equal(metrics.comparison(data).full.saved_hours, null);
  assert.equal(metrics.patientAt(blocked, 20, bounds).status, "waiting");
});
test("empty cohort has a finite zero-duration axis and zero completion duration", () => {
  const data = {scenario: {patients: []}, baseline: policy(0, 0), ccn: policy(0, 0)};
  const bounds = metrics.observationBounds(data);
  assert.equal(bounds.start_hour, 0);
  assert.equal(bounds.end_hour, 0);
  assert.equal(bounds.duration_hours, 0);
  assert.equal(metrics.completion(data).baseline.duration_hours, 0);
});

function fairnessCase() {
  const patients = [{id: "a", hospital_id: "A"}, {id: "b", hospital_id: "A"}, {id: "c", hospital_id: "B"}, {id: "d", hospital_id: "B"}];
  const rows = waits => patients.map((p, index) => ({patient_id: p.id, hospital_id: p.hospital_id, planned_admission_hour: waits[index], wait_hours: waits[index]}));
  return {scenario: {patients, hospitals: [{id: "A"}, {id: "B"}, {id: "empty"}]}, baseline: policy(16, 16, rows([10, 0, 4, 2])), ccn: policy(7, 7, rows([0, 2, 3, 2]))};
}
test("total improvement reports the individual harmed patient rather than hiding it in the sum", () => {
  const data = fairnessCase(), report = metrics.fairness(data);
  assert.equal(metrics.comparison(data).full.saved_hours, 9);
  assert.equal(report.defined, true);
  assert.deepEqual([report.patients.improved_count, report.patients.worsened_count, report.patients.unchanged_count], [2, 1, 1]);
  assert.equal(report.patients.worsened_fraction, 0.25);
  assert.equal(report.patients.max_increase_wait_hours, 2);
  assert.deepEqual(report.patients.rows.find(row => row.patient_id === "b"), {patient_id: "b", hospital_id: "A", baseline_wait_hours: 0, ccn_wait_hours: 2, delta_wait_hours: 2, status: "worsened"});
  assert.deepEqual(report.hospital_comparison, {improved_count: 2, worsened_count: 0, unchanged_count: 0});
  assert.equal(report.hospitals.find(row => row.hospital_id === "A").worsened_patient_count, 1);
  assert.equal(report.hospitals.find(row => row.hospital_id === "empty").status, "empty");
  assert.equal(report.hospitals.find(row => row.hospital_id === "empty").ccn_mean_wait_hours, null);
});
test("tail waiting and worst hospital mean describe different distributions", () => {
  const report = metrics.fairness(fairnessCase());
  assert.ok(Math.abs(report.baseline.p90_wait_hours - 8.2) < 1e-9);
  assert.ok(Math.abs(report.ccn.p90_wait_hours - 2.7) < 1e-9);
  assert.equal(report.baseline.max_wait_hours, 10);
  assert.equal(report.ccn.max_wait_hours, 3);
  assert.equal(report.baseline.worst_hospital_mean_wait_hours, 5);
  assert.equal(report.ccn.worst_hospital_mean_wait_hours, 2.5);
});
test("a lower total does not imply a lower P90 or a lower maximum", () => {
  const data = fairnessCase();
  for (const [key, waits] of [["baseline", [0, 0, 10, 10]], ["ccn", [0, 0, 0, 18]]]) {
    data[key].patients.forEach((row, index) => { row.wait_hours = waits[index]; row.planned_admission_hour = waits[index]; });
    data[key].summary.full_plan_wait_hours = waits.reduce((a, b) => a + b, 0);
  }
  const report = metrics.fairness(data);
  assert.equal(metrics.comparison(data).full.saved_hours, 2);
  assert.equal(report.patients.worsened_count, 1);
  assert.ok(report.ccn.p90_wait_hours > report.baseline.p90_wait_hours);
  assert.ok(report.ccn.max_wait_hours > report.baseline.max_wait_hours);
});
test("one unresolved patient makes both whole-cohort fairness distributions undefined", () => {
  const data = fairnessCase();
  data.ccn.patients[2].planned_admission_hour = null;
  data.ccn.patients[2].wait_hours = null;
  data.ccn.summary.full_plan_wait_hours = null;
  const report = metrics.fairness(data);
  assert.equal(report.defined, false);
  assert.deepEqual(report.undefined_patient_ids.ccn, ["c"]);
  assert.equal(report.baseline.p90_wait_hours, null);
  assert.equal(report.ccn.max_wait_hours, null);
  assert.equal(report.patients.improved_count, null);
  assert.equal(report.patients.worsened_fraction, null);
  assert.equal(report.hospital_comparison.improved_count, null);
  assert.equal(report.patients.rows.length, 4);
  assert.equal(report.hospitals.find(row => row.hospital_id === "A").defined, true);
  assert.equal(report.hospitals.find(row => row.hospital_id === "B").defined, false);
});
test("missing, extra or duplicated outcome rows cannot produce a complete fairness report", () => {
  for (const mutate of [data => data.ccn.patients.pop(), data => data.ccn.patients.push({...data.ccn.patients[0]}), data => data.ccn.patients.push({...data.ccn.patients[0], patient_id: "unexpected"})]) {
    const data = fairnessCase(); mutate(data);
    assert.equal(metrics.fairness(data).defined, false);
    assert.equal(metrics.fairness(data).patients.worsened_count, null);
  }
});
test("changing execution seeds preserves cohort identity while changing demand or preferences does not", () => {
  const scenario = {patients: [{id: "p", ready_hour: 0, eligible_snf_ids: ["S1", "S2"]}], beds: [{id: "b"}], tie_break_seed: 42, optimizer_seed: 20261029};
  const identity = metrics.inputIdentity(scenario);
  assert.equal(identity, metrics.inputIdentity({...scenario, tie_break_seed: 123, optimizer_seed: 0}));
  assert.notEqual(identity, metrics.inputIdentity({...scenario, patients: [{...scenario.patients[0], ready_hour: 1}]}));
  assert.notEqual(identity, metrics.inputIdentity({...scenario, patients: [{...scenario.patients[0], eligible_snf_ids: ["S2", "S1"]}]}));
});
test("floating point noise is unchanged at the canonical waiting comparison tolerance", () => {
  const data = fairnessCase();
  data.ccn.patients = data.baseline.patients.map(row => ({...row, wait_hours: row.wait_hours + 5e-8}));
  data.ccn.summary.full_plan_wait_hours = 16 + 2e-7;
  const report = metrics.fairness(data);
  assert.equal(report.comparison_tolerance_hours, 1e-7);
  assert.equal(report.patients.unchanged_count, 4);
  assert.equal(report.patients.worsened_count, 0);
});
test("an empty fairness cohort has zero totals and undefined distributions and maximum increase", () => {
  const report = metrics.fairness({scenario: {patients: [], hospitals: [{id: "empty"}]}, baseline: policy(0, 0), ccn: policy(0, 0)});
  assert.equal(report.defined, true);
  assert.equal(report.baseline.total_wait_hours, 0);
  assert.equal(report.ccn.p90_wait_hours, null);
  assert.equal(report.patients.max_increase_wait_hours, null);
  assert.equal(report.patients.improved_fraction, null);
  assert.equal(report.hospital_comparison.unchanged_count, 0);
});
