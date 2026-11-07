# 6. Test against a recorded scenario built from real CAL FIRE data

- Status: Accepted
- Date: 2026-11-07

## Context

The interesting behaviour is over time: a fire seen before it is listed, a source outage, a
recovery. Live APIs never reproduce a given day, and this project was built where the live APIs
were not reachable.

## Decision

- `fixtures/caldor-2021` holds four ticks. CAL FIRE data is real: the feed as published on
  August 14-17, 2021, plus its real Peak Fire record with longitude -11846461. FIRMS detections
  and NWS alerts are generated with a fixed seed, in the sources' exact formats, around real
  fire locations; the fixture README labels which is which.
- `fixtures.transport` serves the files through the real httpx client, with ETags, 304s and
  injected status codes, so retries, conditional requests and outages run the production code.
- CI replays the scenario on every push; the live workflow exercises the real APIs.

## Consequences

- The full lifecycle (new fire, confirmation, expiry, outage, recovery) is tested in about 20
  seconds, deterministically.
- Generated detections are plausible, not historical. Real FIRMS archives for those dates would
  make the scenario fully real; the builder script is where to swap them in.

## Alternatives considered

- **Mocks per function**: fast, but never exercises the HTTP and storage paths together.
- **Recording live responses (VCR)**: real, but cannot produce an outage on cue, and was not
  possible from the build environment.
