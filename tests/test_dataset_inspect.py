import io
import zipfile

import pytest

from app.services import dataset_inspect
from app.services.dataset_inspect import DatasetRejected, inspect_dataset
from tests.test_datasets_api import PNG_BYTES, SALES_XLSX, xlsx_bytes

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def inspect(data: bytes, extension: str, name: str = "file"):
    return inspect_dataset(io.BytesIO(data), extension, name)


def rejected(data: bytes, extension: str) -> str:
    with pytest.raises(DatasetRejected) as exc_info:
        inspect(data, extension)
    return exc_info.value.reason


def test_xlsx_sheets_columns_and_row_counts():
    data = xlsx_bytes({"S1": [["a", "b", None], [1, 2, 3], [None, None, None], [4, 5, 6]], "Empty": []})

    assert inspect(data, ".xlsx") == [
        {"name": "S1", "columns": ["a", "b"], "row_count": 2, "rows_truncated": False},
        {"name": "Empty", "columns": [], "row_count": 0, "rows_truncated": False},
    ]


def test_csv_columns_and_row_counts_with_sniffed_delimiter():
    data = "region;amount\nnorth;10\n\nsouth;20\n".encode()

    assert inspect(data, ".csv", "sales.csv") == [
        {"name": "sales.csv", "columns": ["region", "amount"], "row_count": 2, "rows_truncated": False}
    ]


@pytest.mark.parametrize(
    "data",
    ["café,naïve\n1,2\n".encode("utf-8-sig"), "café,naïve\n1,2\n".encode("cp1252")],
    ids=["utf8-bom", "cp1252"],
)
def test_csv_text_encodings_are_accepted(data):
    assert inspect(data, ".csv")[0]["columns"] == ["café", "naïve"]


def test_row_scan_is_bounded(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "ROW_SCAN_LIMIT", 3)
    data = ("h\n" + "x\n" * 10).encode()

    sheet = inspect(data, ".csv")[0]

    assert sheet["row_count"] == 3
    assert sheet["rows_truncated"] is True


@pytest.mark.parametrize(
    ("data", "extension"),
    [
        (PNG_BYTES, ".xlsx"),  # renamed image
        (b"a,b\n1,2\n", ".xlsx"),  # CSV renamed to .xlsx
        (PNG_BYTES, ".csv"),  # binary renamed to .csv
        (SALES_XLSX, ".csv"),  # workbook renamed to .csv
        (SALES_XLSX, ".xls"),  # xlsx renamed to .xls
        (OLE2_MAGIC + b"\x00" * 600, ".xls"),  # right magic, garbage body
        (b"", ".csv"),
    ],
    ids=["png-as-xlsx", "csv-as-xlsx", "png-as-csv", "xlsx-as-csv", "xlsx-as-xls", "corrupt-xls", "empty"],
)
def test_wrong_or_corrupt_content_is_rejected(data, extension):
    rejected(data, extension)


def test_zip_that_is_not_a_workbook_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")

    assert "not an .xlsx" in rejected(buf.getvalue(), ".xlsx")


def test_zip_bomb_by_compression_ratio_is_rejected(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "MAX_COMPRESSION_RATIO", 100)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"\x00" * 2_000_000)

    assert "compression ratio" in rejected(buf.getvalue(), ".xlsx")


def test_zip_bomb_by_total_uncompressed_size_is_rejected(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "MAX_UNCOMPRESSED_BYTES", 1000)

    assert "too large" in rejected(SALES_XLSX, ".xlsx")


def test_too_many_sheets_is_rejected(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "MAX_SHEETS", 1)

    assert "more than 1 sheets" in rejected(SALES_XLSX, ".xlsx")


def test_csv_with_nul_bytes_is_rejected():
    assert "binary" in rejected(b"a,b\n1,\x002\n", ".csv")
