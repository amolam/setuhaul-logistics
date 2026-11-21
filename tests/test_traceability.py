"""Every test named in TRACEABILITY.md exists (database-free): a renamed or deleted test breaks the matrix loudly."""
import re
from pathlib import Path

BUILD = Path(__file__).resolve().parents[1]


def test_every_traced_test_exists():
    text = (BUILD / "docs" / "review" / "TRACEABILITY.md").read_text(encoding="utf-8")
    refs = re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", text)
    assert len(refs) >= 40
    missing = [f"{f}::{name}" for f, name in refs
               if not re.search(rf"^def {name}\(", (BUILD / f).read_text(encoding="utf-8"), re.M)]
    assert missing == []
