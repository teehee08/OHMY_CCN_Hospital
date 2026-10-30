# OHMY Hospital CCN

This project studies regional hospital-to-skilled nursing facility (SNF) placement coordination in New York City. A Capacity Command Network (CCN) shares regional demand and capacity information to support joint placement decisions.

The implementation provides **Phase 1.1 acceptance workflow timing**, **Phase 1.2 static allocation**, **Phase 2 clinical attribute matching**, and an **hourly dynamic placement simulation**. Synthetic examples make their assumptions and outcomes easy to inspect. The experiments use Python 3.10+ and the standard library.

## Run the experiments and report

### Interactive scenario lab

Get the latest `main` branch from GitHub. Either choose **Code → Download ZIP**
and extract the entire archive, or clone the repository:

```powershell
git clone https://github.com/teehee08/OHMY_CCN_Hospital.git
cd OHMY_CCN_Hospital
```

Install Python 3.10 or newer and make its `python` command available in your
terminal. Check the installation with `python --version`. The lab uses the
standard library, so the interface is ready to run after Python is installed.

Start the local browser interface with Python 3.10+:

```powershell
python scenario_lab.py --open
```

On Windows, double-click `Start Scenario Lab.cmd`. Keep its terminal open while
using the lab. The default address is `http://127.0.0.1:8765/`; use `--port 8766`
to choose another port. Stop the server with Ctrl+C.

On macOS or Linux, run `python3 scenario_lab.py --open` from the extracted project
directory. If the browser does not open automatically, visit the address printed
in the terminal. The server runs on the same computer as the browser. Start it
through the launcher or Python command so the page can communicate with the models.

The English interface provides three experiments using the existing Python models:

| Experiment | Baseline | CCN approach | Editable inputs |
| --- | --- | --- | --- |
| Workflow timing | Manual workflow | Digital workflow | Hospitals, SNFs, patients, readiness, travel and processing durations |
| Bed allocation | Greedy nearest available | Exact global allocation | Hospitals, SNFs, capacities, patients, order and travel |
| Clinical matching | Sequential referral | Compatibility filtering and reservation heuristic | Hospitals, SNFs, individual beds, room gender, capabilities, patients, order and rejection/acceptance delays |

Load a bundled example or create your own scenario, then select **Run comparison**.
Results include placed counts, wait and travel summaries, assignment flow diagrams,
patient decisions, and acceptance timelines for the timing experiments. The flow
diagrams are schematic, and travel times are supplied inputs. The allocation
experiment reports acceptance wait as unavailable because it does not model it.

Each experiment retains its inputs while switching experiment tabs on the page.
Reloading the page restores the bundled examples; export custom scenarios to
retain them between sessions. Editing inputs marks
previous results as out of date until another run completes. **Export scenario**
saves a versioned JSON input that can be imported again or used by the corresponding
experiment runner. **Export run & results** includes the exact last-run inputs,
both results, comparison values and model assumptions. Imports can also load a
scenario from a bundled multi-scenario JSON file.

The lab supports up to 12 hospitals, 12 SNFs, 50 patients and 100 finite beds per
scenario. Workflow timing retains unlimited capacity. Mean wait and total travel
describe placed patients; travel reductions are shown only when both strategies
place the same patient set. For clinical matching, the full-population delay
objective is undefined if anyone is unplaced. The interface compares the three
existing experiments separately.

The local server and browser assets use the Python standard library and native
browser features. No additional packages or build step are needed.

### Dynamic placement: Patient Flow Studio

Start the same server and open **Dynamic placement**, or visit
`http://127.0.0.1:8765/dynamic`. **Run comparison** creates synchronized
hospital–patient–SNF scenes for hospital-local planning and all-hospital CCN.
Play, pause, change speed, scrub hours, or step between events. Patient icons
move into named SNF beds at actual admission and beds empty at release. Click a
patient or bed to inspect both strategies; filter by hospital or find a patient.
Movement is event playback and adds no simulated transport time.

