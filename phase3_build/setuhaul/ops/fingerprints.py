"""Configuration fingerprints for deployment evidence (reviews R2.0 S4-R2-04, R2.1 S4-R3-02; DEPLOYMENT_GATES.md).

    python -m setuhaul.ops.fingerprints

Prints the deployment id and the fingerprint for each gate exactly as the service computes them at start-up, so the
deployment pipeline records evidence the service will accept. Environment: SETUHAUL_DSN, SETUHAUL_QUEUE_URL,
SETUHAUL_DEPLOYMENT_MANIFEST (the manifest gives the deployment id and the four gates the service cannot inspect).
"""
from __future__ import annotations

import os

from ..api.db import Db
from ..api.inbound import SqsFifoQueue, database_fingerprint, deployment_id_for, load_manifest, manifest_fingerprints


def main() -> int:
    if os.environ.get("SETUHAUL_DEPLOYMENT_MANIFEST"):
        m = load_manifest(os.environ["SETUHAUL_DEPLOYMENT_MANIFEST"])
        print("DEPLOYMENT_ID", deployment_id_for(m))
        for gate, fp in manifest_fingerprints(m).items():
            print(gate, fp)
    print("AURORA", database_fingerprint(Db(os.environ["SETUHAUL_DSN"])))
    if os.environ.get("SETUHAUL_QUEUE_URL"):
        print("AWS_QUEUE", SqsFifoQueue(os.environ["SETUHAUL_QUEUE_URL"]).fingerprint())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
