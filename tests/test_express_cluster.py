"""Private Aurora with express configuration (setuhaul/aws/express_cluster.py, express_bootstrap.py).

The AWS Free plan creates Aurora only with express configuration; the custom resource must create it inside the VPC
with the internet access gateway off and fail closed otherwise. RDS and Lambda are stubs here; the bootstrap SQL runs
against the local PostgreSQL (and, end to end, in tests/test_aurora_owner.py on an empty cluster).
"""
from __future__ import annotations

import io
import json

import psycopg
import pytest

from setuhaul.aws import express_cluster as xc

PROPS = {"ClusterIdentifier": "setuhaul-pilot", "SubnetGroupName": "setuhaul-pilot", "SecurityGroupId": "sg-db",
         "ParameterGroupName": "setuhaul-pilot", "MinCapacity": "0", "MaxCapacity": "2", "SecondsUntilAutoPause": "300",
         "BackupRetentionDays": "1", "OwnerSecretArn": "arn:owner", "DatabaseName": "setuhaul",
         "BootstrapFunction": "setuhaul-pilot-db-bootstrap", "Tags": {"Project": "SetuHaul"},
         "LogGroupName": "/aws/rds/cluster/setuhaul-pilot/postgresql", "LogRetentionDays": "7"}


class NotFound(Exception):
    pass


class FakeRds:
    """A cluster that comes up on the internet access gateway and then moves into the VPC (what AWS did in the probe),
    unless `stays_public`."""

    class exceptions:
        DBClusterNotFoundFault = NotFound
        DBInstanceNotFoundFault = NotFound

    def __init__(self, stays_public=False, public_instance=False, busy_after_change=0):
        self.calls, self.c, self.ticks = [], None, 0
        self.stays_public, self.public_instance = stays_public, public_instance
        self.busy_after_change, self.busy = busy_after_change, 0       # describes that still read `modifying`

    def create_db_cluster(self, **kw):
        self.calls.append(("create_db_cluster", kw))
        self.c = {"DBClusterIdentifier": kw["DBClusterIdentifier"], "Status": "creating", "DBClusterArn": "arn:cluster",
                  "Endpoint": "setuhaul-pilot.cluster-x.ap-southeast-2.rds.amazonaws.com", "EngineVersion": "17.9",
                  "DbClusterResourceId": "cluster-ABC", "VPCNetworkingEnabled": False, "InternetAccessGatewayEnabled": True,
                  "DBSubnetGroup": kw["DBSubnetGroupName"], "VpcSecurityGroups": [{"VpcSecurityGroupId": kw["VpcSecurityGroupIds"][0]}],
                  "DBClusterMembers": [{"DBInstanceIdentifier": "setuhaul-pilot-instance-1",
                                        "DBClusterParameterGroupStatus": "in-sync"}],
                  "DBClusterParameterGroup": "default.aurora-postgresql17", "HttpEndpointEnabled": False,
                  "DeletionProtection": kw["DeletionProtection"], "EnabledCloudwatchLogsExports": [],
                  "ServerlessV2ScalingConfiguration": kw["ServerlessV2ScalingConfiguration"],
                  "BackupRetentionPeriod": kw["BackupRetentionPeriod"]}

    def describe_db_clusters(self, DBClusterIdentifier):
        if not self.c:
            raise NotFound()
        self.ticks += 1
        if self.ticks == 2 and not self.stays_public:                  # the move into the VPC, once
            self.c.update(Status="available", VPCNetworkingEnabled=True, InternetAccessGatewayEnabled=False)
        if self.busy:
            self.busy -= 1
            return {"DBClusters": [dict(self.c, Status="modifying")]}
        return {"DBClusters": [dict(self.c)]}

    def _changing(self):
        if self.busy:                                                  # what RDS answered on the first deploy
            self.busy -= 1
            e = Exception("DB cluster isn't available for modification with status modifying.")
            e.response = {"Error": {"Code": "InvalidDBClusterStateFault"}}
            raise e
        self.busy = self.busy_after_change

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceStatus": "available", "PubliclyAccessible": self.public_instance}]}

    def enable_http_endpoint(self, ResourceArn):
        self._changing()
        self.calls.append(("enable_http_endpoint", ResourceArn))
        self.c["HttpEndpointEnabled"] = True

    def modify_db_cluster(self, **kw):
        self._changing()
        self.calls.append(("modify_db_cluster", kw))
        if "DBClusterParameterGroupName" in kw:
            self.c["DBClusterParameterGroup"] = kw["DBClusterParameterGroupName"]
            self.c["DBClusterMembers"][0]["DBClusterParameterGroupStatus"] = "pending-reboot"
        if "CloudwatchLogsExportConfiguration" in kw:
            self.c["EnabledCloudwatchLogsExports"] = kw["CloudwatchLogsExportConfiguration"]["EnableLogTypes"]
        if "DeletionProtection" in kw:
            self.c["DeletionProtection"] = kw["DeletionProtection"]
        if "ServerlessV2ScalingConfiguration" in kw:
            self.c["ServerlessV2ScalingConfiguration"] = kw["ServerlessV2ScalingConfiguration"]

    def reboot_db_instance(self, DBInstanceIdentifier):
        self.calls.append(("reboot_db_instance", DBInstanceIdentifier))
        self.c["DBClusterMembers"][0]["DBClusterParameterGroupStatus"] = "in-sync"

    def delete_db_instance(self, **kw):
        self.calls.append(("delete_db_instance", kw))

    def delete_db_cluster(self, **kw):
        self.calls.append(("delete_db_cluster", kw))
        self.c = None