**Configure scenario** opens the workbench. Edit facilities, accepted insurance,
bed gender/care capabilities, patient insurance/required care, ready times,
stays, and ordered eligible SNFs. Every **Run comparison** draws a fresh request
tie-break seed without regenerating patients or beds. The separate **Optimizer
seed** controls CCN search and stays fixed unless edited. The completed-run heading
and JSON export record both actual seeds; playback/reset retain that run. The API
and command-line model still honor an explicitly supplied seed for reproducible
runs. Controlled-experiment labels match patient/resource inputs independently
of the request and optimizer seeds. The observation bounds are
automatic: earliest supplied ready time through the later final SNF release
of the two strategies. Editors and detailed
tables use pagination for larger cohorts. Import/export draft inputs or export
exact completed-run inputs with results. Draft edits mark the saved result
stale until recalculated. Drafts use a new local storage key so an old
five-patient draft does not replace the larger initial example.

| Bundled input | Hospitals | Patients | SNFs | Beds | Arrival-generation span, h |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default one-week cohort | 4 | 96 | 6 | 24 | 168 |
| Larger two-week cohort | 8 | 240 | 12 | 72 | 336 |
| Legacy FIFO reference | 2 | 5 | 3 | 5 | 72 |

The generator supports up to **500 patients, 500 beds, 40 hospitals, and 40 SNFs**.
Counts, arrival-generation span, and random seed are configurable. Seeds reproduce
inputs, not an assumed benefit. Generated patients have at least one compatible
bed if any beds exist; they can still wait because that bed is occupied. Empty
beds or incompatible custom inputs are also supported. Insurance plans
(`Plan_A`, `Plan_B`, `Plan_C`) and care tags are synthetic scenario labels, not
measured provider contracts or clinical staffing data.

#### Scheduling rules

- **Hospital-local baseline** knows its own current patients and future ready
  times/stays, and shared actual bed occupancy and release times. It plans to
  minimize its own eventual total waiting. It does not see other hospitals'
  future patient demand. Only actual accepted requests commit capacity; other
  hospitals can invalidate tentative future plans. Waiting patients carry their
  original ready time forward into replanning. The planner retains its original
  six patient-order constructions and tries capacity-preserving bed ties for
  those same orders. Total waiting remains the primary objective; equal-wait
  plans prefer fewer unused care-capability bed-hours, then fewer flexible-gender
  bed-hours. SNF list order resolves remaining ties. These secondary rules use
  physical bed attributes and the hospital's own patients, never another
  hospital's future demand. They cannot worsen the best original construction
  for a fixed local subproblem, but the resulting regional competition can
  still have a better or worse outcome.
- **CCN** knows all supplied future demand and jointly schedules compatible bed
  intervals. It searches independently of the local-baseline schedule, then
  selects the better global result or local-baseline fallback. Either policy may deliberately defer a
  ready patient if that reduces the objective for its visible cohort.
- **SNF first-come, first-served** applies to qualified submitted requests.
  Hospitals choose when and in which planned order to submit requests.
  Equal-hour hospital ties use the scenario's seeded order. An earlier request
  can use another compatible free bed in its requested SNF; rejected hospitals
  replan at the same hour. Events expose request order and accepted/rejected
  state. No artificial retry duration is added.
- **Same hard constraints:** eligible SNF, bed gender, accepted insurance, and
  every required care capability must match for both policies. `*` accepts any
  insurance. An empty accepted-insurance list rejects everyone; an empty
  required-care list adds no care restriction.
- All SNF beds start empty. Admission, confirmation, and hospital departure are
  simultaneous. Referral, authorization, transport, and cleaning durations are
  zero. A bed released at hour 12 can be reused at hour 12. Stays begin at actual
  admission, and future plans never count as actual occupancy.

