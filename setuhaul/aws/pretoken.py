"""Cognito pre-token-generation trigger (event version V2_0, user pool tier Essentials): the `facility_scope` claim.

The API (setuhaul/api/security.py) reads which facilities a person may see from the access token's `facility_scope`
claim; Cognito access tokens carry no custom claims unless this trigger adds them. The scope is derived ONLY from
the person's Cognito groups named `facility-<FACILITY_ID>`, which only an administrator can assign, and only for the
facilities this deployment serves (FACILITIES). Anything a browser could send (client metadata, user attributes) is
ignored, and a value already present is overridden, never merged. No group, no facility: the API then shows nothing.
"""
from __future__ import annotations

import os
import re

PREFIX = "facility-"
FACILITY = re.compile(r"^[A-Z0-9-]{3,40}$")


def facilities_from_groups(groups: list[str] | None, allowed: set[str]) -> list[str]:
    out = set()
    for g in groups or []:
        if isinstance(g, str) and g.startswith(PREFIX):
            f = g[len(PREFIX):]
            if FACILITY.match(f) and f in allowed:
                out.add(f)
    return sorted(out)


def handler(event, _context):
    allowed = {f for f in os.environ.get("FACILITIES", "").split(",") if f}
    groups = ((event.get("request") or {}).get("groupConfiguration") or {}).get("groupsToOverride")
    scope = ",".join(facilities_from_groups(groups, allowed))
    claims = {"facility_scope": scope}
    event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": claims},
        "idTokenGeneration": {"claimsToAddOrOverride": claims},
    }
    return event
