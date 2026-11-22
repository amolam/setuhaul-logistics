"""Meta WhatsApp template definitions for the driver notifications (build item E6). Preparation only: nothing is
submitted to Meta; the owner submits them in WhatsApp Manager (or the Graph API) after approval.

    python scripts/whatsapp_templates.py            # writes infra/whatsapp_templates.json

Generated from the reviewed notice texts in `setuhaul/notify/outbound.py` (English, Hindi, Hinglish; S4-R2-08), so
the submitted wording can never drift from what the code sends (`tests/test_infra.py` checks the file is current).
Category UTILITY (transactional notices about a booking the driver asked for). No variables: the texts are fixed
(`setuhaul.notify.outbound.TEMPLATE_VARIABLES` is empty, and `template_params` checks the count at send time).

Hinglish (review decision, 22 Nov 2026): the seven definitions are kept, named `setuhaul_<notice>_hl`, but no language
code is assumed for them (`"language": null`). Which code Meta accepts for Latin-script Hindi, and whether it approves
these texts, is decided at a separate gate (HINGLISH_TEMPLATES) when the owner submits them. Until a Hinglish
template is recorded as approved, a Hinglish driver outside the 24-hour window gets the approved English template.
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
    for table, lang, suffix in ((TEMPLATES, "en", ""), (TEMPLATES_HI, "hi", ""), (TEMPLATES_HL, None, "_hl")):
        for key in sorted(table):
            d = {"name": f"setuhaul_{key}{suffix}", "language": lang, "category": "UTILITY",
                 "components": [{"type": "BODY", "text": table[key]}]}
            if lang is None:
                d["submission_gate"] = "HINGLISH_TEMPLATES"
            out.append(d)
    return out


def render() -> str:
    return json.dumps({"generated_from": "setuhaul/notify/outbound.py", "submitted": False,
                       "templates": definitions()}, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"{OUT}: {len(definitions())} templates (not submitted)")
