#!/usr/bin/env bash
# Looks up a job's current record in DynamoDB.
#
# Usage: scripts/check_job.sh <job-table-name> <job-id>
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <job-table-name> <job-id>" >&2
  exit 1
fi

TABLE="$1"
JOB_ID="$2"

aws dynamodb get-item \
  --table-name "$TABLE" \
  --key "{\"job_id\": {\"S\": \"$JOB_ID\"}}" \
  --output json