class FakeLambda:
    def __init__(self, result=None):
        self.calls, self.result = [], result or {"ok": True, "owner": "setuhaul_owner", "server_version": "17.9"}

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.result).encode())}


class FakeSm:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"username": "setuhaul_owner", "password": "synthetic-owner-password"})}


class FakeLogs:
    def __init__(self, existing=None):
        self.groups, self.calls = dict(existing or {}), []

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": n, **({"retentionInDays": d} if d else {})}
                              for n, d in self.groups.items() if n.startswith(logGroupNamePrefix)]}

    def create_log_group(self, logGroupName):
        self.calls.append(("create", logGroupName))
        self.groups[logGroupName] = None

    def put_retention_policy(self, logGroupName, retentionInDays):
        self.calls.append(("retention", logGroupName, retentionInDays))
        self.groups[logGroupName] = retentionInDays

    def delete_log_group(self, **kw):                     # never called
        raise AssertionError("the database log group must never be deleted")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(rds=None, lam=None, deadline=None, logs=None):
    clock = Clock()
    return xc.ExpressCluster(rds or FakeRds(), lam or FakeLambda(), FakeSm(), dict(PROPS), sleep=clock.sleep,
                             clock=clock, deadline=deadline, logs=logs or FakeLogs())


def test_the_database_log_group_is_created_or_adopted_with_7_day_retention_and_never_deleted():
    name = PROPS["LogGroupName"]
    new = FakeLogs()
    make(logs=new).create()
    assert new.calls == [("create", name), ("retention", name, 7)]
    kept = FakeLogs({name: 7})                                   # retained from an earlier deploy, already 7 days
    x = make(logs=kept)
    x.create()
    assert kept.calls == []
    x.rds.c["DeletionProtection"] = False
    x.delete()                                                   # the cluster goes; the log group stays
    assert name in kept.groups
    odd = FakeLogs({name: None})                                 # exists without retention: retention is set
    make(logs=odd).create()
    assert odd.calls == [("retention", name, 7)]


