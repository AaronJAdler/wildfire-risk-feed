"""The interagency feed: parsing, merging with CAL FIRE, perimeters and prescribed burns."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from riskfeed import fires, geo
from riskfeed.models import Hotspot, Incident, Perimeter
from riskfeed.pipeline import merge_incidents
from riskfeed.sources import wfigs

from .conftest import SCENARIO, Replay

NOW = dt.datetime(2021, 8, 15, 21, 30, tzinfo=dt.UTC)


def test_parses_wildfires_and_prescribed_burns_and_tidies_names() -> None:
    payload = json.loads(
        (SCENARIO / "ticks" / "2-calfire-lists-caldor" / "wfigs_incidents.json").read_text()
    )
    incidents, quarantined = wfigs.parse_incidents(payload, NOW)
    assert not quarantined
    by_kind = {i.kind: i for i in incidents}
    assert by_kind["prescribed"].name == "Shamrock VMP RX"
    assert by_kind["wildfire"].name == "Caldor Fire" and by_kind["wildfire"].agency == "USFS"
    assert all(i.id.startswith("wfigs:") and i.source == "wfigs" for i in incidents)


def test_an_arcgis_error_served_with_200_is_a_failure() -> None:
    with pytest.raises(ValueError, match="WFIGS error"):
        wfigs.parse_incidents({"features": [], "error": {"code": 400}}, NOW)


def _incident(id_: str, name: str, lat: float, lon: float, source: str) -> Incident:
    return Incident(id_, name, lat, lon, None, None, None, NOW, source=source)


def test_the_same_fire_from_both_sources_is_kept_once() -> None:
    cal = _incident("c1", "Caldor Fire", 38.586, -120.538, "calfire")
    twin = _incident("wfigs:1", "Caldor Fire", 38.590, -120.535, "wfigs")
    other = _incident("wfigs:2", "Caldor Fire", 40.0, -122.0, "wfigs")  # same name, 180 km away
    kept, aliases = merge_incidents([cal, twin, other])
    assert [i.id for i in kept] == ["c1", "wfigs:2"]
    assert aliases == {"wfigs:1": "c1"}


def test_heat_inside_a_perimeter_belongs_to_that_fire() -> None:
    far = 38.9, -120.0  # 50 km from the incident point, which would not match on distance
    detections = [Hotspot(far[0] + k * 0.002, far[1], NOW, "N", 0.7, 20, "D") for k in range(3)]
    groups = fires.clusters(detections, 7)
    incident = _incident("wfigs:9", "Big Fire", 38.5, -120.5, "wfigs")
    outline = Perimeter(
        "wfigs:9", "BIG", 50_000, frozenset(geo.cell(h.lat, h.lon, 7) for h in detections)
    )
    assert fires.attribute(groups, [incident], {}, 10) == {}
    owners = fires.attribute(groups, [incident], {}, 10, [outline])
    assert {o.name for o in owners.values()} == {"Big Fire"}


def test_a_prescribed_burn_raises_no_alerts(replay: Replay) -> None:
    (first,) = replay(upto=1)
    assert not any("Shamrock" in t.title for t in first.transitions)
    assert sum(t.title == "Possible new fire" for t in first.transitions) == 2  # Caldor, field


def test_map_layers_show_each_fire_once(replay: Replay, tmp_path: Path) -> None:
    replay(upto=2)
    layer = json.loads((tmp_path / "public" / "incidents.geojson").read_text())
    names = [f["properties"]["name"] for f in layer["features"]]
    assert names.count("Caldor Fire") == 1
    assert any(f["properties"]["kind"] == "prescribed" for f in layer["features"])
