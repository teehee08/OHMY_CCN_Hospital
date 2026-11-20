"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const patterns = require("../web/staffing_patterns.js");
const template = require("../scenarios/staffing_v4.json");

const scenario = () => JSON.parse(JSON.stringify(template));

test("changing a staffing pattern updates the same draft's per-person hours", () => {
  const draft = scenario();
  const patients = draft.patients.length, beds = draft.beds.length;
  patterns.apply(draft, "shortage");
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.RN, 3);
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.LPN, 3.6);
  assert.equal(draft.staffing.shifts[0].direct_care_hours.CNA, 7.2);
  assert.equal(patterns.detect(draft), "shortage");
  assert.equal(draft.patients.length, patients);
  assert.equal(draft.beds.length, beds);
  patterns.apply(draft, "full");
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.RN, 5);
  assert.equal(patterns.detect(draft), "full");
});

test("night and uneven patterns affect only the selected shifts or SNF", () => {
  const draft = scenario();
  patterns.apply(draft, "night");
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.RN, 1.5);
  assert.equal(draft.staffing.shifts[1].direct_care_hours_per_staff_per_day.RN, 5);
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.LPN, 6);
  patterns.apply(draft, "uneven");
  assert.equal(draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.RN, 2);
  assert.equal(draft.staffing.shifts[3].direct_care_hours_per_staff_per_day.RN, 5);
  assert.equal(patterns.detect(draft), "uneven");
});

test("manual hours are identified as custom and legacy inputs are preserved", () => {
  const draft = scenario();
  draft.staffing.shifts[0].direct_care_hours_per_staff_per_day.RN = 4.5;
  assert.equal(patterns.detect(draft), "custom");
  const legacy = scenario();
  legacy.staffing.shifts.forEach(shift => {
    delete shift.staff_count;
    delete shift.direct_care_hours_per_staff_per_day;
    shift.direct_care_hours = { RN: 5, LPN: 6, CNA: 12 };
  });
  const before = JSON.stringify(legacy);
  assert.throws(() => patterns.apply(legacy, "shortage"), /legacy shift-hour staffing/);
  assert.equal(JSON.stringify(legacy), before);
});
