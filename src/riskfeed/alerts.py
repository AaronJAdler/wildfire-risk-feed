"""Alert lifecycles, and the rule that missing data never closes an alert.

Two kinds of alert:

risk:incident:<id>  one per fire, not per hexagon: opens when any cell attributed to the fire
                 reaches 70, escalates when the level rises, and clears when every cell is below
                 50, but only on a run where every source is current. A source outage makes
                 scores fall because data is missing, not because the fire is out.
ignition:<...>   opens for a group of detections no known fire explains, is confirmed when
                 CAL FIRE lists a fire there, or expires when the satellites see nothing more for
                 12 hours. Confirming needs fresh CAL FIRE data; expiring needs fresh FIRMS data;
                 opening needs CAL FIRE data that is at least recent, or every fire looks new.

Each transition bumps the alert's version and writes one outbox message with the id
`<key>:v<version>`, in the same transaction as the state change, so a crash can neither lose a
notification nor send one twice under different ids.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from riskfeed import geo
from riskfeed.config import Settings
from riskfeed.fires import Cluster, Owner, explaining_incident
from riskfeed.models import Health, Hotspot, Incident
from riskfeed.scoring import LEVEL_RANK, CellScore
from riskfeed.store import Store


@dataclass(frozen=True)
class Transition:
    key: str
    kind: str
    name: str  # raised, escalated, cleared, confirmed, expired
    version: int
    title: str


def _emit(
    store: Store, alert: dict[str, Any], name: str, title: str, text: str, now: dt.datetime
) -> Transition:
    alert["version"] += 1
    alert["updated_at"] = now
    store.put_alert(alert)
    message_id = f"{alert['key']}:v{alert['version']}"
    payload = {
        "id": message_id,
        "key": alert["key"],
        "kind": alert["kind"],
        "transition": name,
        "at": now.isoformat(),
        "level": alert["level"],
        "title": title,
        "text": text,
        "details": alert["details"],
    }
    store.enqueue(message_id, alert["key"], name, payload, now)
    return Transition(alert["key"], alert["kind"], name, alert["version"], title)


def _cells(count: int) -> str:
    return f"{count} cell" if count == 1 else f"{count} cells"


@dataclass
class Area:
    """Every scored cell attributed to one fire, summarised by its worst cell."""

    key: str
    name: str
    top: CellScore
    cells: int = 0

    def details(self) -> dict[str, Any]:
        lat, lon = geo.centroid(self.top.cell)
        return {
            "fire": self.name,
            "score": self.top.score,
            "level": self.top.level,
            "cells_at_or_above_50": self.cells,
            "top_cell": self.top.cell,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "reasons": self.top.reasons(),
        }


def areas(
    scores: dict[str, CellScore],
    owners: dict[str, Owner],
    incidents: list[Incident],
    settings: Settings,
) -> dict[str, Area]:
    """Cells scoring 50 or more, grouped by the fire they belong to.

    A cell belongs to the fire most of its detections were attributed to, or else to the incident
    that best explains its centre. Heat no known fire explains is left to ignition alerts, so one
    event never pages twice.
    """
    out: dict[str, Area] = {}
    planned = {i.id for i in incidents if i.kind == "prescribed"}
    for score in scores.values():
        if score.score < settings.clear_below:
            continue
        owner = owners.get(score.cell)
        if owner is None:
            lat, lon = geo.centroid(score.cell)
            match = explaining_incident(lat, lon, incidents, settings.ignition_match_km)
            owner = Owner(match[0].id, match[0].name) if match else None
        if owner is None or owner.id in planned:
            continue  # prescribed burns are planned heat: shown on the map, never paged
        key = f"risk:incident:{owner.id}"
        area = out.setdefault(key, Area(key, owner.name, score))
        area.cells += 1
        if (score.score, score.cell) > (area.top.score, area.top.cell):
            area.top = score
    return out


def _risk(
    store: Store,
    now: dt.datetime,
    current_areas: dict[str, Area],
    missing: list[str],
    settings: Settings,
) -> list[Transition]:
    out: list[Transition] = []
    open_alerts = {a["key"]: a for a in store.open_alerts("risk")}
    for area in sorted(current_areas.values(), key=lambda a: (-a.top.score, a.key)):
        top = area.top
        current = open_alerts.get(area.key)
        if current is None:
            if top.score < settings.raise_at:
                continue
            previous = store.alert(area.key)
            if (
                previous
                and previous["closed_at"]
                and now - previous["closed_at"] < settings.cooldown
            ):
                continue  # cleared recently; wait out the cooldown before paging again
            alert = {
                "key": area.key,
                "kind": "risk",
                "status": "open",
                "version": previous["version"] if previous else 0,
                "opened_at": now,
                "closed_at": None,
                "updated_at": now,
                "level": top.level,
                "details": area.details(),
            }
            out.append(
                _emit(
                    store,
                    alert,
                    "raised",
                    f"{top.level.title()} fire risk: {area.name}",
                    f"Score {top.score} across {_cells(area.cells)}. " + "; ".join(top.reasons()),
                    now,
                )
            )
        elif (
            top.score >= settings.raise_at and LEVEL_RANK[top.level] > LEVEL_RANK[current["level"]]
        ):
            current["level"], current["details"] = top.level, area.details()
            out.append(
                _emit(
                    store,
                    current,
                    "escalated",
                    f"Fire risk now {top.level}: {area.name}",
                    f"Score {top.score} across {_cells(area.cells)}. " + "; ".join(top.reasons()),
                    now,
                )
            )
        else:
            current["details"], current["updated_at"] = area.details(), now
            store.put_alert(current)  # keep the latest numbers without notifying anyone

    for key, current in open_alerts.items():
        if key in current_areas:
            continue
        name = current["details"].get("fire", key)
        if missing:
            current["details"]["clear_deferred"] = (
                f"every cell below {settings.clear_below}, but {', '.join(missing)} not current"
            )
            current["updated_at"] = now
            store.put_alert(current)
            continue
        current["details"].pop("clear_deferred", None)
        current["details"]["score"] = 0
        current["status"], current["closed_at"] = "closed", now
        out.append(
            _emit(
                store,
                current,
                "cleared",
                f"Fire risk eased: {name}",
                f"Every cell is now below {settings.clear_below}.",
                now,
            )
        )
    return out


def _ignitions(
    store: Store,
    now: dt.datetime,
    candidates: list[Cluster],
    incidents: list[Incident],
    recent: list[Hotspot],
    health: dict[str, Health],
    settings: Settings,
    not_a_fire: Callable[..., str | None],
) -> list[Transition]:
    out: list[Transition] = []
    open_alerts = store.open_alerts("ignition")
    for group in candidates:
        existing = next(
            (
                a
                for a in open_alerts
                if geo.haversine_km(a["details"]["lat"], a["details"]["lon"], group.lat, group.lon)
                <= settings.ignition_same_alert_km
            ),
            None,
        )
        if existing:
            existing["details"].update(
                detections=len(group.hotspots),
                last_detected=group.last.isoformat(),
                peak_frp=round(group.peak_frp, 1),
            )
            existing["updated_at"] = now
            store.put_alert(existing)
            continue
        if health["calfire"] == "down":
            continue  # with no incident list, every fire would look new
        key = f"ignition:{geo.cell(group.lat, group.lon, 5)}:{group.first:%Y%m%dT%H%MZ}"
        if store.alert(key):
            continue  # this exact group was already reported and closed
        nearest = min(
            (geo.haversine_km(group.lat, group.lon, i.lat, i.lon) for i in incidents), default=None
        )
        alert: dict[str, Any] = {
            "key": key,
            "kind": "ignition",
            "status": "open",
            "version": 0,
            "opened_at": now,
            "closed_at": None,
            "updated_at": now,
            "level": "possible",
            "details": {
                "lat": round(group.lat, 4),
                "lon": round(group.lon, 4),
                "detections": len(group.hotspots),
                "first_detected": group.first.isoformat(),
                "last_detected": group.last.isoformat(),
                "peak_frp": round(group.peak_frp, 1),
                "nearest_incident_km": round(nearest, 1) if nearest else None,
            },
        }
        near = f" The nearest listed incident is {nearest:.0f} km away." if nearest else ""
        out.append(
            _emit(
                store,
                alert,
                "raised",
                "Possible new fire",
                f"{len(group.hotspots)} satellite detections at {group.lat:.3f}, "
                f"{group.lon:.3f} since {group.first:%H:%M} UTC with no reported "
                f"incident, peak {group.peak_frp:.0f} MW.{near}",
                now,
            )
        )
        open_alerts.append(alert)

    for alert in store.open_alerts("ignition"):
        lat, lon = alert["details"]["lat"], alert["details"]["lon"]
        reason = not_a_fire(lat, lon, alert["details"])
        if reason:
            alert["details"]["dismissed"] = reason
            alert["status"], alert["closed_at"], alert["level"] = "closed", now, "dismissed"
            out.append(
                _emit(
                    store,
                    alert,
                    "dismissed",
                    "Possible new fire: not a fire",
                    f"Withdrawn: {reason}.",
                    now,
                )
            )
            continue
        if health["calfire"] == "ok":
            match = explaining_incident(lat, lon, incidents, settings.ignition_match_km)
            if match:
                incident, distance = match
                listed = store.incident_first_seen(incident.id)
                lead = (
                    (listed - alert["opened_at"])
                    if listed and listed >= alert["opened_at"]
                    else None
                )
                alert["details"].update(
                    incident=incident.name,
                    incident_id=incident.id,
                    distance_km=round(distance, 1),
                    lead_minutes=int(lead.total_seconds() // 60) if lead is not None else None,
                )
                alert["status"], alert["closed_at"], alert["level"] = "closed", now, "confirmed"
                ahead = (
                    (
                        f" Satellites flagged it {lead.total_seconds() / 3600:.1f} h before "
                        f"this feed first saw it in CAL FIRE's list."
                    )
                    if lead
                    else ""
                )
                out.append(
                    _emit(
                        store,
                        alert,
                        "confirmed",
                        f"Confirmed: {incident.name}",
                        f"CAL FIRE now lists {incident.name}, {distance:.1f} km from "
                        f"the detections.{ahead}",
                        now,
                    )
                )
                continue
        if health["firms"] == "ok":
            nearby = [
                h
                for h in recent
                if geo.haversine_km(lat, lon, h.lat, h.lon) <= settings.ignition_same_alert_km
            ]
            if not nearby:
                alert["status"], alert["closed_at"], alert["level"] = "closed", now, "expired"
                hours = settings.ignition_window.total_seconds() / 3600
                out.append(
                    _emit(
                        store,
                        alert,
                        "expired",
                        "Possible new fire: no further heat",
                        f"No detections for {hours:.0f} h and no incident reported: "
                        "likely a short burn (agricultural or prescribed) or a false "
                        "detection.",
                        now,
                    )
                )
    return out


def evaluate(
    store: Store,
    now: dt.datetime,
    scores: dict[str, CellScore],
    owners: dict[str, Owner],
    candidates: list[Cluster],
    incidents: list[Incident],
    recent: list[Hotspot],
    health: dict[str, Health],
    settings: Settings,
    not_a_fire: Callable[..., str | None] = lambda *_: None,
) -> list[Transition]:
    """Every alert transition for this run. `candidates` are qualifying unattributed groups."""
    missing = sorted(source for source, state in health.items() if state != "ok")
    current_areas = areas(scores, owners, incidents, settings)
    return [
        *_risk(store, now, current_areas, missing, settings),
        *_ignitions(store, now, candidates, incidents, recent, health, settings, not_a_fire),
    ]


def describe(alert: dict[str, Any], last: dict[str, Any] | None) -> tuple[str, str]:
    """Title and text for an open alert as it stands now, not as it was when last announced."""
    d = alert["details"]
    if alert["kind"] == "risk":
        title = f"{d.get('level', alert['level']).title()} fire risk: {d.get('fire', alert['key'])}"
        text = (
            f"Score {d['score']} across {_cells(d.get('cells_at_or_above_50', 0))}. "
            + "; ".join(d.get("reasons", []))
        )
    else:
        title = (last or {}).get("title", "Possible new fire")
        text = (
            f"{d['detections']} satellite detections at {d['lat']:.3f}, {d['lon']:.3f}, "
            f"last at {d['last_detected'][11:16]} UTC, peak {d['peak_frp']:.0f} MW, "
            "with no reported incident."
        )
    if d.get("clear_deferred"):
        text += f" Not cleared yet: {d['clear_deferred']}."
    return title, text
