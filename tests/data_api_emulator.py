"""Test-only stand-in for the `rds-data` client, backed by a local PostgreSQL (build item E3).

It reproduces what the Data API backend depends on: begin / execute / commit / rollback by transaction id; results as
typed fields (`longValue`, `doubleValue`, `booleanValue`, `stringValue` with PostgreSQL's text form, `arrayValue`,
`blobValue`) with `columnMetadata` type names; `numberOfRecordsUpdated`; and database errors as a
`BadRequestException` whose message ends in `SQLState: <code>`. It refuses statement parameters, because the backend
must never send any (it binds literals). Text forms of timestamps follow PostgreSQL's own output for the session time
zone; how the real Data API formats them is checked against Aurora in the AURORA gate.
"""
from __future__ import annotations

import itertools
import threading

import psycopg

UNSUPPORTED_RESULT_TYPES = {"interval", "tstzrange", "tsrange", "daterange", "int4range", "int8range", "numrange",
                            "tstzmultirange", "tsmultirange", "datemultirange"}
INT_TYPES = {"int2", "int4", "int8", "oid"}
FLOAT_TYPES = {"float4", "float8"}


class ClientError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def _parse_array(text: str) -> list:
    """A one-dimensional PostgreSQL array literal → list of str (None for NULL)."""
    if text == "{}":
        return []
    body, out, cur, quoted, i, was_quoted = text[1:-1], [], [], False, 0, False
    while i < len(body):
        ch = body[i]
        if quoted:
            if ch == "\\":
                i += 1
                cur.append(body[i])
            elif ch == '"':
                quoted = False
            else:
                cur.append(ch)
        elif ch == '"':
            quoted, was_quoted = True, True
        elif ch == ",":
            s = "".join(cur)
            out.append(None if s == "NULL" and not was_quoted else s)
            cur, was_quoted = [], False
        else:
            cur.append(ch)
        i += 1
    s = "".join(cur)
    out.append(None if s == "NULL" and not was_quoted else s)
    return out


class EmulatedRdsData:
    def __init__(self, dsn: str):
        self.dsn, self._tx, self._ids, self._lock = dsn, {}, itertools.count(1), threading.Lock()
        self._types: dict[int, str] = {}
        self.statements = 0

    def _typname(self, oid: int) -> str:
        if oid not in self._types:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                self._types[oid] = c.execute("SELECT typname FROM pg_type WHERE oid = %s", (oid,)).fetchone()[0]
        return self._types[oid]

    def begin_transaction(self, resourceArn, secretArn, database, **_):
        conn = psycopg.connect(self.dsn)                       # not autocommit: the first statement opens BEGIN
        with self._lock:
            tid = f"tx-{next(self._ids)}"
            self._tx[tid] = conn
        return {"transactionId": tid}

    def _conn(self, tid):
        conn = self._tx.get(tid)
        if conn is None:
            raise ClientError("BadRequestException", "Transaction is not found")
        return conn

    def execute_statement(self, resourceArn, secretArn, database, sql, transactionId=None, parameters=None,
                          includeResultMetadata=False, **_):
        if parameters:
            raise AssertionError("the Data API backend must bind literals, not send parameters")
        conn = self._conn(transactionId)
        self.statements += 1
        try:
            cur = conn.cursor()
            cur.execute(sql.encode(), prepare=False)            # bytes: no client-side placeholder processing
        except psycopg.Error as e:
            primary = e.diag.message_primary or str(e)
            raise ClientError("BadRequestException", f"ERROR: {primary}\n  Where: emulator; SQLState: {e.sqlstate}") from None
        if cur.description is None:
            return {"numberOfRecordsUpdated": max(cur.rowcount, 0)}
        res = cur.pgresult
        types = [self._typname(d.type_code) for d in cur.description]
        # As the real Aurora PostgreSQL Data API (seen on the pilot, 22 Nov 2026): range and interval columns cannot
        # be returned; the whole statement is refused. Select lower()/upper() or extract(epoch ...) instead.
        bad = [t for t in types if t in UNSUPPORTED_RESULT_TYPES or t.lstrip("_") in UNSUPPORTED_RESULT_TYPES]
        if bad:
            raise ClientError("UnsupportedResultException",
                              f"The result contains the unsupported data type {bad[0].upper()}.") from None
        records = []
        for r in range(res.ntuples):
            row = []
            for c, t in enumerate(types):
                raw = res.get_value(r, c)
                if raw is None:
                    row.append({"isNull": True})
                    continue
                s = raw.decode()
                if t in INT_TYPES:
                    row.append({"longValue": int(s)})
                elif t in FLOAT_TYPES:
                    row.append({"doubleValue": float(s)})
                elif t == "bool":
                    row.append({"booleanValue": s == "t"})
                elif t == "bytea":
                    row.append({"blobValue": bytes.fromhex(s[2:])})
                elif t.startswith("_"):
                    items = _parse_array(s)
                    base = t[1:]
                    if base in INT_TYPES:
                        row.append({"arrayValue": {"longValues": [int(x) for x in items]}})
                    elif base == "bool":
                        row.append({"arrayValue": {"booleanValues": [x == "t" for x in items]}})
                    else:
                        row.append({"arrayValue": {"stringValues": items}})
                else:
                    row.append({"stringValue": s})
            records.append(row)
        out = {"records": records, "numberOfRecordsUpdated": 0}
        if includeResultMetadata:
            out["columnMetadata"] = [{"name": d.name, "label": d.name, "typeName": t}
                                     for d, t in zip(cur.description, types)]
        return out

    def commit_transaction(self, resourceArn, secretArn, transactionId, **_):
        conn = self._conn(transactionId)
        try:
            conn.commit()
        except psycopg.Error as e:
            raise ClientError("BadRequestException", f"ERROR: {e.diag.message_primary}; SQLState: {e.sqlstate}") from None
        finally:
            conn.close()
            self._tx.pop(transactionId, None)
        return {"transactionStatus": "Transaction Committed"}

    def rollback_transaction(self, resourceArn, secretArn, transactionId, **_):
        conn = self._tx.pop(transactionId, None)
        if conn is not None:
            conn.rollback()
            conn.close()
        return {"transactionStatus": "Rollback Complete"}


ALL_ROLES = {r: f"arn:aws:secretsmanager:ap-southeast-2:000000000000:secret:setuhaul-{r}" for r in
             ("agent", "console", "ingest", "feasibility", "system")}


def install(monkeypatch_target) -> None:
    """Route every `Db.tx()` through `DataApiDb` + this emulator (SETUHAUL_TEST_DATA_API=1 in conftest)."""
    from setuhaul.api import db as dbmod
    from setuhaul.aws.data_api import DataApiDb

    emulators: dict[str, EmulatedRdsData] = {}

    def tx(self, role, scope=None, driver=None, readonly=False, staff=None):
        em = emulators.setdefault(self.dsn, EmulatedRdsData(self.dsn))
        backend = DataApiDb("arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul", "setuhaul", ALL_ROLES, em)
        return backend.tx(role, scope, driver, readonly, staff)

    monkeypatch_target.setattr(dbmod.Db, "tx", tx)