Hospital-local planning uses six patient orders with two bed tie rules, for up
to twelve constructions per subproblem. Small local problems additionally use
bounded lexicographic search. CCN starts with six global orders, then tries
seeded multistart orders, mutations of improving orders, and alternative bed
ties. Its exploration threshold and mutation parents never use the baseline;
the baseline is compared only after independent search finishes. A better
baseline can therefore improve the final fallback without blocking global
candidate exploration. Larger global cases use a fixed, input-size-dependent work budget (up to
1,000 candidates), rather than a machine-speed-dependent stopping time. Solver
metadata reports the search seed, candidate count, initial and final objectives,
and a lower bound, plus the independent search objective and whether a baseline
fallback was selected. For the default 96-person input with request seed 42
and optimizer seed 20261029, the strengthened local baseline is 871 total
wait-hours and CCN is 574 hours. The prior local baseline on that exact cohort
was 1,029 hours. Browser runs draw new request seeds and
can produce different results. Total waiting also equals complete-cohort blocked hospital bed-hours.
The latter is a feasible solution,
not a certified optimum. Every final plan is checked for completeness,
compatibility, readiness, stay length, and non-overlapping bed intervals.

Subproblems with at most eight compatible patients retain bounded
branch-and-bound. Local exact calls share a 30,000-node / two-second total budget;
global exact search uses 30,000 nodes / two seconds. **Heuristic** and
**search-limited** results do not claim exact optimality. A feasible zero-wait
plan, together with the nonnegative-waiting lower bound, proves optimality at
any size; a lower bound of zero alone does not. Baseline metadata describes hospital-local
subproblems, not a global optimum of the decentralized outcome. Wall-time
exhaustion in bounded exact calls can affect the best plan found; preserve
exported results when comparing a particular completed run.

#### Controlled load and competition experiments

In **Configure scenario → Controlled experiment**, choose a case, load its
inputs, then run the comparison. These cases use a separate frozen-cohort
factory; changing the general-purpose generator's bed count also changes its
patients and is therefore not a controlled capacity experiment.

- **Light / medium / heavy:** the same 96 patients, four hospitals, six SNFs,
  168-hour ready-generation span, insurance, clinical needs, arrivals, and stays; only the
  multiplicity of each of 12 clinical bed types changes, giving 48 / 24 / 12 beds.
  Every patient retains a compatible bed at every capacity level.
- **Scarce-bed competition:** the same physical demand, SNFs and beds are paired
  with demand concentrated in one hospital or spread over four hospitals. Only
  patient hospital ownership changes. Flexible patients can use ordinary beds;
  restricted patients need a shared bariatric bed. Insurance and care remain
  hard constraints.

Offered load is `sum(all SNF stays) / (bed count × arrival-generation span)`; it is a
nominal demand-pressure measure, not observed utilization or a simulation cutoff.
Full-cohort waiting, completion duration, 90th
percentile and maximum wait, and the worst hospital's mean wait help expose
tradeoffs when minimizing the total.

The standard batch freezes three cohorts (42, 84, 126), crosses each with twenty
request seeds (0–19), and fixes optimizer seed 20261029: **300 paired runs** over
the five experiments. No favorable draws are selected. Run the standard batch:

```powershell
python run_dynamic_experiments.py --compact --output dynamic_results.json --csv dynamic_summary.csv
```

Use `--cohort-seeds`, `--request-seeds` and `--optimizer-seeds` to declare each
random stream separately. Legacy `--seeds` aliases cohort seeds only. For example:

```powershell
python run_dynamic_experiments.py --cohort-seeds 42 84 126 --request-seeds 0 1 2 3 4 --optimizer-seeds 20261029 --experiment-ids load_medium --compact --output medium.json
python run_dynamic_experiments.py --cohort-seeds 42 84 126 --request-seeds 0 1 2 3 4 --experiment-ids competition_within competition_cross --preference-variants supplied ordinary_first shuffled --preference-seed 0 --compact --output preferences.json
```

Preference sensitivity changes only ordered SNF lists. It preserves feasible bed
sets, all patients' clinical/time attributes, and physical capacity. The original
scarce-bed example is retained even when a stronger baseline eliminates its gain.

