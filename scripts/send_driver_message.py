"""Send one synthetic driver WhatsApp message to the deployed pilot (trusted local machine only).

    python scripts/send_driver_message.py "stuck in traffic, approximately 2 hours late"
    python scripts/send_driver_message.py --phone +99912345678 "YES"          # continue that driver's conversation
    python scripts/send_driver_message.py --facility FAC-GGN-01 "running late"

Uses the operator's AWS CLI session: reads the webhook signing secret from Secrets Manager, signs the body exactly as
Meta does (X-Hub-Signature-256) and posts it to the real API Gateway URL. The secret never leaves this process and is
never printed; a browser never has it. Without --phone it first creates a synthetic driver and shipment (SYN- ids,
a +999 number, which no real phone can have). Then open the staff console's Conversations tab.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_aws import Pilot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--phone", help="an existing synthetic driver's +999 number (continue the conversation)")
    ap.add_argument("--facility", default="FAC-JAI-01", choices=("FAC-JAI-01", "FAC-GGN-01"))
    ap.add_argument("--wait", action="store_true", help="wait for the agent's reply and print it")
    a = ap.parse_args()
    if a.phone and not a.phone.startswith("+999"):
        raise SystemExit("synthetic numbers only (+999...)")
    p = Pilot()
    d = {"phone": a.phone} if a.phone else p.add_driver(eta_min=60, facility=a.facility)
    r = p.turn(d["phone"], a.text) if a.wait else p.say(d["phone"], a.text)
    out = {"driver_phone": d["phone"], "driver_id": d.get("driver_id"), "shipment_id": d.get("shipment_id"),
           "webhook_status": r["status"], "message_id": r["message_id"]}
    if a.wait:
        out["agent_reply"] = r.get("agent_reply")
    print(json.dumps(out, indent=2))
    return 0 if r["status"] == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
