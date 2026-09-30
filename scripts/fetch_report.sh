#!/usr/bin/env bash
# Downloads a completed job's summary.json and rejected_rows.csv.
#
# Usage: scripts/fetch_report.sh <output-bucket-name> <job-id> [destination-dir]
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <output-bucket-name> <job-id> [destination-dir]" >&2
  exit 1
fi

BUCKET="$1"
JOB_ID="$2"
DEST="${3:-.}"

mkdir -p "$DEST"
aws s3 cp "s3://$BUCKET/reports/$JOB_ID/summary.json" "$DEST/summary.json"
aws s3 cp "s3://$BUCKET/reports/$JOB_ID/rejected_rows.csv" "$DEST/rejected_rows.csv"

echo
echo "summary.json:"
cat "$DEST/summary.json"
