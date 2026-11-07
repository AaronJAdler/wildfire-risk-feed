"""Everything the feed remembers between runs, in one DuckDB file.

Timestamps are stored as naive UTC and converted at this boundary, so no time zone database is
needed to read them back.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from riskfeed.fires import Owner
from riskfeed.models import Hotspot, Incident, Perimeter, WeatherAlert

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url VARCHAR PRIMARY KEY, etag VARCHAR, last_modified VARCHAR, body BLOB, stored_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS fetch_log (
    run_id VARCHAR, source VARCHAR, url VARCHAR, status INTEGER, attempts INTEGER,
    elapsed_ms INTEGER, from_cache BOOLEAN, error VARCHAR, logged_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS breaker (
    source VARCHAR PRIMARY KEY, failures INTEGER, open_until TIMESTAMP);
CREATE TABLE IF NOT EXISTS source_state (
    source VARCHAR PRIMARY KEY, last_success TIMESTAMP, records INTEGER, quarantined INTEGER);
CREATE TABLE IF NOT EXISTS incidents (
    id VARCHAR PRIMARY KEY, name VARCHAR, lat DOUBLE, lon DOUBLE, acres DOUBLE,
    contained DOUBLE, started TIMESTAMP, updated TIMESTAMP, counties VARCHAR, url VARCHAR,
    active BOOLEAN, first_seen TIMESTAMP, last_seen TIMESTAMP);
CREATE TABLE IF NOT EXISTS hotspots (
    key VARCHAR PRIMARY KEY, lat DOUBLE, lon DOUBLE, detected_at TIMESTAMP, satellite VARCHAR,
    confidence DOUBLE, frp DOUBLE, daynight VARCHAR, first_seen TIMESTAMP);
CREATE TABLE IF NOT EXISTS weather_alerts (
    id VARCHAR PRIMARY KEY, event VARCHAR, headline VARCHAR, onset TIMESTAMP, ends TIMESTAMP,
    cells VARCHAR, zones VARCHAR);
CREATE TABLE IF NOT EXISTS quarantine (
    run_id VARCHAR, source VARCHAR, reason VARCHAR, record VARCHAR, quarantined_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, run_at TIMESTAMP, completeness VARCHAR, sources VARCHAR);
CREATE TABLE IF NOT EXISTS cell_scores (
    run_id VARCHAR, cell VARCHAR, score INTEGER, level VARCHAR, fire DOUBLE, incident DOUBLE,
    weather DOUBLE, PRIMARY KEY (run_id, cell));
CREATE TABLE IF NOT EXISTS alerts (
    key VARCHAR PRIMARY KEY, kind VARCHAR, status VARCHAR, version INTEGER, opened_at TIMESTAMP,
    closed_at TIMESTAMP, updated_at TIMESTAMP, level VARCHAR, details VARCHAR);
CREATE TABLE IF NOT EXISTS perimeters (
    incident_id VARCHAR PRIMARY KEY, name VARCHAR, acres DOUBLE, cells VARCHAR);
CREATE TABLE IF NOT EXISTS footprint (
    cell VARCHAR PRIMARY KEY, incident_id VARCHAR, incident_name VARCHAR, last_seen TIMESTAMP);
CREATE TABLE IF NOT EXISTS outbox (
    id VARCHAR PRIMARY KEY, alert_key VARCHAR, transition VARCHAR, payload VARCHAR,
    created_at TIMESTAMP, delivered_at TIMESTAMP, attempts INTEGER DEFAULT 0, last_error VARCHAR);
"""


def to_db(moment: dt.datetime | None) -> dt.datetime | None:
    return None if moment is None else moment.astimezone(dt.UTC).replace(tzinfo=None)


def from_db(moment: dt.datetime | None) -> dt.datetime | None:
    return None if moment is None else moment.replace(tzinfo=dt.UTC)


