"""Recovery Release A hardening (owner review, 25 Nov 2026): H-1, the Lambda container that started before its release
was authorized. Before the fix it stayed booking-OFF until AWS recycled it, so the provisional run's health check or
the release-bound smoke could fail and burn the one-hop recovery.

Every test runs the REAL start-up check (setuhaul.api.inbound.check_booking_preconditions through create_app) against
the Option P test database (guard and deployment identity enforced), with a fake SQS client and a fake monotonic
clock. Nothing is mocked between the retry and the database's answer."""
from __future__ import annotations

import json
import re
import shlex
import threading
import uuid
from pathlib import Path

import pytest

import setuhaul.api.app as A
from conftest import BASE_DSN, _pg_available  # noqa: F401
from setuhaul.api.app import Settings
from setuhaul.api.db import Db
from setuhaul.api.inbound import (REQUIRED_GATES, SqsFifoQueue, database_fingerprint, deployment_id_for,
                                  manifest_fingerprints)
from setuhaul.aws import lambdas
from test_api import CLIENT, ISSUER, META_SECRET, SESSION_SECRET, keys  # noqa: F401
from test_aws import http_event
from test_booking_evidence_guard import evidence, record
from test_inbound_delivery import URL, FakeSqs, write_evidence
from test_inbound_delivery import manifest as queue_manifest
from test_move_appointment import SCH, confirmed, sch, staff  # noqa: F401
from test_provisional_authorization import (auth_row, book, c, complete, dsn, env_name, events, issue,  # noqa: F401
                                            new_release, refused)
from test_recovery_release_a import insert_auth, revoke


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def a_release(c, env, art=None):
    """A complete predecessor and a NEW release recorded with its five non-workflow gates, whose evidence carries the
    fingerprints the start-up check computes live (queue, database, manifest). BOOKING_WORKFLOW is missing: the
    container cannot pass its gates until a provisional authorization is issued."""
    complete(c, env)
    art = art or uuid.uuid4().hex + uuid.uuid4().hex
    man = queue_manifest(env=env, RECOVERY_WORKER={"artifact_sha256": art})
    d = {"env": env, "dep": deployment_id_for(man), "art": art, "m": man}
    record(c, d)
    q = SqsFifoQueue(URL, FakeSqs())
    fps = {"AWS_QUEUE": q.fingerprint(), "AURORA": database_fingerprint(Db(c.info.dsn)), **manifest_fingerprints(man)}
    for g in REQUIRED_GATES:
        if g != "BOOKING_WORKFLOW":
            write_evidence(c.info.dsn, env, d["dep"], g, fp=fps[g])
    return d


@pytest.fixture
def container(monkeypatch, tmp_path, keys):  # noqa: F811
    """A fresh Lambda container (the module-level runtime, reset), booking switch ON, configured for one release."""
    clock = Clock()
    calls = []
    real_create = A.create_app

    def counting_create(settings, *a, **k):
        calls.append(settings.booking_enabled)
        return real_create(settings, *a, **k)

    def configure(c, d, environment=None):
        path = tmp_path / f"manifest-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps(d["m"]), encoding="utf-8")
        settings = Settings(dsn=c.info.dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT},
                            session_secret=SESSION_SECRET, meta_app_secret=META_SECRET,
                            facilities=("FAC-JAI-01", "FAC-GGN-01"), jwks=keys["jwks"], booking_enabled=True,
                            environment=environment or d["env"], deployment_manifest=str(path), queue_url=URL)
        monkeypatch.setattr(A.Settings, "from_env", classmethod(lambda cls: settings))

    monkeypatch.setattr(A, "create_app", counting_create)
    monkeypatch.setattr(A, "SqsFifoQueue", lambda url, client=None: SqsFifoQueue(URL, FakeSqs()))
    monkeypatch.setattr(lambdas, "load_app_secrets", lambda env_: env_)
    monkeypatch.setenv("SETUHAUL_QUEUE_URL", URL)
    monkeypatch.setattr("setuhaul.agent.models.model_for", lambda env, db=None, **kw: None)   # never a model client
    lambdas.runtime.cache_clear()
    lambdas._asgi.cache_clear()
    lambdas._orchestrator.cache_clear()
    monkeypatch.setattr(lambdas._RUNTIME, "_clock", clock)
    yield type("Container", (), {"clock": clock, "calls": calls, "configure": staticmethod(configure),
                                 "rt": lambdas._RUNTIME})
    lambdas.runtime.cache_clear()
    lambdas._asgi.cache_clear()
    lambdas._orchestrator.cache_clear()


def api_health():
    r = lambdas.api_handler(http_event("GET", "/health"), None)
    assert r["statusCode"] == 200, r
    return json.loads(r["body"])


