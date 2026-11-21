"""Migration 0011 cutover helper (review R1.9 S4-04; procedure in RUNBOOK_0011.md).

    python -m setuhaul.ops.cutover_0011 check            list every receipt whose turn may have been lost before 0011
    python -m setuhaul.ops.cutover_0011 reopen ID [ID…]  give reviewed ones a fresh generation (the sweep publishes them)

Environment: SETUHAUL_DSN, FACILITIES (comma-separated). Runs as setuhaul_ingest.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from ..api.db import Db
from ..api.service import BookingService


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("check", "reopen"):
        print(__doc__)
        return 2
    svc = BookingService(Db(os.environ["SETUHAUL_DSN"]), lambda: datetime.now(timezone.utc),
                         tuple(os.environ.get("FACILITIES", "FAC-JAI-01,FAC-GGN-01").split(",")))
    if argv[0] == "check":
        rows = svc.legacy_uncertain_receipts()
        print("message_id\tthread\treceived\tlater agent msgs\tfirst later agent msg\tnext driver msg\tprovider id")
        f = lambda t: f"{t:%Y-%m-%d %H:%M}" if t else "-"                                   # noqa: E731
        for r in rows:
            print(f'{r["message_id"]}\t{r["thread_id"]}\t{f(r["received_at"])}\t{r["later_agent_messages"]}\t'
                  f'{f(r["first_later_agent_message_at"])}\t{f(r["next_driver_message_at"])}\t{r["external_message_id"]}')
        print(f"{len(rows)} uncertain receipt(s)", file=sys.stderr)
        return 1 if rows else 0
    n = svc.reopen_receipts([int(x) for x in argv[1:]])
    print(f"reopened {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
