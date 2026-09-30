"""Local command-line runner.

Produces byte-identical summary.json / rejected_rows.csv output to what
the Lambda handler writes to S3, without touching AWS at all. Useful for
development, and for the demo script in README.md.

    python -m file_pipeline.local \\
        --input samples/valid_sales.csv \\
        --output-dir local-output/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from file_pipeline.processor import (
    MAX_INPUT_BYTES,
    InputTooLargeError,
    MalformedCSVError,
    build_summary_dict,
    process_csv,
)

# Exit codes. Distinct from AWS job statuses: locally there is no retry
# queue or dead-letter queue, so "malformed/oversized" and
# "validation_failed" are both terminal here, just with different causes.
EXIT_OK = 0
EXIT_FILE_REJECTED = 1  # malformed CSV or input exceeds the size limit
EXIT_VALIDATION_FAILED = 2  # well-formed CSV, but zero valid rows


def run(input_path: Path, output_dir: Path, max_bytes: int = MAX_INPUT_BYTES) -> int:
    file_size = os.path.getsize(input_path)
    if file_size > max_bytes:
        print(
            f"file_rejected: input_too_large ({file_size} bytes exceeds "
            f"the {max_bytes}-byte limit)",
            file=sys.stderr,
        )
        return EXIT_FILE_REJECTED

    with tempfile.SpooledTemporaryFile(
        max_size=1024 * 1024, mode="w+", encoding="utf-8", newline=""
    ) as rejected_buffer:
        with open(input_path, "rb") as f:
            try:
                result = process_csv(f, rejected_buffer, max_bytes=max_bytes)
            except MalformedCSVError as exc:
                print(f"file_rejected: {exc.code}: {exc.message}", file=sys.stderr)
                return EXIT_FILE_REJECTED
            except InputTooLargeError:
                print(
                    f"file_rejected: input_too_large (limit is {max_bytes} bytes)",
                    file=sys.stderr,
                )
                return EXIT_FILE_REJECTED

        output_dir.mkdir(parents=True, exist_ok=True)

        summary_path = output_dir / "summary.json"
        summary_path.write_text(
            json.dumps(build_summary_dict(result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        rejected_buffer.seek(0)
        rejected_path = output_dir / "rejected_rows.csv"
        rejected_path.write_text(rejected_buffer.read(), encoding="utf-8", newline="")

    print(
        f"status: {result.status} | valid_rows: {result.valid_row_count} | "
        f"rejected_rows: {result.rejected_row_count} | "
        f"total_revenue: {build_summary_dict(result)['total_revenue']}"
    )
    print(f"wrote {summary_path}")
    print(f"wrote {rejected_path}")

    return EXIT_VALIDATION_FAILED if result.status == "validation_failed" else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Path to the input CSV")
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="Directory to write outputs into"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=MAX_INPUT_BYTES,
        help="Maximum accepted input size in bytes (default: 10 MiB)",
    )
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return EXIT_FILE_REJECTED

    return run(args.input, args.output_dir, max_bytes=args.max_bytes)


if __name__ == "__main__":
    raise SystemExit(main())
