# OHMY Hospital CCN

This project studies regional hospital-to-skilled nursing facility (SNF) placement coordination in New York City. A Capacity Command Network (CCN) shares regional demand and capacity information to support joint placement decisions.

The current implementation provides two deterministic experiments: **Phase 1.1 acceptance workflow timing** and **Phase 1.2 static allocation**, plus **Phase 2 clinical attribute matching**. Synthetic examples make their assumptions and outcomes easy to inspect. The experiments use Python 3.10+ and the standard library.

## Run the experiments and report

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
