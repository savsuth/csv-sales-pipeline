#!/usr/bin/env bash
# Uploads a CSV to the input bucket and prints the object's version ID,
# which (together with the bucket and key) determines its job_id.
#
# Usage: scripts/upload.sh <input-bucket-name> <path/to/file.csv> [destination-key]
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <input-bucket-name> <path/to/file.csv> [destination-key]" >&2
  exit 1
fi

BUCKET="$1"
FILE="$2"
KEY="${3:-$(basename "$FILE")}"

if [ ! -f "$FILE" ]; then
  echo "error: file not found: $FILE" >&2
  exit 1
fi

echo "Uploading $FILE to s3://$BUCKET/$KEY ..." >&2
RESULT=$(aws s3api put-object \
  --bucket "$BUCKET" \
  --key "$KEY" \
  --body "$FILE" \
  --content-type text/csv)

VERSION_ID=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['VersionId'])")

echo "$RESULT" >&2
echo
echo "bucket:     $BUCKET"
echo "key:        $KEY"
echo "version_id: $VERSION_ID"
echo
echo "job_id:     $(python3 -c "
import hashlib
print(hashlib.sha256(f'$BUCKET\n$KEY\n$VERSION_ID'.encode()).hexdigest())
")"
