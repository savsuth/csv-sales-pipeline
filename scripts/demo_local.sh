#!/usr/bin/env bash
# Runs entirely locally -- no AWS needed. Walks through: a valid upload
# producing a report, then an invalid input and how you'd investigate it.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN() { python3 -m file_pipeline.local "$@"; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }

echo "=== 1. Valid upload -> report ==="
rm -rf local-output/demo-valid
RUN --input samples/valid_sales.csv --output-dir local-output/demo-valid
echo
echo "--- summary.json ---"
cat local-output/demo-valid/summary.json
echo

echo
echo "=== 2. Invalid input -> failure investigation ==="
rm -rf local-output/demo-invalid
set +e
RUN --input samples/all_invalid.csv --output-dir local-output/demo-invalid
EXIT_CODE=$?
set -e
echo "exit code: $EXIT_CODE (2 = validation_failed: well-formed CSV, zero valid rows)"
echo
echo "--- rejected_rows.csv (why every row was rejected) ---"
cat local-output/demo-invalid/rejected_rows.csv
echo

echo
echo "=== 3. Malformed CSV -> rejected outright, no report written ==="
rm -rf local-output/demo-malformed
set +e
RUN --input samples/malformed.csv --output-dir local-output/demo-malformed
EXIT_CODE=$?
set -e
echo "exit code: $EXIT_CODE (1 = file rejected: malformed CSV, no output written)"
ls local-output/demo-malformed 2>/dev/null || echo "(no output directory was created, as expected)"
