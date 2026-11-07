"""Spatial helpers. The feed's unit of area is an H3 hexagon."""

from __future__ import annotations

import math
from typing import Any

import h3

EARTH_RADIUS_KM = 6371.0088


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return south <= lat <= north and west <= lon <= east


def cell(lat: float, lon: float, resolution: int) -> str:
    return str(h3.latlng_to_cell(lat, lon, resolution))


def ring(center: str, k: int) -> set[str]:
    return set(h3.grid_disk(center, k))


def centroid(cell_id: str) -> tuple[float, float]:
    lat, lon = h3.cell_to_latlng(cell_id)
    return float(lat), float(lon)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def geometry_cells(geometry: dict[str, Any], resolution: int) -> set[str]:
    """Cells whose centres fall inside a GeoJSON Polygon or MultiPolygon.

    A polygon smaller than a cell can contain no cell centre; it then gets the cell under its
    first vertex, so a small warning area is never lost.
    """
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return set()
    cells = {str(c) for c in h3.geo_to_cells(geometry, resolution)}
    if not cells:
        rings = (
            geometry["coordinates"] if geometry["type"] == "Polygon" else geometry["coordinates"][0]
        )
        lon, lat = rings[0][0][:2]
        cells = {cell(lat, lon, resolution)}
    return cells


def cell_polygon(cell_id: str) -> list[list[float]]:
    """GeoJSON ring (longitude first, closed) for a cell."""
    points = [[round(lon, 5), round(lat, 5)] for lat, lon in h3.cell_to_boundary(cell_id)]
    return [*points, points[0]]
