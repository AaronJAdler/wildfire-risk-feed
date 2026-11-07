"""Builds fixtures/caldor-2021 from real CAL FIRE snapshots plus generated detections and alerts.

    python scripts/build_fixtures.py --replay ../ca-wildfire-elt/data/replay

What is real and what is not:
- calfire.json: real. CAL FIRE's feed as it stood at the end of each day, from Project 1's replay
  data (recorded by simonw/ca-fires-history). Tick 1 also carries the feed's real Peak Fire record
  from 2021-07-20, whose longitude was published as -11846461, to exercise quarantine.
- firms.csv: generated, seeded. Detections are placed around the real incident locations at
  realistic VIIRS overpass times, in the exact column format of the FIRMS area API.
- wfigs_incidents.json: generated, in the WFIGS ArcGIS format: a prescribed burn with real heat,
  and the Caldor Fire as WFIGS reports it, to exercise merging with CAL FIRE's record.
- nws_alerts.json and zones/: generated. Real NWS GeoJSON message format; the zone codes
  (CAZ901 and so on) and outlines are invented.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "caldor-2021"
COLUMNS = [
    "latitude",
    "longitude",
    "bright_ti4",
    "scan",
    "track",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence",
    "version",
    "bright_ti5",
    "frp",
    "daynight",
]

TICKS = [
    ("1-satellites-first", "2021-08-15T09:30:00+00:00", "2021-08-14", "2021-08-15T09:10"),
    ("2-calfire-lists-caldor", "2021-08-15T21:30:00+00:00", "2021-08-15", "2021-08-15T20:40"),
    ("3-firms-outage", "2021-08-16T21:30:00+00:00", "2021-08-16", None),
    ("4-recovery", "2021-08-17T21:30:00+00:00", "2021-08-17", "2021-08-17T20:35"),
]

# (label, centre lat, centre lon, radius km, detections, frp range, confidence weights)
FIRES: dict[str, list[tuple[str, float, float, float, int, tuple[float, float], str]]] = {
    "1-satellites-first": [
        ("dixie", 40.05, -121.05, 22, 70, (3, 150), "mix"),
        ("caldor", 38.596, -120.522, 1.2, 3, (8, 35), "nhn"),
        ("monument", 40.70, -123.28, 6, 14, (2, 60), "mix"),
        ("mcfarland", 40.33, -123.05, 5, 8, (2, 40), "mix"),
        ("bell", 39.835, -123.635, 0.5, 1, (1, 3), "l"),
        ("field-burn", 35.905, -119.215, 0.4, 2, (6, 12), "nn"),
        ("hot-roof", 36.80, -120.10, 0.1, 1, (1, 2), "l"),
        ("prescribed-burn", 39.640, -123.440, 0.6, 3, (8, 20), "nn"),
        ("nevada", 39.55, -117.05, 3, 3, (5, 20), "mix"),
        ("oregon", 43.10, -121.60, 2, 2, (5, 20), "mix"),
    ],
    "2-calfire-lists-caldor": [
        ("dixie", 40.10, -120.98, 22, 85, (3, 200), "mix"),
        ("caldor", 38.60, -120.51, 3, 24, (10, 180), "mix"),
        ("monument", 40.70, -123.28, 6, 12, (2, 60), "mix"),
        ("mcfarland", 40.33, -123.05, 5, 6, (2, 40), "mix"),
    ],
    "4-recovery": [
        ("dixie", 40.20, -120.85, 18, 55, (3, 160), "mix"),
        ("caldor", 38.63, -120.44, 8, 95, (15, 320), "mix"),
        # Monument goes quiet (smoke and an inversion), so its alert can clear.
        ("mcfarland", 40.33, -123.05, 4, 2, (2, 20), "mix"),
    ],
}


def fire_rows(rng: random.Random, tick: str, pass_time: str) -> list[dict[str, object]]:
    passed = dt.datetime.fromisoformat(pass_time)
    daynight = "D" if 14 <= passed.hour <= 23 else "N"
    rows: list[dict[str, object]] = []
    for _, lat, lon, radius, count, (lo, hi), conf in FIRES[tick]:
        for n in range(count):
            distance, angle = radius * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
            plat = lat + distance * math.sin(angle) / 111.0
            plon = lon + distance * math.cos(angle) / (111.0 * math.cos(math.radians(lat)))
            when = passed + dt.timedelta(seconds=rng.randint(-90, 90))
            if conf == "mix":
                level = rng.choices("hnl", weights=[25, 65, 10])[0]
            else:
                level = conf[n % len(conf)]
            frp = round(math.exp(rng.uniform(math.log(lo), math.log(hi))), 2)
            rows.append(
                {
                    "latitude": round(plat, 5),
                    "longitude": round(plon, 5),
                    "bright_ti4": round(rng.uniform(305, 367), 2),
                    "scan": 0.39,
                    "track": 0.36,
                    "acq_date": when.strftime("%Y-%m-%d"),
                    "acq_time": int(when.strftime("%H%M")),
                    "satellite": "N",
                    "instrument": "VIIRS",
                    "confidence": level,
                    "version": "2.0NRT",
                    "bright_ti5": round(rng.uniform(280, 312), 2),
                    "frp": frp,
                    "daynight": daynight,
                }
            )
    return rows


def csv_text(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def nws_feature(
    key: str,
    event: str,
    onset: str,
    ends: str,
    *,
    zones: list[str] | None = None,
    geometry: dict[str, object] | None = None,
    references: list[str] | None = None,
    message_type: str = "Alert",
    status: str = "Actual",
) -> dict[str, object]:
    ident = f"urn:oid:2.49.0.1.840.0.fixture.{key}"
    url = f"https://api.weather.gov/alerts/{ident}"
    zone_urls = [f"https://api.weather.gov/zones/fire/{z}" for z in zones or []]
    return {
        "id": url,
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "@id": url,
            "id": ident,
            "areaDesc": ", ".join(zones or []) or "Fixture polygon",
            "geocode": {"UGC": zones or []},
            "affectedZones": zone_urls,
            "references": [
                {
                    "@id": f"https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.fixture.{r}",
                    "identifier": f"urn:oid:2.49.0.1.840.0.fixture.{r}",
                    "sender": "w-nws.webmaster@noaa.gov",
                    "sent": onset,
                }
                for r in references or []
            ],
            "sent": onset,
            "effective": onset,
            "onset": onset,
            "expires": ends,
            "ends": ends,
            "status": status,
            "messageType": message_type,
            "category": "Met",
            "severity": "Severe",
            "certainty": "Likely",
            "urgency": "Expected",
            "event": event,
            "senderName": "NWS Sacramento CA (fixture)",
            "headline": f"{event} in effect until {ends} (fixture)",
            "description": "Generated for tests. Not a real NWS product.",
        },
    }


def box(south: float, north: float, west: float, east: float) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def nws_ticks() -> dict[str, list[dict[str, object]]]:
    rfw1 = nws_feature(
        "rfw.1",
        "Red Flag Warning",
        "2021-08-15T06:00:00+00:00",
        "2021-08-16T03:00:00+00:00",
        zones=["CAZ901", "CAZ902"],
    )
    rfw2 = nws_feature(
        "rfw.2",
        "Red Flag Warning",
        "2021-08-15T06:00:00+00:00",
        "2021-08-16T09:00:00+00:00",
        zones=["CAZ901", "CAZ902", "CAZ903"],
        references=["rfw.1"],
        message_type="Update",
    )
    fww1 = nws_feature(
        "fww.1",
        "Fire Weather Watch",
        "2021-08-15T18:00:00+00:00",
        "2021-08-16T06:00:00+00:00",
        geometry=box(40.1, 41.0, -123.6, -122.8),
    )
    fww_cancel = nws_feature(
        "fww.2",
        "Fire Weather Watch",
        "2021-08-16T01:00:00+00:00",
        "2021-08-16T06:00:00+00:00",
        references=["fww.1"],
        message_type="Cancel",
        geometry=box(40.1, 41.0, -123.6, -122.8),
    )
    test = nws_feature(
        "test.1",
        "Red Flag Warning",
        "2021-08-15T06:00:00+00:00",
        "2021-08-16T06:00:00+00:00",
        geometry=box(33.0, 34.0, -118.5, -117.0),
        status="Test",
    )
    heat = nws_feature(
        "heat.1",
        "Heat Advisory",
        "2021-08-15T17:00:00+00:00",
        "2021-08-16T03:00:00+00:00",
        geometry=box(36.0, 37.0, -120.5, -119.0),
    )
    return {
        "1-satellites-first": [rfw1, fww1, test, heat],
        "2-calfire-lists-caldor": [rfw2, rfw1, fww1],
        "3-firms-outage": [rfw2, fww_cancel, fww1],
        "4-recovery": [],
    }


ZONES = {
    "CAZ901": ("Fixture fire weather zone: northern Sierra", box(39.6, 40.7, -121.7, -120.3)),
    "CAZ902": ("Fixture fire weather zone: central Sierra", box(38.3, 39.0, -121.0, -119.9)),
}


def wfigs_incident(
    irwin: str,
    name: str,
    kind: str,
    lat: float,
    lon: float,
    acres: float | None,
    contained: float | None,
    discovered: str,
    agency: str,
) -> dict[str, object]:
    ms = int(dt.datetime.fromisoformat(discovered).timestamp() * 1000)
    return {
        "attributes": {
            "IrwinID": irwin,
            "IncidentName": name,
            "IncidentTypeCategory": kind,
            "IncidentSize": acres,
            "PercentContained": contained,
            "FireDiscoveryDateTime": ms,
            "ModifiedOnDateTime_dt": ms,
            "POOCounty": "Fixture",
            "POOProtectingAgency": agency,
            "UniqueFireIdentifier": f"2021-FIX-{irwin[1:7]}",
        },
        "geometry": {"x": lon, "y": lat},
    }


def wfigs_ticks() -> dict[str, list[dict[str, object]]]:
    burn = wfigs_incident(
        "{0F1XTURE-0000-0000-0000-000000000001}",
        "SHAMROCK VMP RX",
        "RX",
        39.640,
        -123.440,
        120,
        None,
        "2021-08-15T06:00:00+00:00",
        "CAL FIRE",
    )
    caldor = wfigs_incident(
        "{0F1XTURE-0000-0000-0000-000000000002}",
        "CALDOR",
        "WF",
        38.590,
        -120.535,
        None,
        0,
        "2021-08-15T01:54:00+00:00",
        "USFS",
    )
    return {
        "1-satellites-first": [burn],
        "2-calfire-lists-caldor": [burn, caldor],
        "3-firms-outage": [caldor],
        "4-recovery": [caldor],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", required=True, help="ca-wildfire-elt/data/replay")
    replay = Path(parser.parse_args().replay)
    rng = random.Random(2021)  # noqa: S311 - repeatable fixtures
    peak = next(
        r
        for r in json.loads(gzip.decompress((replay / "2021-07-20.json.gz").read_bytes()))[
            "incidents"
        ]
        if r["Name"] == "Peak Fire"
    )
    scenario = {"ticks": []}
    alerts = nws_ticks()
    for name, now, day, pass_time in TICKS:
        folder = OUT / "ticks" / name
        folder.mkdir(parents=True, exist_ok=True)
        incidents = json.loads(gzip.decompress((replay / f"{day}.json.gz").read_bytes()))[
            "incidents"
        ]
        if name.startswith("1-"):
            incidents = [*incidents, peak]
        (folder / "calfire.json").write_text(json.dumps(incidents, indent=1))
        firms_file, status_file = folder / "firms.csv", folder / "firms.csv.status"
        if pass_time:
            rows = fire_rows(rng, name, pass_time)
            if name.startswith("1-"):
                broken = dict(rows[0], confidence="?", frp="")
                rows.append(broken)
            firms_file.write_text(csv_text(rows))
            status_file.unlink(missing_ok=True)
        else:
            firms_file.unlink(missing_ok=True)
            status_file.write_text("503\n")
        (folder / "wfigs_incidents.json").write_text(
            json.dumps(
                {
                    "objectIdFieldName": "OBJECTID",
                    "spatialReference": {"wkid": 4326},
                    "features": wfigs_ticks()[name],
                },
                indent=1,
            )
        )
        (folder / "wfigs_perimeters.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": []})
        )
        (folder / "nws_alerts.json").write_text(
            json.dumps({"type": "FeatureCollection", "features": alerts[name]}, indent=1)
        )
        scenario["ticks"].append({"name": name, "now": now})
    zones = OUT / "zones"
    zones.mkdir(parents=True, exist_ok=True)
    for code, (title, geometry) in ZONES.items():
        (zones / f"{code}.json").write_text(
            json.dumps(
                {
                    "id": f"https://api.weather.gov/zones/fire/{code}",
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {"id": code, "type": "fire", "name": title},
                },
                indent=1,
            )
        )
    (OUT / "scenario.json").write_text(json.dumps(scenario, indent=1) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