JSON preserves frozen input templates, exact random seeds, patient plans, source
hashes, independent audits, all fairness results and nested aggregates. `--compact`
omits event/snapshot playback and duplicated per-run scenarios; the template and
seed fields reconstruct each input exactly. Omit it to retain complete playback.
CSV contains one row per cohort/request/optimizer/case combination. All
seeds and zero savings are retained. A lower total-wait objective does not
guarantee earlier cohort completion or improvement for each patient/hospital.

Use `nested_aggregate` for uncertainty: request draws are first summarized within
each frozen cohort, optimizer seeds and preference variants remain separate,
and a predeclared bootstrap resamples equally weighted cohort means. It never
treats twenty requests from one cohort as twenty independent patient cohorts.
Three cohorts give unstable nominal 95% intervals, explicitly flagged in output;
one cohort has no between-cohort interval. Legacy `aggregate` is descriptive only.

The within-hospital control matters: CCN also receives more solver effort than
the local baseline, so its measured advantage includes search quality as well
as information and coordination. These experiments do not identify a pure
causal value of information. Parameters are synthetic mechanism/stress tests,
not calibrated hospital workloads or real-world CCN effect estimates.

#### Metrics and time

The primary result is **total patient waiting over the complete schedule**:
`sum(admission - ready time)` for every supplied patient. It is the same quantity CCN
optimizes, exposed as `summary.full_plan_wait_hours`. Savings and percentages
use this total and the baseline total as denominator. A zero baseline leaves
the relative percentage undefined. The primary total stays fixed during
playback; it does not describe only the currently displayed hour.

Hour 0 is Monday 00:00. Future-not-ready patients appear separately from the
ready waiting queue and do not yet contribute blocked-bed metrics. **Waiting
hospital beds** count ready patients still in hospital at that instant;
**blocked bed-hours** integrate that count over the complete common observation interval.
These measure this cohort's extra occupancy after readiness, not total hospital
occupancy. The start is the earliest supplied **ready** time, including waiting
before the first actual hospital departure. The end is the later of both
strategies' final SNF releases. Every feasible patient has a complete admission,
stay and release trajectory. `mean_full_plan_wait_hours` divides total waiting
by all supplied patients. `mean_admitted_wait_hours` and its compatibility alias
`mean_wait_hours` use the eventually admitted cohort; when everyone is placeable,
these means are identical.
Empty means are `null`.

The second main metric is each strategy's cohort-completion duration: its last
SNF release minus the shared start. Both animations use the same start/end; an
earlier-finishing strategy remains empty until the other finishes.

The third main card is P90 patient waiting, using linear interpolation. The
fairness panel shows improved/worsened/unchanged patients, maximum wait and wait
increase, highest hospital mean, and each hospital's totals and means. Positive
patient deltas mean CCN waits longer; hospital savings use baseline minus CCN.
The main objective remains total waiting: no fairness constraint or weight was
silently added. An unplaceable patient leaves all whole-cohort distribution
statistics undefined; complete individual hospital subsets are labelled separately.

SNF utilization is occupied bed-hours divided by beds × the **same complete
observation duration** for both strategies. Since this fixed cohort has the same
SNF stays under both schedules, total occupied bed-hours equal the sum of all
stays, and both utilization values are equal. It describes the workload rather
than identifying a scheduling advantage. It appears as background context rather
than a primary benefit card.

If someone has no compatible bed, there is no finite complete-cohort end, so
completion duration, full-cohort total/mean waiting and utilization are
`null` and that patient remains visible. A finite solver value for the compatible
subset is not the full-cohort total. Batch full-cohort tail statistics are also
undefined when any patient is unplaceable; aggregation retains undefined seeds
instead of silently dropping them. The UI may preview all known events, with
the unresolved patients still waiting; that finite preview is not presented as
cohort completion. No input or animation presupposes a CCN benefit.

Animation, live counts, time bars, event logs, totals and exports all cover this
complete interval. A planned admission occupies a bed only when playback reaches
that event. The result's shared `observation` object records the automatic
start/end/duration and completeness; `objective` records population and units.

