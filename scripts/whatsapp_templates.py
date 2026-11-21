"""Meta WhatsApp template definitions for the driver notifications (build item E6). Preparation only: nothing is
submitted to Meta; the owner submits them in WhatsApp Manager (or the Graph API) after approval.

    python scripts/whatsapp_templates.py            # writes infra/whatsapp_templates.json

Generated from the reviewed notice texts in `setuhaul/notify/outbound.py` (English, Hindi, Hinglish; S4-R2-08), so
the submitted wording can never drift from what the code sends (`tests/test_infra.py` checks the file is current).
Category UTILITY (transactional notices about a booking the driver asked for). No variables: the texts are fixed.

Hinglish has no language code of its own at Meta. It is listed under `en` (Latin script) with the suffix `_hl`; the
owner confirms that choice at submission (an owner decision recorded in docs/release/05_STEP6_GATE_STATUS.md).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setuhaul.notify.outbound import TEMPLATES, TEMPLATES_HI, TEMPLATES_HL  # noqa: E402

OUT = ROOT / "infra" / "whatsapp_templates.json"


def definitions() -> list[dict]:
    out = []
    for table, lang, suffix in ((TEMPLATES, "en", ""), (TEMPLATES_HI, "hi", ""), (TEMPLATES_HL, "en", "_hl")):
        for key in sorted(table):
            out.append({"name": f"setuhaul_{key}{suffix}", "language": lang, "category": "UTILITY",
                        "components": [{"type": "BODY", "text": table[key]}]})
    return out


def render() -> str:
    return json.dumps({"generated_from": "setuhaul/notify/outbound.py", "submitted": False,
                       "templates": definitions()}, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"{OUT}: {len(definitions())} templates (not submitted)")
