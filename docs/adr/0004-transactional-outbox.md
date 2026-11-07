# 4. Transactional outbox and at-least-once delivery with stable ids

- Status: Accepted
- Date: 2026-11-07

## Context

Notifications go to a webhook (Slack-compatible) and a file. A run can crash between deciding an
alert changed and sending the message, or a webhook can be down. Lost alerts are dangerous;
duplicate alerts erode trust.

## Decision

- Each transition increments the alert's version and inserts one outbox row with id
  `<alert key>:v<version>`, in the same DuckDB transaction as the alert state change.
- A delivery step sends every undelivered row to every sink, marks it delivered only when all
  sinks accept it, and retries the rest on the next run.
- Webhooks receive the id as an `Idempotency-Key` header.
- Rerunning the same moment produces no new transitions, because state already reflects them.

## Consequences

- A crash can never record a transition without its message, or send a message for a
  transition that was rolled back.
- Delivery is at least once. If one of two sinks fails, the other may see the message twice;
  receivers deduplicate on the id. Tests cover a failing webhook and a repeated run.

## Alternatives considered

- **Send inline during evaluation**: loses or duplicates messages on crashes.
- **Exactly-once delivery**: not possible across an HTTP webhook without the receiver's help,
  which is what the idempotency key is.
