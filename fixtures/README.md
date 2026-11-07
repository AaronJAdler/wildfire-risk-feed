# Recorded scenario: Caldor Fire, August 2021

Built by `scripts/build_fixtures.py` from Project 1's replay data. Rebuild with:

    uv run python scripts/build_fixtures.py --replay ../ca-wildfire-elt/data/replay

| File | Real or generated |
|---|---|
| `ticks/*/calfire.json` | **Real.** CAL FIRE's incident feed as published at the end of each day (recorded by [simonw/ca-fires-history](https://github.com/simonw/ca-fires-history)). Tick 1 also includes the feed's real Peak Fire record from 2021-07-20, whose longitude was published as -11846461. |
| `ticks/*/firms.csv` | **Generated** with a fixed seed, in the FIRMS area-API column format, placed around real fire locations at realistic VIIRS overpass times. Includes rows in Nevada and Oregon and one malformed row. |
| `ticks/3-firms-outage/firms.csv.status` | Makes every FIRMS request in tick 3 answer 503. |
| `ticks/*/nws_alerts.json`, `zones/` | **Generated** in the real NWS GeoJSON format. Zone codes `CAZ901`-`CAZ903` and their box outlines are invented. Includes an update that supersedes a warning, a cancellation, a test message and a non-fire event. |
