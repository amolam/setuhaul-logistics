# Redaction record: `option_d_manifest.json` (24 Nov 2026)

**What:** the AWS account id in two Step Functions ARNs of `docs/release/evidence/option_d_manifest.json` was replaced by
the project's standard mask `************`, as the owner's rule requires ("mask AWS account IDs in every output and
evidence file").

| Field (`gates.BOOKING_WORKFLOW`) | Before | After |
|---|---|---|
| `state_machine_arn` | `arn:aws:states:ap-southeast-2:<account id>:stateMachine:setuhaul-pilot-approval:live` | `arn:aws:states:ap-southeast-2:************:stateMachine:setuhaul-pilot-approval:live` |
| `state_machine_version` | `arn:aws:states:ap-southeast-2:<account id>:stateMachine:setuhaul-pilot-approval:1` | `arn:aws:states:ap-southeast-2:************:stateMachine:setuhaul-pilot-approval:1` |

No other byte changed. The file stays valid JSON with the same structure, and `manifest_problems` reports none. No
other id was substituted.

## Integrity

| | SHA-256 of the file | `deployment_id_for(manifest)` |
|---|---|---|
| Original (commit `6b0a8ec`, tag `option-d-evidence-20261123`) | `b1424b42067c623236bc6548e0918afcc852127f65509d378a8b6cab3b95710e` | `m-5bb1fd39371dec9621df873b22cdf5a9d7f2e339` (the recorded deployment) |
| Redacted (this commit) | `920142df602de477d072ff576e90a1d270f64d7330870d66dd4aedd902399d9a` | `m-7bfe066687447dc83ecd12e2e2625f4f5d0d0548`: **not a deployment id; never use it** |

* A deployment id is the hash of the manifest's content, so **only the original file reproduces the recorded id**
  `m-5bb1fd39…` that `14_OPTION_D_ACTIVATION.md` and `option_d_activation.json` cite. The original is preserved,
  unchanged, at commit `6b0a8ec` and tag `option-d-evidence-20261123`. History was not rewritten and no tag was moved. To
  verify the deployment id, check out the tag.
* The pilot's database holds its own copy of the manifest (`deployment_manifests`). It wasn't touched, and the functions
  read that copy, never this file.

## Dependencies checked

* No code or test reads `option_d_manifest.json` (repository search). Its only references are the deployment id in
  `docs/release/14_OPTION_D_ACTIVATION.md` and `docs/release/evidence/option_d_activation.json`, which are unchanged and
  still correct for the original.
* No evidence-validation or deployment-verification code matches on an account id. `manifest_problems` checks
  environments and SHA-256 fields; the runtime compares the database's manifest with the function's own code hash.
* Other tracked files: the only other ARN-shaped 12-digit ids in the repository are the standard fake `123456789012` in
  tests.

## Limits

* The account id remains in **git history**: commit `6b0a8ec`, the tag, and every later commit that contains the file,
  including `release/fde-sydney-pilot` (frozen, not modified here). Open branches (PR #5, PR #6) still carry the unmasked
  file until they merge `main`. Removing it from history would mean rewriting history and moving the tag. That's an
  owner decision and was not done.
* With the original SHA-256 above and the redacted content, the 12 digits could in principle be recovered by brute
  force. This adds no exposure beyond the history, which already contains them.
