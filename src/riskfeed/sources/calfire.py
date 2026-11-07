"""CAL FIRE's public incident list: one record per fire it is tracking."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from riskfeed.config import CA_BBOX, Settings
from riskfeed.geo import in_bbox
from riskfeed.http import Fetcher
from riskfeed.models import Incident, SourceResult


class CalFireRecord(BaseModel):
    """The fields the feed relies on. Anything else CAL FIRE adds is ignored, not an error."""

    model_config = ConfigDict(extra="ignore")

    unique_id: str = Field(alias="UniqueId", min_length=1)
    name: str = Field(alias="Name")
    latitude: float | None = Field(alias="Latitude", default=None)
    longitude: float | None = Field(alias="Longitude", default=None)
    acres: float | None = Field(alias="AcresBurned", default=None, ge=0)
    contained: float | None = Field(alias="PercentContained", default=None, ge=0, le=100)
    started: dt.datetime | None = Field(alias="Started", default=None)
    updated: dt.datetime = Field(alias="Updated")
    active: bool | None = Field(alias="Active", default=None)
    is_active: bool | None = Field(alias="IsActive", default=None)  # older shape of the feed
    final: bool | None = Field(alias="Final", default=None)
    counties: list[str] | str | None = Field(alias="Counties", default=None)
    county: str | None = Field(alias="County", default=None)  # older shape of the feed
    canonical_url: str | None = Field(alias="CanonicalUrl", default=None)

    @field_validator("started", "updated")
    @classmethod
    def assume_utc(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value


def parse(payload: Any) -> tuple[list[Incident], list[dict[str, Any]]]:
    """Valid active incidents, and the records that failed validation with the reason."""
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list of incidents, got {type(payload).__name__}")
    incidents: list[Incident] = []
    quarantined: list[dict[str, Any]] = []
    for raw in payload:
        try:
            record = CalFireRecord.model_validate(raw)
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            quarantined.append({"reason": reason, "record": raw})
            continue
        if not (record.active or record.is_active) or record.final:
            continue
        lat, lon = record.latitude, record.longitude
        if lat is None or lon is None or not in_bbox(lat, lon, CA_BBOX):
            # The 2021 feed once published a longitude of -11846461.
            quarantined.append(
                {"reason": f"location outside California: {lat}, {lon}", "record": raw}
            )
            continue
        counties = (
            record.counties
            if isinstance(record.counties, list)
            else [
                c.strip() for c in (record.counties or record.county or "").split(",") if c.strip()
            ]
        )
        url = f"https://www.fire.ca.gov{record.canonical_url}" if record.canonical_url else None
        incidents.append(
            Incident(
                record.unique_id,
                record.name,
                lat,
                lon,
                record.acres,
                record.contained,
                record.started,
                record.updated,
                tuple(counties),
                url,
            )
        )
    return incidents, quarantined


def fetch(fetcher: Fetcher, settings: Settings) -> SourceResult:
    response = fetcher.get("calfire", settings.calfire_url, accept="application/json")
    if not response.ok or response.body is None:
        return SourceResult("calfire", ok=False, detail=response.error or "no body")
    try:
        incidents, quarantined = parse(json.loads(response.body))
    except ValueError as exc:  # includes JSON errors: an HTML error page served with 200
        return SourceResult("calfire", ok=False, detail=f"unreadable payload: {exc}"[:200])
    detail = f"{len(incidents)} active incidents" + (
        " (not modified)" if response.from_cache else ""
    )
    return SourceResult(
        "calfire", ok=True, records=incidents, quarantined=quarantined, detail=detail
    )
