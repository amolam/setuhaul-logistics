"""Migration runner: idempotent, ordered, refuses edited migrations; 0001 is the frozen Phase 2 baseline."""
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest

from conftest import BASE_DSN
from setuhaul.db.migrate import MIGRATIONS, MigrationError, discover, migrate

ROOT = Path(__file__).resolve().parents[1]


def test_0001_is_the_frozen_phase2_schema():
    assert (MIGRATIONS / "0001_core_schema.sql").read_bytes() == (ROOT / "docs" / "2-design" / "schema.sql").read_bytes()


def test_rerun_is_a_noop(dsn):
    assert migrate(dsn) == []
    with psycopg.connect(dsn) as c:
        versions = [r[0] for r in c.execute("SELECT version FROM setuhaul_meta.schema_migrations ORDER BY version")]
    assert versions == [v for v, _, _ in discover()]


def test_edited_migration_is_refused(dsn, tmp_path):
    folder = tmp_path / "m"
    shutil.copytree(MIGRATIONS, folder)
    p = folder / "0002_request_functions.sql"
    p.write_text(p.read_text(encoding="utf-8") + "\n-- sneaky edit\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="immutable"):
        migrate(dsn, folder)


def test_failed_migration_rolls_back_and_is_not_recorded(dsn, tmp_path):
    folder = tmp_path / "m"
    shutil.copytree(MIGRATIONS, folder)
    (folder / "9999_broken.sql").write_text("CREATE TABLE setuhaul.tmp_ok (x int);\nSELECT 1/0;\n", encoding="utf-8")
    with pytest.raises(psycopg.Error):
        migrate(dsn, folder)
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT to_regclass('setuhaul.tmp_ok')").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM setuhaul_meta.schema_migrations WHERE version='9999_broken'").fetchone()[0] == 0