The five-patient reference explicitly selects `baseline_policy: fcfs`. Its
52 → 0 hour result belongs to the legacy ready-only policy, not the new
hospital-local benchmark.

#### Input and API

`dynamic_model.py` is separate from the three previous experiments. Existing
JSON remains readable: missing `baseline_policy` defaults to `local`, patient
insurance to `unspecified`, SNF accepted insurance to `["*"]`, and care lists to
empty. `tie_break_seed` defaults to 42 and `optimizer_seed` to 20261029. Both
accept the full unsigned 32-bit range. Canonical exports include these fields.
IDs must be unique within each list, ready times are nonnegative, stays are
positive, and hour inputs are finite and no greater than 1,000,000.

`dynamic_scenarios.generate(params)` creates cohorts without extra dependencies.
Use `GET /api/dynamic/examples`, `POST /api/dynamic/run` with `{"scenario": ...}`,
or `POST /api/dynamic/generate` with counts, `arrival_span_hours`, and seed.
Legacy `horizon_hours` is accepted as an alias for arrival-generation metadata;
it never truncates a simulation or rejects a later custom ready time. The
legacy three-mode API and its limits are unchanged.

```powershell
python dynamic_model.py --output dynamic-result.json
python dynamic_model.py my-scenario.json --output dynamic-result.json
```

### Suggested demonstration

1. Open **Bed allocation**, load **Limited Beds A First**, and run the comparison.
   Both strategies place two patients; total travel is 18 minutes for baseline and
   10 for global allocation. Move the second patient earlier and run again to show
   how baseline allocation depends on order.
2. Open **Clinical matching**, load **Hard Dependency Bariatric Reservation**, and
   run. Baseline places one patient; CCN places both by preserving the bariatric
   bed. Total travel is 10 versus 20 minutes because CCN transports an additional
   patient. Each placed patient travels 10 minutes.
3. Open **Workflow timing** to examine how supplied contact, review and confirmation
   durations determine acceptance wait. **Shorter Digital Processing** assumes
   120 versus 10 minutes of processing; the difference is conditional on those
   inputs. The routes remain the same under unlimited capacity.
4. Edit an input, select **Run comparison**, and inspect patient-level decisions.
   Export the scenario for another session, and export the run for its exact
   inputs, outcomes and assumptions. Export dialogs offer JSON copying and download.

Compare placed counts and unplaced IDs alongside wait and travel. Summed travel is
patient travel time, rather than elapsed scenario duration; mean acceptance wait
only includes placed patients. Clinical matching uses the supplied patient list
in input order, with readiness timestamps rather than time-driven arrival events.

### Command-line experiments

Run these commands from the project directory:

```powershell
python phase1_workflow.py
python phase1.py
python phase1_report.py
python phase2.py
```

The report command writes `results/phase1/phase1-report.html` and `results/phase1/phase1-results.json`. Open the HTML file in a browser for the assumptions, outcome tables and patient timelines. It works offline and can be printed. The JSON file includes the exact scenario inputs and full results.

Use another output directory or input files:

```powershell
python phase1_report.py --output-dir results/custom --workflow-input my_workflows.json --allocation-input my_allocations.json
```

Both individual experiment runners accept `--input PATH`, `--scenario NAME` and `--output PATH`:

```powershell
python phase1_workflow.py --scenario shorter_digital_processing --output results/workflow.json
python phase1.py --scenario limited_beds_a_first --output results/allocation.json
```

## Phase 1.1: acceptance workflow timing

Every patient starts independently at their own discharge-ready time **T1**. All patients are eligible for every SNF, beds are unlimited, and the nearest SNF accepts the first request. Nearest-SNF ties follow SNF input order. There are no shared worker or resource queues. Manual and digital modes differ only in their supplied sequential, nonoverlapping processing durations.

```text
T2 = T1 + contact duration + review duration + confirmation duration
Placement delay = T2 - T1
Assumed arrival time = T2 + travel time, assuming immediate departure at T2
```

