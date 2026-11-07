"""NIFC's interagency wildland fire feed (WFIGS) for California.

CAL FIRE's incident list covers fires CAL FIRE reports on. WFIGS is the national system every
agency feeds, so it also has Forest Service, BLM, Park Service and local fires, and prescribed
burns. In November 2026 CAL FIRE's active list was empty while WFIGS had 69 California incidents,
30 of them prescribed burns. WFIGS also publishes mapped perimeters, which say where a fire is
now rather than where it started.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from riskfeed.config import CA_BBOX, Settings
from riskfeed.geo import geometry_cells, in_bbox
from riskfeed.http import Fetcher
from riskfeed.models import Incident, Perimeter, SourceResult

KINDS = {"WF": "wildfire", "RX": "prescribed"}
PERIMETER_RESOLUTION = 7


def _time(ms: Any) -> dt.datetime | None:
    if ms in (None, ""):
        return None
    return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.UTC)


def _name(raw: str, kind: str) -> str:
    """WFIGS names are often upper case and omit "Fire": "SHAMROCK VMP RX" -> "Shamrock VMP RX"."""
    words = [w if (len(w) <= 3 and w.isupper()) else w.capitalize() for w in raw.split()]
    name = " ".join(words) or "Unnamed"
    if kind == "wildfire" and not name.lower().endswith(("fire", "complex")):
        name += " Fire"
    return name


def parse_incidents(payload: Any, now: dt.datetime) -> tuple[list[Incident], list[dict[str, Any]]]:
    if not isinstance(payload, dict) or "features" not in payload:
        raise ValueError(f"unexpected WFIGS payload: {str(payload)[:120]}")
    if "error" in payload:  # ArcGIS reports query errors with status 200
        raise ValueError(f"WFIGS error: {payload['error']}")
    incidents: list[Incident] = []
    quarantined: list[dict[str, Any]] = []
    for feature in payload["features"]:
        a = feature.get("attributes") or {}
        g = feature.get("geometry") or {}
        kind = KINDS.get(str(a.get("IncidentTypeCategory")))
        if kind is None:
            continue  # complexes and other records duplicate their child fires
        try:
            lat, lon = float(g["y"]), float(g["x"])
            contained = a.get("PercentContained")
            if contained is not None and not 0 <= float(contained) <= 100:
                raise ValueError(f"PercentContained {contained}")
            acres = a.get("IncidentSize")
            if acres is not None and float(acres) < 0:
                raise ValueError(f"IncidentSize {acres}")
            if not in_bbox(lat, lon, CA_BBOX):
                raise ValueError(f"location outside California: {lat}, {lon}")
            incidents.append(
                Incident(
                    id=f"wfigs:{a['IrwinID']}",
                    name=_name(str(a.get("IncidentName") or ""), kind),
                    lat=lat,
                    lon=lon,
                    acres=float(acres) if acres is not None else None,
                    contained=float(contained) if contained is not None else None,
                    started=_time(a.get("FireDiscoveryDateTime")),
                    updated=_time(a.get("ModifiedOnDateTime_dt")) or now,
                    counties=(a["POOCounty"],) if a.get("POOCounty") else (),
                    url=None,
                    source="wfigs",
                    kind=kind,
                    agency=a.get("POOProtectingAgency"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            quarantined.append({"reason": f"{type(exc).__name__}: {exc}", "record": feature})
    return incidents, quarantined


def parse_perimeters(payload: Any) -> list[Perimeter]:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError(f"unexpected perimeter payload: {str(payload)[:120]}")
    out: list[Perimeter] = []
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        irwin = props.get("attr_IrwinID")
        if not irwin or not geometry:
            continue
        cells = geometry_cells(geometry, PERIMETER_RESOLUTION)
        if cells:
            out.append(
                Perimeter(
                    f"wfigs:{irwin}",
                    str(props.get("poly_IncidentName") or ""),
                    props.get("poly_GISAcres"),
                    frozenset(cells),
                )
            )
    return out


def fetch(fetcher: Fetcher, settings: Settings, now: dt.datetime) -> SourceResult:
    """Incidents and perimeters. Perimeters are optional: incidents alone count as success."""
    response = fetcher.get("wfigs", settings.wfigs_incidents_url, accept="application/json")
    if not response.ok or response.body is None:
        return SourceResult("wfigs", ok=False, detail=response.error or "no body")
    try:
        incidents, quarantined = parse_incidents(json.loads(response.body), now)
    except ValueError as exc:
        return SourceResult("wfigs", ok=False, detail=f"unreadable payload: {exc}"[:200])
    perimeters: list[Perimeter] | None = None
    outline = fetcher.get("wfigs", settings.wfigs_perimeters_url, accept="application/geo+json")
    if outline.ok and outline.body is not None:
        try:
            perimeters = parse_perimeters(json.loads(outline.body))
        except ValueError:
            perimeters = None
    burns = sum(1 for i in incidents if i.kind == "prescribed")
    fires_ = len(incidents) - burns
    detail = (
        f"{fires_} wildfire{'s' if fires_ != 1 else ''} and "
        f"{burns} prescribed burn{'s' if burns != 1 else ''}"
    )
    detail += (
        f", {len(perimeters)} perimeters" if perimeters is not None else ", perimeters unavailable"
    )
    result = SourceResult(
        "wfigs", ok=True, records=incidents, quarantined=quarantined, detail=detail
    )
    result.extra = perimeters
    return result
