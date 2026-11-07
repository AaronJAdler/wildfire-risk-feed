"""Sends queued alert messages. Delivery is at least once; receivers deduplicate on the id.

A message stays in the outbox until every sink accepts it, and is retried on the next run.
If one of two sinks fails, the other may receive the message again next time, which is why
each message carries a stable id and webhooks get it as an Idempotency-Key header.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx

from riskfeed.store import Store

log = logging.getLogger(__name__)


class Sink(Protocol):
    name: str

    def send(self, message: dict[str, Any]) -> None: ...


class FileSink:
    """Appends one JSON line per message. Used locally and in CI."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, sort_keys=True) + "\n")


class WebhookSink:
    """POSTs JSON with a Slack-compatible `text` field and the full alert alongside it."""

    name = "webhook"

    def __init__(self, url: str, client: httpx.Client) -> None:
        self.url, self.client = url, client

    def send(self, message: dict[str, Any]) -> None:
        body = {"text": f"*{message['title']}*\n{message['text']}", "alert": message}
        response = self.client.post(
            self.url, json=body, timeout=10.0, headers={"Idempotency-Key": message["id"]}
        )
        response.raise_for_status()


def deliver(store: Store, sinks: list[Sink], now: dt.datetime) -> dict[str, int]:
    sent = failed = 0
    for message in store.pending():
        try:
            for sink in sinks:
                sink.send(message["payload"])
        except (httpx.HTTPError, OSError) as exc:
            store.failed(message["id"], f"{type(exc).__name__}: {exc}"[:300])
            log.warning("delivery of %s failed: %s", message["id"], exc)
            failed += 1
            continue
        store.delivered(message["id"], now)
        sent += 1
    return {"sent": sent, "failed": failed}