**T2 is placement confirmation.** Travel happens after T2 and is excluded from placement delay. T2 does not by itself define actual hospital departure or bed release.

The runner reports each patient's timestamps, route, placement delay and travel time, together with placed count, total and mean placement delay, and total travel time. Total delay is the sum of patient acceptance waits, measured in patient-minutes. It is not elapsed scenario time or observed blocked hospital bed-hours.

### Bundled workflow cases

`scenarios/phase1_workflow_examples.json` contains three cases. Each has two patients and 9 patient-minutes of total travel in both modes.

| Scenario | Manual mean delay | Digital mean delay | Mean reduction, manual minus digital |
| --- | --- | --- | --- |
| `equal_processing` | 30 min | 30 min | 0 min |
| `shorter_digital_processing` | 120 min | 10 min | 110 min |
| `longer_digital_processing` | 20 min | 30 min | -10 min |

These durations are illustrative inputs. A shorter assumed digital process produces a shorter modeled delay by construction. The examples do not establish real-world effectiveness.

### Workflow input

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "name": "example_workflow",
      "hospitals": [{"id": "A"}],
      "snfs": [{"id": "X"}],
      "patients": [
        {"id": "patient_A", "hospital_id": "A", "ready_time_minutes": 0}
      ],
      "travel_time_minutes": {"A": {"X": 4}},
      "workflows": {
        "manual": {
          "contact_minutes": 40,
          "review_minutes": 60,
          "confirmation_minutes": 20
        },
        "digital": {
          "contact_minutes": 2,
          "review_minutes": 5,
          "confirmation_minutes": 3
        }
      }
    }
  ]
}
```

Scenario names and IDs within each list must be unique. Patient hospital references must exist. Ready times, processing durations and travel times are finite, nonnegative numbers. Supply one travel time for every hospital-to-SNF pair. Missing travel values are not zero-cost routes. A nonempty patient list requires at least one SNF.

## Phase 1.2: static allocation

All patients are ready in the same snapshot, every SNF is eligible, and each patient uses one bed. Assignments consume the initial available capacity; no beds are released within the snapshot. Both strategies start from the same capacities and travel-time matrix.

| Strategy | Rule |
| --- | --- |
| Greedy nearest available | Process patients in input order and choose the nearest SNF with a remaining bed. Travel ties follow SNF input order. |
| Global allocation | Maximize placed count first, then minimize total travel using exact min-cost max-flow. |

With complete eligibility, both strategies place the smaller of the patient count and total available beds. This experiment examines travel cost and the effect of patient order under capacity competition. The exact solver targets small experiments.

### Bundled allocation cases

`scenarios/phase1_examples.json` uses travel times of A to X/Y = 4/5 minutes and B to X/Y = 5/14 minutes, with one patient from each hospital.

| Scenario | X / Y available beds | Patient order | Greedy total travel | Global total travel |
| --- | --- | --- | --- | --- |
| `ample_beds_a_first` | 2 / 2 | A, B | 9 min | 9 min |
| `limited_beds_a_first` | 1 / 1 | A, B | 18 min | 10 min |
| `limited_beds_b_first` | 1 / 1 | B, A | 10 min | 10 min |

Both strategies place two patients in all three examples. With one bed per SNF and A first, greedy sends A to X and B to Y. Global allocation sends A to Y and B to X. Reversing patient order also gives greedy the 10-minute result.

### Allocation input

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "name": "limited_beds_a_first",
      "hospitals": [{"id": "A"}, {"id": "B"}],
      "snfs": [
        {"id": "X", "available_beds": 1},
        {"id": "Y", "available_beds": 1}
      ],
      "patients": [
        {"id": "patient_A", "hospital_id": "A"},
        {"id": "patient_B", "hospital_id": "B"}
      ],
      "travel_time_minutes": {
        "A": {"X": 4, "Y": 5},
        "B": {"X": 5, "Y": 14}
      }
    }
  ]
}
```

