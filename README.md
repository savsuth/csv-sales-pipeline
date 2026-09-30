# Serverless Sales Processor

This project computes per-product revenue and quantity totals from a
sales CSV, using Decimal arithmetic to avoid floating-point rounding
error, and records every invalid row with a specific rejection reason
instead of failing the whole file over one bad line.

The validation and aggregation logic is implemented once and runs
unmodified in two environments: as a local command-line tool, and as an
AWS Lambda function triggered by S3 object-creation events. Each upload
is assigned a deterministic job ID derived from the object's bucket, key,
and version, claimed atomically in DynamoDB, and tracked through to a
terminal status and an SNS notification, so a retried or duplicated event
cannot silently reprocess a job that already completed. The repository
contains the application, its test suite, and the Terraform configuration
used to deploy the AWS stack.

## Features

- Streaming CSV validation: required-column checks, per-row validation,
  and duplicate-header detection without loading the whole file into
  memory.
- Decimal-based revenue calculations using Python's `Decimal` type instead
  of binary floating point.
- A local CLI that produces the same output format as the AWS deployment,
  so behavior can be verified without AWS access.
- An AWS Lambda handler with idempotent, at-least-once job processing:
  deterministic job IDs, DynamoDB-backed claims with expiring leases, and
  safe retries.
- Terraform configuration for the full AWS stack (S3, SQS, Lambda,
  DynamoDB, SNS, CloudWatch), plus a separate bootstrap stack for Terraform
  state.
- GitHub Actions workflow for linting and tests on pushes to `main` and on
  every pull request, and a separate, manually triggered workflow for
  deployment.

## Architecture

![CSV sales processing architecture: an uploaded CSV moves from an S3 input bucket through an SQS queue to a Lambda processor, which claims the job in DynamoDB, writes a report to an S3 output bucket, and publishes a completion notice to SNS, with a dead-letter queue and CloudWatch alarms handling failures.](docs/architecture.svg)

