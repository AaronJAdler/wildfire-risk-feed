"""Replays recorded source responses through the real HTTP client.

A scenario folder holds one subfolder per tick with what each source returned at that moment:

    scenario.json                 ticks in order, each with its simulated time
    zones/<ZONE>.json             NWS zone outlines, shared by all ticks
    ticks/<name>/calfire.json     CAL FIRE incident list
    ticks/<name>/firms.csv        FIRMS detections
    ticks/<name>/nws_alerts.json  NWS alerts
    ticks/<name>/wfigs_incidents.json, wfigs_perimeters.geojson   NIFC WFIGS
    ticks/<name>/calfire_contained.json   optional: CAL FIRE's contained list
    ticks/<name>/<file>.status    optional: answer every request for <file> with this status

The transport sets an ETag on every response and answers If-None-Match with 304, so the
conditional-request path runs too.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class Tick:
    name: str
    now: dt.datetime


def ticks(scenario: str | Path) -> list[Tick]:
    data = json.loads((Path(scenario) / "scenario.json").read_text())
    return [Tick(t["name"], dt.datetime.fromisoformat(t["now"])) for t in data["ticks"]]


def transport(scenario: str | Path, tick: str, log: list[str] | None = None) -> httpx.MockTransport:
    root = Path(scenario)
    folder = root / "ticks" / tick

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if log is not None:
            log.append(f"{request.method} {host}{path}")
        if host == "www.fire.ca.gov":
            contained = "inactive=true" in str(request.url.query, "ascii")
            file = folder / ("calfire_contained.json" if contained else "calfire.json")
        elif host == "services3.arcgis.com":
            file = folder / (
                "wfigs_perimeters.geojson" if "Perimeters" in path else "wfigs_incidents.json"
            )
        elif host == "firms.modaps.eosdis.nasa.gov":
            file = folder / "firms.csv"
        elif host == "api.weather.gov" and path.startswith("/alerts"):
            file = folder / "nws_alerts.json"
        elif host == "api.weather.gov" and path.startswith("/zones/"):
            file = root / "zones" / f"{path.rsplit('/', 1)[-1]}.json"
        else:
            return httpx.Response(404)
        status_file = file.with_name(file.name + ".status")
        if status_file.exists():
            return httpx.Response(int(status_file.read_text().strip()))
        if not file.exists():
            return httpx.Response(404)
        body = file.read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, content=body, headers={"ETag": etag})

    return httpx.MockTransport(handler)
