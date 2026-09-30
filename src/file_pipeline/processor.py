"""Pure CSV validation and aggregation logic.

No AWS or filesystem-path dependencies live here: everything operates on
file-like byte streams and text writers so the exact same code runs in the
local CLI runner (local.py) and the Lambda handler (handler.py).

Design decisions (documented here because the task spec leaves them open):

* Column matching is case-insensitive and ignores surrounding whitespace,
  e.g. " Date" and "date" both satisfy the "date" requirement.
* Duplicate header names (after normalization) make the whole file
  malformed, because we can no longer say which column a value belongs to.
* Extra, unrecognized columns are allowed. Their values are preserved
  verbatim in rejected_rows.csv (since that file echoes the raw row) but
  are otherwise ignored -- they never affect validation or aggregation.
* A row with a different number of fields than the header ("ragged" CSV)
  is treated as an invalid *row* (skipped, recorded with a reason), not a
  file-level malformed-CSV error, because the header itself still parsed.
* A structural parse failure (unterminated quote, undecodable bytes, an
  empty file, missing required columns, duplicate headers) is a
  file-level error: the whole file is rejected and no outputs are
  produced. See MalformedCSVError / InputTooLargeError.
* If the file parses fine but zero rows pass validation, the caller
  should mark the job validation_failed even though summary.json and
  rejected_rows.csv are still produced (there's nothing wrong with the
  file's structure, just its content).
* Monetary values are Decimal throughout and serialized as plain decimal
  strings (no scientific notation, no float) to avoid float rounding
  error and precision loss.
* rejected_rows.csv values are defended against spreadsheet formula
  injection: any field beginning with '=', '+', '-', '@', a tab, or a
  carriage return is prefixed with a leading apostrophe before being
  written, per OWASP's CSV injection guidance. This changes how the cell
  looks if re-imported programmatically, but keeps it inert when opened
  in Excel/Sheets/Numbers.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import IO, TextIO

MAX_INPUT_BYTES = 10 * 1024 * 1024  # 10 MiB

REQUIRED_COLUMNS = ("date", "product", "quantity", "unit_price")

_CHUNK_SIZE = 64 * 1024

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_QUANTITY_RE = re.compile(r"^\d+$")
_UNIT_PRICE_RE = re.compile(r"^\d+(\.\d+)?$")
_UNIT_PRICE_NEGATIVE_RE = re.compile(r"^-\d+(\.\d+)?$")
_NON_FINITE_TOKENS = {
    "nan",
    "inf",
    "+inf",
    "-inf",
    "infinity",
    "+infinity",
    "-infinity",
}

_FORMULA_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

REJECTED_CSV_EXTRA_COLUMNS = ("row_number", "rejection_reason")

STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_REJECTIONS = "completed_with_rejections"
STATUS_VALIDATION_FAILED = "validation_failed"


class MalformedCSVError(Exception):
    """The file as a whole cannot be safely processed; reject it entirely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InputTooLargeError(Exception):
    """The input stream exceeded the configured byte limit."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Input exceeded the {max_bytes}-byte limit")
        self.max_bytes = max_bytes


@dataclass(frozen=True)
class ProductTotal:
    quantity: int
    revenue: Decimal


@dataclass
class ProcessingResult:
    status: str
    valid_row_count: int
    rejected_row_count: int
    total_revenue: Decimal
    by_product: dict[str, ProductTotal] = field(default_factory=dict)


class _CountingRawReader(io.RawIOBase):
    """Wraps any object with .read(n) and enforces a byte ceiling while
    streaming, so an oversized or mislabeled input is caught mid-read
    instead of only after being fully buffered."""

    def __init__(self, source: IO[bytes], max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._read_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        data = self._source.read(len(b))
        if not data:
            return 0
        n = len(data)
        b[:n] = data
        self._read_bytes += n
        if self._read_bytes > self._max_bytes:
            raise InputTooLargeError(self._max_bytes)
        return n


def _sanitize_csv_field(value: str) -> str:
    if value and value[0] in _FORMULA_INJECTION_PREFIXES:
        return "'" + value
    return value


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")


def _open_text_stream(binary_stream: IO[bytes], max_bytes: int) -> TextIO:
    raw = _CountingRawReader(binary_stream, max_bytes)
    buffered = io.BufferedReader(raw)
    return io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="")


def _normalize_header_name(name: str) -> str:
    return name.strip().lower()


def _validate_header(header: list[str]) -> dict[str, int]:
    normalized = [_normalize_header_name(h) for h in header]

    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in normalized:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise MalformedCSVError(
            "duplicate_header",
            f"Duplicate column name(s) in header: {sorted(duplicates)}",
        )

    column_index = {name: idx for idx, name in enumerate(normalized)}
    missing = [c for c in REQUIRED_COLUMNS if c not in column_index]
    if missing:
        raise MalformedCSVError(
            "missing_required_columns",
            f"Missing required column(s): {missing}",
        )
    return column_index


def _validate_row(
    row: list[str], column_index: dict[str, int], header_len: int
) -> tuple[str | None, int | None, Decimal | None, str | None]:
    """Returns (product, quantity, unit_price, rejection_reason).

    On success, rejection_reason is None and the other three are set.
    On failure, rejection_reason is set and the other three may be None.
    """
    if len(row) != header_len:
        return None, None, None, "row_length_mismatch"

    date_raw = row[column_index["date"]].strip()
    product_raw = row[column_index["product"]].strip()
    quantity_raw = row[column_index["quantity"]].strip()
    unit_price_raw = row[column_index["unit_price"]].strip()

    if not _DATE_RE.match(date_raw):
        return None, None, None, "invalid_date"
    try:
        year, month, day = (int(p) for p in date_raw.split("-"))
        datetime.date(year, month, day)
    except ValueError:
        return None, None, None, "invalid_date"

    if not product_raw:
        return None, None, None, "empty_product"

    if not _QUANTITY_RE.match(quantity_raw):
        return None, None, None, "invalid_quantity"
    quantity = int(quantity_raw)
    if quantity <= 0:
        return None, None, None, "non_positive_quantity"

    if unit_price_raw.lower() in _NON_FINITE_TOKENS:
        return None, None, None, "non_finite_unit_price"
    if _UNIT_PRICE_NEGATIVE_RE.match(unit_price_raw):
        return None, None, None, "negative_unit_price"
    if not _UNIT_PRICE_RE.match(unit_price_raw):
        return None, None, None, "invalid_unit_price"
    try:
        unit_price = Decimal(unit_price_raw)
    except Exception:
        return None, None, None, "invalid_unit_price"
    if not unit_price.is_finite():
        return None, None, None, "non_finite_unit_price"

    return product_raw, quantity, unit_price, None


def process_csv(
    binary_stream: IO[bytes],
    rejected_csv_writer_target: TextIO,
    *,
    max_bytes: int = MAX_INPUT_BYTES,
) -> ProcessingResult:
    """Streams and validates a CSV file, writing rejected rows as it goes.

    `rejected_csv_writer_target` should be a caller-owned, writable text
    stream (e.g. a SpooledTemporaryFile) that the caller only persists to
    its final destination (disk, S3) after this function returns
    successfully. On MalformedCSVError / InputTooLargeError, whatever was
    written to it is incomplete and must be discarded by the caller.
    """
    try:
        text_stream = _open_text_stream(binary_stream, max_bytes)
        reader = csv.reader(text_stream)
        try:
            header = next(reader)
        except StopIteration:
            raise MalformedCSVError("empty_file", "CSV file is empty") from None

        column_index = _validate_header(header)
        header_len = len(header)

        rejected_writer = csv.writer(rejected_csv_writer_target)
        rejected_writer.writerow(
            [_sanitize_csv_field(h) for h in header] + list(REJECTED_CSV_EXTRA_COLUMNS)
        )

        valid_row_count = 0
        rejected_row_count = 0
        total_revenue = Decimal("0")
        by_product: dict[str, ProductTotal] = {}

        row_number = 0
        for row in reader:
            row_number += 1
            if not row or row == [""]:
                # csv module yields [] for a fully blank physical line;
                # treat it as an empty row rather than a length mismatch.
                continue

            product, quantity, unit_price, reason = _validate_row(
                row, column_index, header_len
            )

            if reason is not None:
                rejected_row_count += 1
                sanitized_row = [_sanitize_csv_field(v) for v in row]
                rejected_writer.writerow([*sanitized_row, row_number, reason])
                continue

            assert product is not None and quantity is not None and unit_price is not None
            row_revenue = Decimal(quantity) * unit_price
            valid_row_count += 1
            total_revenue += row_revenue
            existing = by_product.get(product)
            if existing is None:
                by_product[product] = ProductTotal(quantity=quantity, revenue=row_revenue)
            else:
                by_product[product] = ProductTotal(
                    quantity=existing.quantity + quantity,
                    revenue=existing.revenue + row_revenue,
                )
    except UnicodeDecodeError as exc:
        raise MalformedCSVError("invalid_encoding", str(exc)) from exc
    except csv.Error as exc:
        raise MalformedCSVError("csv_parse_error", str(exc)) from exc

    if valid_row_count == 0:
        status = STATUS_VALIDATION_FAILED
    elif rejected_row_count == 0:
        status = STATUS_COMPLETED
    else:
        status = STATUS_COMPLETED_WITH_REJECTIONS

    return ProcessingResult(
        status=status,
        valid_row_count=valid_row_count,
        rejected_row_count=rejected_row_count,
        total_revenue=total_revenue,
        by_product=by_product,
    )


def build_summary_dict(result: ProcessingResult) -> dict:
    return {
        "total_revenue": _decimal_to_str(result.total_revenue),
        "valid_row_count": result.valid_row_count,
        "rejected_row_count": result.rejected_row_count,
        "by_product": {
            product: {
                "quantity": totals.quantity,
                "revenue": _decimal_to_str(totals.revenue),
            }
            for product, totals in sorted(result.by_product.items())
        },
    }


def summary_json_bytes(result: ProcessingResult) -> bytes:
    """Canonical summary.json bytes -- used by both local.py and
    storage.py so local and AWS output are byte-for-byte identical."""
    import json

    return (json.dumps(build_summary_dict(result), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