def _utc(moment: dt.datetime) -> dt.datetime:
    result = from_db(moment)
    assert result is not None  # noqa: S101 - only called on NOT NULL columns
    return result


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(path))
        self.db.execute(SCHEMA)
        # Columns added after the first live runs; existing state files gain them in place.
        for column, kind in (
            ("source", "VARCHAR DEFAULT 'calfire'"),
            ("kind", "VARCHAR DEFAULT 'wildfire'"),
            ("agency", "VARCHAR"),
        ):
            self.db.execute(f"ALTER TABLE incidents ADD COLUMN IF NOT EXISTS {column} {kind}")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.begin()
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self.db.execute(sql, params or [])
        names = [d[0] for d in cursor.description or []]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # HTTP cache and fetch log
    def cache_get(self, url: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT etag, last_modified, body FROM http_cache WHERE url = ?", [url])
        return rows[0] if rows else None

    def cache_put(self, url: str, etag: str | None, last_modified: str | None, body: bytes) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?, now()::TIMESTAMP)",
            [url, etag, last_modified, body],
        )

    def log_fetch(
        self,
        run_id: str,
        source: str,
        url: str,
        status: int,
        attempts: int,
        elapsed_ms: int,
        from_cache: bool,
        error: str | None,
        at: dt.datetime,
    ) -> None:
        self.db.execute(
            "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, source, url, status, attempts, elapsed_ms, from_cache, error, to_db(at)],
        )

    # Circuit breaker and source freshness
    def breaker(self, source: str) -> tuple[int, dt.datetime | None]:
        rows = self._rows("SELECT failures, open_until FROM breaker WHERE source = ?", [source])
        return (rows[0]["failures"], from_db(rows[0]["open_until"])) if rows else (0, None)

    def set_breaker(self, source: str, failures: int, open_until: dt.datetime | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO breaker VALUES (?, ?, ?)", [source, failures, to_db(open_until)]
        )

    def mark_success(self, source: str, at: dt.datetime, records: int, quarantined: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO source_state VALUES (?, ?, ?, ?)",
            [source, to_db(at), records, quarantined],
        )

    def last_success(self, source: str) -> dt.datetime | None:
        rows = self._rows("SELECT last_success FROM source_state WHERE source = ?", [source])
        return from_db(rows[0]["last_success"]) if rows else None

    def quarantine(
        self, run_id: str, source: str, items: list[dict[str, Any]], at: dt.datetime
    ) -> None:
        for item in items:
            self.db.execute(
                "INSERT INTO quarantine VALUES (?, ?, ?, ?, ?)",
                [
                    run_id,
                    source,
                    item["reason"],
                    json.dumps(item["record"], default=str),
                    to_db(at),
                ],
            )

    # Incidents: the latest successful CAL FIRE fetch is the truth; first_seen survives.
    def replace_incidents(
        self, incidents: list[Incident], at: dt.datetime, source: str = "calfire"
    ) -> None:
        """The latest successful fetch from one source replaces that source's incidents."""
        self.db.execute("UPDATE incidents SET active = false WHERE source = ?", [source])
        for i in incidents:
            self.db.execute(
                """
                INSERT INTO incidents (id, name, lat, lon, acres, contained, started, updated,
                    counties, url, active, first_seen, last_seen, source, kind, agency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET name = excluded.name, lat = excluded.lat,
                    lon = excluded.lon, acres = excluded.acres, contained = excluded.contained,
                    started = excluded.started, updated = excluded.updated,
                    counties = excluded.counties, url = excluded.url, active = true,
                    last_seen = excluded.last_seen, source = excluded.source,
                    kind = excluded.kind, agency = excluded.agency
                """,
                [
                    i.id,
                    i.name,
                    i.lat,
                    i.lon,
                    i.acres,
                    i.contained,
                    to_db(i.started),
                    to_db(i.updated),
                    json.dumps(list(i.counties)),
                    i.url,
                    to_db(at),
                    to_db(at),
                    i.source,
                    i.kind,
                    i.agency,
                ],
            )

    def active_incidents(self) -> list[Incident]:
        return [
            Incident(
                r["id"],
                r["name"],
                r["lat"],
                r["lon"],
                r["acres"],
                r["contained"],
                from_db(r["started"]),
                _utc(r["updated"]),
                tuple(json.loads(r["counties"])),
                r["url"],
                r["source"] or "calfire",
                r["kind"] or "wildfire",
                r["agency"],
            )
            for r in self._rows("SELECT * FROM incidents WHERE active ORDER BY id")
        ]

    def incident_first_seen(self, incident_id: str) -> dt.datetime | None:
        rows = self._rows("SELECT first_seen FROM incidents WHERE id = ?", [incident_id])
        return from_db(rows[0]["first_seen"]) if rows else None

    # Hotspots accumulate; a detection is stored once however many feeds repeat it.
    def add_hotspots(self, hotspots: list[Hotspot], at: dt.datetime) -> int:
        before = self.db.execute("SELECT count(*) FROM hotspots").fetchone()
        for h in hotspots:
            self.db.execute(
                "INSERT OR IGNORE INTO hotspots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    h.key,
                    h.lat,
                    h.lon,
                    to_db(h.detected_at),
                    h.satellite,
                    h.confidence,
                    h.frp,
                    h.daynight,
                    to_db(at),
                ],
            )
        after = self.db.execute("SELECT count(*) FROM hotspots").fetchone()
        return int(after[0] - before[0]) if after and before else 0

    def persistent_cells(
        self, now: dt.datetime, resolution: int, days: int = 30, min_days: int = 3
    ) -> set[str]:
        """Cells with detections on `min_days` or more separate days in the last `days`.

        Real wildfires move; refineries, landfills and power plants do not. This is the same idea
        as NASA's static thermal anomaly mask, learned from the feed's own history.
        """
        from riskfeed.geo import cell  # local import: geo has no store dependency

        rows = self.db.execute(
            "SELECT lat, lon, CAST(detected_at AS DATE) FROM hotspots WHERE detected_at > ?",
            [to_db(now - dt.timedelta(days=days))],
        ).fetchall()
        seen: dict[str, set[object]] = {}
        for lat, lon, day in rows:
            seen.setdefault(cell(lat, lon, resolution), set()).add(day)
        return {c for c, d in seen.items() if len(d) >= min_days}

    def forget_hotspots(self, before: dt.datetime) -> None:
        self.db.execute("DELETE FROM hotspots WHERE detected_at < ?", [to_db(before)])

    def hotspots_between(self, start: dt.datetime, end: dt.datetime) -> list[Hotspot]:
        rows = self._rows(
            "SELECT * FROM hotspots WHERE detected_at > ? AND detected_at <= ? ORDER BY key",
            [to_db(start), to_db(end)],
        )
        return [
            Hotspot(
                r["lat"],
                r["lon"],
                _utc(r["detected_at"]),
                r["satellite"],
                r["confidence"],
                r["frp"],
                r["daynight"],
            )
            for r in rows
        ]

    # Perimeters: the latest successful WFIGS fetch is the truth.
    def replace_perimeters(self, perimeters: list[Perimeter]) -> None:
        self.db.execute("DELETE FROM perimeters")
        for p in perimeters:
            self.db.execute(
                "INSERT OR REPLACE INTO perimeters VALUES (?, ?, ?, ?)",
                [p.incident_id, p.name, p.acres, json.dumps(sorted(p.cells))],
            )

    def perimeters(self) -> list[Perimeter]:
        return [
            Perimeter(r["incident_id"], r["name"], r["acres"], frozenset(json.loads(r["cells"])))
            for r in self._rows("SELECT * FROM perimeters ORDER BY incident_id")
        ]

    # Footprint: ground each fire has burned recently, so its spread stays attributed to it.
    def footprint(self, since: dt.datetime) -> dict[str, Owner]:
        rows = self._rows(
            "SELECT cell, incident_id, incident_name FROM footprint WHERE last_seen > ?",
            [to_db(since)],
        )
        return {r["cell"]: Owner(r["incident_id"], r["incident_name"]) for r in rows}

    def update_footprint(
        self, cells: dict[str, tuple[Owner, dt.datetime]], forget_before: dt.datetime
    ) -> None:
        for cell_id, (owner, seen) in cells.items():
            self.db.execute(
                """
                INSERT INTO footprint VALUES (?, ?, ?, ?)
                ON CONFLICT (cell) DO UPDATE SET incident_id = excluded.incident_id,
                    incident_name = excluded.incident_name,
                    last_seen = greatest(footprint.last_seen, excluded.last_seen)
                """,
                [cell_id, owner.id, owner.name, to_db(seen)],
            )
        self.db.execute("DELETE FROM footprint WHERE last_seen < ?", [to_db(forget_before)])

    # Weather alerts: the latest successful NWS fetch is the truth.
    def replace_weather(self, alerts: list[WeatherAlert]) -> None:
        self.db.execute("DELETE FROM weather_alerts")
        for a in alerts:
            self.db.execute(
                "INSERT INTO weather_alerts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    a.id,
                    a.event,
                    a.headline,
                    to_db(a.onset),
                    to_db(a.ends),
                    json.dumps(sorted(a.cells)),
                    json.dumps(list(a.zones)),
                ],
            )

    def weather_alerts(self) -> list[WeatherAlert]:
        return [
            WeatherAlert(
                r["id"],
                r["event"],
                r["headline"],
                from_db(r["onset"]),
                from_db(r["ends"]),
                frozenset(json.loads(r["cells"])),
                tuple(json.loads(r["zones"])),
            )
            for r in self._rows("SELECT * FROM weather_alerts ORDER BY id")
        ]

    # Runs and scores
    def record_run(
        self,
        run_id: str,
        at: dt.datetime,
        completeness: str,
        sources: dict[str, Any],
        scores: list[dict[str, Any]],
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?)",
            [run_id, to_db(at), completeness, json.dumps(sources, default=str)],
        )
        self.db.execute("DELETE FROM cell_scores WHERE run_id = ?", [run_id])
        for s in scores:
            self.db.execute(
                "INSERT INTO cell_scores VALUES (?, ?, ?, ?, ?, ?, ?)",
                [run_id, s["cell"], s["score"], s["level"], s["fire"], s["incident"], s["weather"]],
            )

    # Alert state and outbox
    def alert(self, key: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM alerts WHERE key = ?", [key])
        return self._alert(rows[0]) if rows else None

    def open_alerts(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM alerts WHERE status = 'open'", []
        if kind:
            sql, params = sql + " AND kind = ?", [kind]
        return [self._alert(r) for r in self._rows(sql + " ORDER BY key", params)]

    @staticmethod
    def _alert(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "details": json.loads(row["details"]),
            "opened_at": from_db(row["opened_at"]),
            "closed_at": from_db(row["closed_at"]),
            "updated_at": from_db(row["updated_at"]),
        }

    def put_alert(self, alert: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                alert["key"],
                alert["kind"],
                alert["status"],
                alert["version"],
                to_db(alert["opened_at"]),
                to_db(alert["closed_at"]),
                to_db(alert["updated_at"]),
                alert["level"],
                json.dumps(alert["details"], default=str),
            ],
        )

    def enqueue(
        self,
        message_id: str,
        alert_key: str,
        transition: str,
        payload: dict[str, Any],
        at: dt.datetime,
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO outbox (id, alert_key, transition, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [message_id, alert_key, transition, json.dumps(payload, default=str), to_db(at)],
        )

    def latest_messages(self) -> dict[str, dict[str, Any]]:
        """The most recent message for each alert, for showing open alerts with their text."""
        rows = self._rows(
            "SELECT alert_key, payload FROM outbox QUALIFY row_number() OVER "
            "(PARTITION BY alert_key ORDER BY created_at DESC) = 1"
        )
        return {r["alert_key"]: json.loads(r["payload"]) for r in rows}

    def pending(self) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT id, payload, attempts FROM outbox WHERE delivered_at IS NULL "
            "ORDER BY created_at, id"
        )
        return [{**r, "payload": json.loads(r["payload"])} for r in rows]

    def delivered(self, message_id: str, at: dt.datetime) -> None:
        self.db.execute(
            "UPDATE outbox SET delivered_at = ?, attempts = attempts + 1 WHERE id = ?",
            [to_db(at), message_id],
        )

    def failed(self, message_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            [error, message_id],
        )
