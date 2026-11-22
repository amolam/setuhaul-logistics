# 10 — Aurora PostgreSQL with express configuration, private in the VPC

**Why:** the first low-cost deploy (run 44999642677) was refused by RDS: *"To use Aurora clusters with free plan
accounts you need to set WithExpressConfiguration."* The account stays on the Free plan (owner decision).

## Phase 0 evidence

| Capability | AWS evidence | Result |
|---|---|---|
| Express in Sydney | AWS docs: every Region except GovCloud, UAE, Bahrain. The CloudFormation schema in ap-southeast-2 has `WithExpressConfiguration` (create-only). | Yes |
| Inside our VPC | RDS API (botocore ≥ 1.44.7) `CreateDBCluster`: `EnableVPCNetworking=true`, `EnableInternetAccessGateway=false`. CloudFormation has no such properties. | API only → custom resource |
| Internet gateway off | `DBCluster.InternetAccessGatewayEnabled` reads back `false` | Yes |
| Data API | `EnableHttpEndpoint` after create; docs: master username/password not supported | Non-master owner login |
| IAM DB auth | Master auth type `iam-db-auth`, cannot be modified | Bootstrap signs in with a token |
| Encryption | AWS/RDS service-owned key; no customer key (owner accepted 22 Nov 2026) | Accepted |
| Engine | Region default, not selectable | 17.9 |

## Probe (22 Nov 2026, isolated VPC `10.250.0.0/16`, deleted afterwards)

- **Free plan accepted the create.** It came up on the gateway, then moved into the VPC in about 50 s on its own: `VPCNetworkingEnabled=true`, `InternetAccessGatewayEnabled=false`, instance `PubliclyAccessible=false`.
- **Engine:** PostgreSQL 17.9 (aarch64). `StorageEncrypted` reads `false` and there is no KMS key id, because the AWS-owned key is not reported.
- **Master:** `postgres` signs in only with an IAM token, over TLS, from an in-VPC Lambda. It is a CREATEROLE + CREATEDB member of `rds_superuser`, not SUPERUSER.
- **Data API:** works with a non-master login, `SET LOCAL` in a transaction, and `pg_try_advisory_xact_lock`.
- **Pause and resume:** ServerlessV2 0–2 ACU with 300 s was kept. CloudWatch showed **0 ACU at 14:16 IST**, five minutes after the Data API's pooled connections closed (about 3 min after the last call). A Data API call then got `DatabaseResumingException` and was answered after **14.6 s**.
- **Parameter group:** a custom `aurora-postgresql17` group (pgaudit, rds.force_ssl) applied after a reboot, and `pgaudit` loaded.

## Implementation

- `setuhaul/aws/express_cluster.py` is the custom resource, running from the verified zip. Steps:
  1. Create the cluster (express, VPC on, gateway off, stack subnet group and security group, 0–2 ACU, 300 s, 1-day backup).
  2. **Fail closed** unless the live cluster reads back private.
  3. Turn on the Data API.
  4. Attach the parameter group and reboot.
  5. Keep the log group (create or adopt, 7 days, never delete).
  6. Enable the log export.
  7. Run the bootstrap.
  8. Turn on deletion protection.

  A protected cluster is retained on stack delete. An unfinished one is deleted on rollback. The handler continues in a fresh invocation rather than reach the Lambda time limit.
- `setuhaul/aws/express_bootstrap.py` is the only function in the VPC. Port 5432 is open from its security group only. It uses an IAM token as `postgres` to create `setuhaul_owner`, the database owner, with the master's attributes: CREATEROLE, CREATEDB, `rds_superuser`, never SUPERUSER. It also creates the `setuhaul` database. The password reaches it only as a SCRAM-SHA-256 verifier.
- **Unchanged:**
  - Migrations, logins and seed run through the Data API as `setuhaul_owner` (`OwnerSecretArn`).
  - Application Lambdas stay outside the VPC and use their per-role logins.
  - Booking off, WhatsApp off, test sink.
- **CI:** tests on `postgres:17`; `boto3/botocore==1.44.7`.
