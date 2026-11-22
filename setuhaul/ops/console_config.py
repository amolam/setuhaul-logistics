"""Hosted staff console: a check that nothing secret is in the page's files (CI, before the zip is trusted).

    python -m setuhaul.ops.console_config --check setuhaul/api/console_static

The console is a public page served by the API (setuhaul/api/console_site.py); its config.json is built at run time
from public values only (Cognito domain, public client id). The check fails on anything in the files that looks like a
credential, a webhook secret, a model key or a private key, and on any file type the page does not use.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ALLOWED_FILES = {".html", ".js", ".css", ".json"}
CONFIG_KEYS = ("cognitoDomain", "clientId", "apiBase")
SECRET_PATTERNS = {
    "AWS access key id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS secret key assignment": re.compile(r"aws_secret_access_key|secretAccessKey", re.I),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "OpenRouter key": re.compile(r"\bsk-or-[A-Za-z0-9-]{10,}"),
    "webhook / session secret name": re.compile(r"META_APP_SECRET|SESSION_SECRET|WHATSAPP_TOKEN|x-hub-signature", re.I),
    "client secret": re.compile(r"client_secret", re.I),
    "Secrets Manager ARN": re.compile(r"arn:aws:secretsmanager:"),
    "database endpoint": re.compile(r"\.rds\.amazonaws\.com|rds-data", re.I),
    "HMAC signing": re.compile(r"\bcreateHmac\b|crypto\.subtle\.sign|HMAC", re.I),
}


def problems(folder: str | Path) -> list[str]:
    out = []
    root = Path(folder)
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = f.relative_to(root).as_posix()
        if f.suffix not in ALLOWED_FILES:
            out.append(f"{rel}: file type {f.suffix or '(none)'} is not part of the console")
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        out += [f"{rel}: looks like a {name}" for name, rx in SECRET_PATTERNS.items() if rx.search(text)]
        if rel == "config.json":
            keys = set(json.loads(text))
            if keys != set(CONFIG_KEYS):
                out.append(f"config.json has keys {sorted(keys)}, expected exactly {list(CONFIG_KEYS)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, help="the console's static folder")
    a = ap.parse_args()
    p = problems(a.check)
    for x in p:
        print("REFUSED:", x, file=sys.stderr)
    print(json.dumps({"checked": a.check, "problems": len(p)}))
    return 1 if p else 0


if __name__ == "__main__":
    sys.exit(main())