def booking_on_attempts(calls):
    return sum(1 for x in calls if x)


# ================================================================ H-1: the API path
def test_an_api_container_started_before_authorization_switches_to_booking_on_without_being_recycled(c, container,
                                                                                                    sch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)

    fallback = lambdas.runtime()                                     # cold start: no authorization yet
    assert lambdas.in_booking_fallback(fallback) and fallback.state.service.booking_enabled is False
    h = api_health()
    assert h["booking_enabled"] is False and h["booking_state"] == "refused"
    assert "BOOKING_WORKFLOW" in h["booking_refusal"]
    assert container.calls == [True, False]                          # tried ON, runs OFF

    issue(c, d)                                                      # the pipeline issues the authorization
    container.clock.t += 10                                          # inside the retry interval: no rebuild
    assert lambdas.runtime() is fallback and api_health()["booking_state"] == "refused"
    assert booking_on_attempts(container.calls) == 1

    container.clock.t += 21                                          # 31 s after the cold start: one retry
    h = api_health()                                                 # the SAME warm container, via the handler
    assert h["booking_enabled"] is True and h["booking_state"] == "authorized", h
    assert h["booking_provisional_until"]
    app = lambdas.runtime()
    assert app is not fallback and not lambdas.in_booking_fallback(app)
    assert app.state.service.booking_enabled is True
    assert app.state.service.db.deployment == {"environment": env, "deployment_id": d["dep"], "code_sha256": d["art"]}
    assert booking_on_attempts(container.calls) == 2 and container.rt.generation == 2
    for _ in range(20):                                              # booking-ON now: never rebuilt again
        container.clock.t += 60
        assert lambdas.runtime() is app
    assert booking_on_attempts(container.calls) == 2
    assert book(c, d)                                                # and the database authorizes the release


# ================================================================ H-1: the agent path
def test_an_agent_container_started_before_authorization_switches_to_booking_on_without_being_recycled(c, container,
                                                                                                      sch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)

    assert lambdas.agent_handler({"Records": []}, None, sqs=object()) == {"batchItemFailures": []}
    first = lambdas._orchestrator()
    assert first.svc.booking_enabled is False and lambdas.in_booking_fallback(lambdas.runtime())

    issue(c, d)
    container.clock.t += 29
    lambdas.agent_handler({"Records": []}, None, sqs=object())
    assert lambdas._orchestrator() is first                          # throttled: same orchestrator, still OFF

    container.clock.t += 2
    assert lambdas.agent_handler({"Records": []}, None, sqs=object()) == {"batchItemFailures": []}
    orch = lambdas._orchestrator()
    assert orch is not first and orch.svc.booking_enabled is True
    assert orch.svc is lambdas.runtime().state.service               # built from the published booking-ON app
    assert orch.svc.db.deployment["deployment_id"] == d["dep"]
    container.clock.t += 600
    lambdas.agent_handler({"Records": []}, None, sqs=object())
    assert lambdas._orchestrator() is orch                           # not rebuilt per invocation
    assert book(c, d, role="agent")


