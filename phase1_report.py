"""Generate an offline report for the Phase 1 workflow and allocation experiments."""

from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
from typing import Any

from phase1 import compare, load_scenarios
from phase1_workflow import compare_workflows, load_workflow_scenarios


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _number(value: int | float | None) -> str:
    return "n/a" if value is None else f"{value:,.6g}"


def _table(
    headers: list[str], rows: list[list[Any]], caption: str, *, timeline: bool = False
) -> str:
    heading = "".join(f'<th scope="col">{_text(item)}</th>' for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_text(item)}</td>" for item in row) + "</tr>"
        for row in rows
    )
    table_class = ' class="timeline-table"' if timeline else ""
    return (
        f'<div class="table-scroll"><table{table_class}>'
        f"<caption>{_text(caption)}</caption><thead><tr>{heading}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def build_report(allocation_input: Path, workflow_input: Path) -> dict[str, Any]:
    """Evaluate both input files through their respective model interfaces."""
    workflow_scenarios = load_workflow_scenarios(workflow_input)
    allocation_scenarios = load_scenarios(allocation_input)
    return {
        "schema_version": 1,
        "workflow_experiments": [compare_workflows(item) for item in workflow_scenarios],
        "allocation_experiments": [compare(item) for item in allocation_scenarios],
    }


def render_html(report: dict[str, Any]) -> str:
    """Render an English, self-contained report; input strings are HTML-escaped."""
    workflows = report["workflow_experiments"]
    allocations = report["allocation_experiments"]
    workflow_rows: list[list[Any]] = []
    duration_rows: list[list[Any]] = []
    reduction_rows: list[list[Any]] = []
    for item in workflows:
        name = item["scenario"]["name"]
        for mode in ("manual", "digital"):
            result = item["results"][mode]
            duration = item["scenario"]["workflows"][mode]
            duration_rows.append([
                name, mode, *(_number(duration[key]) for key in (
                    "contact_minutes", "review_minutes", "confirmation_minutes"
                )),
            ])
            workflow_rows.append([
                name, mode, result["placed_count"],
                _number(result["mean_placement_delay_minutes"]),
                _number(result["total_placement_delay_minutes"]),
                _number(result["total_travel_time_minutes"]),
            ])
        comparison = item["comparison"]
        reduction_rows.append([
            name,
            _number(comparison["mean_placement_delay_reduction_minutes"]),
            _number(comparison["total_placement_delay_reduction_minutes"]),
            _number(comparison["total_travel_time_reduction_minutes"]),
        ])

    timeline = next(
        (item for item in workflows
         if item["scenario"]["name"] == "shorter_digital_processing"),
        workflows[0] if workflows else None,
    )
    timeline_html = "<p>No workflow experiment is available.</p>"
    if timeline is not None:
        timeline_rows: list[list[Any]] = []
        for mode in ("manual", "digital"):
            for placement in timeline["results"][mode]["placements"]:
                timeline_rows.append([
                    placement["patient_id"], mode,
                    f"{placement['hospital_id']} to {placement['snf_id']}",
                    *(_number(placement[key]) for key in (
                        "t1_minutes", "contact_started_minutes",
                        "contact_completed_minutes", "review_completed_minutes",
                        "t2_minutes", "placement_delay_minutes",
                        "travel_time_minutes", "arrival_time_minutes",
                    )),
                ])
        timeline_html = (
            f"<h3>Timeline detail: {_text(timeline['scenario']['name'])}</h3>"
            "<p>Times are minutes from the scenario origin. T1, contact start, "
            "contact end, review end, T2 and assumed arrival are timestamps. "
            "Delay and travel are durations.</p>"
            + _table(
                ["Patient", "Mode", "Route", "T1 ready", "Contact start",
                 "Contact end", "Review end", "T2 confirmed", "Delay", "Travel",
                 "Assumed arrival"],
                timeline_rows, "Patient timelines in minutes", timeline=True,
            )
        )

    allocation_rows: list[list[Any]] = []
    allocation_details: list[str] = []
    for item in allocations:
        scenario = item["scenario"]
        name = scenario["name"]
        details: list[list[Any]] = []
        for strategy in ("greedy", "global"):
            result = item["results"][strategy]
            unplaced = ", ".join(result["unplaced_patient_ids"]) or "none"
            allocation_rows.append([
                name, strategy, result["placed_count"], result["unplaced_count"],
                _number(result["total_travel_time_minutes"]),
                _number(result["mean_travel_time_minutes"]), unplaced,
            ])
            for assignment in result["assignments"]:
                details.append([
                    strategy, assignment["patient_id"], assignment["hospital_id"],
                    assignment["snf_id"], _number(assignment["travel_time_minutes"]),
                ])
        order = ", ".join(patient["id"] for patient in scenario["patients"]) or "none"
        beds = ", ".join(
            f"{snf['id']}: {snf['available_beds']}" for snf in scenario["snfs"]
        ) or "none"
        allocation_details.append(
            f"<h3>{_text(name)}</h3>"
            f"<p>Patient order: {_text(order)}. Available beds: {_text(beds)}.</p>"
            + _table(
                ["Strategy", "Patient", "Hospital", "SNF", "Travel (min)"],
                details, "Assignments",
            )
        )

    workflow_table = _table(
        ["Scenario", "Mode", "Placed", "Mean delay (min/patient)",
         "Total delay (patient-min)", "Total travel (patient-min)"],
        workflow_rows, "Phase 1.1 workflow outcomes",
    )
    duration_table = _table(
        ["Scenario", "Mode", "Contact (min)", "Review (min)", "Confirmation (min)"],
        duration_rows, "Illustrative processing durations supplied as inputs",
    )
    reduction_table = _table(
        ["Scenario", "Mean delay reduction (min/patient)",
         "Total delay reduction (patient-min)", "Total travel reduction (patient-min)"],
        reduction_rows, "Manual minus digital; negative values mean digital takes longer",
    )
    allocation_table = _table(
        ["Scenario", "Strategy", "Placed", "Unplaced", "Total travel (patient-min)",
         "Mean travel (min/placed patient)", "Unplaced patient IDs"],
        allocation_rows, "Phase 1.2 allocation outcomes",
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OHMY Hospital CCN: Phase 1 experiments</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f5f4f0; color: #222b31; font: 16px/1.6 system-ui, sans-serif; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 40px 30px 64px; background: #fff; }}
header {{ border-bottom: 3px solid #8b2635; padding-bottom: 24px; margin-bottom: 32px; }}
h1 {{ font-size: clamp(1.9rem, 5vw, 2.8rem); line-height: 1.18; margin: 8px 0 16px; }}
h2 {{ font-size: 1.5rem; line-height: 1.3; margin-top: 38px; }}
h3 {{ font-size: 1.1rem; margin-top: 28px; }}
p, ul {{ max-width: 90ch; }}
li {{ margin-bottom: 6px; }}
.eyebrow {{ font-weight: 700; color: #8b2635; letter-spacing: .08em; font-size: .85rem; }}
.intro {{ font-size: 1.12rem; }}
.formula {{ border-left: 3px solid #8b2635; padding: 10px 18px; background: #f8f5f3; }}
.table-scroll {{ max-width: 100%; overflow-x: auto; margin: 20px 0 28px; }}
table {{ border-collapse: collapse; width: 100%; font-size: .88rem; line-height: 1.45; }}
.timeline-table {{ min-width: 1100px; }}
caption {{ text-align: left; font-weight: 650; margin-bottom: 8px; color: #38464e; }}
th, td {{ text-align: left; padding: 10px; vertical-align: top; border-bottom: 1px solid #dce1e3; overflow-wrap: anywhere; }}
th {{ background: #edf1f2; font-weight: 650; }}
tbody tr:nth-child(even) {{ background: #fafbfb; }}
code {{ background: #eef1f2; padding: .1em .3em; }}
footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #dce1e3; color: #4b5961; }}
@media (max-width: 640px) {{ main {{ padding: 24px 16px 40px; }} table {{ min-width: 640px; }} }}
@media print {{
  body {{ background: white; font-size: 10pt; }}
  main {{ max-width: none; padding: 0; }}
  header {{ margin-bottom: 18px; padding-bottom: 12px; }}
  h1 {{ font-size: 23pt; }} h2 {{ font-size: 16pt; }}
  h2, h3, caption {{ break-after: avoid; }}
  .table-scroll {{ overflow: visible; }}
  table {{ min-width: 0; table-layout: fixed; font-size: 7.5pt; }}
  .timeline-table {{ min-width: 0; font-size: 7pt; }}
  th, td {{ padding: 5px; }} tr {{ break-inside: avoid; }}
  thead {{ display: table-header-group; }}
}}
</style>
</head>
<body><main>
<header>
<div class="eyebrow">OHMY HOSPITAL PROJECT</div>
<h1>Capacity Command Network: Phase 1 experiments</h1>
<p class="intro">Two small, deterministic experiments separate acceptance processing
time from capacity-constrained allocation. The results describe the supplied
synthetic inputs and model rules.</p>
<p>This report contains {_text(len(workflows))} workflow scenarios and
{_text(len(allocations))} allocation scenarios. All times are in minutes.</p>
</header>
<section aria-labelledby="coverage">
<h2 id="coverage">What the experiments cover</h2>
<p>Phase 1.1 compares manual and digital acceptance workflows with unlimited beds.
Phase 1.2 compares sequential nearest-bed allocation with joint allocation in a
fixed capacity snapshot. They are separate experiments, so workflow delays and
allocation travel savings should be read separately.</p>
<p>All patients are eligible for every SNF. Travel times form a complete,
nonnegative hospital-to-SNF matrix. There are no random draws.</p>
</section>
<section aria-labelledby="workflow">
<h2 id="workflow">Phase 1.1: manual and digital acceptance workflows</h2>
<p>Each patient independently starts at their own discharge-ready time T1.
The nearest SNF accepts the first request, with unlimited beds and no shared
worker or resource queues. Nearest-SNF ties follow SNF input order. Each mode
supplies sequential, nonoverlapping contact, review and confirmation durations.
Equal travel times are used in both modes.</p>
<div class="formula">
<p><strong>T2 = T1 + contact duration + review duration + confirmation duration</strong><br>
Placement delay = T2 &minus; T1<br>
Assumed arrival time = T2 + travel time, assuming immediate departure at T2</p>
</div>
<p>T2 means placement confirmation. Assumed arrival is a later timestamp when
travel is positive. Travel is excluded from placement delay. Total delay sums
individual patient acceptance waits, measured in patient-minutes rather than
elapsed scenario time. Mean delay divides this total by the number of placed
patients. These are modeled acceptance waits, not observed blocked bed-hours.</p>
{duration_table}
{workflow_table}
{reduction_table}
<p>The duration inputs determine the workflow comparison. The bundled examples
include equal, shorter and longer digital processing, so the comparison can
show zero, positive or negative delay reduction. These outcomes demonstrate
the supplied timing assumptions rather than observed effects.</p>
{timeline_html}
</section>
<section aria-labelledby="allocation">
<h2 id="allocation">Phase 1.2: static allocation with finite beds</h2>
<p>All patients are ready in the same snapshot. Each patient uses one bed.
Assignments consume the initial available capacity; no beds are released within
the snapshot. Greedy allocation processes the patient list
in order and chooses the nearest SNF with a remaining bed. Global allocation
uses exact min-cost max-flow to maximize placements first and minimize total
travel second.</p>
<p>With complete eligibility, both strategies place the smaller of the patient
count and total available beds. The experiment tests travel costs and the
effect of patient order under capacity competition.</p>
{allocation_table}
{''.join(allocation_details)}
</section>
<section aria-labelledby="limits">
<h2 id="limits">Interpretation and limits</h2>
<ul>
<li>Workflow durations are illustrative inputs. A shorter digital duration
produces a shorter modeled delay by construction. Equal and longer digital
durations are included to expose this dependence. The experiment provides no
empirical estimate of digital coordination effectiveness.</li>
<li>During a bed shortage, global allocation may leave different patients
unplaced. Compare placed counts and unplaced patient IDs alongside mean travel.
A lower mean among placed patients does not establish fairness, priority or
shorter waiting time for the original patient population.</li>
<li>The workflow model has no bed contention, referral rejection, retry,
capacity reservation, stale information or shared staff queues. The allocation
model has no arrivals, acceptance processing, patient choice, payer or clinical
constraints, or bed turnover.</li>
<li>Placement confirmation at T2 is distinct from hospital departure and SNF
arrival. The arrival calculation assumes immediate departure at T2.
Blocked hospital bed-hours require an explicit departure or release rule and
cannot be inferred as an operational outcome from these experiments.</li>
<li>These models use synthetic cases. Facility data, workflow measurements and
calibration are needed before estimating regional effects.</li>
</ul>
</section>
<footer><p>The accompanying <code>phase1-results.json</code> contains the scenario
inputs and complete results used to generate these tables. This HTML file works
offline and contains no external assets or scripts.</p></footer>
</main></body></html>
"""


def main() -> None:
    base = Path(__file__).parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("results") / "phase1")
    parser.add_argument(
        "--allocation-input", type=Path,
        default=base / "scenarios" / "phase1_examples.json",
    )
    parser.add_argument(
        "--workflow-input", type=Path,
        default=base / "scenarios" / "phase1_workflow_examples.json",
    )
    args = parser.parse_args()
    html_path = args.output_dir / "phase1-report.html"
    json_path = args.output_dir / "phase1-results.json"
    try:
        protected = {
            args.allocation_input.resolve(), args.workflow_input.resolve(),
            Path(__file__).resolve(), (base / "phase1.py").resolve(),
            (base / "phase1_workflow.py").resolve(),
        }
        if any(path.resolve() in protected for path in (html_path, json_path)):
            raise ValueError("Report output paths must differ from input and program files")
        report = build_report(args.allocation_input, args.workflow_input)
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        html = render_html(report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        json_path.write_text(serialized, encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Saved HTML report to {html_path.resolve()}")
    print(f"Saved JSON results to {json_path.resolve()}")


if __name__ == "__main__":
    main()
