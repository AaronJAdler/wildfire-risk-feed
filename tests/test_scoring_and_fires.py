"""Properties of the score, and attribution of detections to fires."""

from __future__ import annotations

import datetime as dt

from riskfeed import fires, geo
from riskfeed.models import Hotspot, Incident, WeatherAlert
from riskfeed.scoring import level, score_cells

NOW = dt.datetime(2021, 8, 15, 21, 30, tzinfo=dt.UTC)
LAT, LON = 38.60, -120.51


def _hot(
    n: int,
    lat: float = LAT,
    lon: float = LON,
    age_h: float = 0,
    frp: float = 20,
    confidence: float = 0.7,
) -> list[Hotspot]:
    when = NOW - dt.timedelta(hours=age_h)
    return [Hotspot(lat + k * 0.002, lon, when, "N", confidence, frp, "D") for k in range(n)]


def _incident(
    acres: float | None,
    contained: float | None,
    lat: float = LAT,
    lon: float = LON,
    name: str = "Test Fire",
) -> Incident:
    return Incident(name, name, lat, lon, acres, contained, None, NOW)


def _score(
    hotspots: list[Hotspot],
    incidents: list[Incident] = (),  # type: ignore[assignment]
    weather: list[WeatherAlert] = (),
) -> int:  # type: ignore[assignment]
    scores = score_cells(hotspots, list(incidents), list(weather), NOW, 6, 6.0)
    cell = geo.cell(LAT, LON, 6)
    return scores[cell].score if cell in scores else 0


def test_more_heat_never_lowers_the_score() -> None:
    values = [_score(_hot(n)) for n in range(0, 12)]
    assert values == sorted(values) and values[-1] > values[1] > 0


def test_detections_fade_with_age() -> None:
    assert _score(_hot(5, age_h=0)) > _score(_hot(5, age_h=6)) > _score(_hot(5, age_h=18))


def test_containment_lowers_the_score() -> None:
    open_fire = _score([], [_incident(6500, 0)])
    assert open_fire > _score([], [_incident(6500, 50)]) > _score([], [_incident(6500, 100)])
    assert _score([], [_incident(6500, 100)]) == 0


def test_red_flag_warning_multiplies_and_sets_a_floor() -> None:
    cell = geo.cell(LAT, LON, 6)
    warning = WeatherAlert("w", "Red Flag Warning", "", None, None, frozenset({cell}))
    assert _score(_hot(3), weather=[warning]) > _score(_hot(3))
    assert _score([], weather=[warning]) == 25


def test_levels() -> None:
    assert [level(v) for v in (0, 24, 25, 49, 50, 74, 75, 100)] == [
        "low",
        "low",
        "moderate",
        "moderate",
        "high",
        "high",
        "extreme",
        "extreme",
    ]


def test_clusters_join_touching_cells_only() -> None:
    near = _hot(3)
    far = _hot(2, lat=LAT + 0.5)
    groups = fires.clusters(near + far, 7)
    assert sorted(len(g.hotspots) for g in groups) == [2, 3]


def test_match_radius_grows_with_size_and_unknown_size_counts_as_large() -> None:
    assert fires.match_radius_km(_incident(10, 0), 10) == 10
    assert fires.match_radius_km(_incident(500_000, 30), 10) > 50
    assert fires.match_radius_km(_incident(None, None), 10) == 25


def test_a_large_fire_claims_its_front_over_a_small_nearer_fire() -> None:
    big = _incident(500_000, 30, lat=LAT - 0.35, name="Big Fire")  # about 39 km south
    small = _incident(50, 0, lat=LAT + 0.08, name="Small Fire")  # about 9 km north
    match = fires.explaining_incident(LAT, LON, [big, small], 10)
    assert match and match[0].name == "Big Fire"


def test_footprint_and_neighbours_attribute_a_spreading_fire() -> None:
    incident = _incident(6500, 0)
    origin = _hot(2)
    spread = _hot(2, lat=LAT + 0.15)  # about 17 km away: too far to match directly
    beyond = _hot(2, lat=LAT + 0.175)  # about 3 km past that
    groups = fires.clusters(spread + beyond, 7)
    footprint = {
        geo.cell(h.lat + 0.01, h.lon, 7): fires.Owner(incident.id, incident.name) for h in spread
    }  # yesterday's heat, next to today's
    owners = fires.attribute(groups, [incident], footprint, 10)
    assert len(owners) == len(groups)  # nothing left over to look like a new fire
    assert fires.attribute(fires.clusters(spread + beyond, 7), [incident], {}, 10) == {}
    assert fires.attribute(fires.clusters(origin, 7), [incident], {}, 10)


def test_the_state_outline_keeps_reno_and_las_vegas_out() -> None:
    assert geo.in_california(38.58, -121.49)  # Sacramento
    assert geo.in_california(32.72, -117.16)  # San Diego
    assert geo.in_california(41.75, -124.10)  # Crescent City
    assert not geo.in_california(39.618, -119.261)  # Reno area, from the first live run
    assert not geo.in_california(36.365, -114.900)  # Las Vegas area, from the first live run


def test_weak_heat_on_one_pass_is_not_a_new_fire() -> None:
    refinery = fires.Cluster(_hot(3, frp=2))  # the 1-3 MW detections seen live in November
    assert not refinery.qualifies
    assert fires.Cluster(_hot(2, frp=8)).qualifies
    two_passes = _hot(1, frp=2) + _hot(1, frp=2, age_h=3)
    assert fires.Cluster(two_passes).qualifies
    assert fires.Cluster(_hot(1, frp=15, confidence=0.95)).qualifies


def test_heat_seen_on_many_days_counts_as_a_persistent_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from riskfeed.store import Store

    store = Store(tmp_path / "s.duckdb")
    for day in range(4):
        store.add_hotspots(_hot(1, age_h=24 * day + 1), NOW)
    store.add_hotspots(_hot(1, lat=LAT + 1, age_h=1), NOW)  # once only
    persistent = store.persistent_cells(NOW, 7)
    assert geo.cell(LAT, LON, 7) in persistent
    assert geo.cell(LAT + 1, LON, 7) not in persistent
    store.close()
