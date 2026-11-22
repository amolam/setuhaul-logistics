"""`Db` over the RDS Data API (build item E3; AURORA and WORKLOAD_IDENTITY gates).

Same contract as `setuhaul.api.db.Db.tx()`: one transaction per call, role, scope, staff context and turn fence set
inside it, committed on success and rolled back on any error.

**Workload identity.** Each workload is given the Secrets Manager secret of one database login per application role
it may use (`SETUHAUL_DB_SECRET_ARNS`, a JSON object role → secret ARN). A role without a secret is refused here, and
in the database the login is a member of that one application role only, so `SET LOCAL ROLE` to any other role fails
there too. IAM grants each Lambda `secretsmanager:GetSecretValue` on its own secrets only.

**Parameters are bound client-side as SQL literals.** The Data API declares a type for every parameter (a string is
`varchar`), whereas psycopg sends strings untyped and lets PostgreSQL infer the type from context (a uuid column, a
`text[]` argument, a function overload). Sending typed parameters would make SQL that works today fail on Aurora. So
values are rendered as literals, the way psycopg2 did: strings are single-quoted with quotes doubled, and every
transaction first sets `standard_conforming_strings = on`, so a backslash is an ordinary character and doubling the
quote is the complete escape. NUL characters are refused (PostgreSQL text cannot hold them). Typed values (timestamps,
uuids, JSON, ranges) carry an explicit cast. `tests/test_data_api.py` checks hostile strings round-trip unchanged.

**Results** are decoded from the Data API's typed fields using the column metadata, to the same Python types psycopg
returns (`Decimal`, aware `datetime`, `UUID`, parsed JSON, `Range`, lists). A timestamp without an offset is read as UTC;
the AURORA gate confirms the exact text form the Data API returns for `timestamptz`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Json, Jsonb
from psycopg.types.range import Range

log = logging.getLogger("setuhaul.data_api")

from ..api.db import FENCED_ROLES, ROLES, TURN_FENCE

_PLACEHOLDER = re.compile(r"%%|%s(::[A-Za-z_][A-Za-z0-9_]*(?:\[\])?)?")   # a cast is one word, optionally []


class _Diag:
    def __init__(self, message_primary: str | None):
        self.message_primary = message_primary


class DataApiDatabaseError(psycopg.DatabaseError):
    """A Data API refusal as the psycopg error the application already handles: `sqlstate` and
    `diag.message_primary` carry what the service maps to its answers (HOLD_CONFLICT, 23P01, 42501 ...)."""

    def __init__(self, message: str, sqlstate: str | None, primary: str | None):
        self._sqlstate, self._diag = sqlstate, _Diag(primary)          # before psycopg's __init__ reads sqlstate
        super().__init__(message)

    @property
    def sqlstate(self):                                            # noqa: D401 (psycopg's attribute)
        return self._sqlstate

    @property
    def diag(self):
        return self._diag


_SQLSTATE = re.compile(r";?\s*SQLState:\s*([0-9A-Z]{5})\s*$")


def translate(e: Exception) -> Exception:
    """botocore ClientError (or an emulator's equivalent) → DataApiDatabaseError. The Aurora PostgreSQL Data API
    reports database errors as BadRequestException "ERROR: <message> ... ; SQLState: <code>"."""
    err = (getattr(e, "response", None) or {}).get("Error", {})
    msg = err.get("Message") or str(e)
    m = _SQLSTATE.search(msg)
    sqlstate = m.group(1) if m else None
    body = _SQLSTATE.sub("", msg).strip()
    primary = body.split("\n", 1)[0].removeprefix("ERROR:").strip() if err.get("Code") == "BadRequestException" else None
    return _error_class(sqlstate)(f"{err.get('Code', type(e).__name__)}: {body[:500]}", sqlstate, primary)


_CLASSES: dict[str, type] = {}


def _error_class(sqlstate: str | None) -> type:
    """The psycopg class for this SQLSTATE (InsufficientPrivilege for 42501, ExclusionViolation for 23P01 ...), so code
    that catches a specific psycopg error behaves exactly as it does with a direct connection."""
    if not sqlstate:
        return DataApiDatabaseError
    if sqlstate not in _CLASSES:
        try:
            base = psycopg.errors.lookup(sqlstate)
        except KeyError:
            base = None
        _CLASSES[sqlstate] = (type(f"DataApi{base.__name__}", (DataApiDatabaseError, base), {})
                              if base is not None and base is not psycopg.DatabaseError else DataApiDatabaseError)
    return _CLASSES[sqlstate]


# ---------------------------------------------------------------- literals
def _quote(s: str) -> str:
    if "\x00" in s:
        raise ValueError("a string parameter contains a NUL character")
    return "'" + s.replace("'", "''") + "'"


def _ts(v: datetime) -> str:
    if v.tzinfo is None:
        return _quote(v.isoformat(sep=" ")) + "::timestamp"
    return _quote(v.astimezone(timezone.utc).isoformat(sep=" ")) + "::timestamptz"


def _array_element(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, datetime):
        v = v.astimezone(timezone.utc).isoformat(sep=" ") if v.tzinfo else v.isoformat(sep=" ")
    s = str(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def literal(v: Any) -> str:
    """One Python value as a PostgreSQL literal. Untyped where psycopg would let the server infer the type."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return _quote(str(v)) + "::float8"
        return repr(v)
    if isinstance(v, Decimal):
        if not v.is_finite():
            return _quote(str(v)) + "::numeric"
        return str(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, datetime):
        return _ts(v)
    if isinstance(v, date):
        return _quote(v.isoformat()) + "::date"
    if isinstance(v, time):
        return _quote(v.isoformat()) + ("::timetz" if v.tzinfo else "::time")
    if isinstance(v, timedelta):
        return _quote(f"{v.total_seconds()} seconds") + "::interval"
    if isinstance(v, uuid.UUID):
        return _quote(str(v)) + "::uuid"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _quote("\\x" + bytes(v).hex()) + "::bytea"           # hex input; a plain backslash (standard strings)
    if isinstance(v, Jsonb):
        return _quote(json.dumps(v.obj, default=str)) + "::jsonb"
    if isinstance(v, Json):
        return _quote(json.dumps(v.obj, default=str)) + "::json"
    if isinstance(v, dict):
        return _quote(json.dumps(v, default=str)) + "::jsonb"
    if isinstance(v, Range):
        if v.isempty:
            return "'empty'"
        lo = "" if v.lower is None else _array_element(v.lower)
        hi = "" if v.upper is None else _array_element(v.upper)
        kind = "::tstzrange" if isinstance(v.lower or v.upper, datetime) else ""
        return _quote(f"{v.bounds[0]}{lo},{hi}{v.bounds[1]}") + kind
    if isinstance(v, (list, tuple)):
        return _quote("{" + ",".join(_array_element(x) for x in v) + "}")      # untyped: the context decides
    raise TypeError(f"cannot send a {type(v).__name__} through the Data API")


def bind(sql: str, params: Any = None) -> str:
    """psycopg-style `%s` / `%%` SQL with a parameter sequence → one SQL text with literals."""
    params = list(params or ())
    it = iter(params)
    used = 0

    def sub(m: re.Match) -> str:
        nonlocal used
        if m.group(0) == "%%":
            return "%"
        try:
            v = next(it)
        except StopIteration:
            raise ValueError("fewer parameters than placeholders") from None
        used += 1
        lit = literal(v)
        cast = m.group(1)
        return f"CAST({lit} AS {cast[2:]})" if cast else lit

    out = _PLACEHOLDER.sub(sub, sql)
    if used != len(params):
        raise ValueError("more parameters than placeholders")
    return out


# ---------------------------------------------------------------- results
def _parse_ts(s: str) -> datetime:
    s = s.strip()
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    d = datetime.fromisoformat(s.replace(" ", "T", 1))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


_INTERVAL = re.compile(r"(?:(-?\d+) years?)?\s*(?:(-?\d+) mons?)?\s*(?:(-?\d+) days?)?\s*(?:(-?)(\d+):(\d+):(\d+(?:\.\d+)?))?")


def _parse_interval(s: str) -> timedelta:
    m = _INTERVAL.fullmatch(s.strip())
    if not m:
        raise ValueError(f"interval {s!r}")
    y, mo, d, neg, h, mi, se = m.groups()
    days = int(y or 0) * 365 + int(mo or 0) * 30 + int(d or 0)
    t = timedelta(hours=int(h or 0), minutes=int(mi or 0), seconds=float(se or 0))
    return timedelta(days=days) + (-t if neg else t)


def _parse_range(s: str, base: str) -> Range:
    if s == "empty":
        return Range(empty=True)
    lb, body, ub = s[0], s[1:-1], s[-1]
    lo, hi = (p.strip().strip('"') for p in body.split(",", 1))
    conv = _parse_ts if base in ("tstzrange", "tsrange") else (date.fromisoformat if base == "daterange" else int)
    return Range(conv(lo) if lo else None, conv(hi) if hi else None, lb + ub)


def _scalar(s: str, t: str):
    if t in ("numeric", "decimal"):
        return Decimal(s)
    if t in ("timestamptz", "timestamp with time zone"):
        return _parse_ts(s)
    if t in ("timestamp", "timestamp without time zone"):
        return datetime.fromisoformat(s.replace(" ", "T", 1))
    if t == "date":
        return date.fromisoformat(s)
    if t in ("time", "time without time zone"):
        return time.fromisoformat(s)
    if t == "uuid":
        return uuid.UUID(s)
    if t in ("json", "jsonb"):
        return json.loads(s)
    if t in ("tstzrange", "tsrange", "daterange", "int4range", "int8range"):
        return _parse_range(s, t)
    if t == "interval":
        return _parse_interval(s)
    if t in ("int2", "int4", "int8", "integer", "bigint", "smallint", "serial", "bigserial"):
        return int(s)
    if t in ("float4", "float8", "real", "double precision"):
        return float(s)
    if t in ("bool", "boolean"):
        return s in ("t", "true", "1")
    return s


def _array(a: dict, t: str) -> list:
    base = t[1:] if t.startswith("_") else t.removesuffix("[]")
    if "arrayValues" in a:
        return [_array(x, t) for x in a["arrayValues"]]
    for key in ("longValues", "doubleValues", "booleanValues"):
        if key in a:
            return list(a[key])
    return [None if v is None else _scalar(v, base) for v in a.get("stringValues", [])]


def decode(field: dict, type_name: str):
    t = (type_name or "").lower()
    if field.get("isNull"):
        return None
    for key in ("booleanValue", "longValue", "doubleValue"):
        if key in field:
            v = field[key]
            return Decimal(str(v)) if key == "doubleValue" and t in ("numeric", "decimal") else v
    if "blobValue" in field:
        return bytes(field["blobValue"])
    if "arrayValue" in field:
        return _array(field["arrayValue"], t)
    return _scalar(field["stringValue"], t)


class Column:
    def __init__(self, name: str, type_name: str):
        self.name, self.type_name = name, type_name


class Cursor:
    def __init__(self, response: dict):
        meta = response.get("columnMetadata") or []
        self.description = [Column(m.get("label") or m.get("name"), m.get("typeName", "")) for m in meta] or None
        types = [m.get("typeName", "") for m in meta]
        self._rows = [tuple(decode(f, types[i]) for i, f in enumerate(rec)) for rec in response.get("records") or []]
        self.rowcount = response.get("numberOfRecordsUpdated", 0) if not self._rows and not meta else len(self._rows)
        self._i = 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self._rows[self._i:], len(self._rows)
        return rest

    def __iter__(self):
        while (r := self.fetchone()) is not None:
            yield r


class Connection:
    """What `Db.tx()` yields: `execute(sql, params)` returning a cursor, as with psycopg."""

    def __init__(self, client, resource_arn: str, secret_arn: str, database: str, transaction_id: str):
        self._c, self._kw = client, {"resourceArn": resource_arn, "secretArn": secret_arn, "database": database,
                                     "transactionId": transaction_id}

    def execute(self, sql: str, params: Any = None) -> Cursor:
        text = bind(sql, params)
        try:
            return Cursor(self._c.execute_statement(sql=text, includeResultMetadata=True, **self._kw))
        except Exception as e:
            if getattr(e, "response", None) is None:
                raise                                              # not a service answer: a bug, never translated
            raise translate(e) from None


# ---------------------------------------------------------------- the backend
RESUMING = "DatabaseResumingException"


def begin_with_resume(client, kw: dict, max_wait_s: float = 60.0, sleep=None, clock=None) -> str:
    """BeginTransaction, waiting while a paused Aurora Serverless v2 cluster resumes (auto-pause at 0 ACU).

    The Data API answers `DatabaseResumingException` while the cluster wakes (~15 s, sometimes longer). The call is
    repeated with backoff (1, 2, 4, then 5 s) until it succeeds or `max_wait_s` has passed; then the last error is
    raised. Only this error is retried: nothing has started, so a retry cannot repeat work. Every other error is
    raised at once."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    deadline, delay = clock() + max_wait_s, 1.0
    while True:
        try:
            return client.begin_transaction(**kw)["transactionId"]
        except Exception as e:
            code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code")
            if code != RESUMING or clock() + delay > deadline:
                raise
            log.info("database resuming from auto-pause; retrying BeginTransaction in %.0f s", delay)
            sleep(delay)
            delay = min(delay * 2, 5.0)


class DataApiDb:
    def __init__(self, cluster_arn: str, database: str, secret_arns: dict[str, str], client=None,
                 resume_wait_s: float = 60.0):
        unknown = set(secret_arns) - set(ROLES)
        if unknown:
            raise ValueError(f"unknown database roles in SETUHAUL_DB_SECRET_ARNS: {sorted(unknown)}")
        if client is None:
            import boto3                                            # only in AWS; tests pass an emulator
            client = boto3.client("rds-data")
        self.cluster_arn, self.database, self.secret_arns, self.client = cluster_arn, database, dict(secret_arns), client
        self.resume_wait_s = resume_wait_s
        self.dsn = f"rds-data:{database}"                              # for log lines; carries no credential

    @classmethod
    def from_env(cls, env: dict | None = None, client=None) -> "DataApiDb":
        env = os.environ if env is None else env
        return cls(env["SETUHAUL_DB_CLUSTER_ARN"], env.get("SETUHAUL_DB_NAME", "setuhaul"),
                   json.loads(env["SETUHAUL_DB_SECRET_ARNS"]), client,
                   resume_wait_s=float(env.get("SETUHAUL_DB_RESUME_WAIT_S", "60")))

    @contextmanager
    def tx(self, role: str, scope=None, driver: str | None = None, readonly: bool = False,
           staff=None) -> Iterator[Connection]:
        if role not in ROLES:
            raise ValueError(role)
        if staff is not None and (role != "console" or readonly):
            raise ValueError("a staff identity belongs to a console write transaction")
        secret = self.secret_arns.get(role)
        if secret is None:
            raise PermissionError(f"this workload has no database login for role {role!r}")
        kw = {"resourceArn": self.cluster_arn, "secretArn": secret, "database": self.database}
        try:
            tid = begin_with_resume(self.client, kw, self.resume_wait_s)
        except Exception as e:
            if getattr(e, "response", None) is None:
                raise
            raise translate(e) from None
        c = Connection(self.client, self.cluster_arn, secret, self.database, tid)
        try:
            if readonly:
                c.execute("SET TRANSACTION READ ONLY")
            # Its own statement, with no literal in it: the escaping of every later literal depends on it.
            c.execute("SET LOCAL standard_conforming_strings = on")
            # One round trip for the rest (each Data API call is a network request). set_config(..., true) is
            # SET LOCAL; the select list is evaluated left to right, so the role is in force before the scope.
            c.execute("SELECT set_config('role', %s, true), set_config('search_path', 'setuhaul', true), "
                      "set_config('TimeZone', 'Asia/Kolkata', true), set_config('app.facility_scope', %s, true), "
                      "set_config('app.driver_id', %s, true)", (ROLES[role], ",".join(scope or ()), driver or ""))
            if staff is not None:
                principal, roles, facilities = staff
                c.execute("SELECT begin_staff_context(%s,%s,%s)", (principal, sorted(roles), list(facilities)))
            fence = TURN_FENCE.get()
            if fence and not readonly and role in FENCED_ROLES:
                c.execute("SELECT assert_turn_fence(%s,%s,%s)", fence)
            yield c
        except BaseException:
            try:
                self.client.rollback_transaction(resourceArn=self.cluster_arn, secretArn=secret, transactionId=tid)
            except Exception:                                      # the original error matters more; the Data API
                pass                                               # also ends an abandoned transaction after 3 minutes
            raise
        try:
            self.client.commit_transaction(resourceArn=self.cluster_arn, secretArn=secret, transactionId=tid)
        except Exception as e:
            if getattr(e, "response", None) is None:
                raise
            raise translate(e) from None


def make_db(dsn: str):
    """`SETUHAUL_DSN=rds-data` selects the Data API backend (configured from the environment); anything else is a
    psycopg DSN for local development and tests."""
    if dsn == "rds-data":
        return DataApiDb.from_env()
    from ..api.db import Db
    return Db(dsn)
