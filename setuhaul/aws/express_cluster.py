"""Aurora PostgreSQL with express configuration, private in the stack's VPC (docs/release/10_EXPRESS_AURORA.md).

The AWS Free plan creates Aurora only "WithExpressConfiguration", and CloudFormation's AWS::RDS::DBCluster cannot ask
for VPC networking with the internet access gateway off. This CloudFormation custom resource makes that one API call
itself, and fails closed: the deploy succeeds only once the live cluster reads back VPC networking on, the internet
access gateway off and no public instance. Then it
  1. turns the Data API on (express clusters start with it off),
  2. applies the stack's cluster parameter group (pgaudit, rds.force_ssl) and reboots once so it is in sync,
  3. has the in-VPC bootstrap function (express_bootstrap.py) sign in as the IAM-only master and create the
     non-master owner login and the `setuhaul` database (the Data API refuses the master user on express clusters),
  4. turns deletion protection on.
A stack delete leaves a protected cluster in place (as RemovalPolicy RETAIN would); a create that failed part-way is
deleted, so a rollback leaves nothing running.

The owner's password reaches the bootstrap function only as a SCRAM-SHA-256 verifier, never in plain text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.request

log = logging.getLogger("setuhaul.express_cluster")
log.setLevel(logging.INFO)


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """PostgreSQL's stored form of a SCRAM-SHA-256 password (RFC 5802/7677), as `CREATE ROLE ... PASSWORD` accepts."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


class Fail(Exception):
    pass


class OutOfTime(Exception):
    """This invocation is about to reach the Lambda time limit; the handler continues in a fresh one."""


def wait(what: str, check, timeout_s: int, every: int = 10, sleep=time.sleep, clock=time.monotonic,
         deadline: float | None = None):
    end = clock() + timeout_s
    while True:
        got = check()
        if got:
            return got
        if deadline is not None and clock() > deadline:
            raise OutOfTime(what)
        if clock() > end:
            raise Fail(f"timed out waiting for {what}")
        sleep(every)


