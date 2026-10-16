"use strict";

const MODES = {
  workflow: {title: "Workflow timing", baseline: "Manual", ccn: "Digital", description: "Manual vs digital acceptance workflows. Unlimited beds isolate the effect of processing durations.", assumptions: ["Each patient independently uses the nearest SNF. All beds are compatible and capacity is unlimited.", "T1 is discharge readiness; T2 is acceptance confirmation. Contact, review and confirmation run sequentially.", "Travel follows T2 and is excluded from placement delay. Arrival assumes immediate departure.", "Durations are supplied assumptions. This experiment has no shared staff queues or bed turnover."]},
  allocation: {title: "Bed allocation", baseline: "Baseline", ccn: "CCN", description: "Nearest available beds vs coordinated allocation. The global solver maximizes placements first, then minimizes total travel.", assumptions: ["All patients are ready in one static snapshot. Every patient is eligible for every SNF.", "Baseline processes patients in input order. The global solver maximizes placements, then minimizes travel.", "Each placement consumes one bed; no bed is released during the experiment.", "This experiment measures travel and placement count. It does not calculate acceptance delay."]},
  clinical: {title: "Clinical matching", baseline: "Baseline", ccn: "CCN", description: "Sequential referrals vs compatibility filtering and a reservation heuristic. Match room gender and hard capability requirements.", assumptions: ["The complete patient batch and initial bed inventory are supplied. Patient input order governs decisions.", "CCN filters incompatible beds and prefers preserving a compatible option for each currently feasible remaining patient.", "Individual option checks are a reservation heuristic; they do not guarantee maximum matching for shared capacity.", "Room gender is fixed in this snapshot. All beds within the same SNF and room must agree; ‘any’ accepts either modeled gender.", "T2 equals T1 plus acceptance delay and, for baseline, rejection penalties. There are no arrival events, turnover or shared staff queues.", "If anyone is unplaced, the full-population delay objective is undefined. Wait and travel summaries describe placed patients only."]}
};
const $ = (selector) => document.querySelector(selector);
const clone = (value) => JSON.parse(JSON.stringify(value));
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const e = escapeHTML;
const human = (value) => String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const number = (value) => value == null ? "—" : new Intl.NumberFormat("en", {maximumFractionDigits:2}).format(value);
let activeMode = "allocation";
let library = {};
const workspaces = {};
let pendingImport = null;
let importing = false;
const openSections = {network:true,patients:true,beds:false,travel:true,timing:true};

function current() { return workspaces[activeMode]; }
function input(field, value, attrs = "") { return `<input data-field="${field}" value="${e(value)}" ${attrs}>`; }
function field(label, content, wide = false) { return `<label class="field${wide ? " wide" : ""}"><span>${label}</span>${content}</label>`; }
function select(fieldName, value, options, label = "") { return `<select data-field="${fieldName}" ${label ? `aria-label="${e(label)}"` : ""} required>${options.map((item) => `<option value="${e(item.value)}" ${item.value === value ? "selected" : ""}>${e(item.label)}</option>`).join("")}</select>`; }
function genderSelect(name, value, any = false) { return select(name, value, [...(any ? ["any"] : []),"male","female"].map((item) => ({value:item,label:human(item)}))); }
function hospitalSelect(value) { return select("hospital_id", value, current().draft.hospitals.map((item) => ({value:item.id,label:item.id}))); }
function snfSelect(value) { return select("snf_id", value, current().draft.snfs.map((item) => ({value:item.id,label:item.id}))); }
function section(key, title, count, content) { return `<details class="editor-section" data-section="${key}" ${openSections[key] ? "open" : ""}><summary>${title}<span class="section-count">${e(count)}</span></summary><div class="editor-content">${content}</div></details>`; }
function addButton(table, text) { return `<button type="button" class="add-button" data-action="add" data-table="${table}">+ ${text}</button>`; }
function removeButton(table, index, id) { return `<button type="button" class="icon-button" data-action="remove" data-table="${table}" data-index="${index}" aria-label="Remove ${e(id)}">×</button>`; }
function message(text) { $("#editor-message").textContent = text; }

