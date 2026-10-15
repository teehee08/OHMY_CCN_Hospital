"""Compare manual and digital acceptance timelines with unlimited SNF beds."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Any

from phase1 import Hospital, Scenario


def _minutes(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    try:
        minutes = float(value)
    except OverflowError as error:
        raise ValueError(f"{field} must be a finite nonnegative number") from error
    if not math.isfinite(minutes) or minutes < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return minutes


def _sum_minutes(*values: float) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as error:
        raise ValueError("The resulting timeline exceeds the numeric range") from error
    if not math.isfinite(total):
        raise ValueError("The resulting timeline exceeds the numeric range")
    return total


@dataclass(frozen=True)
class WorkflowParameters:
    contact_minutes: float
    review_minutes: float
    confirmation_minutes: float

    @classmethod
    def from_dict(cls, data: Any) -> WorkflowParameters:
        fields = ("contact_minutes", "review_minutes", "confirmation_minutes")
        if not isinstance(data, dict) or set(data) != set(fields):
            raise ValueError("Each workflow needs contact, review, and confirmation minutes")
        parameters = cls(**{field: _minutes(data[field], field) for field in fields})
        _sum_minutes(*asdict(parameters).values())
        return parameters


@dataclass(frozen=True)
class WorkflowSNF:
    id: str


@dataclass(frozen=True)
class ReadyPatient:
    id: str
    hospital_id: str
    ready_time_minutes: float


@dataclass(frozen=True)
class WorkflowScenario:
    name: str
    hospitals: tuple[Hospital, ...]
    snfs: tuple[WorkflowSNF, ...]
    patients: tuple[ReadyPatient, ...]
    travel_time_minutes: dict[str, dict[str, float]]
    workflows: dict[str, WorkflowParameters]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowScenario:
        """Read explicit elapsed-time assumptions and a shared facility network."""
        if not isinstance(data, dict):
            raise ValueError("Each workflow scenario must be an object")
        snf_rows = data.get("snfs")
        if not isinstance(snf_rows, list) or any(
            not isinstance(row, dict) for row in snf_rows
        ):
            raise ValueError("snfs must be a list of objects")
        if any(set(row) != {"id"} for row in snf_rows):
            raise ValueError("Workflow SNFs require only an id; capacity is unlimited")
        # Reuse the shared ID/reference/matrix validator. The temporary zero
        # capacities are discarded; this experiment has unlimited SNF beds.
        network = Scenario.from_dict({
            "name": data.get("name"),
            "hospitals": data.get("hospitals"),
            "snfs": [{"id": row.get("id"), "available_beds": 0} for row in snf_rows],
            "patients": data.get("patients"),
            "travel_time_minutes": data.get("travel_time_minutes"),
        })
        if network.patients and not network.snfs:
            raise ValueError("A scenario with patients must include at least one SNF")
        patients = tuple(
            ReadyPatient(
                patient.id,
                patient.hospital_id,
                _minutes(row.get("ready_time_minutes"), "ready_time_minutes"),
            )
            for patient, row in zip(network.patients, data["patients"])
        )
        workflow_rows = data.get("workflows")
        if not isinstance(workflow_rows, dict) or set(workflow_rows) != {"manual", "digital"}:
            raise ValueError("workflows must contain manual and digital parameter objects")
        workflows = {
            mode: WorkflowParameters.from_dict(workflow_rows[mode])
            for mode in ("manual", "digital")
        }
        return cls(
            name=network.name,
            hospitals=network.hospitals,
            snfs=tuple(WorkflowSNF(snf.id) for snf in network.snfs),
            patients=patients,
            travel_time_minutes=network.travel_time_minutes,
            workflows=workflows,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hospitals": [asdict(hospital) for hospital in self.hospitals],
            "snfs": [asdict(snf) for snf in self.snfs],
            "patients": [asdict(patient) for patient in self.patients],
            "travel_time_minutes": {
                hospital_id: dict(times)
                for hospital_id, times in self.travel_time_minutes.items()
            },
            "workflows": {
                mode: asdict(parameters) for mode, parameters in self.workflows.items()
            },
        }


@dataclass(frozen=True)
class PatientTimeline:
    patient_id: str
    hospital_id: str
    snf_id: str
    t1_minutes: float
    contact_started_minutes: float
    contact_completed_minutes: float
    review_completed_minutes: float
    t2_minutes: float
    travel_time_minutes: float
    arrival_time_minutes: float

    @property
    def placement_delay_minutes(self) -> float:
        return self.t2_minutes - self.t1_minutes

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "placement_delay_minutes": self.placement_delay_minutes,
        }


@dataclass(frozen=True)
class WorkflowResult:
    mode: str
    placements: tuple[PatientTimeline, ...]

    @property
    def placed_count(self) -> int:
        return len(self.placements)

    @property
    def total_placement_delay_minutes(self) -> float:
        return _sum_minutes(*(patient.placement_delay_minutes for patient in self.placements))

    @property
    def mean_placement_delay_minutes(self) -> float | None:
        if not self.placements:
            return None
        return self.total_placement_delay_minutes / self.placed_count

    @property
    def total_travel_time_minutes(self) -> float:
        return _sum_minutes(*(patient.travel_time_minutes for patient in self.placements))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "placements": [patient.to_dict() for patient in self.placements],
            "placed_count": self.placed_count,
            "total_placement_delay_minutes": self.total_placement_delay_minutes,
            "mean_placement_delay_minutes": self.mean_placement_delay_minutes,
            "total_travel_time_minutes": self.total_travel_time_minutes,
        }


def simulate_workflow(scenario: WorkflowScenario, mode: str) -> WorkflowResult:
    """Calculate paired acceptance timelines using explicit duration inputs.

    Each patient starts independently at T1. Contact, review, and confirmation
    are consecutive elapsed intervals, with no shared-worker queues. All SNFs
    are compatible and accept the first referral. Nearest-SNF ties follow the
    input order. Arrival assumes immediate departure after acceptance (T2).
    """
    if mode not in ("manual", "digital"):
        raise ValueError("mode must be manual or digital")
    parameters = scenario.workflows[mode]
    placements = []
    for patient in scenario.patients:
        times = scenario.travel_time_minutes[patient.hospital_id]
        snf = min(scenario.snfs, key=lambda facility: times[facility.id])
        t1 = patient.ready_time_minutes
        contact_completed = _sum_minutes(t1, parameters.contact_minutes)
        review_completed = _sum_minutes(t1, parameters.contact_minutes, parameters.review_minutes)
        t2 = _sum_minutes(
            t1,
            parameters.contact_minutes,
            parameters.review_minutes,
            parameters.confirmation_minutes,
        )
        travel = times[snf.id]
        placements.append(PatientTimeline(
            patient_id=patient.id,
            hospital_id=patient.hospital_id,
            snf_id=snf.id,
            t1_minutes=t1,
            contact_started_minutes=t1,
            contact_completed_minutes=contact_completed,
            review_completed_minutes=review_completed,
            t2_minutes=t2,
            travel_time_minutes=travel,
            arrival_time_minutes=_sum_minutes(t2, travel),
        ))
    return WorkflowResult(mode, tuple(placements))


def compare_workflows(scenario: WorkflowScenario) -> dict[str, Any]:
    manual = simulate_workflow(scenario, "manual")
    digital = simulate_workflow(scenario, "digital")
    mean_reduction = None
    if manual.placed_count:
        mean_reduction = manual.mean_placement_delay_minutes - digital.mean_placement_delay_minutes
    return {
        "scenario": scenario.to_dict(),
        "results": {"manual": manual.to_dict(), "digital": digital.to_dict()},
        "comparison": {
            "total_placement_delay_reduction_minutes": (
                manual.total_placement_delay_minutes - digital.total_placement_delay_minutes
            ),
            "mean_placement_delay_reduction_minutes": mean_reduction,
            "total_travel_time_reduction_minutes": (
                manual.total_travel_time_minutes - digital.total_travel_time_minutes
            ),
        },
    }


def load_workflow_scenarios(path: Path) -> list[WorkflowScenario]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("Input must be an object with schema_version: 1")
    records = document.get("scenarios")
    if not isinstance(records, list) or not records:
        raise ValueError("scenarios must be a nonempty list")
    scenarios = [WorkflowScenario.from_dict(record) for record in records]
    names = [scenario.name for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("Scenario names must be unique")
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent / "scenarios" / "phase1_workflow_examples.json",
    )
    parser.add_argument("--scenario", help="Run only the scenario with this exact name")
    parser.add_argument("--output", type=Path, help="Save the inputs and timeline results as JSON")
    args = parser.parse_args()
    try:
        scenarios = load_workflow_scenarios(args.input)
        if args.scenario is not None:
            scenarios = [scenario for scenario in scenarios if scenario.name == args.scenario]
            if not scenarios:
                raise ValueError(f"Unknown scenario: {args.scenario}")
        reports = [compare_workflows(scenario) for scenario in scenarios]
        report = {"schema_version": 1, "model": "phase1_workflow", "scenarios": reports}
        if args.output is not None:
            if args.output.resolve() in {args.input.resolve(), Path(__file__).resolve()}:
                raise ValueError("Output must differ from the input and program files")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    for entry in reports:
        print(f"\n{entry['scenario']['name']}")
        for mode, result in entry["results"].items():
            mean = result["mean_placement_delay_minutes"]
            mean_label = "n/a" if mean is None else f"{mean:g}"
            print(
                f"  {mode:7} placed={result['placed_count']} "
                f"total_delay={result['total_placement_delay_minutes']:g} min "
                f"mean_delay={mean_label} min "
                f"travel={result['total_travel_time_minutes']:g} min"
            )
            for patient in result["placements"]:
                print(
                    f"    {patient['patient_id']}->{patient['snf_id']} "
                    f"T1={patient['t1_minutes']:g} T2={patient['t2_minutes']:g} "
                    f"delay={patient['placement_delay_minutes']:g} "
                    f"assumed_arrival={patient['arrival_time_minutes']:g} min"
                )
    if args.output is not None:
        print(f"\nSaved results to {args.output.resolve()}")


if __name__ == "__main__":
    main()