class ExpressCluster:
    def __init__(self, rds, lam, sm, props: dict, sleep=None, clock=None, deadline: float | None = None, logs=None):
        self.rds, self.lam, self.sm, self.p, self.logs = rds, lam, sm, props, logs
        self.id = props["ClusterIdentifier"]
        self.sleep, self.clock, self.deadline = sleep or time.sleep, clock or time.monotonic, deadline

    def _wait(self, what, check, timeout_s, every=10):
        return wait(what, check, timeout_s, every, self.sleep, self.clock, self.deadline)

    def describe(self) -> dict | None:
        try:
            return self.rds.describe_db_clusters(DBClusterIdentifier=self.id)["DBClusters"][0]
        except self.rds.exceptions.DBClusterNotFoundFault:
            return None

    def instances(self, c: dict) -> list[dict]:
        return [self.rds.describe_db_instances(DBInstanceIdentifier=m["DBInstanceIdentifier"])["DBInstances"][0]
                for m in c.get("DBClusterMembers", [])]

    def private_problems(self, c: dict) -> list[str]:
        problems = []
        if c.get("VPCNetworkingEnabled") is not True:
            problems.append("VPC networking is not on")
        if c.get("InternetAccessGatewayEnabled") is not False:
            problems.append("the internet access gateway is not off")
        if c.get("DBSubnetGroup") != self.p["SubnetGroupName"]:
            problems.append(f"subnet group is {c.get('DBSubnetGroup')!r}")
        if {g["VpcSecurityGroupId"] for g in c.get("VpcSecurityGroups", [])} != {self.p["SecurityGroupId"]}:
            problems.append("security groups are not exactly the stack's database group")
        if any(i.get("PubliclyAccessible") for i in self.instances(c)):
            problems.append("an instance is publicly accessible")
        return problems

    # ------------------------------------------------------------------ create
    def create(self) -> dict:
        p = self.p
        if not self.describe():
            self.rds.create_db_cluster(
                DBClusterIdentifier=self.id, Engine="aurora-postgresql", WithExpressConfiguration=True,
                EnableVPCNetworking=True, EnableInternetAccessGateway=False,
                DBSubnetGroupName=p["SubnetGroupName"], VpcSecurityGroupIds=[p["SecurityGroupId"]],
                ServerlessV2ScalingConfiguration={"MinCapacity": float(p["MinCapacity"]),
                                                  "MaxCapacity": float(p["MaxCapacity"]),
                                                  "SecondsUntilAutoPause": int(p["SecondsUntilAutoPause"])},
                BackupRetentionPeriod=int(p["BackupRetentionDays"]), DeletionProtection=False,
                Tags=[{"Key": k, "Value": v} for k, v in p.get("Tags", {}).items()])
            log.info("create requested for %s (express, VPC networking, no internet access gateway)", self.id)

        def private_and_available():
            c = self.describe()
            ok = (c and c["Status"] == "available" and not self.private_problems(c)
                  and all(i["DBInstanceStatus"] == "available" for i in self.instances(c)) and c["DBClusterMembers"])
            return c if ok else None
        c = self._wait("the cluster to be available inside the VPC", private_and_available, 1500, 15)

        if not c.get("HttpEndpointEnabled"):
            self.rds.enable_http_endpoint(ResourceArn=c["DBClusterArn"])
        self._wait("the Data API", lambda: (self.describe() or {}).get("HttpEndpointEnabled"), 600)

        self.apply_parameter_group()
        self.enable_log_export()
        self.bootstrap(c)
        self.rds.modify_db_cluster(DBClusterIdentifier=self.id, DeletionProtection=True, ApplyImmediately=True)
        self._wait("deletion protection", lambda: self._settled(lambda c: c.get("DeletionProtection")), 600)
        return self.data()

    def _settled(self, cond):
        c = self.describe()
        return c if c and c["Status"] == "available" and cond(c) else None

    def apply_parameter_group(self):
        want = self.p["ParameterGroupName"]
        c = self.describe()
        if c.get("DBClusterParameterGroup") != want:
            self.rds.modify_db_cluster(DBClusterIdentifier=self.id, DBClusterParameterGroupName=want, ApplyImmediately=True)
        member = lambda c: c["DBClusterMembers"][0].get("DBClusterParameterGroupStatus")
        c = self._wait("the parameter group to be attached",
                       lambda: self._settled(lambda c: c.get("DBClusterParameterGroup") == want
                                             and member(c) in ("pending-reboot", "in-sync")), 900)
        if member(c) == "pending-reboot":
            self.rds.reboot_db_instance(DBInstanceIdentifier=c["DBClusterMembers"][0]["DBInstanceIdentifier"])
            self.sleep(30)
        self._wait("the parameter group to be in sync after the reboot",
                   lambda: self._settled(lambda c: member(c) == "in-sync"
                                         and all(i["DBInstanceStatus"] == "available" for i in self.instances(c))), 900)

    def keep_log_group(self):
        """The export's log group with its retention: created if missing, adopted if it exists, never deleted."""
        name, days = self.p["LogGroupName"], int(self.p["LogRetentionDays"])
        found = [g for g in self.logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"] if g["logGroupName"] == name]
        if not found:
            self.logs.create_log_group(logGroupName=name)
        if not found or found[0].get("retentionInDays") != days:
            self.logs.put_retention_policy(logGroupName=name, retentionInDays=days)

    def enable_log_export(self):
        self.keep_log_group()
        c = self.describe()
        if "postgresql" in (c.get("EnabledCloudwatchLogsExports") or []):
            return
        self.rds.modify_db_cluster(DBClusterIdentifier=self.id, ApplyImmediately=True,
                                   CloudwatchLogsExportConfiguration={"EnableLogTypes": ["postgresql"]})
        self._wait("the postgresql log export",
                   lambda: self._settled(lambda c: "postgresql" in (c.get("EnabledCloudwatchLogsExports") or [])), 900)

    def bootstrap(self, c: dict):
        owner = json.loads(self.sm.get_secret_value(SecretId=self.p["OwnerSecretArn"])["SecretString"])
        payload = {"host": c["Endpoint"], "database": self.p["DatabaseName"], "owner": owner["username"],
                   "owner_verifier": scram_verifier(owner["password"])}
        r = self.lam.invoke(FunctionName=self.p["BootstrapFunction"], Payload=json.dumps(payload).encode())
        body = json.loads(r["Payload"].read() or b"{}")
        if r.get("FunctionError") or not body.get("ok"):
            raise Fail(f"database bootstrap failed: {body.get('errorType') or body.get('error')}: "
                       f"{str(body.get('errorMessage', ''))[:300]}")
        log.info("bootstrap: %s", json.dumps({k: v for k, v in body.items() if k != "ok"}))

    def data(self) -> dict:
        c = self.describe()
        return {"ClusterArn": c["DBClusterArn"], "Endpoint": c["Endpoint"], "EngineVersion": c["EngineVersion"],
                "ResourceId": c["DbClusterResourceId"]}

    # ------------------------------------------------------------------ update / delete
    def update(self) -> dict:
        p, c = self.p, self.describe()
        want = {"MinCapacity": float(p["MinCapacity"]), "MaxCapacity": float(p["MaxCapacity"]),
                "SecondsUntilAutoPause": int(p["SecondsUntilAutoPause"])}
        have = {k: c.get("ServerlessV2ScalingConfiguration", {}).get(k) for k in want}
        if have != want or c.get("BackupRetentionPeriod") != int(p["BackupRetentionDays"]):
            self.rds.modify_db_cluster(DBClusterIdentifier=self.id, ServerlessV2ScalingConfiguration=want,
                                       BackupRetentionPeriod=int(p["BackupRetentionDays"]), ApplyImmediately=True)
            self._wait("the scaling change", lambda: self._settled(lambda c: True), 900)
        problems = self.private_problems(self.describe())
        if problems:
            raise Fail("the cluster is not private: " + "; ".join(problems))
        return self.data()

    def delete(self) -> None:
        c = self.describe()
        if not c:
            return
        if c.get("DeletionProtection"):
            log.info("%s has deletion protection; left in place (retained)", self.id)
            return
        for m in c.get("DBClusterMembers", []):
            try:
                self.rds.delete_db_instance(DBInstanceIdentifier=m["DBInstanceIdentifier"], SkipFinalSnapshot=True)
            except self.rds.exceptions.DBInstanceNotFoundFault:
                pass
        self.rds.delete_db_cluster(DBClusterIdentifier=self.id, SkipFinalSnapshot=True)
        self._wait("the unfinished cluster to be deleted", lambda: self.describe() is None, 1500, 20)


# ---------------------------------------------------------------------- CloudFormation custom resource handler
def respond(event: dict, status: str, physical_id: str, data: dict | None = None, reason: str = "") -> None:
    body = json.dumps({"Status": status, "Reason": reason[:900] or "see CloudWatch logs", "PhysicalResourceId": physical_id,
                       "StackId": event["StackId"], "RequestId": event["RequestId"],
                       "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}}).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                 headers={"content-type": "", "content-length": str(len(body))})
    urllib.request.urlopen(req, timeout=30).read()


MAX_ROUNDS = 6                    # 6 x 15 min: well inside CloudFormation's one-hour custom resource limit


def handler(event, context, clients=None):
    import boto3
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    rds, lam, sm, logs = clients or (boto3.client("rds", region_name=region), boto3.client("lambda", region_name=region),
                                     boto3.client("secretsmanager", region_name=region),
                                     boto3.client("logs", region_name=region))
    props = {k: v for k, v in event["ResourceProperties"].items() if k != "ServiceToken"}
    deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - 90
    x = ExpressCluster(rds, lam, sm, props, deadline=deadline, logs=logs)
    kind = event["RequestType"]
    physical = event.get("PhysicalResourceId") or x.id
    rounds = int(event.get("SetuHaulRound", 1))
    try:
        if kind == "Create":
            data = x.create()
        elif kind == "Update":
            data = x.update()
        else:
            x.delete()
            data = {}
        respond(event, "SUCCESS", x.id, data)
    except OutOfTime as e:
        if rounds >= MAX_ROUNDS:
            respond(event, "FAILED", physical, reason=f"gave up after {rounds} rounds waiting for {e}")
            return
        log.info("continuing in a fresh invocation (round %d) while waiting for %s", rounds + 1, e)
        lam.invoke(FunctionName=context.function_name, InvocationType="Event",
                   Payload=json.dumps({**event, "SetuHaulRound": rounds + 1}).encode())
    except Exception as e:                                            # noqa: BLE001 - always answer CloudFormation
        log.exception("%s failed", kind)
        respond(event, "FAILED", physical, reason=f"{type(e).__name__}: {e}")
