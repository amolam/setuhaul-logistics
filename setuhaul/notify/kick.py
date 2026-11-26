"""Demo release: event-driven outbound delivery.

A driver-facing message is stored by its producer (the agent's reply, a planner's decision) and then waited for the
outbound worker's next one-minute tick, so a reply reached the driver up to 60 s after it was ready (26 Nov 2026,
measured: replies stored ~10 s after the driver's message, notifications created on the :17 tick).

kick_outbound() asks the outbound function to run NOW, asynchronously (InvocationType=Event): the producer does not
wait, and a failed or throttled kick changes nothing, because the scheduled tick still runs and the work is stored
in PostgreSQL. Every safety rule of the relay is unchanged: one claim per notification under a lock (claim_notifications,
FOR UPDATE SKIP LOCKED), begin_send fencing by attempt number, bounded retries with backoff, the Telegram allowlist.
A kick and a tick running together is safe for the same reason the documented drain is (setuhaul/ops/pilot_mode.py).

The function name comes from SETUHAUL_OUTBOUND_FUNCTION (set by the stack); without it (tests, local runs) nothing
happens. Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("setuhaul.kick")
_client = None
_lock = threading.Lock()
_last = {"t": 0.0}
MIN_INTERVAL_S = 0.5            # one kick per producer burst (a turn storing two replies kicks once)


def _lambda():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("lambda")
    return _client


def kick_outbound(reason: str, client=None, env: dict | None = None) -> bool:
    """Ask the outbound worker to run now. True when a kick was sent. Never raises, never waits for the worker."""
    env = os.environ if env is None else env
    name = env.get("SETUHAUL_OUTBOUND_FUNCTION")
    if not name:
        return False
    with _lock:
        now = time.monotonic()
        if now - _last["t"] < MIN_INTERVAL_S:
            return False
        _last["t"] = now
    try:
        (client or _lambda()).invoke(FunctionName=name, InvocationType="Event",
                                     Payload=b'{"source": "kick", "reason": "%s"}' % reason[:40].encode())
        log.info("outbound kicked (%s)", reason[:40])
        return True
    except Exception as e:                                          # noqa: BLE001 — the tick is the fallback
        log.warning("outbound kick failed (%s); the scheduled tick delivers it", type(e).__name__)
        return False
