"""Download and inspect NYC street data from OpenStreetMap via OSMnx."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import osmnx as ox


PLACE = "New York City, New York, USA"


def _column_summary(frame: Any) -> dict[str, dict[str, Any]]:
    """Return compact, JSON-serializable metadata for a GeoDataFrame."""
    summary: dict[str, dict[str, Any]] = {}
    for column in frame.columns:
        values = frame[column]
        try:
            unique_count = int(values.nunique(dropna=True))
        except TypeError:
            unique_count = int(values.dropna().map(repr).nunique())
        summary[str(column)] = {
            "dtype": str(values.dtype),
            "missing": int(values.isna().sum()),
            "unique": unique_count,
        }
    return summary


def inspect_network(graph: Any) -> dict[str, Any]:
    """Summarize the downloaded network so its schema is easy to explore."""
    nodes, edges = ox.convert.graph_to_gdfs(graph, nodes=True, edges=True)
    return {
        "place": PLACE,
        "network_type": "drive",
        "crs": str(edges.crs),
        "node_count": int(len(nodes)),
        "edge_count": int(len(edges)),
        "directed_edge_count": int(len(edges)),
        "node_columns": _column_summary(nodes),
        "edge_columns": _column_summary(edges),
        "edge_geometry_types": {
            str(key): int(value)
            for key, value in edges.geometry.geom_type.value_counts().items()
        },
        "edge_length_meters": {
            "min": float(edges["length"].min()),
            "mean": float(edges["length"].mean()),
            "max": float(edges["length"].max()),
        },
    }


def download_nyc_streets(output_dir: Path) -> dict[str, Any]:
    """Download the drivable NYC network and save reusable GIS outputs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.log_console = True

    print(f"Fetching or loading cached {PLACE} street data...")
    graph = ox.graph.graph_from_place(PLACE, network_type="drive", simplify=True)
    graph = ox.routing.add_edge_speeds(graph)
    graph = ox.routing.add_edge_travel_times(graph)

    nodes, edges = ox.convert.graph_to_gdfs(graph, nodes=True, edges=True)
    edges.to_file(output_dir / "nyc_streets.gpkg", layer="edges", driver="GPKG")
    nodes.to_file(output_dir / "nyc_streets.gpkg", layer="nodes", driver="GPKG")
    ox.io.save_graphml(graph, filepath=output_dir / "nyc_streets.graphml")

    summary = inspect_network(graph)
    (output_dir / "nyc_streets_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    print(f"Saved outputs to {output_dir.resolve()}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download and inspect NYC drivable street data from OpenStreetMap."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "nyc_streets",
        help="Directory for GeoPackage, GraphML, and JSON summary outputs.",
    )
    args = parser.parse_args()
    download_nyc_streets(args.output_dir)


if __name__ == "__main__":
    main()
