/* Dynamic placement workspace. Draft inputs and the last completed run are separate. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const clone = value => JSON.parse(JSON.stringify(value));
  const state = { draft: null, examples: [], limits: { max_patients: 50, max_beds: 100, max_facilities: 12 }, lastRun: null, submitted: null, runTime: null, hour: 0, playing: null, busy: false, exportText: "", exportName: "scenario.json" };
  const storageKey = "ohmy.dynamic.draft.v1";
  const maxHours = 1000000, maxTextLength = 80;
  const genders = [["female", "Female"], ["male", "Male"]];
  const bedGenders = [["any", "Any gender"], ...genders];
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function button(text, className, action, title) {
    const node = el("button", className, text);
    node.type = "button";
    if (title) { node.title = title; node.setAttribute("aria-label", title); }
    node.addEventListener("click", action);
    return node;
  }
  function field(text, control, wide = false) {
    const label = el("label", `field${wide ? " wide" : ""}`);
    label.append(el("span", "", text), control);
    return label;
  }
  function input(value, onInput, options = {}) {
    const node = el("input");
    node.type = options.type || "text";
    node.value = value ?? "";
    if (node.type === "text") node.maxLength = maxTextLength;
    if (node.type === "number") node.max = maxHours;
    if (options.min !== undefined) node.min = options.min;
    if (options.max !== undefined) node.max = options.max;
    if (options.step !== undefined) node.step = options.step;
    if (options.label) node.setAttribute("aria-label", options.label);
    node.addEventListener(options.commit ? "change" : "input", () => {
      onInput(node.type === "number" ? (node.value === "" ? null : Number(node.value)) : node.value, node);
    });
    return node;
  }
  function select(value, options, change, label) {
    const node = el("select");
    if (label) node.setAttribute("aria-label", label);
    for (const [key, title] of options) { const option = el("option", "", title); option.value = key; node.append(option); }
    if (!options.length) { const option = el("option", "", "Add a facility first"); option.value = ""; node.append(option); }
    node.value = value;
    node.addEventListener("change", () => change(node.value));
    return node;
  }
  function fmt(value) { return value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 }); }
  function clock(hour, includeHours = false) {
    if (hour === null || hour === undefined || !Number.isFinite(Number(hour))) return "—";
    const minutes = Math.round(Number(hour) * 60), day = Math.floor(minutes / 1440), remainder = minutes % 1440;
    const week = Math.floor(day / 7);
    const result = `${days[day % 7]}${week ? ` +${week}w` : ""} ${String(Math.floor(remainder / 60)).padStart(2, "0")}:${String(remainder % 60).padStart(2, "0")}`;
    return includeHours ? `${result} · ${fmt(hour)} h` : result;
  }
  function showError(id, message) { $(id).textContent = message || ""; $(id).hidden = !message; }
  function message(text) { $("dynamic-editor-message").textContent = text; }
  function dirty() {
    try { localStorage.setItem(storageKey, JSON.stringify(state.draft)); } catch (_) { /* Saving is optional. */ }
    const stale = Boolean(state.lastRun) && JSON.stringify(state.draft) !== JSON.stringify(state.submitted);
    $("dynamic-stale").hidden = !stale;
    $("dynamic-result-status").textContent = state.busy ? "Running…" : stale ? "Inputs changed" : state.lastRun ? "Run complete" : "Ready to run";
    $("dynamic-result-status").className = `result-status${stale ? " stale" : state.lastRun ? " complete" : ""}`;
  }
  function nextId(prefix, entities) { let n = 1; const ids = new Set(entities.map(item => item.id)); while (ids.has(`${prefix}${n}`)) n++; return `${prefix}${n}`; }
  function section(key, title, count, content) {
    const node = el("details", "editor-section"); node.dataset.section = key; node.open = true;
    const summary = el("summary", "", title); summary.append(el("span", "section-count", `${count}`)); node.append(summary, content); return node;
  }
  function rename(kind, index, value, control) {
    const entities = state.draft[kind], oldId = entities[index].id, newId = value.trim();
    if (!newId || newId.length > maxTextLength || entities.some((item, i) => i !== index && item.id === newId)) {
      control.value = oldId; message("Each ID must be non-empty, unique within its type and at most 80 characters."); return;
    }
    entities[index].id = newId;
    if (kind === "hospitals") state.draft.patients.forEach(p => { if (p.hospital_id === oldId) p.hospital_id = newId; });
    if (kind === "snfs") {
      state.draft.beds.forEach(b => { if (b.snf_id === oldId) b.snf_id = newId; });
      state.draft.patients.forEach(p => { p.eligible_snf_ids = p.eligible_snf_ids.map(id => id === oldId ? newId : id); });
    }
    dirty(); renderEditor(); message("ID updated; linked references updated too.");
  }
  function renderEditor() {
    const root = $("dynamic-editor"), open = new Map([...root.querySelectorAll("details[data-section]")].map(n => [n.dataset.section, n.open]));
    root.replaceChildren();
    const s = state.draft;
    const basics = el("div", "dynamic-fields");
    basics.append(field("Scenario name", input(s.name, v => { s.name = v; dirty(); })), field("Observe through (h)", input(s.horizon_hours, v => { s.horizon_hours = v; dirty(); }, { type: "number", min: 0.01, step: "any" })));
    root.append(basics);
    const facilities = el("div", "editor-content facility-grid");
    for (const [kind, title, prefix] of [["hospitals", "Hospitals", "Hospital_"], ["snfs", "SNFs", "SNF"]]) {
      const col = el("div"); col.append(el("h3", "", title));
      s[kind].forEach((item, index) => {
        const row = el("div", "facility-row");
        row.append(input(item.id, (v, control) => rename(kind, index, v, control), { commit: true, label: `${title} ID ${index + 1}` }), button("×", "icon-button", () => {
          const removed = s[kind].splice(index, 1)[0].id;
          if (kind === "hospitals") s.patients.forEach(p => { if (p.hospital_id === removed) p.hospital_id = s.hospitals[0]?.id || ""; });
          else { s.beds = s.beds.filter(b => b.snf_id !== removed); s.patients.forEach(p => { p.eligible_snf_ids = p.eligible_snf_ids.filter(id => id !== removed); }); }
          dirty(); renderEditor(); message(kind === "snfs" ? `Removed ${removed}, its beds and eligibility references.` : `Removed ${removed}. Affected patients reassigned to the first remaining hospital, if any.`);
        }, `Delete ${item.id}`)); col.append(row);
      });
      if (!s[kind].length) col.append(el("p", "no-entities", "No facilities added."));
      const add = button(`+ Add ${kind === "snfs" ? "SNF" : "hospital"}`, "add-button", () => {
        s[kind].push({ id: nextId(prefix, s[kind]) }); dirty(); renderEditor();
      }); add.disabled = s[kind].length >= state.limits.max_facilities; col.append(add); facilities.append(col);
    }
    root.append(section("facilities", "Hospitals & SNFs", `${s.hospitals.length} / ${s.snfs.length}`, facilities));
    const beds = el("div", "editor-content");
    beds.append(el("p", "small-note", "Each row is one physical bed. All beds are empty at hour 0 and become available again at release."));
    const table = el("table", "entity-table"); const head = el("thead"), tr = el("tr");
    for (const text of ["Bed ID", "SNF", "Gender", ""]) tr.append(el("th", "", text)); head.append(tr); table.append(head);
    const body = el("tbody");
    s.beds.forEach((bed, index) => {
      const row = el("tr");
      const controls = [input(bed.id, (v, control) => rename("beds", index, v, control), { commit: true, label: `Bed ${index + 1} ID` }), select(bed.snf_id, s.snfs.map(f => [f.id, f.id]), v => { bed.snf_id = v; dirty(); }, `SNF for ${bed.id}`), select(bed.gender, bedGenders, v => { bed.gender = v; dirty(); }, `Gender for ${bed.id}`), button("×", "icon-button", () => { s.beds.splice(index, 1); dirty(); renderEditor(); }, `Delete bed ${bed.id}`)];
      controls.forEach(control => { const cell = el("td"); cell.append(control); row.append(cell); }); body.append(row);
    }); table.append(body); beds.append(table);
    if (!s.beds.length) beds.append(el("p", "no-entities", "No beds. Patients will remain waiting."));
    const addBed = button("+ Add bed", "add-button", () => { s.beds.push({ id: nextId("Bed_", s.beds), snf_id: s.snfs[0]?.id || "", gender: "any" }); dirty(); renderEditor(); });
    addBed.disabled = s.beds.length >= state.limits.max_beds || !s.snfs.length; beds.append(addBed); root.append(section("beds", "SNF beds", s.beds.length, beds));
    const patients = el("div", "editor-content");
    patients.append(el("p", "small-note", "Ready = medically ready to leave hospital. Stays begin at actual SNF admission. Input order breaks baseline ties at the same ready hour."));
    s.patients.forEach((p, index) => {
      const card = el("article", "entity-row"); const heading = el("div", "entity-heading");
      heading.append(el("span", "patient-row-name", `PATIENT ${index + 1} / ${p.id}`)); const actions = el("div", "row-actions");
      for (const [direction, glyph] of [[-1, "↑"], [1, "↓"]]) {
        const move = button(glyph, "icon-button", () => { const target = index + direction; [s.patients[index], s.patients[target]] = [s.patients[target], s.patients[index]]; dirty(); renderEditor(); }, `Move ${p.id} ${direction < 0 ? "earlier" : "later"} in priority`);
        move.disabled = index + direction < 0 || index + direction >= s.patients.length; actions.append(move);
      }
      actions.append(button("×", "icon-button", () => { s.patients.splice(index, 1); dirty(); renderEditor(); }, `Delete patient ${p.id}`)); heading.append(actions); card.append(heading);
      const fields = el("div", "entity-fields");
      fields.append(field("Patient ID", input(p.id, (v, control) => rename("patients", index, v, control), { commit: true })), field("Hospital", select(p.hospital_id, s.hospitals.map(f => [f.id, f.id]), v => { p.hospital_id = v; dirty(); })), field("Gender", select(p.gender, genders, v => { p.gender = v; dirty(); })), field("Ready hour", input(p.ready_hour, v => { p.ready_hour = v; dirty(); }, { type: "number", min: 0, step: "any" })), field("SNF stay (hours)", input(p.length_of_stay_hours, v => { p.length_of_stay_hours = v; dirty(); }, { type: "number", min: 0.01, step: "any" })));
      card.append(fields, el("p", "preference-caption", "Eligible SNFs · click to include; arrows reorder preference"));
      const choices = el("div", "preference-list");
      const ordered = [...p.eligible_snf_ids, ...s.snfs.map(f => f.id).filter(id => !p.eligible_snf_ids.includes(id))];
      ordered.forEach(id => {
        const rank = p.eligible_snf_ids.indexOf(id), chip = el("div", `preference-chip${rank >= 0 ? " included" : ""}`);
        const toggle = button("", "", () => { p.eligible_snf_ids = rank >= 0 ? p.eligible_snf_ids.filter(v => v !== id) : [...p.eligible_snf_ids, id]; dirty(); renderEditor(); }, `${rank >= 0 ? "Remove" : "Include"} ${id} for ${p.id}`);
        toggle.setAttribute("aria-pressed", String(rank >= 0)); if (rank >= 0) toggle.append(el("span", "rank", rank + 1)); toggle.append(document.createTextNode(id)); chip.append(toggle);
        if (rank >= 0) {
          for (const [delta, glyph] of [[-1, "←"], [1, "→"]]) {
            const reorder = button(glyph, "", () => { const target = rank + delta; [p.eligible_snf_ids[rank], p.eligible_snf_ids[target]] = [p.eligible_snf_ids[target], p.eligible_snf_ids[rank]]; dirty(); renderEditor(); }, `Move ${id} ${delta < 0 ? "earlier" : "later"} in ${p.id} preference`);
            reorder.disabled = rank + delta < 0 || rank + delta >= p.eligible_snf_ids.length; chip.append(reorder);
          }
        }
        choices.append(chip);
      }); card.append(choices);
      if (!p.eligible_snf_ids.length) card.append(el("p", "dynamic-editor-warning", "No eligible SNF selected. This patient cannot be placed."));
      patients.append(card);
    });
    const addPatient = button("+ Add patient", "add-button", () => { s.patients.push({ id: nextId("P", s.patients), hospital_id: s.hospitals[0]?.id || "", gender: "female", ready_hour: 0, length_of_stay_hours: 24, eligible_snf_ids: s.snfs.map(f => f.id) }); dirty(); renderEditor(); });
    addPatient.disabled = s.patients.length >= state.limits.max_patients || !s.hospitals.length; patients.append(addPatient); root.append(section("patients", "Patients & future demand", s.patients.length, patients));
    for (const node of root.querySelectorAll("details[data-section]")) if (open.has(node.dataset.section)) node.open = open.get(node.dataset.section);
  }
  function validate(s) {
    if (!s || typeof s !== "object" || Array.isArray(s)) throw new Error("Import a scenario object, or an exported run containing a scenario.");
    if (s.schema_version !== 1) throw new Error("This editor requires dynamic scenario schema_version: 1.");
    if (typeof s.name !== "string" || !s.name.trim() || s.name.length > maxTextLength) throw new Error("Give the scenario a name of 1–80 characters.");
    if (!Number.isFinite(s.horizon_hours) || s.horizon_hours <= 0 || s.horizon_hours > maxHours) throw new Error("Observation horizon must be positive and at most 1,000,000 hours.");
    for (const [key, max] of [["hospitals", state.limits.max_facilities], ["snfs", state.limits.max_facilities], ["beds", state.limits.max_beds], ["patients", state.limits.max_patients]]) {
      if (!Array.isArray(s[key]) || s[key].length > max) throw new Error(`${key} must be an array with at most ${max} entries.`);
      const seen = new Set();
      for (const item of s[key]) { if (!item || typeof item.id !== "string" || !item.id.trim() || item.id.length > maxTextLength || seen.has(item.id)) throw new Error(`Every ${key} ID must be non-empty, unique and at most 80 characters.`); seen.add(item.id); }
    }
    const hospitalIds = new Set(s.hospitals.map(v => v.id)), snfIds = new Set(s.snfs.map(v => v.id));
    for (const b of s.beds) { if (!snfIds.has(b.snf_id)) throw new Error(`Bed ${b.id} must refer to an existing SNF.`); if (!bedGenders.some(v => v[0] === b.gender)) throw new Error(`Bed ${b.id} gender must be any, female or male.`); }
    for (const p of s.patients) {
      if (!hospitalIds.has(p.hospital_id)) throw new Error(`Patient ${p.id} must refer to an existing hospital.`);
      if (!genders.some(v => v[0] === p.gender)) throw new Error(`Patient ${p.id} gender must be female or male.`);
      if (!Number.isFinite(p.ready_hour) || p.ready_hour < 0 || p.ready_hour > s.horizon_hours || p.ready_hour > maxHours) throw new Error(`Patient ${p.id} ready hour must be between 0 and the observation horizon, at most 1,000,000 hours.`);
      if (!Number.isFinite(p.length_of_stay_hours) || p.length_of_stay_hours <= 0 || p.length_of_stay_hours > maxHours) throw new Error(`Patient ${p.id} SNF stay must be positive and at most 1,000,000 hours.`);
      if (!Array.isArray(p.eligible_snf_ids) || p.eligible_snf_ids.some(id => !snfIds.has(id)) || new Set(p.eligible_snf_ids).size !== p.eligible_snf_ids.length) throw new Error(`Patient ${p.id} must list existing, non-repeated eligible SNFs.`);
    }
    return s;
  }
  async function api(url, options) {
    const response = await fetch(url, options);
    let data; try { data = await response.json(); } catch (_) { throw new Error("The server did not return JSON. Check that the scenario lab server is running."); }
    if (!response.ok) {
      const detail = data.error || data.detail || data.message || `Request failed (${response.status}).`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }
  async function run(event) {
    event.preventDefault(); if (state.busy) return;
    stopPlayback(); showError("dynamic-run-error", "");
    let submitted; try { submitted = clone(validate(state.draft)); } catch (error) { showError("dynamic-run-error", error.message); return; }
    state.busy = true; $("dynamic-run").disabled = true; $("dynamic-quick-run").disabled = true; $("dynamic-run").firstElementChild.textContent = "Simulating…"; dirty();
    try {
      const data = await api("/api/dynamic/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario: submitted }) });
      stopPlayback();
      state.lastRun = data; state.submitted = submitted; state.runTime = new Date().toISOString(); state.hour = 0;
      renderResults(); message("Run complete. Playback and exports use this saved run.");
    } catch (error) { showError("dynamic-run-error", error.message); }
    finally { state.busy = false; $("dynamic-run").disabled = false; $("dynamic-quick-run").disabled = false; $("dynamic-run").firstElementChild.textContent = "Run dynamic comparison"; dirty(); }
  }
  function metric(title, baseline, ccn, delta, suffix = "") {
    const card = el("div", "dynamic-stat"); card.append(el("h4", "", title));
    for (const [key, label, value] of [["baseline", "Baseline", baseline], ["ccn", "CCN", ccn]]) { const row = el("div", "stat-comparison"); row.append(el("span", "", label), el("strong", `${key}-value`, `${fmt(value)}${suffix}`)); card.append(row); }
    card.append(el("p", "stat-delta", delta)); return card;
  }
  function renderResults() {
    const data = state.lastRun, s = data.scenario || state.submitted, b = data.baseline.summary, c = data.ccn.summary;
    $("dynamic-empty").hidden = true; $("dynamic-output").hidden = false;
    $("dynamic-run-name").textContent = s.name;
    $("dynamic-run-subtitle").textContent = `${s.hospitals.length} hospitals · ${s.patients.length} patients · ${s.snfs.length} SNFs · ${s.beds.length} beds · ${fmt(s.horizon_hours)} h window`;
    const solver = data.ccn.solver;
    $("dynamic-solver-note").hidden = !solver || solver.status === "optimal";
    $("dynamic-solver-note").textContent = solver && solver.status !== "optimal" ? `Search limit reached after ${fmt(solver.nodes)} nodes. CCN shows the best feasible plan found; global optimality is not established.` : "";
    const savings = data.comparison.blocked_bed_hours_saved;
    const savedLabel = savings < 0 ? `${fmt(Math.abs(savings))} more with CCN` : `${fmt(savings)} fewer with CCN`;
    $("dynamic-metrics").replaceChildren(
      metric("Hospital waiting · bed-hours", b.blocked_bed_hours, c.blocked_bed_hours, savedLabel, " h"),
      metric("Admitted by window end", b.placed_count, c.placed_count, `${fmt(b.waiting_count)} baseline / ${fmt(c.waiting_count)} CCN still waiting`),
      metric("SNF bed utilization", b.snf_utilization == null ? null : 100 * b.snf_utilization, c.snf_utilization == null ? null : 100 * c.snf_utilization, "Occupied bed-hours ÷ capacity-hours", "%")
    );
    const fullCohortUnavailable = b.full_plan_wait_hours == null || c.full_plan_wait_hours == null;
    const summary = el("p", "small-note");
    summary.textContent = `Full-plan total waiting, including admissions after the window: baseline ${fmt(b.full_plan_wait_hours)} h; CCN ${fmt(c.full_plan_wait_hours)} h. ` + (fullCohortUnavailable
      ? `${solver?.status === "optimal" ? "Optimal waiting" : "CCN plans to minimize waiting"} for patients with compatible beds; full-cohort eventual waiting is undefined.`
      : `CCN plans to minimize this full-cohort objective.${solver?.status === "optimal" ? " Optimal for the supplied simplified scenario." : ""}`);
    $("dynamic-metrics").append(summary); summary.style.gridColumn = "1 / -1";
    const impossible = new Set([...(b.structurally_unplaceable_patient_ids || []), ...(c.structurally_unplaceable_patient_ids || [])]);
    if (impossible.size) { const warning = el("p", "dynamic-editor-warning", `No compatible eligible bed exists for: ${[...impossible].join(", ")}. These patients remain waiting; a full-cohort eventual waiting total is undefined (—).`); warning.style.gridColumn = "1 / -1"; $("dynamic-metrics").append(warning); }
    $("dynamic-time").max = s.horizon_hours; $("dynamic-time").step = Math.min(0.25, s.horizon_hours / 100); $("dynamic-time").value = 0;
    $("dynamic-range-end").textContent = clock(s.horizon_hours, true);
    const assumptions = $("dynamic-assumptions"); assumptions.replaceChildren();
    for (const text of data.assumptions || []) assumptions.append(el("li", "", text));
    renderJourneys(); renderPlacements(); renderEvents(); renderPlayback();
  }
  function snapshotAt(result, hour) {
    const snapshots = result.snapshots || []; let current = snapshots[0] || { waiting_count: 0, occupied_count: 0, completed_count: 0, hospital_waiting: {}, snf_occupied: {}, bed_occupants: {}, waiting_patient_ids: [] };
    for (const snapshot of snapshots) { if (snapshot.time_hour <= hour + 1e-8) current = snapshot; else break; }
    return current;
  }
  function renderPlayback() {
    if (!state.lastRun) return;
    const s = state.lastRun.scenario || state.submitted, live = $("dynamic-live-state"), grid = $("dynamic-bed-grid"); live.replaceChildren(); grid.replaceChildren();
    $("dynamic-clock").textContent = clock(state.hour, true); $("dynamic-time").value = state.hour;
    for (const key of ["baseline", "ccn"]) {
      const result = state.lastRun[key], snap = snapshotAt(result, state.hour);
      const counts = el("div"); counts.append(el("strong", "", key === "baseline" ? "Baseline" : "CCN"), document.createTextNode(`${snap.waiting_count} waiting · ${snap.occupied_count} in SNF · ${snap.completed_count} completed`)); live.append(counts);
      const col = el("div"); col.append(el("h4", `occupancy-heading ${key}`, key === "baseline" ? "Baseline · bed occupancy" : "CCN · bed occupancy"));
      for (const facility of s.snfs) {
        const block = el("div", "snf-block"), beds = s.beds.filter(b => b.snf_id === facility.id), heading = el("div", "snf-heading");
        heading.append(el("span", "", facility.id), el("span", "", `${snap.snf_occupied?.[facility.id] || 0}/${beds.length} occupied`)); block.append(heading);
        beds.forEach(bed => {
          const occupant = snap.bed_occupants?.[bed.id], cell = el("div", `bed-cell ${key}${occupant ? " occupied" : ""}`), label = el("span", "", bed.id);
          label.append(el("small", "", bed.gender === "any" ? "Any gender" : bed.gender === "male" ? "Male bed" : "Female bed")); cell.append(label, el("strong", "", occupant || "Available")); block.append(cell);
        });
        if (!beds.length) block.append(el("p", "no-entities", "No beds")); col.append(block);
      }
      const waiting = el("div", "hospital-wait");
      for (const hospital of s.hospitals) waiting.append(el("div", "", `${hospital.id}: ${snap.hospital_waiting?.[hospital.id] || 0} waiting`));
      if (snap.waiting_patient_ids?.length) waiting.append(el("p", "waiting-tags", snap.waiting_patient_ids.join(" · "))); col.append(waiting); grid.append(col);
    }
    for (const cursor of $("dynamic-journeys").querySelectorAll(".journey-cursor")) cursor.style.left = `${Math.min(100, state.hour / s.horizon_hours * 100)}%`;
    $("dynamic-previous").disabled = state.hour <= 0; $("dynamic-next").disabled = state.hour >= s.horizon_hours;
  }
  function segment(start, end, horizon, className) {
    if (start == null || end == null || end <= start || start >= horizon) return null;
    const bar = el("div", `journey-segment ${className}`); bar.style.left = `${Math.max(0, start) / horizon * 100}%`; bar.style.width = `${(Math.min(horizon, end) - Math.max(0, start)) / horizon * 100}%`; return bar;
  }
  function renderJourneys() {
    const root = $("dynamic-journeys"), data = state.lastRun, s = data.scenario || state.submitted, h = s.horizon_hours; root.replaceChildren();
    const axis = el("div", "journey-axis"); [0, h / 3, h * 2 / 3, h].forEach(t => axis.append(el("span", "", `${fmt(t)} h`))); root.append(axis);
    const maps = Object.fromEntries(["baseline", "ccn"].map(key => [key, new Map(data[key].patients.map(p => [p.patient_id, p]))]));
    for (const p of s.patients) {
      const row = el("div", "journey-patient"), label = el("div", "journey-patient-id", p.id); label.append(el("small", "", clock(p.ready_hour))); row.append(label); const lanes = el("div");
      for (const key of ["baseline", "ccn"]) {
        const plan = maps[key].get(p.id), lane = el("div", `journey-track ${key}`); if (!plan) continue;
        const admit = plan.planned_admission_hour, release = plan.planned_release_hour;
        const waiting = segment(p.ready_hour, admit ?? h, h, "waiting"), stay = segment(admit, release, h, `stay ${key}`);
        if (waiting) lane.append(waiting); if (stay) lane.append(stay);
        lane.title = `${key === "ccn" ? "CCN" : "Baseline"} ${p.id}: ready ${clock(p.ready_hour)}, admission ${clock(admit)}, release ${clock(release)}; waiting ${fmt(plan.wait_hours)} h`;
        lane.append(el("span", "", `${key === "ccn" ? "CCN" : "Baseline"} · ${admit == null ? "no compatible bed" : `${fmt(plan.wait_hours)} h wait`}`), el("div", "journey-cursor")); lanes.append(lane);
      }
      row.append(lanes); root.append(row);
    }
    if (!s.patients.length) root.append(el("p", "no-entities", "No patients in this scenario."));
  }
  function table(headers) {
    const node = el("table", "result-table"), head = el("thead"), row = el("tr"); headers.forEach(h => row.append(el("th", "", h))); head.append(row); node.append(head); const body = el("tbody"); node.append(body); return [node, body];
  }
  function renderPlacements() {
    const data = state.lastRun, s = data.scenario || state.submitted, [node, body] = table(["Patient / strategy", "Bed", "Admission → release", "Wait", "At window end"]);
    const maps = Object.fromEntries(["baseline", "ccn"].map(key => [key, new Map(data[key].patients.map(p => [p.patient_id, p]))]));
    for (const patient of s.patients) for (const key of ["baseline", "ccn"]) {
      const p = maps[key].get(patient.id); if (!p) continue;
      const row = el("tr"), name = el("td", "", p.patient_id); name.append(el("span", `assignment-detail strategy-badge ${key}`, key === "baseline" ? "Baseline" : "CCN"));
      const bed = el("td", "", p.bed_id || "—"); bed.append(el("span", "assignment-detail", p.snf_id || "No compatible bed"));
      const timing = el("td", "", `${clock(p.planned_admission_hour)} → ${clock(p.planned_release_hour)}`);
      if (p.planned_admission_hour > s.horizon_hours) timing.append(el("span", "assignment-detail", "Admission planned beyond window"));
      else if (p.planned_release_hour > s.horizon_hours) timing.append(el("span", "assignment-detail", "Release planned beyond window"));
      const wait = el("td", "", `${fmt(p.wait_hours)} h`); wait.append(el("span", "assignment-detail", `${fmt(p.wait_in_window_hours)} h in window`));
      const status = el("td", p.status === "waiting" ? "unplaced" : "", ({ waiting: "Waiting in hospital", in_snf: "In SNF", completed: "SNF stay complete" })[p.status] || p.status);
      row.append(name, bed, timing, wait, status); body.append(row);
    }
    $("dynamic-placement-table").replaceChildren(node);
  }
  function renderEvents() {
    if (!state.lastRun) return;
    const filter = $("dynamic-log-strategy").value, [node, body] = table(["Time", "Strategy", "Event", "Patient / bed"]);
    const entries = [];
    for (const key of ["baseline", "ccn"]) if (filter === "both" || key === filter) (state.lastRun[key].events || []).forEach((e, index) => entries.push({ ...e, key, index }));
    entries.sort((a, b) => a.time_hour - b.time_hour || a.key.localeCompare(b.key) || a.index - b.index);
    for (const entry of entries) {
      const row = el("tr"), time = el("td", "event-time", clock(entry.time_hour)); time.append(el("span", "assignment-detail", `${fmt(entry.time_hour)} h`));
      const detail = el("td", "", ({ ready: "Patient ready", admit: "SNF admission", release: "Bed released", wait: "Waiting" })[entry.type] || entry.type);
      if (entry.detail) detail.append(el("span", "assignment-detail", entry.detail));
      const target = el("td", "", entry.patient_id || "—"); target.append(el("span", "assignment-detail", [entry.hospital_id, entry.snf_id, entry.bed_id].filter(Boolean).join(" · ")));
      row.append(time, el("td", `strategy-badge ${entry.key}`, entry.key === "ccn" ? "CCN" : "Baseline"), detail, target); body.append(row);
    }
    $("dynamic-events").replaceChildren(node);
    if (!entries.length) $("dynamic-events").append(el("p", "no-entities", "No events in this observation window."));
  }
  function stopPlayback() { if (state.playing) clearInterval(state.playing); state.playing = null; $("dynamic-play").textContent = "Play"; $("dynamic-play").setAttribute("aria-pressed", "false"); }
  function jumpEvent(direction) {
    if (!state.lastRun) return; stopPlayback(); const h = (state.lastRun.scenario || state.submitted).horizon_hours;
    const times = [...new Set([0, h, ...["baseline", "ccn"].flatMap(key => state.lastRun[key].events.map(e => e.time_hour))])].sort((a, b) => a - b);
    state.hour = direction > 0 ? times.find(t => t > state.hour + 1e-8) ?? h : times.filter(t => t < state.hour - 1e-8).at(-1) ?? 0; renderPlayback();
  }
  function exportJson(value, filename, description) {
    state.exportText = JSON.stringify(value, null, 2); state.exportName = filename;
    $("dynamic-export-json").value = state.exportText; $("dynamic-export-description").textContent = description; $("dynamic-export-message").textContent = ""; $("dynamic-export-dialog").showModal();
  }
  function download() {
    const blob = new Blob([state.exportText], { type: "application/json" }), url = URL.createObjectURL(blob), link = el("a"); link.href = url; link.download = state.exportName; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("dynamic-form").addEventListener("submit", run);
  $("dynamic-load").addEventListener("click", () => {
    const example = state.examples[Number($("dynamic-example").value)]; if (!example) return;
    state.draft = clone(example); dirty(); renderEditor(); message("Example loaded. Run the comparison to update results.");
  });
  $("dynamic-import").addEventListener("click", () => $("dynamic-import-file").click());
  $("dynamic-import-file").addEventListener("change", async event => {
    const file = event.target.files[0]; if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error("Please import a JSON file smaller than 2 MB.");
      const data = JSON.parse(await file.text()), scenario = data.scenario || (Array.isArray(data.scenarios) ? data.scenarios[0] : data);
      state.draft = clone(validate(scenario)); dirty(); renderEditor(); showError("dynamic-run-error", ""); message(`Imported ${file.name}. Run to simulate these inputs.${data.scenarios?.length > 1 ? " Loaded the first scenario in the file." : ""}`);
    } catch (error) { showError("dynamic-run-error", `Import failed: ${error.message}`); }
    finally { event.target.value = ""; }
  });
  $("dynamic-export").addEventListener("click", () => {
    try { validate(state.draft); exportJson(state.draft, "dynamic-scenario.json", "The current editable scenario, without results."); } catch (error) { showError("dynamic-run-error", error.message); }
  });
  $("dynamic-export-run").addEventListener("click", () => {
    if (state.lastRun) exportJson({ schema_version: 1, exported_at: new Date().toISOString(), run_at: state.runTime, scenario: clone(state.submitted), results: clone(state.lastRun) }, "dynamic-run.json", "Exact inputs submitted for the last successful run, plus its results. Unsaved draft changes are excluded.");
  });
  $("dynamic-export-close").addEventListener("click", () => $("dynamic-export-dialog").close());
  $("dynamic-export-download").addEventListener("click", download);
  $("dynamic-export-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.exportText); $("dynamic-export-message").textContent = "Copied JSON."; }
    catch (_) { $("dynamic-export-json").focus(); $("dynamic-export-json").select(); $("dynamic-export-message").textContent = "Copy is unavailable here. The JSON is selected for manual copying."; }
  });
  $("dynamic-time").addEventListener("input", event => { stopPlayback(); state.hour = Number(event.target.value); renderPlayback(); });
  $("dynamic-previous").addEventListener("click", () => jumpEvent(-1)); $("dynamic-next").addEventListener("click", () => jumpEvent(1));
  $("dynamic-play").addEventListener("click", () => {
    if (!state.lastRun) return; if (state.playing) { stopPlayback(); return; }
    const h = (state.lastRun.scenario || state.submitted).horizon_hours;
    if (state.hour >= h) state.hour = 0;
    $("dynamic-play").textContent = "Pause"; $("dynamic-play").setAttribute("aria-pressed", "true"); renderPlayback();
    state.playing = setInterval(() => { state.hour = Math.min(h, state.hour + Math.min(1, h / 24)); renderPlayback(); if (state.hour >= h) stopPlayback(); }, 350);
  });
  $("dynamic-log-strategy").addEventListener("change", renderEvents);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPlayback(); });
  async function init() {
    try {
      const data = await api("/api/dynamic/examples"); state.examples = data.scenarios || []; state.limits = { ...state.limits, ...(data.limits || {}) };
      if (!state.examples.length) throw new Error("No dynamic example scenarios are available.");
      state.examples.forEach((s, index) => { const option = el("option", "", s.name); option.value = index; $("dynamic-example").append(option); });
      state.draft = clone(state.examples[0]);
      try { const saved = localStorage.getItem(storageKey); if (saved) { state.draft = clone(validate(JSON.parse(saved))); message("Restored your last draft. Run to generate results."); } } catch (_) { message("Loaded the default scenario."); }
      $("dynamic-limits").textContent = `Up to ${state.limits.max_patients} patients, ${state.limits.max_beds} beds and ${state.limits.max_facilities} of each facility type.`;
      renderEditor();
    } catch (error) { showError("dynamic-startup-error", error.message); $("dynamic-run").disabled = true; $("dynamic-quick-run").disabled = true; }
  }
  init();
})();
