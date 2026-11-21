"""Build the Lambda artifact (build item E5): Linux wheels for Python 3.13 plus the `setuhaul` package, as a folder for
CDK (`build/lambda/`) and a reproducible zip (`build/lambda.zip`) whose SHA-256 goes into the deployment manifest.

    python scripts/build_lambda.py

Downloads pinned wheels from PyPI (requirements-aws.txt); no AWS call. Fixed timestamps and sorted entries make the
zip byte-identical for identical inputs, so the same source gives the same artifact_sha256.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "lambda"
ZIP = ROOT / "build" / "lambda.zip"
EPOCH = (1980, 1, 1, 0, 0, 0)


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--target", str(OUT),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.13",
                    "--only-binary=:all:", "-r", str(ROOT / "requirements-aws.txt")], check=True)
    shutil.copytree(ROOT / "setuhaul", OUT / "setuhaul", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Console-script launchers (bin/) are built for the machine running pip and differ on every build; the RECORD files
    # list them. A Lambda needs neither, so they are left out: the zip then depends only on the pinned wheels and source.
    shutil.rmtree(OUT / "bin", ignore_errors=True)
    files = sorted(p for p in OUT.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and not (p.name == "RECORD" and p.parent.name.endswith(".dist-info")))
    ZIP.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(OUT).as_posix(), EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    print(f"{ZIP} {ZIP.stat().st_size // 1024} KiB sha256 {hashlib.sha256(ZIP.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