# ================================================================ H-1: persistent failure
def test_while_authorization_stays_missing_the_retry_is_throttled_and_booking_stays_off(c, container,
                                                                                         sch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    fallback = lambdas.runtime()
    for _ in range(50):                                              # a burst of requests: no rebuild
        assert lambdas.runtime() is fallback
    assert booking_on_attempts(container.calls) == 1
    for step in range(1, 6):                                         # one attempt per 30 s, never more
        container.clock.t += 30
        for _ in range(25):
            assert lambdas.runtime() is fallback
        assert booking_on_attempts(container.calls) == 1 + step
        assert container.rt.retries == step
    assert "BOOKING_WORKFLOW" in container.rt.last_retry_error
    h = api_health()
    assert h["booking_enabled"] is False and h["booking_state"] == "refused"
    assert "BOOKING_WORKFLOW" in h["booking_refusal"]                 # the latest refusal is reported
    assert fallback.state.service.booking_enabled is False
    refused(lambda: book(c, d), "BOOKING_WORKFLOW")                   # and the database refuses


def test_an_unexpected_error_in_the_retry_leaves_the_fallback_in_place(c, container, monkeypatch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    fallback = lambdas.runtime()
    issue(c, d)
    real = A.create_app

    def broken(settings, *a, **k):
        if settings.booking_enabled:
            raise ConnectionError("database unreachable during the rebuild")
        return real(settings, *a, **k)

    monkeypatch.setattr(A, "create_app", broken)
    container.clock.t += 31
    assert lambdas.runtime() is fallback and lambdas.in_booking_fallback(fallback)
    assert "ConnectionError" in container.rt.last_retry_error
    monkeypatch.setattr(A, "create_app", real)
    container.clock.t += 31
    assert not lambdas.in_booking_fallback(lambdas.runtime())        # the next interval succeeds


# ================================================================ H-1: concurrency
def test_concurrent_requests_start_exactly_one_rebuild_and_never_see_a_partial_app(c, container,
                                                                                     monkeypatch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    fallback = lambdas.runtime()
    issue(c, d)
    real = A.create_app
    entered, release_, inside = threading.Event(), threading.Event(), []

    def slow(settings, *a, **k):
        if settings.booking_enabled:
            inside.append(1)
            entered.set()
            assert release_.wait(20)
        return real(settings, *a, **k)

    monkeypatch.setattr(A, "create_app", slow)
    container.clock.t += 31
    seen, errors = [], []

    def call():
        try:
            seen.append(lambdas.runtime())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    leader = threading.Thread(target=call)
    leader.start()
    assert entered.wait(20)                                          # one caller is rebuilding, blocked
    others = [threading.Thread(target=call) for _ in range(12)]
    for t in others:
        t.start()
    for t in others:
        t.join(20)
    assert not errors and len(seen) == 12 and all(a is fallback for a in seen)   # nobody waited, nobody saw a new app
    assert len(inside) == 1                                          # exactly one rebuild in flight
    release_.set()
    leader.join(20)
    new = seen[-1]
    assert new is not fallback and not lambdas.in_booking_fallback(new)
    assert new.state.service.booking_enabled is True and new.state.relay is not None   # complete when published
    assert len(inside) == 1 and container.rt.retries == 1
    assert all(lambdas.runtime() is new for _ in range(10))


# ================================================================ H-1: the database stays authoritative
def test_after_recovery_the_kill_switch_revocation_and_identity_still_refuse(c, container, sch):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    lambdas.runtime()
    issue(c, d)
    container.clock.t += 31
    assert api_health()["booking_state"] == "authorized"
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = true, changed_by = 'test', reason = 'H-1 drill'")
    try:
        h = api_health()
        assert h["booking_enabled"] is False and h["booking_kill_switch"] == "ENGAGED"
        refused(lambda: book(c, d), "BOOKING_KILLED")
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = false, changed_by = 'test', reason = 'over'")
    refused(lambda: book(c, d, login="setuhaul_login_console_" + "9" * 12), "IDENTITY_MISMATCH")   # wrong release
    revoke(c, env)
    h = api_health()                                                 # the recovered warm container
    assert h["booking_enabled"] is False and "REVOKED" in h["booking_refusal"]
    refused(lambda: book(c, d), "REVOKED")


def test_a_revoked_or_expired_authorization_never_turns_a_fallback_container_on(c, container):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    fallback = lambdas.runtime()
    issue(c, d)
    revoke(c, env)                                                   # revoked before the retry
    container.clock.t += 31
    assert lambdas.runtime() is fallback and lambdas.in_booking_fallback(fallback)
    h = api_health()
    assert h["booking_enabled"] is False and h["booking_state"] == "refused"
    assert "BOOKING_WORKFLOW" in container.rt.last_retry_error        # a revoked window is no window

    env2 = env_name()
    d2 = a_release(c, env2)
    lambdas.runtime.cache_clear()
    lambdas._asgi.cache_clear()
    container.configure(c, d2)
    fb2 = lambdas.runtime()
    prev = c.execute("SELECT deployment_id FROM setuhaul.deployment_manifests WHERE environment=%s "
                     "AND deployment_id <> %s ORDER BY manifest_id DESC LIMIT 1", (env2, d2["dep"])).fetchone()[0]
    insert_auth(c, d2, prev, "-61 minutes", "-1 minute")             # a window that has already ended
    container.clock.t += 31
    assert lambdas.runtime() is fb2 and lambdas.in_booking_fallback(fb2)
    assert api_health()["booking_enabled"] is False


def test_a_production_environment_never_leaves_the_fallback(c, container):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    issue(c, d)                                                      # an open pilot window exists...
    container.configure(c, d, environment="production")              # ...but this container declares production
    fb = lambdas.runtime()
    for _ in range(3):
        container.clock.t += 31
        assert lambdas.runtime() is fb and lambdas.in_booking_fallback(fb)
    st = c.execute("SELECT setuhaul.booking_authorization_status('production', %s, %s)",
                   (d["dep"], d["art"])).fetchone()[0]
    assert st["authorized"] is False and st["verdict"].startswith("PRODUCTION_DISABLED")


def test_a_container_with_the_switch_off_never_retries(c, container, monkeypatch, keys):  # noqa: F811
    env = env_name()
    d = a_release(c, env)
    container.configure(c, d)
    off = Settings(dsn=c.info.dsn, cognito_issuer=ISSUER, cognito_client_ids={CLIENT}, session_secret=SESSION_SECRET,
                   meta_app_secret=META_SECRET, facilities=("FAC-JAI-01",), jwks=keys["jwks"], environment=env)
    monkeypatch.setattr(A.Settings, "from_env", classmethod(lambda cls: off))
    app = lambdas.runtime()
    issue(c, d)
    for _ in range(3):
        container.clock.t += 31
        assert lambdas.runtime() is app
    assert container.calls == [False] and api_health()["booking_state"] == "off"


# ================================================================ M-1: every failure after issuance revokes at once
ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "activate-booking.yml"
RUNBOOK = ROOT / "docs" / "booking-renewal" / "OPTION_P_M2_RELEASE_RUNBOOK.md"


def _job(name: str) -> str:
    t = WF.read_text(encoding="utf-8")
    body = t[t.index(f"\n  {name}:\n"):]
    nxt = re.search(r"\n  [a-z-]+:\n", body[3:])
    return body[:nxt.start() + 3] if nxt else body


def _step_blocks(job_text: str) -> list[str]:
    return ["      - " + s for s in job_text.split("\n      - ")[1:]]


def test_a_failed_or_cancelled_renewal_run_revokes_before_it_reports_and_fails_loudly_if_anything_stays_open():
    job = _job("close-window-on-failure")
    head = job.split("\n    steps:")[0]
    assert "needs: [test, synth, activate]" in head                  # any job's failure, tests and synth included
    cond = re.search(r"\n    if: (.*)", head).group(1)
    assert cond.startswith("(failure() || cancelled())")             # failure AND cancellation
    assert "github.ref == 'refs/heads/release/fde-sydney-pilot'" in cond
    assert "github.event.inputs.mode == 'renewal'" in cond and "github.event.inputs.mode == 'provisional'" in cond
    assert "environment: pilot" in head and "id-token: write" in head
    steps = _step_blocks(job)
    assert "REFUSED: $DEPLOY_REF" in steps[0]                        # the ref allow-list comes first
    read = next(s for s in steps if "Read the stack's database outputs" in s)
    assert "cdk-hnb659fds-lookup-role" in read and "describe-stacks" in read
    assert "{deploy_evidence}" in read and "OwnerSecretArn" not in read   # the deploy-evidence secret only
    revoke = next(s for s in steps if "revoke the provisional authorization at once" in s)
    assert re.search(r"\n\s+if: (.*)", revoke).group(1) == "env.MODE == 'renewal'"
    assert "--revoke-provisional" in revoke and "--artifact-uri" not in revoke
    guard = next(s for s in steps if "No provisional authorization may stay open" in s)
    assert re.search(r"\n\s+if: (.*)", guard).group(1) == "always()"   # runs even when the revoke step failed
    assert "--assert-no-open-provisional" in guard and "::error::" in guard and "exit 1" in guard
    status = next(s for s in steps if "What the database now records" in s)
    assert re.search(r"\n\s+if: (.*)", status).group(1) == "always()" and "--provisional-status" in status
    assert steps.index(revoke) < steps.index(guard) < steps.index(status)
    assert "continue-on-error" not in job                            # no failure is ever hidden
    assert "outputs.json" not in job.split("\n    steps:")[1]        # works when the deploy left no outputs


def _data_api(c, monkeypatch):
    from data_api_emulator import EmulatedRdsData
    import boto3
    em = EmulatedRdsData(c.info.dsn)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: em)
    monkeypatch.setenv("SETUHAUL_DB_CLUSTER_ARN", "arn:aws:rds:ap-southeast-2:000000000000:cluster:setuhaul")
    monkeypatch.setenv("SETUHAUL_DB_SECRET_ARNS", json.dumps({"deploy_evidence": "arn:secret:deploy_evidence"}))
    monkeypatch.setenv("SETUHAUL_DB_NAME", c.info.dbname)
    return em


def _status(env, capsys):
    from setuhaul.ops import gate_evidence as GE
    capsys.readouterr()
    assert GE.main(["--environment", env, "--provisional-status"]) == 0
    return json.loads(capsys.readouterr().out)["provisional"]


def _runbook_revoke_argv(env):
    """The exact revoke command runbook §7.5 gives the operator, with this test's environment."""
    text = RUNBOOK.read_text(encoding="utf-8")
    cmd = re.search(r"^python -m setuhaul\.ops\.gate_evidence --environment pilot-ap-southeast-2 "
                    r"--revoke-provisional .*$", text, re.M).group(0)
    argv = shlex.split(cmd)[3:]
    argv[argv.index("pilot-ap-southeast-2")] = env
    return argv


@pytest.mark.parametrize("failure", ["smoke failed", "renewal evidence refused"])
def test_after_a_smoke_or_evidence_failure_the_runbook_revoke_closes_the_window_and_everything_refuses(
        c, sch, monkeypatch, capsys, failure):  # noqa: F811
    from setuhaul.ops import gate_evidence as GE
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    assert book(c, d)                                                # the window was open
    if failure == "renewal evidence refused":                        # the renewal's transaction fails: rolled back
        with pytest.raises(RuntimeError):
            with c.transaction():
                evidence(c, d, "BOOKING_WORKFLOW")
                raise RuntimeError("a later check refused the renewal")
    assert auth_row(c, d)[0] == "ISSUED"                             # nothing consumed: still open, so revoke now
    _data_api(c, monkeypatch)
    assert _status(env, capsys)["open"] is True
    assert GE.main(_runbook_revoke_argv(env)) == 0                   # the documented command
    st = _status(env, capsys)
    assert st["status"] == "REVOKED" and st["open"] is False
    assert auth_row(c, d)[0] == "REVOKED" and events(c, d) == ["ISSUED", "REVOKED"]   # audited, append-only
    refused(lambda: book(c, d), "REVOKED")
    assert "REVOKED" in c.execute("SELECT setuhaul.booking_authorization_status(%s,%s,%s)",
                                  (env, d["dep"], d["art"])).fetchone()[0]["verdict"]
    c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = true, changed_by = 'test', reason = 'M-1 drill'")
    try:
        refused(lambda: book(c, d), "BOOKING_KILLED")                # the kill switch still refuses first
    finally:
        c.execute("UPDATE setuhaul.booking_kill_switch SET engaged = false, changed_by = 'test', reason = 'over'")
    assert GE.main(["--environment", env, "--assert-no-open-provisional"]) == 0
    refused(lambda: evidence(c, d, "BOOKING_WORKFLOW"), "PROVISIONAL_CLOSED")   # no renewal after a revoke


def test_a_failed_revoke_is_loud_and_the_window_is_still_reported_open(c, monkeypatch, capsys):  # noqa: F811
    from setuhaul.ops import gate_evidence as GE
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    em = _data_api(c, monkeypatch)
    real = em.execute_statement

    def failing(**kw):
        if "revoke_provisional_authorization" in kw.get("sql", ""):
            raise RuntimeError("Data API unavailable")
        return real(**kw)

    monkeypatch.setattr(em, "execute_statement", failing)
    with pytest.raises(RuntimeError, match="Data API unavailable"):  # the revoke step fails (non-zero exit)
        GE.main(["--environment", env, "--revoke-provisional", "renewal run 1-1 failed"])
    assert auth_row(c, d)[0] == "ISSUED"
    monkeypatch.setattr(em, "execute_statement", real)
    assert GE.main(["--environment", env, "--assert-no-open-provisional"]) == 1   # the guard step fails the run
    assert _status(env, capsys)["open"] is True


def test_a_revoke_after_successful_consumption_changes_nothing(c, sch, monkeypatch, capsys):  # noqa: F811
    from setuhaul.ops import gate_evidence as GE
    env = env_name()
    complete(c, env)
    d = new_release(c, env)
    issue(c, d)
    evidence(c, d, "BOOKING_WORKFLOW")                               # the renewal consumed the window atomically
    assert auth_row(c, d)[0] == "CONSUMED"
    _data_api(c, monkeypatch)
    capsys.readouterr()
    assert GE.main(["--environment", env, "--revoke-provisional", "renewal health step failed after consumption"]) == 0
    assert json.loads(capsys.readouterr().out)["revoked"] == 0
    assert auth_row(c, d)[0] == "CONSUMED" and events(c, d) == ["ISSUED", "CONSUMED"]
    assert _status(env, capsys)["status"] == "CONSUMED"
    assert book(c, d)                                                # booking now stands on normal evidence


# ================================================================ M-2: the report records the observed booking state
class _Resp:
    def __init__(self, status, body=None, raw=None):
        self.status_code, self._body, self._raw = status, body, raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not JSON")
        return self._body


def _observe(resp=None, exc=None, url="https://api.example/"):
    from setuhaul.ops.renewal import observe_booking_state

    def get(u):
        assert u == "https://api.example/health"
        if exc:
            raise exc
        return resp
    return observe_booking_state(url, get=get)


D4_OFF = {"status": "ok", "booking_enabled": False, "booking_state": "off"}
D4_REFUSED = {"status": "ok", "booking_enabled": False, "booking_state": "refused", "booking_switch": "on",
              "booking_refusal": "EVIDENCE_NOT_CURRENT: BOOKING_WORKFLOW ... REVOKED", "booking_kill_switch": "disengaged"}
D4_AUTHORIZED = {"status": "ok", "booking_enabled": True, "booking_state": "authorized", "booking_switch": "on",
                 "booking_kill_switch": "disengaged", "booking_provisional_until": "2026-11-26T10:00:00+00:00"}
PRE_D4_OFF = {"status": "ok", "database": "ok", "booking_enabled": False, "environment": "pilot-ap-southeast-2",
              "version": "0.2.0"}                                    # the live pilot's /health on 25 Nov 2026


@pytest.mark.parametrize("body,state,enabled", [
    (D4_OFF, "off", False), (PRE_D4_OFF, "off", False), (D4_REFUSED, "refused", False),
    (D4_AUTHORIZED, "authorized", True)])
def test_the_observed_booking_state_distinguishes_off_refused_and_authorized(body, state, enabled):
    o = _observe(_Resp(200, body))
    assert o["booking_state"] == state and o["booking_enabled"] is enabled and "error" not in o
    assert o["source"] == "https://api.example/health" and o["observed_utc"] and o["http_status"] == 200
    assert o["meaning"].startswith({"off": "booking disabled", "refused": "booking enabled but not authorized",
                                    "authorized": "booking enabled and authorized"}[state])


@pytest.mark.parametrize("resp,exc,why", [
    (None, ConnectionError("down"), "unavailable (ConnectionError)"),
    (_Resp(503, {"status": "degraded"}), None, "HTTP 503"),
    (_Resp(200, raw="<html>"), None, "unavailable (ValueError)"),
    (_Resp(200, ["not", "a", "dict"]), None, "without a JSON object"),
    (_Resp(200, {"booking_state": "authorized", "booking_enabled": False}), None, "inconsistent"),
    (_Resp(200, {"booking_state": "off", "booking_enabled": True}), None, "inconsistent"),
    (_Resp(200, {"booking_state": "unknown", "booking_enabled": False}), None, "malformed"),
    (_Resp(200, {"booking_state": "authorized", "booking_enabled": "true"}), None, "malformed"),
    (_Resp(200, {"status": "ok", "booking_enabled": True}), None, "before D4"),   # switch on: cannot tell
    (_Resp(200, {"status": "ok"}), None, "before D4"),
])
def test_an_unreadable_health_never_invents_a_booking_state(resp, exc, why):
    o = _observe(resp, exc)
    assert o["booking_state"] == "unavailable" and o["booking_enabled"] is None and why in o["error"]


def test_no_api_url_is_unavailable():
    from setuhaul.ops.renewal import observe_booking_state
    o = observe_booking_state(None, get=lambda u: pytest.fail("no request without a URL"))
    assert o["booking_state"] == "unavailable" and o["booking_enabled"] is None


def _report(obs, **over):
    r = {"mode": "renewal", "booking_enabled": obs["booking_enabled"], "booking_observed": obs,
         "environment": "pilot-ap-southeast-2", "approval_ref": "OWNER-APPROVAL-20261125-RECOVERY-A",
         "collected_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
         "artifact_sha256": "a" * 64}
    r.update(over)
    return r


def test_the_five_gate_report_is_judged_on_the_observed_state_recovery_and_normal():
    from setuhaul.ops.gate_evidence import provisional_report_problems as P
    ref = "OWNER-APPROVAL-20261125-RECOVERY-A"
    off, refused_, on = (_observe(_Resp(200, b)) for b in (PRE_D4_OFF, D4_REFUSED, D4_AUTHORIZED))
    bad = _observe(None, ConnectionError("down"))
    assert P(_report(off), "a" * 64, ref, recovery=True) == []            # Recovery A: the pilot is booking OFF
    assert P(_report(refused_), "a" * 64, ref, recovery=True) == []
    assert any("expected off or refused" in x for x in P(_report(on), "a" * 64, ref, recovery=True))
    assert P(_report(on), "a" * 64, ref) == []                            # normal Option P: ON and authorized
    for o in (off, refused_):
        assert any("expected authorized" in x for x in P(_report(o), "a" * 64, ref))
    for rec in (True, False):
        assert any("no verified booking state" in x for x in P(_report(bad), "a" * 64, ref, recovery=rec))
        assert any("no verified booking state" in x
                   for x in P({k: v for k, v in _report(off).items() if k != "booking_observed"}, "a" * 64, ref,
                              recovery=rec))
    lie = _report(off, booking_enabled=True)                               # the old hard-coded value
    assert any("is not what was observed" in x for x in P(lie, "a" * 64, ref, recovery=True))


def test_the_six_gate_renewal_needs_the_pilot_observed_authorized():
    from setuhaul.ops.renewal import booking_observation_problems as B
    on, off, refused_ = (_observe(_Resp(200, b)) for b in (D4_AUTHORIZED, PRE_D4_OFF, D4_REFUSED))
    ctx = "a renewal report must come from the active pilot (booking ON and authorized)"
    assert B(_report(on), ("authorized",), ctx) == []
    for o in (off, refused_, _observe(_Resp(503, {}))):
        assert B(_report(o), ("authorized",), ctx)
    no_time = {**on, "observed_utc": None}
    assert any("no source or time" in x for x in B(_report(no_time), ("authorized",), ctx))


def test_a_renewal_report_that_observed_booking_off_is_never_treated_as_an_activation_report():
    from setuhaul.ops import gate_evidence as GE
    from test_inbound_delivery import manifest as m_
    off = _observe(_Resp(200, PRE_D4_OFF))
    with pytest.raises(GE.EvidenceRefused, match="renewal report refused: .*expected authorized"):
        GE.rows_from_report(_report(off, gates={}), m_(env="pilot-ap-southeast-2"), "pilot-ap-southeast-2",
                            "s3://e/r.json", b"{}", {})


def test_the_collector_no_longer_hard_codes_the_booking_state():
    src = (ROOT / "scripts" / "step6_gate_evidence.py").read_text(encoding="utf-8")
    assert '"booking_enabled": True' not in src and '"booking_enabled": False' not in src
    assert '"booking_enabled": observed["booking_enabled"], "booking_observed": observed' in src


# ================================================================ M-3: previously verified = committed and unchanged
C1, C2 = "87915a37-918e-46b5-8872-8ecaa4fc7a24", "29ffac03-d8bd-4dac-b35c-66b0f8ebf24b"
PROV = "docs/release/evidence/provisional_gates.json"
OWNER = json.loads((ROOT / "docs" / "release" / "evidence" / "owner_attribution_20261123.json").read_text(
    encoding="utf-8"))


def _collector():
    import importlib.util
    spec = importlib.util.spec_from_file_location("step6_collector_m3", ROOT / "scripts" / "step6_gate_evidence.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ids(aid):
    return sorted(r["request_id"] for r in next(e for e in OWNER["executions"]
                                                if e["approval_id"] == aid)["cloudwatch"]["requests"])


def _report_with(corr: dict) -> str:
    return json.dumps({"mode": "renewal", "gates": {"BOOKING_WORKFLOW": {"evidence": {
        "api_log_corroboration": corr}}}}, indent=1)


VERIFIED = {C1: {"status": "verified", "request_ids": _ids(C1)}, C2: {"status": "verified", "request_ids": _ids(C2)}}


class Repo:
    def __init__(self, root: Path):
        import subprocess
        self.root, self.run = root, lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                                               capture_output=True)
        (root / "docs" / "release" / "evidence").mkdir(parents=True)
        self.run("init", "-q")
        self.run("config", "user.email", "test@example.invalid")
        self.run("config", "user.name", "test")
        self.run("config", "commit.gpgsign", "false")
        self.write("README", "x")
        self.commit("init")

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, msg="c", *paths):
        self.run("add", *(paths or ["-A"]))
        self.run("commit", "-q", "-m", msg)
        return self.run("rev-parse", "HEAD").stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def _late(repo_root):
    """The collector after the API log's retention: every request is beyond retention, the log has nothing."""
    from datetime import datetime, timezone
    m = _collector()
    return m.owner_api_log_corroboration(OWNER, "SetuHaulPilot-LogsApi-from-the-stack",
                                         datetime(2026, 12, 5, tzinfo=timezone.utc),
                                         logs=FakeLogsM3(), repo_root=repo_root)


class FakeLogsM3:
    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": logGroupNamePrefix, "retentionInDays": 7}]}

    def filter_log_events(self, **kw):                                 # never reached past retention
        raise AssertionError("an expired line is never searched")


def test_a_committed_unchanged_report_vouches_for_exactly_its_request_ids(repo):
    repo.write(PROV, _report_with(VERIFIED))
    sha = repo.commit("step-2 report", PROV)
    got = _late(repo.root)
    for aid in (C1, C2):
        assert got[aid]["status"] == "previously_verified", got[aid]
        assert got[aid]["verified_in"] == {"path": PROV, "commit": sha}


def test_an_older_committed_version_still_vouches_after_the_report_is_replaced(repo):
    repo.write(PROV, _report_with(VERIFIED))
    sha = repo.commit("step-2 report", PROV)
    repo.write(PROV, _report_with({C2: {"status": "previously_verified", "request_ids": _ids(C2)}}))
    repo.commit("a later provisional report", PROV)                   # read from history: no transitive trust
    got = _late(repo.root)
    assert got[C2]["status"] == "previously_verified" and got[C2]["verified_in"]["commit"] == sha


@pytest.mark.parametrize("case", ["untracked", "modified", "staged"])
def test_an_untracked_or_locally_changed_report_is_never_trusted(repo, case):
    if case == "untracked":
        repo.write(PROV, _report_with(VERIFIED))
        why = "is not tracked by git"
    else:
        repo.write(PROV, _report_with({}))
        repo.commit("an honest report", PROV)
        repo.write(PROV, _report_with(VERIFIED))                       # a local edit claims verification
        if case == "staged":
            repo.run("add", PROV)
        why = "differs from its committed version"
    got = _late(repo.root)
    for aid in (C1, C2):
        assert got[aid]["status"] == "beyond_retention_unverified" and why in got[aid]["detail"]


def test_a_missing_report_proves_nothing(repo):
    got = _late(repo.root)
    assert got[C2]["status"] == "beyond_retention_unverified"
    assert "no committed report verified exactly these requests" in got[C2]["detail"]


@pytest.mark.parametrize("corr", [
    {"0f3c2a1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b": {"status": "verified", "request_ids": _ids(C2)}},   # wrong approval
    {C2: {"status": "verified", "request_ids": ["882b6fca-0000-0000-0000-000000000000"]}},      # wrong request ids
    {C2: {"status": "verified", "request_ids": _ids(C2) + ["extra"]}},                          # extra request id
    {C2: {"status": "previously_verified", "request_ids": _ids(C2)}},                           # not "verified"
    {C2: {"status": "missing", "request_ids": _ids(C2)}},
    {C2: {"status": "VERIFIED", "request_ids": _ids(C2)}},
])
def test_only_a_verified_entry_for_the_same_approval_and_request_ids_counts(repo, corr):
    repo.write(PROV, _report_with(corr))
    repo.commit("report", PROV)
    assert _late(repo.root)[C2]["status"] == "beyond_retention_unverified"


@pytest.mark.parametrize("text,why", [
    ("{not json", "is malformed"),
    ('{"gates": {"BOOKING_WORKFLOW": {"evidence": {"api_log_corroboration": {"%s": {"status": "verified", '
     '"request_ids": ["a"]}, "%s": {"status": "verified", "request_ids": %s}}}}}}' % (C2, C2, json.dumps(_ids(C2))),
     "duplicate key"),
    (json.dumps({"gates": {"BOOKING_WORKFLOW": {"evidence": {"api_log_corroboration": ["not", "an", "object"]}}}}),
     "is not an object"),
    (_report_with({C2: {"status": "verified", "request_ids": "882b6fca"}}), "malformed request ids"),
])
def test_a_malformed_or_duplicate_committed_report_fails_closed(repo, text, why):
    repo.write(PROV, _report_with(VERIFIED))
    repo.commit("a good version", PROV)
    repo.write(PROV, text)
    repo.commit("a broken version", PROV)
    got = _late(repo.root)
    assert got[C2]["status"] == "beyond_retention_unverified" and why in got[C2]["detail"]


def test_a_duplicate_or_unexpected_report_file_fails_closed(repo):
    repo.write(PROV, _report_with(VERIFIED))
    repo.commit("step-2 report", PROV)
    repo.write("docs/release/evidence/provisional_gates_copy.json", _report_with(VERIFIED))
    repo.commit("a second report that vouches too")
    got = _late(repo.root)
    assert got[C2]["status"] == "beyond_retention_unverified"
    assert "unexpected report file docs/release/evidence/provisional_gates_copy.json" in got[C2]["detail"]


def test_without_git_history_nothing_is_previously_verified(tmp_path):
    plain = tmp_path / "not-a-repo"
    (plain / "docs" / "release" / "evidence").mkdir(parents=True)
    (plain / PROV).write_text(_report_with(VERIFIED), encoding="utf-8")
    got = _late(plain)
    assert got[C2]["status"] == "beyond_retention_unverified" and "git metadata is unavailable" in got[C2]["detail"]


def test_the_collector_reads_prior_verification_only_from_fixed_paths_and_head():
    import inspect
    m = _collector()
    assert m.PRIOR_VERIFICATION_REPORTS == ("docs/release/evidence/provisional_gates.json",
                                            "docs/release/evidence/renewal_gates.json")
    src = inspect.getsource(m.committed_prior_verifications)
    assert '"log", "--format=%H", "HEAD"' in src                       # history of HEAD only, no caller ref
    params = inspect.signature(m.owner_api_log_corroboration).parameters
    assert "evidence_dir" not in params                                # no caller-chosen evidence folder
    assert "renewal_gate(" in inspect.getsource(m) and "owner_api_log_corroboration(owner, api_group, now)" in \
        inspect.getsource(m.renewal_gate)                              # production passes no repo or path