On the normal path, a Lambda invocation reads the exact object version
referenced by the triggering S3 event, validates and aggregates the CSV,
writes two report files to the output bucket, updates the DynamoDB job
record, and publishes a completion notification. Malformed input, a
duplicate delivery of an already-finished job, and internal errors each
take a different, shorter path -- see
[Reliability and limitations](#reliability-and-limitations).

## Local Quick Start

Prerequisites: Python 3.12+. Input files must be UTF-8 (an optional
leading byte-order mark is accepted and stripped); the default maximum
input size is 10 MiB, overridable with `--max-bytes`.

Run all commands below from the repository root.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the processor on a sample file:

```bash
python -m file_pipeline.local --input samples/valid_sales.csv --output-dir local-output/
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Job completed (with or without rejected rows) |
| `1` | The whole file was rejected (malformed CSV or over the size limit) -- no output is written |
| `2` | The file was structurally valid but zero rows passed validation (`validation_failed`) -- `summary.json` and `rejected_rows.csv` are still written, so the rejection reasons are visible |

Other sample files, each covering one validation scenario, are in
[`samples/`](samples/): `mixed_valid_invalid.csv`, `missing_columns.csv`,
`all_invalid.csv`, and `malformed.csv` (a duplicate-header file).

A guided walkthrough (valid upload, then an all-invalid file and how to read
its rejection reasons) is in [`scripts/demo_local.sh`](scripts/demo_local.sh).

## Input Requirements and Example Output

Required columns (matched case-insensitively, surrounding whitespace
trimmed):

| Column | Requirement |
|---|---|
| `date` | `YYYY-MM-DD`, a real calendar date |
| `product` | Non-empty after trimming whitespace |
| `quantity` | Positive integer |
| `unit_price` | Non-negative, finite decimal (no `NaN`/`Infinity`, no scientific notation) |

Example input:

```csv
date,product,quantity,unit_price
2024-01-05,Widget,3,9.99
2024-01-06,Gadget,1,19.50
2024-02-30,Widget,2,9.99
2024-01-06,,1,5.00
```

`summary.json`:

```json
{
  "by_product": {
    "Gadget": { "quantity": 1, "revenue": "19.50" },
    "Widget": { "quantity": 3, "revenue": "29.97" }
  },
  "rejected_row_count": 2,
  "total_revenue": "49.47",
  "valid_row_count": 2
}
```

`rejected_rows.csv`:

```csv
date,product,quantity,unit_price,row_number,rejection_reason
2024-02-30,Widget,2,9.99,3,invalid_date
2024-01-06,,1,5.00,4,empty_product
```

A file missing a required column, with duplicate headers, that can't be
decoded, or with a field exceeding Python's CSV field-size limit is
rejected as a whole file: nothing is written, no report and no partial
output. Malformed quoting, including an unterminated quoted field, is not
a file-level error here -- Python's CSV parser absorbs everything through
the next matching quote (or the end of the file) into a single field
rather than raising a parse error, which typically surfaces as a
row-level `row_length_mismatch` rejection for that row instead (and can
absorb what looks like a subsequent data row into the same field, rather
than parsing it separately). An individual invalid row is otherwise
recorded in `rejected_rows.csv` with a reason and excluded from the
totals -- the rest of the file is still processed. If every row turns out
invalid, the job status is also `validation_failed`, but in that case
`summary.json` and `rejected_rows.csv` are still produced, showing zero
valid rows and why each row failed. **This distinction matters when
retrieving reports**: a file-level rejection produces no report to
download; a structurally valid file with zero valid rows does.

Extra columns are allowed and ignored for valid rows; for a rejected row
they are retained in `rejected_rows.csv`, which echoes the raw row, subject
to the same spreadsheet formula-injection escaping applied to every field
(below).

Monetary values are computed with Python's `Decimal` type at its default
precision (28 significant digits), not binary floating point, and
serialized as plain decimal strings -- no scientific notation and no float
rounding error. This is exact arithmetic within that precision, not
arbitrary-precision arithmetic; it does not guarantee exactness for
inputs whose aggregated values exceed 28 significant digits.

Fields written to `rejected_rows.csv` that begin with `=`, `+`, `-`, `@`, a
tab, or a carriage return are prefixed with a leading apostrophe before
being written, per OWASP's CSV-injection guidance -- this prevents a value
like `=SUM(A1:A9)` in a rejected row from executing as a formula when the
file is opened in spreadsheet software.

<details>
<summary>Row-level rejection reason codes and internal field names</summary>

Row-level rejection reasons: `row_length_mismatch`, `invalid_date`,
`empty_product`, `invalid_quantity`, `non_positive_quantity`,
`invalid_unit_price`, `negative_unit_price`, `non_finite_unit_price`. A
duplicate header name (after normalization) makes the whole file
malformed, since the column a value belongs to would be ambiguous. A row
with a different number of fields than the header is rejected
individually, not treated as a file-level error.

In the AWS deployment, a file-level rejection and an all-rows-invalid
file are both recorded with the same `validation_failed` status on the
DynamoDB job record; the field that distinguishes them is the presence of
`output_summary_key` / `output_rejected_key` (absent for a file-level
rejection, present when a report was produced). The local CLI
distinguishes them directly, through its exit code (`1` vs `2`, above).

</details>

## AWS Deployment and Configuration

Prerequisites: Terraform >= 1.10 (required for S3-native state locking --
see [`infra/versions.tf`](infra/versions.tf)) and an authenticated AWS CLI
session (see [Authentication](#authentication)).

Two separate Terraform roots:

| Root | Purpose |
|---|---|
| `infra/bootstrap` | Creates the S3 bucket that holds the main stack's Terraform state. Uses local state itself. |
| `infra` | The application stack: S3 buckets, SQS, Lambda, DynamoDB, SNS, CloudWatch. Uses an S3 backend. |

`aws_region` and `project_name` must be set to the same values in both
roots. `infra/bootstrap` names the state bucket and, when GitHub OIDC is
enabled, scopes the deploy role's permissions from its own copy of these
variables; a mismatch between the two roots leaves the state bucket or the
deploy role pointed at resources under a different name or region than the
application stack actually uses.

### Authentication

The AWS CLI, Terraform, and Boto3 all read credentials from the standard
AWS credential chain, which can include long-lived access keys if you
configure it that way. The recommended options here use temporary
credentials instead:

- From a laptop: an AWS CLI profile backed by IAM Identity Center SSO
  (`aws configure sso`) or the AWS CLI's browser-based `aws login`.
- From GitHub Actions: the deploy workflow (`deploy.yml`) is wired for
  OIDC only (`aws-actions/configure-aws-credentials` with
  `role-to-assume`) -- it has no path for static access keys.

### Deploying

```bash
# 1. One-time: create the Terraform state bucket
cd infra/bootstrap
terraform init
terraform plan -out=tfplan
# Review the plan above -- it should show only the state bucket and its
# supporting resources -- before applying it.
terraform apply tfplan
terraform output -raw backend_hcl > ../backend.hcl
cd ..

# 2. The application stack (now in infra/, using the backend file just written)
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
# Review this plan before applying it too.
terraform apply tfplan

# Return to the repository root -- later commands in this README assume it.
cd ..
```

Key variables (see [`infra/variables.tf`](infra/variables.tf) for the full
list with defaults):

| Variable | Purpose |
|---|---|
| `aws_region` | Region for every resource (default `us-east-2`). Some AWS accounts restrict which regions general workloads may run in via an AWS Organizations Service Control Policy; if resource creation fails with a region-specific authorization error, check for such a policy and set `aws_region` accordingly. |
| `notification_email` | Subscribes an email address to the SNS topic. Empty by default. |
| `lambda_max_concurrency` | Caps concurrent Lambda invocations from the SQS trigger (2-1000, default 5). |
| `enable_budget_alert`, `budget_limit_usd`, `budget_alert_email` | Optional AWS Budget alert. See [Cost and cleanup](#monitoring-troubleshooting-and-cleanup). |

Set these in a gitignored `infra/terraform.tfvars`. `infra/bootstrap` has
its own separate `variables.tf` and reads its own gitignored
`infra/bootstrap/terraform.tfvars` -- there is no sharing between the two
files, so if you override `aws_region` or `project_name` from their shared
defaults, set the same values in both files. Each tfvars file stays on
your machine and is never available to GitHub Actions -- a fresh checkout
in CI has no copy of it, so the deploy workflow only ever sees each root's
variable defaults plus the repository/environment variables listed below.
A local-only customization (for example a non-default `notification_email`)
needs to be reproduced another way for a GitHub Actions deploy: as an
explicit `-var` flag added to `deploy.yml`, or as Actions variables the
workflow is updated to pass through.

<details>
<summary>Enabling GitHub Actions deploys (optional)</summary>

1. In `infra/bootstrap`, set `enable_github_oidc = true` and
   `github_repository = "owner/repo"`, then apply. If an OIDC provider for
   `token.actions.githubusercontent.com` already exists in the account, set
   `existing_github_oidc_provider_arn` instead of creating a second one.
2. Create a GitHub Environment (`production` by default) with required
   reviewers and a deployment-branch rule. GitHub Actions jobs that declare
   an environment put that environment -- not the branch -- in the OIDC
   subject claim, and the generated trust policy matches on it exactly.
3. Set repository variables `AWS_DEPLOY_ROLE_ARN` (from the bootstrap
   output), `AWS_REGION`, and `TF_STATE_BUCKET`. These are Actions
   *variables*, not secrets -- none of them is sensitive.
4. Run the `Deploy infrastructure` workflow via `workflow_dispatch`. It
   supports two inputs: `plan` (default) and `apply`. Every run -- for
   either input -- first runs `terraform plan -out=tfplan`; the `apply`
   input additionally applies that freshly generated plan file in the same
   job. Pull requests never trigger this workflow.

</details>

### IAM Permissions

The Lambda's execution role is scoped to project resources and the
relevant prefixes on them: reading the input bucket, writing the output
bucket under `reports/`, consuming its own SQS queue, reading and writing
its own DynamoDB table, publishing to its own SNS topic, and writing to
its own CloudWatch log group -- see [`infra/iam.tf`](infra/iam.tf).

## AWS Usage

Via the AWS console: upload a `.csv` object to the input bucket, then
download the report from `reports/{job_id}/` in the output bucket once
processing completes (see the note above on which outcomes produce a
report to download).

Via script. Replace each `<...>` placeholder with the actual value before
running -- a shell interprets literal angle brackets as redirection, so
these won't run as typed. Bucket, table, and queue names are available via
`terraform output` from `infra/` after deploying (for example `terraform
output -raw input_bucket_name`).

```bash
scripts/upload.sh <input-bucket-name> samples/mixed_valid_invalid.csv
# prints the bucket, key, version ID, and the resulting job_id

scripts/check_job.sh <job-table-name> <job-id>
# poll until status is completed / completed_with_rejections / validation_failed

scripts/fetch_report.sh <output-bucket-name> <job-id> ./downloaded-report/
```

## Reliability and Limitations

- **Job identity.** The job ID is deterministic: `sha256(bucket + "\n" +
  key + "\n" + version_id)`. Re-delivering the same S3 event always
  produces the same job ID, which is what lets duplicate events be
  detected.
- **Duplicate delivery.** SQS and S3 event delivery are at-least-once. A
  duplicate event for an already-terminal job (`completed`,
  `completed_with_rejections`, or `validation_failed`) does not reprocess
  the CSV. A failed or interrupted attempt is retried and can genuinely
  process the CSV again -- that is the intended recovery path.
- **Lease-conditioned writes.** DynamoDB job-record updates after the
  initial claim require the matching lease token, so a worker whose lease
  has been taken over by a newer attempt cannot overwrite that attempt's
  result there. The S3 report writes described below do not check that
  token.
- **Report writes.** `summary.json` and `rejected_rows.csv` are written
  through two separate `PutObject` calls, not one atomic operation, at
  fixed keys, so a retry corrects a partial write from an earlier failed
  attempt. Because the output bucket is versioned, each retry's writes add
  new object versions rather than replacing history. Wait for the job to
  reach a terminal status (`completed`, `completed_with_rejections`, or
  `validation_failed`) before retrieving its report.
- **Notifications.** Publishing to SNS and recording that success in
  DynamoDB are two separate operations, so a subscriber can occasionally
  receive a duplicate notification for the same job. A successful SNS
  `Publish` call means SNS accepted the message for delivery to confirmed
  subscribers, not that a particular subscriber received it.

## Testing

```bash
pytest tests/ -v
ruff check src tests
```

Tests run without real AWS credentials or network calls: pure-Python logic
is tested directly, and the Lambda handler's AWS interactions are tested
against [moto](https://github.com/getmoto/moto)'s in-memory AWS emulation,
covering duplicate and concurrent delivery, lease expiry and recovery,
transient AWS errors, partial-output-write retry, SNS-failure retry, S3
keys with spaces and escaped characters, and retrieval of a specific
object version.

## Project Structure

```
src/file_pipeline/
  processor.py      # CSV validation and aggregation (no AWS dependency)
  local.py          # CLI runner
  storage.py        # S3 reads/writes
  jobs.py           # DynamoDB job state, claims, leases
  notifications.py  # SNS
  handler.py        # SQS-triggered Lambda entry point
tests/              # pytest; moto for AWS interaction tests
samples/            # Example CSVs, one per validation scenario
infra/              # Terraform (main stack)
infra/bootstrap/    # Terraform (state bucket, optional OIDC role)
scripts/            # Upload, status, report, redrive, teardown helpers
.github/workflows/  # CI (always) and deploy (manual, gated)
```

## Monitoring, Troubleshooting, and Cleanup

Lambda logs are structured JSON, one object per line, including `job_id`
and `error_code` fields. CSV row content, secrets, and credentials are
never logged.

As in [AWS Usage](#aws-usage), replace each `<...>` placeholder below with
the actual value (`terraform output` in `infra/`, or the job ID from a
notification or `scripts/check_job.sh`) before running -- a shell treats
literal angle brackets as redirection.

<details>
<summary>Finding logs for a job</summary>

```bash
aws logs tail /aws/lambda/<function-name> --since 1h --filter-pattern '"<job-id>"'
```

Or CloudWatch Logs Insights, since every line is valid JSON:

```
fields @timestamp, level, message, error_code
| filter job_id = "<job-id>"
| sort @timestamp
```

</details>

<details>
<summary>Inspecting and redriving failed messages</summary>

```bash
aws sqs receive-message --queue-url <dead-letter-queue-url> --max-number-of-messages 10
```

Investigate and fix the underlying cause first (check the job's
`error_code`/`error_message` with `scripts/check_job.sh`), then move
messages back onto the main queue:

```bash
scripts/redrive_dlq.sh <dlq-url> <processing-queue-arn>
```

This uses SQS's native `StartMessageMoveTask`. Because processing is
idempotent per job ID (see
[Reliability and limitations](#reliability-and-limitations)), redriving a
message whose job already completed is harmless.

</details>

<details>
<summary>Investigating a missing notification email</summary>

Check the job's `notification_status` field with `scripts/check_job.sh`.
`pending` or `failed` means SNS publication hasn't been recorded as
successful yet -- not that an email wasn't delivered; these fields track
publication, not inbox delivery. Confirm the SNS email subscription was
actually confirmed, and check the Lambda logs for an `sns_publish_failed`
entry.

</details>

<details>
<summary>Cost and cleanup</summary>

Cost drivers: S3 storage (a versioned bucket retains every version,
including from retries), Lambda invocation time, DynamoDB and SNS (both
pay-per-request), and CloudWatch Logs storage (bounded by
`log_retention_days`). Actual cost depends on usage volume.

An AWS Budget alert (`enable_budget_alert`) only sends a notification when
spend crosses a threshold -- it does not throttle, block, or cap spending
in any way.

Neither data bucket is created with `force_destroy`, so `terraform
destroy` fails on a non-empty bucket rather than silently deleting
uploads and reports. To empty a bucket deliberately (irreversible):

```bash
python scripts/empty_bucket.py <bucket-name>            # dry run, counts only
python scripts/empty_bucket.py <bucket-name> --delete    # asks for confirmation
```

The state bucket created by `infra/bootstrap` has `prevent_destroy` set
and is not touched by the main stack's `terraform destroy`.

</details>
