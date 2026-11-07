"""Writes the public outputs: the GeoJSON feed, alerts, source status and a map page.

Each file is written under a temporary name and renamed, so a reader never sees half a file.
Every output says whether the run was complete, and which sources were not, so consumers can
tell "no risk" from "no data".
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from importlib import resources
from pathlib import Path
from typing import Any

from riskfeed.alerts import describe
from riskfeed.geo import cell_polygon
from riskfeed.scoring import CellScore


def _write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def publish(
    out_dir: str | Path,
    now: dt.datetime,
    run_id: str,
    completeness: str,
    sources: dict[str, dict[str, Any]],
    scores: dict[str, CellScore],
    open_alerts: list[dict[str, Any]],
    last_messages: dict[str, dict[str, Any]],
    layers: dict[str, Any] | None = None,
) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    header = {"generated_at": now.isoformat(), "run_id": run_id, "completeness": completeness}
    features = [
        {
            "type": "Feature",
            "id": s.cell,
            "geometry": {"type": "Polygon", "coordinates": [cell_polygon(s.cell)]},
            "properties": {
                "score": s.score,
                "level": s.level,
                "fire": round(s.fire, 3),
                "incident": round(s.incident, 3),
                "weather": s.weather,
                "detections": s.detections,
                "reasons": s.reasons(),
            },
        }
        for s in sorted(scores.values(), key=lambda s: (-s.score, s.cell))
    ]
    feed = {"type": "FeatureCollection", **header, "sources": sources, "features": features}
    alerts = {
        **header,
        "open": [
            {
                "key": a["key"],
                "kind": a["kind"],
                "level": a["level"],
                "opened_at": a["opened_at"].isoformat(),
                "title": title,
                "text": text,
                "details": a["details"],
            }
            for a in open_alerts
            for title, text in [describe(a, last_messages.get(a["key"]))]
        ],
    }
    status = {
        **header,
        "sources": sources,
        "cells": dict(Counter(s.level for s in scores.values())),
        "open_alerts": len(open_alerts),
    }
    _write(out / "feed.geojson", json.dumps(feed, separators=(",", ":")))
    _write(out / "alerts.json", json.dumps(alerts, indent=2, default=str))
    _write(out / "status.json", json.dumps(status, indent=2, default=str))
    _write_layers(out, header, layers or {})
    page = resources.files("riskfeed").joinpath("web/index.html").read_text(encoding="utf-8")
    _write(out / "index.html", page)


def _point(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def _write_layers(out: Path, header: dict[str, Any], layers: dict[str, Any]) -> None:
    """Context layers for the map: known incidents, perimeters, and this year's contained fires.

    A layer that could not be fetched this run is left as it was, rather than emptied.
    """
    if layers.get("incidents") is not None:
        features = [
            _point(
                i.lon,
                i.lat,
                {
                    "name": i.name,
                    "kind": i.kind,
                    "source": i.source,
                    "agency": i.agency,
                    "acres": i.acres,
                    "contained": i.contained,
                    "url": i.url,
                    "started": i.started.date().isoformat() if i.started else None,
                },
            )
            for i in layers["incidents"]
        ]
        _write(
            out / "incidents.geojson",
            json.dumps({"type": "FeatureCollection", **header, "features": features}),
        )
    if layers.get("perimeters") is not None:
        features = [
            {
                "type": "Feature",
                "properties": {"name": p.name, "acres": p.acres},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[cell_polygon(c)] for c in sorted(p.cells)],
                },
            }
            for p in layers["perimeters"]
        ]
        _write(
            out / "perimeters.geojson",
            json.dumps({"type": "FeatureCollection", **header, "features": features}),
        )
    if layers.get("contained") is not None:
        features = [
            _point(f["lon"], f["lat"], {k: v for k, v in f.items() if k not in {"lat", "lon"}})
            for f in layers["contained"]
        ]
        _write(
            out / "contained.geojson",
            json.dumps({"type": "FeatureCollection", **header, "features": features}),
        )
