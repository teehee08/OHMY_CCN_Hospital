"""Reproducible, explicitly synthetic staffing inputs for the V4 demo."""
from pathlib import Path
import json


def example():
    return json.loads((Path(__file__).parent / "scenarios" / "staffing_v4.json").read_text(encoding="utf-8"))