`available_beds` must be a nonnegative integer. The input needs a complete, finite, nonnegative hospital-to-SNF travel-time matrix. Patient list order determines greedy processing order. Scenario names and IDs within each list must be unique, and patient hospital references must exist.

Results include assignments, unplaced patient IDs, placed and unplaced counts, total travel time, and mean travel time among placed patients. The mean is `null` when nobody is placed.

## Phase 2: clinical attribute matching

Phase 2 models room-level inventory rather than treating an SNF's advertised bed count
as universally compatible. A bed has a room gender (`male`, `female`, or `any`) and
zero or more capabilities. A patient has a gender and hard required capabilities.
All beds in the same `(snf_id, room_id)` must have the same room gender. Room IDs
may repeat across SNFs; `any` is a fixed input category for this static snapshot.
The ready timestamp is **T1** and the acceptance timestamp is **T2**; the primary
objective is the sum of `T2 - T1` for every placed patient.

`scenarios/phase2_examples.json` contains:

| Scenario | Baseline | CCN |
| --- | --- | --- |
| `ghost_bed_gender_mismatch` | Tries the first visible bed, receives a rejection after 120 minutes, then retries | Filters the incompatible room and accepts immediately |
| `hard_dependency_bariatric_reservation` | Uses the only bariatric bed for a standard patient, leaving the bariatric patient blocked | Uses the standard bed for the standard patient and reserves the bariatric bed |

## Interpreting the results
The baseline intentionally represents sequential referral/rejection work. The CCN
strategy filters incompatible beds and prefers beds that preserve at least one
feasible option for every remaining patient, considering both gender and capabilities.
Patients who already have no compatible bed do not affect this reservation check.
Patients are processed in input order. This reservation heuristic checks each
remaining patient's options individually; it does not guarantee a maximum matching
when several patients compete for the same beds. If every candidate removes
someone's last option, the current patient still receives a compatible bed using
the normal selection priorities: fewer capabilities for patients without required
capabilities, then travel time and input order. Patients with required capabilities
use travel time and input order.
An unplaced patient makes
the objective value `null`, because a finite `T2` does not exist; the result still
reports the placed-patient delay and blocked patient IDs. Travel is reported as a
secondary metric and does not contribute to `T2 - T1`.

During a shortage, global allocation may leave a different set of patients unplaced. Always compare placed counts and unplaced patient IDs alongside mean travel. A lower average for placed patients does not establish fairness, patient priority or reduced waiting for the original population.

The two experiments isolate different mechanisms. Phase 1.1 has no finite-bed competition, rejection, retry or shared staff queues. Phase 1.2 has no processing time, new arrivals or bed turnover. Neither currently models payer restrictions, clinical compatibility, patient choice, capacity reservations or stale capacity information. Hospital bed-blocking measures require an explicit departure or bed-release model.

Synthetic input durations and travel costs support algorithm checks. Regional effect estimates require measured workflows, facility data and calibration.

## Verification

```powershell
python -m unittest discover -s tests -v
```

If Node.js is available, also check the browser's pure reporting semantics:

```powershell
node --test tests/test_dynamic_metrics.js
```

These checks include a case where total waiting improves while final completion
is later, nonzero starts, arrivals after the legacy span, empty cohorts, zero
denominators and unplaceable patients.

## Optional NYC road-data preparation

The road-data scripts prepare inputs for future hospital-to-SNF travel-time calculations:

- `fetch_nyc_streets.py` downloads the NYC drivable OpenStreetMap network with OSMnx, estimates edge speeds and travel times, and exports GeoPackage, GraphML and a JSON summary.
- `inspect_raw_nyc.py` reads the downloaded GeoPackage and displays fields, sample edges and estimated travel times.

Install their separate dependencies only when using those scripts:

```powershell
python -m pip install -r requirements.txt
python fetch_nyc_streets.py
python inspect_raw_nyc.py
```

Road data defaults to `data/nyc_streets/`. These scripts do not yet generate the hospital-to-SNF matrix automatically. The experiment runners and report generator do not need OSMnx or a road-data download.
