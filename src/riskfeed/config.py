"""Settings for one run of the feed: everything tunable, with the reason for its value."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

# California with a margin, so fires on the Oregon and Nevada lines are kept.
CA_BBOX = (-124.9, 32.0, -113.6, 42.5)  # west, south, east, north

FIRMS_SATELLITES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")


@dataclass(frozen=True)
class Settings:
    # NWS asks every client to identify itself. This names the project, not a person.
    user_agent: str = "wildfire-risk-feed/0.1 (+https://github.com/AaronJAdler/wildfire-risk-feed)"
    firms_map_key: str | None = None
    webhook_url: str | None = None

    calfire_url: str = "https://www.fire.ca.gov/umbraco/api/IncidentApi/List?inactive=false"
    nws_alerts_url: str = "https://api.weather.gov/alerts/active?area=CA"
    firms_area_url: str = (
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{satellite}/{bbox}/1"
    )
    # Used when no FIRMS key is configured: NASA's public 24-hour file for the lower 48 states.
    firms_public_url: str = (
        "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/"
        "SUOMI_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv"
    )

    # How old a source's last good data may be before the source counts as down.
    max_age: dict[str, dt.timedelta] = field(
        default_factory=lambda: {
            "calfire": dt.timedelta(hours=6),
            "firms": dt.timedelta(hours=12),
            "nws": dt.timedelta(hours=2),
        }
    )

    h3_resolution: int = 6  # cells of about 36 km2
    hotspot_window: dt.timedelta = dt.timedelta(hours=24)
    hotspot_half_life_hours: float = 6.0

    # Risk alerts open at raise_at and close below clear_below. The gap stops a cell that
    # hovers around one threshold from paging people every run.
    raise_at: int = 70
    clear_below: int = 50
    cooldown: dt.timedelta = dt.timedelta(hours=6)

    # A hotspot cluster is a possible new fire when no known incident is this close.
    ignition_match_km: float = 10.0
    ignition_cluster_resolution: int = 7  # cells of about 5 km2
    ignition_window: dt.timedelta = dt.timedelta(hours=12)
    ignition_same_alert_km: float = 5.0

    # Retries within one run, and the circuit breaker across runs.
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    breaker_threshold: int = 3
    breaker_cooldown: dt.timedelta = dt.timedelta(minutes=30)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            firms_map_key=os.environ.get("FIRMS_MAP_KEY") or None,
            webhook_url=os.environ.get("RISKFEED_WEBHOOK_URL") or None,
        )
