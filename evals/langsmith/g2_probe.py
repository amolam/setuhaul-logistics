"""Phase G.2: why does LangSmith refuse the key? Read-only diagnosis; it never prints the key.

    python -m evals.langsmith.g2_probe

Uses LANGSMITH_API_KEY / LANGSMITH_ENDPOINT / LANGSMITH_WORKSPACE_ID from the shell. Makes only GET calls: the
server info (no key), the workspaces the key can see, and one dataset and project listing. Prints each HTTP status
and LangSmith's own error text, scrubbed by the G.1 redaction (keys, tokens, e-mails, phones removed).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from evals.langsmith.g2_smoke import config  # noqa: E402
from setuhaul.observability.redaction import scrub  # noqa: E402

PROBES = [("server info (no key)", "/info", False), ("workspaces this key sees", "/api/v1/workspaces", True),
          ("datasets (legacy path)", "/datasets?limit=1", True), ("datasets (/api/v1)", "/api/v1/datasets?limit=1", True),
          ("projects (/api/v1)", "/api/v1/sessions?limit=1", True)]


def main() -> int:
    env = dict(os.environ)
    cfg = config(env)
    print(f"endpoint {cfg['endpoint']}  key {cfg['key']}  workspace_id {cfg['workspace_id'] or '(not set)'}")
    key = (env.get("LANGSMITH_API_KEY") or "").strip()
    if not key or cfg["endpoint"] not in ("https://api.smith.langchain.com", "https://eu.api.smith.langchain.com",
                                          "https://apac.api.smith.langchain.com"):
        print("set LANGSMITH_API_KEY and a LangSmith Cloud LANGSMITH_ENDPOINT first")
        return 2
    hdr = {"x-api-key": key}
    if cfg["workspace_id"]:
        hdr["X-Tenant-Id"] = cfg["workspace_id"]
    with httpx.Client(timeout=10.0) as http:
        for label, path, auth in PROBES:
            try:
                r = http.get(cfg["endpoint"] + path, headers=hdr if auth else {})
                body = r.text
                try:
                    j = r.json()
                    if path == "/api/v1/workspaces" and isinstance(j, list):
                        body = json.dumps([{"id": w.get("id"), "name": w.get("display_name") or w.get("name")} for w in j])
                    elif r.status_code < 300 and isinstance(j, list):
                        body = f"[{len(j)} item(s)]"
                    elif r.status_code < 300 and isinstance(j, dict) and path == "/info":
                        body = json.dumps({k: j.get(k) for k in ("version", "instance_flags") if k in j})[:200]
                except ValueError:
                    pass
                print(f"{r.status_code}  {label:26} {scrub(body)[:240]}")
            except httpx.HTTPError as e:
                print(f"ERR  {label:26} {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