def test_create_asks_for_express_inside_the_vpc_with_the_internet_gateway_off_and_finishes_private():
    rds, lam = FakeRds(), FakeLambda()
    data = make(rds, lam).create()
    (name, kw), = [c for c in rds.calls if c[0] == "create_db_cluster"]
    assert kw["WithExpressConfiguration"] is True and kw["EnableVPCNetworking"] is True
    assert kw["EnableInternetAccessGateway"] is False
    assert kw["DBSubnetGroupName"] == "setuhaul-pilot" and kw["VpcSecurityGroupIds"] == ["sg-db"]
    assert kw["ServerlessV2ScalingConfiguration"] == {"MinCapacity": 0.0, "MaxCapacity": 2.0, "SecondsUntilAutoPause": 300}
    assert kw["BackupRetentionPeriod"] == 1 and kw["Engine"] == "aurora-postgresql"
    assert "MasterUserPassword" not in kw and "KmsKeyId" not in kw
    order = [c[0] for c in rds.calls]
    assert order.index("enable_http_endpoint") < order.index("reboot_db_instance")     # Data API, then parameters
    assert rds.c["DBClusterParameterGroup"] == "setuhaul-pilot" and rds.c["EnabledCloudwatchLogsExports"] == ["postgresql"]
    assert rds.c["DeletionProtection"] is True and rds.calls[-1][1].get("DeletionProtection") is True   # last step
    assert data == {"ClusterArn": "arn:cluster", "Endpoint": rds.c["Endpoint"], "EngineVersion": "17.9",
                    "ResourceId": "cluster-ABC"}


def test_each_change_waits_until_the_cluster_is_available_again():
    """Regression (deploy run 45011607396): ModifyDBCluster right after EnableHttpEndpoint was refused with
    InvalidDBClusterStateFault ("status modifying"). Every change now waits for `available` and retries that refusal."""
    rds = FakeRds(busy_after_change=3)
    make(rds).create()
    assert rds.c["DeletionProtection"] is True and rds.c["DBClusterParameterGroup"] == "setuhaul-pilot"
    assert [c[0] for c in rds.calls].count("modify_db_cluster") == 3      # parameter group, log export, protection


def test_a_state_refusal_is_retried_but_other_errors_are_not():
    rds = FakeRds()
    x = make(rds)
    x.create()
    rds.busy = 2
    real = rds.describe_db_clusters
    rds.describe_db_clusters = lambda **kw: {"DBClusters": [dict(rds.c)]}    # reads available, still refuses twice
    x.modify("a test change", DeletionProtection=True)
    assert rds.busy == 0
    rds.describe_db_clusters = real

    def bad(**kw):
        e = Exception("bad")
        e.response = {"Error": {"Code": "InvalidParameterCombination"}}
        raise e
    with pytest.raises(Exception, match="bad"):
        x.when_available("a bad change", bad)


def test_the_bootstrap_gets_a_password_verifier_never_the_password():
    lam = FakeLambda()
    make(lam=lam).create()
    (call,) = lam.calls
    payload = json.loads(call["Payload"])
    assert call["FunctionName"] == "setuhaul-pilot-db-bootstrap"
    assert payload["owner"] == "setuhaul_owner" and payload["database"] == "setuhaul"
    assert payload["owner_verifier"].startswith("SCRAM-SHA-256$4096:")
    assert "synthetic-owner-password" not in call["Payload"].decode()


def test_a_cluster_that_stays_on_the_internet_gateway_fails_the_deploy():
    with pytest.raises(xc.Fail, match="inside the VPC"):
        make(FakeRds(stays_public=True)).create()
    with pytest.raises(xc.Fail):
        make(FakeRds(public_instance=True)).create()


def test_a_failed_bootstrap_fails_the_deploy_before_deletion_protection():
    rds = FakeRds()
    with pytest.raises(xc.Fail, match="bootstrap failed"):
        make(rds, FakeLambda({"errorType": "OperationalError", "errorMessage": "no route"})).create()
    assert rds.c["DeletionProtection"] is False          # so the rollback's Delete removes the unfinished cluster


