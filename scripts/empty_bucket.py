#!/usr/bin/env python3
"""Permanently deletes EVERY object version and delete marker in one S3
bucket, so `terraform destroy` can remove it. Irreversible.

Dry run by default: it only counts what it would delete. To actually
delete, pass --delete and then type the bucket name when prompted.

Refuses to touch any bucket whose name doesn't start with --prefix
(default: csv-sales-pipeline-), and never the state bucket.

    python scripts/empty_bucket.py <bucket-name>            # dry run
    python scripts/empty_bucket.py <bucket-name> --delete   # asks to confirm
"""

import argparse
import sys

import boto3


def iter_versions(s3, bucket):
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        for entry in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
            yield {"Key": entry["Key"], "VersionId": entry["VersionId"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("bucket")
    parser.add_argument("--delete", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--prefix", default="csv-sales-pipeline-")
    args = parser.parse_args()

    if not args.bucket.startswith(args.prefix):
        print(f"refusing: {args.bucket!r} does not start with {args.prefix!r}", file=sys.stderr)
        return 2
    if "tfstate" in args.bucket:
        print("refusing: this looks like the Terraform state bucket", file=sys.stderr)
        return 2

    s3 = boto3.client("s3")
    total = sum(1 for _ in iter_versions(s3, args.bucket))
    print(f"{args.bucket}: {total} object versions/delete markers")

    if not args.delete:
        print("dry run only; re-run with --delete to remove them")
        return 0
    if total == 0:
        return 0

    if input(f"Type the bucket name to permanently delete all {total}: ") != args.bucket:
        print("name did not match; nothing deleted")
        return 1

    batch, deleted = [], 0
    for item in iter_versions(s3, args.bucket):
        batch.append(item)
        if len(batch) == 1000:
            s3.delete_objects(Bucket=args.bucket, Delete={"Objects": batch, "Quiet": True})
            deleted, batch = deleted + len(batch), []
    if batch:
        s3.delete_objects(Bucket=args.bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch)
    print(f"deleted {deleted} versions/delete markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