function renderMode() {
  const config = MODES[activeMode];
  document.querySelectorAll("[data-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === activeMode)));
  $("#mode-description").textContent = config.description;
  $("#model-assumptions").innerHTML = `<ul>${config.assumptions.map((text) => `<li>${e(text)}</li>`).join("")}</ul>`;
  $("#example-select").innerHTML = library[activeMode].map((scenario, index) => `<option value="${index}">${e(human(scenario.name))}</option>`).join("");
  $("#example-select").value = String(current().exampleIndex ?? 0);
  message("");
  $(".limits").textContent = activeMode === "workflow" ? "Up to 12 hospitals, 12 SNFs and 50 patients. Bed capacity is unlimited." : "Up to 12 hospitals, 12 SNFs, 50 patients and 100 beds.";
  renderEditor(); renderResults();
}

function renderEditor() {
  const data = current().draft;
  const mode = activeMode;
  const hRows = data.hospitals.map((item, index) => `<tr data-table="hospitals" data-index="${index}"><td>${input("id",item.id,'required aria-label="Hospital ID"')}</td><td>${removeButton("hospitals",index,item.id)}</td></tr>`).join("");
  const sRows = data.snfs.map((item, index) => `<tr data-table="snfs" data-index="${index}"><td>${input("id",item.id,'required aria-label="SNF ID"')}</td>${mode === "allocation" ? `<td>${input("available_beds",item.available_beds,'type="number" min="0" max="100" step="1" required aria-label="Available beds"')}</td>` : ""}<td>${removeButton("snfs",index,item.id)}</td></tr>`).join("");
  const network = `<table class="entity-table"><thead><tr><th>Hospital ID</th><th></th></tr></thead><tbody>${hRows}</tbody></table>${addButton("hospitals","Add hospital")}<p class="small-note">Use unique IDs. Reassign patients before removing their hospital.</p><table class="entity-table"><thead><tr><th>SNF ID</th>${mode === "allocation" ? "<th>Available beds</th>" : ""}<th></th></tr></thead><tbody>${sRows}</tbody></table>${addButton("snfs","Add SNF")}${mode === "clinical" ? '<p class="small-note">Add individual beds in Bed inventory. Remove a facility’s beds before removing its SNF.</p>' : ""}`;
  const patients = data.patients.map((patient, index) => `<article class="entity-row" data-table="patients" data-index="${index}"><div class="entity-heading"><span>PATIENT ${String(index+1).padStart(2,"0")}</span><div class="row-actions"><button type="button" class="icon-button" data-action="up" data-index="${index}" aria-label="Move ${e(patient.id)} earlier" ${index===0?"disabled":""}>↑</button><button type="button" class="icon-button" data-action="down" data-index="${index}" aria-label="Move ${e(patient.id)} later" ${index===data.patients.length-1?"disabled":""}>↓</button>${removeButton("patients",index,patient.id)}</div></div><div class="entity-fields">${field("Patient ID",input("id",patient.id,"required"))}${field("Hospital",hospitalSelect(patient.hospital_id))}${mode !== "allocation" ? field("Ready at · minutes",input("ready_time_minutes",patient.ready_time_minutes,'type="number" min="0" max="1000000000" step="any" required')) : ""}${mode === "clinical" ? field("Gender",genderSelect("gender",patient.gender)) + field("Required capabilities · comma separated",input("required_capabilities",(patient.required_capabilities || []).join(", "),'placeholder="e.g. bariatric, iv"'),true) : ""}</div></article>`).join("") || '<p class="empty-editor">No patients yet. Add a patient to build the scenario.</p>';
  const travel = `<p class="small-note">One-way travel in minutes. Enter every hospital-to-SNF pair; these are supplied times, not geographic distances.</p><div class="matrix-wrap"><table class="matrix"><thead><tr><th>From / to</th>${data.snfs.map((snf)=>`<th>${e(snf.id)}</th>`).join("")}</tr></thead><tbody>${data.hospitals.map((hospital,h)=>`<tr><th>${e(hospital.id)}</th>${data.snfs.map((snf,s)=>`<td><input type="number" min="0" max="1000000000" step="any" required data-travel-h="${h}" data-travel-s="${s}" value="${e(data.travel_time_minutes[hospital.id]?.[snf.id] ?? "")}" aria-label="Travel from ${e(hospital.id)} to ${e(snf.id)} in minutes"></td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  let beds = "";
  if (mode === "clinical") beds = data.beds.map((bed,index)=>`<article class="entity-row" data-table="beds" data-index="${index}"><div class="entity-heading"><span>BED ${String(index+1).padStart(2,"0")}</span>${removeButton("beds",index,bed.id)}</div><div class="entity-fields">${field("Bed ID",input("id",bed.id,"required"))}${field("SNF",snfSelect(bed.snf_id))}${field("Room ID",input("room_id",bed.room_id,"required"))}${field("Room gender",genderSelect("room_gender",bed.room_gender,true))}${field("Capabilities · comma separated",input("capabilities",(bed.capabilities || []).join(", "),'placeholder="e.g. bariatric, iv"'),true)}</div></article>`).join("") + addButton("beds","Add bed");
  let timing = "";
  if (mode === "workflow") timing = `<div class="workflow-grid">${["manual","digital"].map((key)=>`<div><h3 class="${key === "manual"?"baseline":"ccn"}-text">${human(key)} workflow</h3>${["contact_minutes","review_minutes","confirmation_minutes"].map((name)=>field(human(name.replace("_minutes",""))+" · min",`<input type="number" min="0" max="1000000000" step="any" required data-workflow="${key}" data-parameter="${name}" value="${e(data.workflows[key][name])}">`)).join("")}</div>`).join("")}</div>`;
  if (mode === "clinical") timing = `<div class="entity-fields">${field("Rejection delay · minutes",`<input data-global="rejection_delay_minutes" type="number" min="0" max="1000000000" step="any" required value="${e(data.rejection_delay_minutes ?? 120)}">`)}${field("Acceptance delay · minutes",`<input data-global="acceptance_delay_minutes" type="number" min="0" max="1000000000" step="any" required value="${e(data.acceptance_delay_minutes ?? 0)}">`)}</div><p class="small-note">Acceptance delay applies to both strategies. Each incompatible baseline referral adds one rejection delay.</p>`;
  $("#scenario-editor").innerHTML = `<label class="field scenario-name"><span>Scenario name</span><input data-name value="${e(data.name)}" required maxlength="200"></label>${section("network","Hospitals & SNFs",`${data.hospitals.length} / ${data.snfs.length}`,network)}${section("patients","Patient batch",`${data.patients.length} patients`,patients+addButton("patients","Add patient")+'<p class="small-note">Arrows change processing order. Baseline allocation and both clinical strategies use this order; global allocation considers the batch jointly.</p>')}${mode === "clinical" ? section("beds","Bed inventory",`${data.beds.length} beds`,beds) : ""}${section("travel","Travel times","minutes",travel)}${mode !== "allocation" ? section("timing","Processing times","minutes",timing) : ""}`;
  $("#scenario-count").textContent = `${data.patients.length} patients · ${data.snfs.length} SNFs`;
  $("#scenario-editor").querySelectorAll("details").forEach((detail)=>detail.addEventListener("toggle",()=>{openSections[detail.dataset.section]=detail.open;}));
}

function changed() {
  current().dirty = true; current().error = ""; message(""); renderResults();
}
function rename(table, oldId, newId) {
  const data = current().draft;
  if (table === "hospitals") {
    data.patients.forEach((patient)=>{if(patient.hospital_id===oldId) patient.hospital_id=newId;});
    const travel = Object.fromEntries(Object.entries(data.travel_time_minutes).map(([key,value])=>[key===oldId?newId:key,value]));
    data.travel_time_minutes=travel;
  }
  if (table === "snfs") {
    (data.beds || []).forEach((bed)=>{if(bed.snf_id===oldId) bed.snf_id=newId;});
    for (const key of Object.keys(data.travel_time_minutes)) data.travel_time_minutes[key]=Object.fromEntries(Object.entries(data.travel_time_minutes[key]).map(([id,value])=>[id===oldId?newId:id,value]));
  }
}
$("#scenario-editor").addEventListener("input",updateDraft);
$("#scenario-editor").addEventListener("change",updateDraft);
$("#scenario-editor").addEventListener("focusout",(event)=>{
  if(event.target.dataset.field==="id" && ["hospitals","snfs"].includes(event.target.closest("[data-table]")?.dataset.table))updateDraft(event);
});
function updateDraft(event) {
  const target=event.target, data=current().draft;
  if (target.hasAttribute("data-name")) data.name=target.value;
  else if (target.hasAttribute("data-travel-h")) {
    const h=data.hospitals[Number(target.dataset.travelH)].id,s=data.snfs[Number(target.dataset.travelS)].id;
    data.travel_time_minutes[h][s]=target.value === "" ? null : Number(target.value);
  } else if (target.dataset.workflow) data.workflows[target.dataset.workflow][target.dataset.parameter]=target.value === "" ? null : Number(target.value);
  else if (target.dataset.global) data[target.dataset.global]=target.value === "" ? null : Number(target.value);
  else if (target.dataset.field) {
    const row=target.closest("[data-table]"),table=row.dataset.table,index=Number(row.dataset.index),name=target.dataset.field;
    let value=target.value;
    if (["required_capabilities","capabilities"].includes(name)) value=value.split(",").map((item)=>item.trim()).filter(Boolean);
    if (["available_beds","ready_time_minutes"].includes(name)) value=value===""?null:Number(value);
    if (name === "id" && ["hospitals","snfs"].includes(table)) {
      if(event.type==="input"){changed();return;}
      if (!String(value).trim() || data[table].some((item,i)=>i!==index && item.id===value)) { target.value=data[table][index].id; message("Facility IDs must be unique and nonempty."); return; }
      if(value===data[table][index].id)return;
      rename(table,data[table][index].id,value);
    }
    data[table][index][name]=value;
  }
  changed();
  if (target.dataset.field === "id" && ["hospitals","snfs"].includes(target.closest("[data-table]")?.dataset.table)) refreshFacilityViews();
}


function refreshFacilityViews() {
  const data=current().draft,editor=$("#scenario-editor");
  for(const [fieldName,table,entity] of [["hospital_id","hospitals","patients"],["snf_id","snfs","beds"]]){
    editor.querySelectorAll(`select[data-field="${fieldName}"]`).forEach((control)=>{
      const index=Number(control.closest("[data-index]").dataset.index),value=data[entity][index][fieldName];
      control.innerHTML=data[table].map((item)=>`<option value="${e(item.id)}" ${item.id===value?"selected":""}>${e(item.id)}</option>`).join("");
    });
  }
  for(const table of ["hospitals","snfs"])editor.querySelectorAll(`button[data-action="remove"][data-table="${table}"]`).forEach((button)=>button.setAttribute("aria-label",`Remove ${data[table][Number(button.dataset.index)].id}`));
  editor.querySelectorAll(".matrix thead th").forEach((cell,index)=>{if(index>0)cell.textContent=data.snfs[index-1].id;});
  editor.querySelectorAll(".matrix tbody tr").forEach((row,index)=>{row.querySelector("th").textContent=data.hospitals[index].id;});
  editor.querySelectorAll("input[data-travel-h]").forEach((control)=>{const h=data.hospitals[Number(control.dataset.travelH)].id,s=data.snfs[Number(control.dataset.travelS)].id;control.setAttribute("aria-label",`Travel from ${h} to ${s} in minutes`);control.value=data.travel_time_minutes[h][s]??"";});
}

function newId(prefix, items) {let index=items.length+1;while(items.some((item)=>item.id===`${prefix}_${index}`))index++;return `${prefix}_${index}`;}
$("#scenario-editor").addEventListener("click",(event)=>{
  const button=event.target.closest("button[data-action]");if(!button)return;
  const data=current().draft,{action,table}=button.dataset,index=Number(button.dataset.index);
  if (action === "add") {
    if (table === "hospitals") {
      if(data.hospitals.length>=12){message("This lab supports up to 12 hospitals.");return;}
      const id=newId("hospital",data.hospitals);data.hospitals.push({id});data.travel_time_minutes[id]=Object.fromEntries(data.snfs.map((s)=>[s.id,10]));
    } else if (table === "snfs") {
      if(data.snfs.length>=12){message("This lab supports up to 12 SNFs.");return;}
      const id=newId("snf",data.snfs);data.snfs.push(activeMode==="allocation"?{id,available_beds:1}:{id});for(const h of data.hospitals)data.travel_time_minutes[h.id][id]=10;
    } else if (table === "patients") {
      if(!data.hospitals.length){message("Add a hospital before adding a patient.");return;}
      if(data.patients.length>=50){message("This lab supports up to 50 patients.");return;}
      const patient={id:newId("patient",data.patients),hospital_id:data.hospitals[0].id};
      if(activeMode!=="allocation")patient.ready_time_minutes=0;
      if(activeMode==="clinical"){patient.gender="male";patient.required_capabilities=[];}
      data.patients.push(patient);
    } else if (table === "beds") {
      if(!data.snfs.length){message("Add an SNF before adding a bed.");return;}
      if(data.beds.length>=100){message("This lab supports up to 100 beds.");return;}
      const id=newId("bed",data.beds);data.beds.push({id,snf_id:data.snfs[0].id,room_id:id,room_gender:"any",capabilities:[]});
    }
  } else if (action === "remove") {
    const id=data[table][index].id;
    if(table==="hospitals" && data.patients.some((p)=>p.hospital_id===id)){message("Reassign or remove this hospital’s patients first.");return;}
    if(table==="snfs" && (data.beds||[]).some((b)=>b.snf_id===id)){message("Reassign or remove this SNF’s beds first.");return;}
    data[table].splice(index,1);
    if(table==="hospitals")delete data.travel_time_minutes[id];
    if(table==="snfs")for(const row of Object.values(data.travel_time_minutes))delete row[id];
  } else {
    const next=action==="up"?index-1:index+1;
    [data.patients[index],data.patients[next]]=[data.patients[next],data.patients[index]];
  }
  changed();renderEditor();
});

function metric(title, left, right, unit, foot) {
  const config=MODES[activeMode];
  return `<article class="metric"><h4>${title}</h4><div class="metric-pair"><span><i class="baseline-dot"></i>${config.baseline}</span><strong>${number(left)}${left==null?"":` <small>${unit}</small>`}</strong></div><div class="metric-pair"><span><i class="ccn-dot"></i>${config.ccn}</span><strong>${number(right)}${right==null?"":` <small>${unit}</small>`}</strong></div><p class="metric-foot">${e(foot)}</p></article>`;
}
function short(value) {return String(value).length>15?String(value).slice(0,14)+"…":String(value);}
function flow(data,result,isCCN) {
  const hospitals=data.hospitals,snfs=data.snfs,count=Math.max(hospitals.length,snfs.length,2),height=42+count*55;
  const y=(index,length)=>36+(index+.5)*(height-55)/Math.max(length,1);
  const links=new Map();for(const row of result.assignments){const key=JSON.stringify([row.hospital_id,row.snf_id]);links.set(key,(links.get(key)||0)+1);}
  const edges=[...links].map(([key,n])=>{const [h,s]=JSON.parse(key),a=hospitals.findIndex((item)=>item.id===h),b=snfs.findIndex((item)=>item.id===s),y1=y(a,hospitals.length),y2=y(b,snfs.length);return `<path class="flow-link" stroke-width="${1.5+Math.min(n,8)*1.3}" d="M 114 ${y1} C 170 ${y1}, 210 ${y2}, 266 ${y2}"/><text class="flow-quantity" x="190" y="${(y1+y2)/2-5}" text-anchor="middle">${n}</text>`;}).join("");
  const nodes=(items,x,isSNF)=>items.map((item,index)=>{const yy=y(index,items.length);return `<g><title>${e(item.id)}</title><rect class="flow-node${isSNF?" snf":""}" x="${x}" y="${yy-16}" rx="5" width="108" height="32"/><text class="flow-label" x="${x+54}" y="${yy+4}" text-anchor="middle">${e(short(item.id))}</text></g>`;}).join("");
  return `<svg class="flow-svg${isCCN?" ccn-flow":""}" viewBox="0 0 380 ${height}" role="img" aria-label="${e(result.label)}: ${result.placed_count} patients placed. Assignment counts from hospitals to SNFs."><text class="flow-caption" x="60" y="13" text-anchor="middle">HOSPITALS</text><text class="flow-caption" x="320" y="13" text-anchor="middle">SNFs</text>${edges}${nodes(hospitals,6,false)}${nodes(snfs,266,true)}</svg>`;
}
function assignmentCell(assignment, mode) {
  if(!assignment)return '<span class="unplaced">Unplaced</span>';
  const detail=[];
  if(assignment.bed_id)detail.push(assignment.bed_id);
  if(mode!=="allocation")detail.push(`wait ${number(assignment.placement_delay_minutes)} min`);
  detail.push(`travel ${number(assignment.travel_time_minutes)} min`);
  if(assignment.rejection_count>0)detail.push(`${assignment.rejection_count} rejection${assignment.rejection_count===1?"":"s"}`);
  return `${e(assignment.snf_id)}<span class="assignment-detail">${detail.map(e).join(" · ")}</span>`;
}
function timelines(result) {
  const rows=result.scenario.patients;
  const a=new Map(result.baseline.assignments.map((x)=>[x.patient_id,x])),b=new Map(result.ccn.assignments.map((x)=>[x.patient_id,x]));
  const max=Math.max(1,...[...a.values(),...b.values()].map((x)=>x.t2_minutes));
  const lane=(assignment,isCCN)=>assignment?`<div class="timeline-lane" aria-label="${isCCN?"CCN approach":"Baseline"} T1 ${number(assignment.t1_minutes)}, T2 ${number(assignment.t2_minutes)} minutes"><span class="timeline-bar ${isCCN?"ccn":""}" style="left:${assignment.t1_minutes/max*100}%;width:${Math.max(0,(assignment.t2_minutes-assignment.t1_minutes)/max*100)}%"></span><span class="timeline-label">T1 ${number(assignment.t1_minutes)} → T2 ${number(assignment.t2_minutes)}</span></div>`:'<div class="timeline-lane"><span class="timeline-label unplaced">Unplaced</span></div>';
  return `<div class="results-section-heading"><h3>Acceptance timeline</h3><span>minutes from scenario origin</span></div><div class="timeline-key"><i class="baseline-dot"></i>${MODES[activeMode].baseline}<i class="ccn-dot"></i>${MODES[activeMode].ccn}</div><div class="timelines">${rows.map((p)=>`<div class="timeline-row"><span class="timeline-id">${e(p.id)}</span><div>${lane(a.get(p.id),false)}${lane(b.get(p.id),true)}</div></div>`).join("")}</div><p class="small-note">Shared scale: 0–${number(max)} min. Bars span readiness (T1) to acceptance (T2); travel follows acceptance.</p>`;
}
function renderResults() {
  const state=current(),config=MODES[activeMode];
  $("#run-error").hidden=!state.error;$("#run-error").textContent=state.error||"";
  $("#quick-run").disabled=!!state.busy;
  $("#run-button").disabled=!!state.busy;$("#run-button span").textContent=state.busy?"Running…":"Run comparison";
  const status=$("#result-status");status.className="result-status";
  $("#stale-message").hidden=!state.result||!state.dirty;
  $("#export-results").hidden=!state.result;
  if(!state.result){status.textContent=state.busy?"Running":"Ready to run";$("#results").innerHTML=`<div class="empty-state"><span class="empty-glyph" aria-hidden="true">⇄</span><h3>A shared scenario.<br>Two ways to place.</h3><p>Start with an example or edit the inputs,<br>then run a comparison to see the difference.</p><div class="empty-legend"><span class="baseline-dot"></span>${config.baseline}<span class="ccn-dot"></span>${config.ccn}</div></div>`;return;}
  status.textContent=state.busy?"Running":state.dirty?"Inputs changed":"Run complete";status.classList.add(state.dirty?"stale":"complete");
  const result=state.result,a=result.baseline,b=result.ccn,c=result.comparison,data=result.scenario;
  const travelFoot=c.travel_reduction_minutes==null?"Placed patients differ; compare travel with care.":c.travel_reduction_minutes===0?"Same total travel.":`${number(Math.abs(c.travel_reduction_minutes))} min ${c.travel_reduction_minutes>0?"less":"more"} with ${config.ccn}.`;
  const delayFoot=activeMode==="allocation"?"Acceptance delay is outside this experiment.":(a.unplaced_count||b.unplaced_count)?"Among placed patients; some patients remain unplaced.":"Readiness to acceptance; excludes travel.";
  const aMap=new Map(a.assignments.map((x)=>[x.patient_id,x])),bMap=new Map(b.assignments.map((x)=>[x.patient_id,x]));
  $("#results").innerHTML=`<div class="run-heading"><div><h3>${e(human(data.name))}</h3><p>${data.hospitals.length} hospitals · ${data.snfs.length} SNFs · ${data.patients.length} patients</p></div><span class="run-label">${e(config.title)}</span></div><div class="metric-grid">${metric("Patients placed",a.placed_count,b.placed_count,`/ ${data.patients.length}`,c.placement_gain===0?"Same placement count.":`${Math.abs(c.placement_gain)} ${c.placement_gain>0?"more":"fewer"} placed with ${config.ccn}.`)}${metric("Mean acceptance wait",a.mean_placement_delay_minutes,b.mean_placement_delay_minutes,"min",delayFoot)}${metric("Total travel · placed patients",a.total_travel_time_minutes,b.total_travel_time_minutes,"min",travelFoot)}</div>${a.unplaced_count||b.unplaced_count?`<p class="comparison-note">${e(config.baseline)}: ${a.unplaced_count} unplaced. ${e(config.ccn)}: ${b.unplaced_count} unplaced. ${activeMode==="clinical"?"The full-population delay objective is undefined for a strategy with unplaced patients.":""}</p>`:""}<div class="results-section-heading"><h3>Assignment flow</h3><span>Schematic · labels count patients</span></div><div class="flow-grid"><div><h4 class="strategy-heading"><i class="baseline-dot"></i>${config.baseline}</h4><p class="strategy-name">${e(a.label)}</p>${flow(data,a,false)}<p class="flow-summary">Unplaced: ${a.unplaced_patient_ids.length?a.unplaced_patient_ids.map(e).join(", "):"none"}</p></div><div><h4 class="strategy-heading"><i class="ccn-dot"></i>${config.ccn}</h4><p class="strategy-name">${e(b.label)}</p>${flow(data,b,true)}<p class="flow-summary">Unplaced: ${b.unplaced_patient_ids.length?b.unplaced_patient_ids.map(e).join(", "):"none"}</p></div></div><div class="results-section-heading"><h3>Patient-level decisions</h3><span>Input order</span></div><div class="result-table-wrap"><table class="result-table"><thead><tr><th>Patient / hospital</th><th>${config.baseline}</th><th>${config.ccn}</th></tr></thead><tbody>${data.patients.map((p)=>`<tr><td>${e(p.id)}<span class="assignment-detail">${e(p.hospital_id)}</span></td><td>${assignmentCell(aMap.get(p.id),activeMode)}</td><td>${assignmentCell(bMap.get(p.id),activeMode)}</td></tr>`).join("")||'<tr><td colspan="3">No patients in this scenario.</td></tr>'}</tbody></table></div>${activeMode!=="allocation"?timelines(result):""}`;
}

$("#scenario-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if($("#scenario-editor").contains(document.activeElement)){const focused=document.activeElement;updateDraft({target:focused});focused.blur();}
  const mode=activeMode,state=current(),snapshot=clone(state.draft);state.busy=true;state.error="";message("");renderResults();
  try {
    const response=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode,scenario:snapshot})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||"The comparison could not be completed.");
    state.result=result;state.dirty=JSON.stringify(state.draft)!==JSON.stringify(snapshot);
  } catch(error){state.error=error.message||"The local server could not be reached.";}
  finally {state.busy=false;if(activeMode===mode)renderResults();}
});
document.querySelectorAll("[data-mode]").forEach((button)=>button.addEventListener("click",()=>{if(!workspaces[button.dataset.mode])return;activeMode=button.dataset.mode;renderMode();}));
$("#load-example").addEventListener("click",()=>{const index=Number($("#example-select").value);current().draft=clone(library[activeMode][index]);current().exampleIndex=index;changed();renderEditor();message("Example loaded. Run comparison to update the results.");});
$("#assumption-toggle").addEventListener("click",()=>{const expanded=$("#assumption-toggle").getAttribute("aria-expanded")==="true";$("#assumption-toggle").setAttribute("aria-expanded",String(!expanded));$("#model-assumptions").hidden=expanded;});
function download(name,data) {const blob=new Blob([JSON.stringify(data,null,2)+"\n"],{type:"application/json"});const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
let exportPayload = null;
function exportJSON(name,data,title) {
  exportPayload={name,data};$("#export-title").textContent=title;$("#export-json").value=JSON.stringify(data,null,2)+"\n";$("#export-message").textContent="";$("#export-dialog").showModal();
}
$("#close-export").addEventListener("click",()=>$("#export-dialog").close());
$("#download-export").addEventListener("click",()=>download(exportPayload.name,exportPayload.data));
$("#copy-export").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("#export-json").value);$("#export-message").textContent="JSON copied.";}catch{$("#export-json").focus();$("#export-json").select();$("#export-message").textContent="Select and copy the JSON above.";}});
const filename=(name)=>String(name).replace(/[^a-zA-Z0-9_-]+/g,"-").slice(0,100)||"scenario";
$("#export-scenario").addEventListener("click",()=>exportJSON(`${filename(current().draft.name)}.json`,{schema_version:1,mode:activeMode,scenarios:[clone(current().draft)]},"Export scenario"));
$("#export-results").addEventListener("click",()=>exportJSON(`${filename(current().result.scenario.name)}-results.json`,{schema_version:1,...current().result},"Export run & results"));
$("#import-button").addEventListener("click",()=>$("#import-file").click());
function inferMode(scenario) {return scenario.workflows?"workflow":scenario.beds?"clinical":"allocation";}
async function importScenario(scenario,mode) {
  if(importing)throw new Error("Another import is still running.");
  importing=true;$("#import-button").disabled=true;
  try {
  // Validate with the same model before replacing an editable workspace.
  const response=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode,scenario})});
  const result=await response.json();if(!response.ok)throw new Error(result.error||"Invalid scenario.");
  activeMode=mode;workspaces[mode]={draft:clone(scenario),result,dirty:false,error:"",exampleIndex:0};renderMode();message("Scenario imported and comparison completed.");
  } finally {importing=false;$("#import-button").disabled=false;}
}
$("#import-file").addEventListener("change",async(event)=>{
  const file=event.target.files[0];if(!file)return;
  try {
    if(file.size>1048576)throw new Error("Choose a JSON file smaller than 1 MiB.");
    const doc=JSON.parse(await file.text()),scenarios=doc.scenarios||[doc.scenario||doc];
    if(!Array.isArray(scenarios)||!scenarios.length)throw new Error("The file needs at least one scenario.");
    const mode=doc.mode;
    if(mode && !MODES[mode])throw new Error("Unknown experiment mode.");
    if(scenarios.length===1)await importScenario(scenarios[0],mode||inferMode(scenarios[0]));
    else {pendingImport={scenarios,mode};$("#import-choice").innerHTML=scenarios.map((s,i)=>`<option value="${i}">${e(s.name||`Scenario ${i+1}`)}</option>`).join("");$("#import-dialog").returnValue="";$("#import-dialog").showModal();}
  } catch(error){current().error=`Import: ${error.message}`;renderResults();}
  finally {event.target.value="";}
});
$("#import-dialog").addEventListener("close",async()=>{
  const selected=pendingImport;pendingImport=null;
  if($("#import-dialog").returnValue!=="load"||!selected)return;
  try{const scenario=selected.scenarios[Number($("#import-choice").value)];await importScenario(scenario,selected.mode||inferMode(scenario));}catch(error){current().error=`Import: ${error.message}`;renderResults();}
});

const startupControls = ["#quick-run","#run-button","#load-example","#import-button","#export-scenario","#example-select"];
startupControls.forEach((id)=>{$(id).disabled=true;});
async function start() {
  try {
    const response=await fetch("/api/examples");if(!response.ok)throw new Error("The examples could not be loaded.");library=await response.json();
    for(const mode of Object.keys(MODES)){const index=mode==="allocation"?1:mode==="workflow"?1:0;workspaces[mode]={draft:clone(library[mode][index]),exampleIndex:index,dirty:true,error:"",result:null};}
    renderMode();
    startupControls.forEach((id)=>{$(id).disabled=false;});
    $("#scenario-form").requestSubmit();
  } catch(error){$("#startup-error").hidden=false;$("#startup-error").textContent=`Could not connect to the local model server. ${error.message}`;$("#run-button").disabled=true;}
}
start();
