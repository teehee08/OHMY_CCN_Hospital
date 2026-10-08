"""Print a readable inspection of the downloaded NYC street layers."""

from pathlib import Path

import geopandas as gpd


DATASET = Path(__file__).parent / "data" / "nyc_streets" / "nyc_streets.gpkg"


def main() -> None:
    if not DATASET.exists():
        raise FileNotFoundError(f"Dataset not found: {DATASET}")

    nodes = gpd.read_file(DATASET, layer="nodes")
    edges = gpd.read_file(DATASET, layer="edges")

    print(f"Dataset: {DATASET}")
    print(f"Nodes: {len(nodes):,} | Edges: {len(edges):,}")
    print(f"CRS: {edges.crs}")
    print("\nNode columns:")
    print(nodes.columns.tolist())
    print("\nEdge columns:")
    print(edges.columns.tolist())

    columns = [
        "u",
        "v",
        "name",
        "highway",
        "oneway",
        "length",
        "speed_kph",
        "travel_time",
    ]
    print("\nFirst 20 edges:")
    print(edges[columns].head(20).to_string(index=False))

    print("\n20 longest estimated travel times:")
    print(
        edges[
            ["name", "highway", "length", "speed_kph", "travel_time"]
        ]
        .sort_values("travel_time", ascending=False)
        .head(20)
        .to_string(index=False)
    )


if __name__ == "__main__":
    main()