def test_delete_keeps_a_protected_cluster_and_removes_an_unfinished_one():
    rds = FakeRds()
    x = make(rds)
    x.create()
    x.delete()
    assert not [c for c in rds.calls if c[0].startswith("delete")] and rds.c is not None        # retained
    rds.c["DeletionProtection"] = False
    x.delete()
    assert [c[0] for c in rds.calls if c[0].startswith("delete")] == ["delete_db_instance", "delete_db_cluster"]


def test_update_rechecks_that_the_cluster_is_still_private():
    rds = FakeRds()
    x = make(rds)
    x.create()
    rds.c["InternetAccessGatewayEnabled"] = True
    with pytest.raises(xc.Fail, match="not private"):
        x.update()


# ---------------------------------------------------------------- the CloudFormation handler
class Ctx:
    function_name = "setuhaul-pilot-db-express"

    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


EVENT = {"RequestType": "Create", "ResourceProperties": {"ServiceToken": "arn:fn", **PROPS}, "StackId": "s",
         "RequestId": "r", "LogicalResourceId": "Db", "ResponseURL": "https://example.invalid/response"}


def test_the_handler_continues_in_a_fresh_invocation_instead_of_timing_out(monkeypatch):
    sent = []
    monkeypatch.setattr(xc, "respond", lambda event, status, pid, data=None, reason="": sent.append((status, reason)))
    monkeypatch.setattr(xc.time, "sleep", lambda s: None)
    rds, lam = FakeRds(stays_public=True), FakeLambda()
    xc.handler(dict(EVENT), Ctx(60_000), clients=(rds, lam, FakeSm(), FakeLogs()))     # 60 s left: below the 90 s margin
    assert sent == []                                                        # no answer yet: not done, not failed
    (again,) = [c for c in lam.calls if c.get("InvocationType") == "Event"]
    assert again["FunctionName"] == "setuhaul-pilot-db-express" and json.loads(again["Payload"])["SetuHaulRound"] == 2
    xc.handler({**EVENT, "SetuHaulRound": xc.MAX_ROUNDS}, Ctx(60_000),
               clients=(FakeRds(stays_public=True), lam, FakeSm(), FakeLogs()))
    assert sent and sent[-1][0] == "FAILED" and "gave up" in sent[-1][1]


def test_the_handler_answers_failed_with_the_reason(monkeypatch):
    sent = []
    monkeypatch.setattr(xc, "respond", lambda event, status, pid, data=None, reason="": sent.append((status, pid, reason)))
    monkeypatch.setattr(xc.time, "sleep", lambda s: None)
    xc.handler(dict(EVENT), Ctx(900_000), clients=(FakeRds(), FakeLambda({"error": "x"}), FakeSm(), FakeLogs()))
    assert sent[-1][0] == "FAILED" and sent[-1][1] == "setuhaul-pilot" and "bootstrap failed" in sent[-1][2]


# ---------------------------------------------------------------- the SCRAM verifier and the bootstrap SQL
def test_postgres_stores_the_verifier_as_given_and_accepts_the_password(dsn):
    """The verifier is accepted as a SCRAM secret (not re-hashed), and the password signs in with it."""
    v = xc.scram_verifier("synthetic-pw-1", salt=b"0123456789abcdef")
    assert v == xc.scram_verifier("synthetic-pw-1", salt=b"0123456789abcdef")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS setuhaul_scram_probe")
        c.execute(f"CREATE ROLE setuhaul_scram_probe LOGIN PASSWORD '{v}'")
        try:
            stored = c.execute("SELECT rolpassword FROM pg_authid WHERE rolname = 'setuhaul_scram_probe'").fetchone()[0]
            assert stored == v
        finally:
            c.execute("DROP ROLE setuhaul_scram_probe")


def test_the_bootstrap_refuses_malformed_names_and_plain_passwords():
    from setuhaul.aws.express_bootstrap import run
    with pytest.raises(ValueError):
        run(None, "setuhaul; DROP DATABASE x", "setuhaul_owner", xc.scram_verifier("p"))
    with pytest.raises(ValueError):
        run(None, "setuhaul", "setuhaul_owner", "a-plain-password")
