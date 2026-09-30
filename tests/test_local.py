import json
from pathlib import Path

from file_pipeline.local import EXIT_FILE_REJECTED, EXIT_OK, EXIT_VALIDATION_FAILED, main

SAMPLES = Path(__file__).parent.parent / "samples"


def test_valid_sales_end_to_end(tmp_path):
    out = tmp_path / "out"
    code = main(["--input", str(SAMPLES / "valid_sales.csv"), "--output-dir", str(out)])
    assert code == EXIT_OK

    summary = json.loads((out / "summary.json").read_text())
    assert summary["valid_row_count"] == 4
    assert summary["rejected_row_count"] == 0
    assert (out / "rejected_rows.csv").exists()


def test_mixed_valid_invalid_end_to_end(tmp_path):
    out = tmp_path / "out"
    code = main(
        ["--input", str(SAMPLES / "mixed_valid_invalid.csv"), "--output-dir", str(out)]
    )
    assert code == EXIT_OK

    summary = json.loads((out / "summary.json").read_text())
    assert summary["valid_row_count"] > 0
    assert summary["rejected_row_count"] > 0


def test_missing_columns_writes_no_output(tmp_path):
    out = tmp_path / "out"
    code = main(["--input", str(SAMPLES / "missing_columns.csv"), "--output-dir", str(out)])
    assert code == EXIT_FILE_REJECTED
    assert not out.exists()


def test_all_invalid_is_validation_failed_but_writes_output(tmp_path):
    out = tmp_path / "out"
    code = main(["--input", str(SAMPLES / "all_invalid.csv"), "--output-dir", str(out)])
    assert code == EXIT_VALIDATION_FAILED
    summary = json.loads((out / "summary.json").read_text())
    assert summary["valid_row_count"] == 0
    assert (out / "rejected_rows.csv").exists()


def test_malformed_csv_writes_no_output(tmp_path):
    out = tmp_path / "out"
    code = main(["--input", str(SAMPLES / "malformed.csv"), "--output-dir", str(out)])
    assert code == EXIT_FILE_REJECTED
    assert not out.exists()


def test_input_size_limit_enforced_before_processing(tmp_path):
    big_file = tmp_path / "big.csv"
    big_file.write_text("date,product,quantity,unit_price\n" + "2024-01-01,Widget,1,1.00\n" * 1000)
    out = tmp_path / "out"
    code = main(
        [
            "--input",
            str(big_file),
            "--output-dir",
            str(out),
            "--max-bytes",
            "100",
        ]
    )
    assert code == EXIT_FILE_REJECTED
    assert not out.exists()


def test_missing_input_file(tmp_path):
    code = main(
        ["--input", str(tmp_path / "does-not-exist.csv"), "--output-dir", str(tmp_path / "out")]
    )
    assert code == EXIT_FILE_REJECTED
