"""Resource bounds of the .xlsx inspector against hostile files (WP1 review findings)."""

import io
import re
import time
import tracemalloc
import zipfile

import pytest

from app.services import dataset_inspect
from app.services.dataset_inspect import DatasetRejected, inspect_dataset
from tests.test_datasets_api import xlsx_bytes

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MEMORY_BUDGET = 20 * 1024 * 1024  # Python-level peak while inspecting


def build_xlsx(
    sheet_xml: str,
    shared_strings: list[str] | None = None,
    extra_parts: dict[str, bytes] | None = None,
    sheet_rel_type: str = "worksheet",
) -> bytes:
    """A minimal hand-built workbook: one sheet `S` whose <sheetData> body is `sheet_xml`."""
    parts = {
        "[Content_Types].xml": b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
        "_rels/.rels": (
            f"<Relationships xmlns='{PKG_REL}'><Relationship Id='rId1' "
            f"Type='{REL}/officeDocument' Target='xl/workbook.xml'/></Relationships>"
        ).encode(),
        "xl/workbook.xml": (
            f"<workbook xmlns='{MAIN_NS}' xmlns:r='{REL}'><sheets>"
            f"<sheet name='S' sheetId='1' r:id='rId1'/></sheets></workbook>"
        ).encode(),
        "xl/_rels/workbook.xml.rels": (
            f"<Relationships xmlns='{PKG_REL}'>"
            f"<Relationship Id='rId1' Type='{REL}/{sheet_rel_type}' Target='worksheets/sheet1.xml'/>"
            f"<Relationship Id='rId2' Type='{REL}/sharedStrings' Target='sharedStrings.xml'/>"
            f"<Relationship Id='rId3' Type='{REL}/styles' Target='styles.xml'/></Relationships>"
        ).encode(),
        "xl/worksheets/sheet1.xml": f"<worksheet xmlns='{MAIN_NS}'><sheetData>{sheet_xml}</sheetData></worksheet>".encode(),
    }
    if shared_strings is not None:
        items = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
        parts["xl/sharedStrings.xml"] = f"<sst xmlns='{MAIN_NS}'>{items}</sst>".encode()
    parts |= extra_parts or {}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return out.getvalue()


def _rewrite_part(xlsx: bytes, part: str, edit) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == part:
                data = edit(data.decode()).encode()
            dst.writestr(info.filename, data)
    return out.getvalue()


def _measured(data: bytes):
    tracemalloc.start()
    started = time.monotonic()
    try:
        result = inspect_dataset(io.BytesIO(data), ".xlsx", "f")
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return result, time.monotonic() - started, peak


def _reason(data: bytes) -> str:
    with pytest.raises(DatasetRejected) as exc_info:
        inspect_dataset(io.BytesIO(data), ".xlsx", "f")
    return exc_info.value.reason


def test_header_from_shared_strings_and_inline_strings():
    sheet = (
        "<row r='1'><c r='A1' t='s'><v>1</v></c><c r='C1' t='inlineStr'><is><t>inline</t></is></c>"
        "<c r='D1'><v>42</v></c></row>"
        "<row r='2'><c r='A2'><v>1</v></c></row><row r='3'/><row r='5'><c r='B5' t='s'><v>0</v></c></row>"
    )

    (s,) = inspect_dataset(io.BytesIO(build_xlsx(sheet, ["zero", "region"])), ".xlsx", "f")

    assert s == {"name": "S", "columns": ["region", "", "inline", "42"], "row_count": 2, "rows_truncated": False}


def test_chartsheets_are_skipped():
    assert inspect_dataset(io.BytesIO(build_xlsx("", sheet_rel_type="chartsheet")), ".xlsx", "f") == []


def _sparse_sheet(row_ref: int, dimension: str | None) -> bytes:
    """Two real rows, the second at row `row_ref` (openpyxl would yield a filler row per gap)."""

    def edit(xml: str) -> str:
        xml = re.sub(r'<row r="2"', f'<row r="{row_ref}"', xml)
        xml = re.sub(r'r="([A-Z]+)2"', rf'r="\g<1>{row_ref}"', xml)
        if dimension is None:
            return re.sub(r"<dimension[^>]*/>", "", xml)
        return re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="{dimension}"/>', xml)

    return _rewrite_part(xlsx_bytes({"S": [["a", "b"], [1, 2]]}), "xl/worksheets/sheet1.xml", edit)


@pytest.mark.parametrize(("row_ref", "dimension"), [(1_048_576, "A1:XFD1048576"), (900_000_000, None)])
def test_sparse_sheet_is_fast(row_ref, dimension):
    (sheet,), elapsed, peak = _measured(_sparse_sheet(row_ref, dimension))

    assert elapsed < 2, elapsed
    assert sheet == {"name": "S", "columns": ["a", "b"], "row_count": 1, "rows_truncated": False}


def test_single_enormous_row_uses_bounded_memory():
    cells = "<c><v>1</v></c>" * 60_000  # openpyxl would materialize every cell before any width cap
    data = build_xlsx(f"<row r='1'>{cells}</row><row r='2'>{cells}</row>")

    (sheet,), elapsed, peak = _measured(data)

    assert peak < MEMORY_BUDGET, peak
    assert len(sheet["columns"]) == dataset_inspect.MAX_COLUMNS
    assert sheet["row_count"] == 1


def test_huge_styles_part_is_never_read():
    # openpyxl loads styles whole whatever its rels say; the scanner never opens it.
    styles = b"<styleSheet>" + b"<numFmt/>" * 3_000_000 + b"</styleSheet>"

    (_,), elapsed, peak = _measured(build_xlsx("<row r='1'><c t='inlineStr'><is><t>a</t></is></c></row>", extra_parts={"xl/styles.xml": styles}))

    assert peak < MEMORY_BUDGET, peak


def test_huge_shared_strings_part_is_streamed_and_scan_stops_after_needed_index():
    strings = ["h"] + ["x" * 50] * 300_000
    data = build_xlsx("<row r='1'><c t='s'><v>0</v></c></row>", shared_strings=strings)

    (sheet,), elapsed, peak = _measured(data)

    assert sheet["columns"] == ["h"]
    assert peak < MEMORY_BUDGET, peak
    assert elapsed < 1, elapsed


def test_long_header_text_is_truncated_without_buffering_it():
    long_text = "y" * 900_000  # under MAX_XML_TOKEN_BYTES; Excel itself caps cells at 32,767 chars
    data = build_xlsx(f"<row r='1'><c t='inlineStr'><is><t>{long_text}</t></is></c></row>")

    (sheet,), _, peak = _measured(data)

    assert sheet["columns"] == ["y" * dataset_inspect.MAX_COLUMN_NAME_CHARS]
    assert peak < MEMORY_BUDGET, peak


def test_dtd_in_worksheet_is_rejected():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
    data = _rewrite_part(build_xlsx("<row r='1'/>"), "xl/worksheets/sheet1.xml", lambda xml: bomb + xml)

    assert "DTD" in _reason(data)


def test_dtd_in_workbook_xml_is_rejected():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">]>'
    data = _rewrite_part(build_xlsx("<row r='1'/>"), "xl/workbook.xml", lambda xml: bomb + xml)

    _reason(data)


def test_malformed_sheet_xml_is_rejected():
    data = build_xlsx("<row r='1'><c><v>1</v></row>")

    assert "malformed" in _reason(data)


def test_time_limit_applies_to_xlsx(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "TIME_LIMIT_S", 0)

    assert "too long" in _reason(build_xlsx("<row r='1'><c><v>1</v></c></row>"))


def test_time_limit_rejects_slow_csv(monkeypatch):
    monkeypatch.setattr(dataset_inspect, "TIME_LIMIT_S", 0)
    monkeypatch.setattr(dataset_inspect, "_DEADLINE_CHECK_EVERY", 1)

    with pytest.raises(DatasetRejected, match="too long"):
        inspect_dataset(io.BytesIO(b"h\n1\n2\n"), ".csv", "f")


def test_adversarial_csv_sniff_sample_is_fast():
    started = time.monotonic()
    inspect_dataset(io.BytesIO(b',"a' * 30_000), ".csv", "f")

    assert time.monotonic() - started < 5


def test_text_run_over_token_limit_is_rejected():
    data = build_xlsx(f"<row r='1'><c t='inlineStr'><is><t>{'y' * 1_100_000}</t></is></c></row>")

    assert "oversized" in _reason(data)


def test_deep_nesting_is_rejected_with_bounded_memory():
    depth = 200_000
    data = build_xlsx("<a>" * depth + "</a>" * depth)

    tracemalloc.start()
    try:
        assert "nested too deeply" in _reason(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MEMORY_BUDGET, peak


@pytest.mark.parametrize(
    "tag",
    [
        "<" + "a" * 1_100_000 + "/>",  # huge tag name
        "<row " + " ".join(f"a{i}='1'" for i in range(200_000)) + "/>",  # ~2 MB of attributes
        "<row r='" + "1" * 1_100_000 + "'/>",  # huge attribute value
    ],
    ids=["tag-name", "many-attributes", "attribute-value"],
)
def test_oversized_single_tag_is_rejected(tag):
    assert "oversized" in _reason(build_xlsx(tag))


def test_deep_nesting_in_workbook_xml_is_rejected():
    nested = "<a>" * 1_000 + "</a>" * 1_000
    data = _rewrite_part(build_xlsx("<row r='1'/>"), "xl/workbook.xml", lambda xml: xml.replace("<sheets>", nested + "<sheets>"))

    assert "nested too deeply" in _reason(data)


def test_gt_inside_attribute_values_does_not_reset_the_token_cap():
    # '>' is legal inside attribute values; a tag made of them must still hit the cap.
    tag = "<row " + " ".join(f"a{i}='>'" for i in range(200_000)) + "/>"

    assert "oversized" in _reason(build_xlsx(tag))


@pytest.mark.parametrize(
    "markup",
    [
        "<!-- " + "<a>" * 10 + " -->",
        "<![CDATA[ <x> ]]>",
        "<?evil instruction?>",
    ],
    ids=["comment", "cdata", "processing-instruction"],
)
def test_comments_cdata_and_processing_instructions_are_rejected(markup):
    data = build_xlsx(f"<row r='1'>{markup}</row>")

    assert "must not contain" in _reason(data)


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["plain", "utf8-bom"])
def test_xml_declaration_is_allowed(bom):
    def with_decl(xml: str) -> str:
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + xml

    data = _rewrite_part(build_xlsx("<row r='1'><c t='inlineStr'><is><t>a</t></is></c></row>"), "xl/worksheets/sheet1.xml", with_decl)
    if bom:
        src = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as dst:
            for info in src.infolist():
                body = src.read(info.filename)
                dst.writestr(info.filename, bom + body if info.filename == "xl/worksheets/sheet1.xml" else body)
        data = out.getvalue()

    (sheet,) = inspect_dataset(io.BytesIO(data), ".xlsx", "f")

    assert sheet["columns"] == ["a"]


def test_utf16_parts_are_rejected():
    # In UTF-16 the byte 0x3C also occurs inside other characters (e.g. U+3C3C), which would defeat the
    # raw '<' scan; parts are parsed as UTF-8 regardless of their declaration.
    xml = f"<?xml version='1.0' encoding='UTF-16'?><worksheet xmlns='{MAIN_NS}'><sheetData><row r='1'/></sheetData></worksheet>"
    data = _rewrite_part(build_xlsx("<row r='1'/>"), "xl/worksheets/sheet1.xml", lambda _: "")
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            dst.writestr(info.filename, xml.encode("utf-16") if info.filename == "xl/worksheets/sheet1.xml" else body)

    assert "UTF-8" in _reason(out.getvalue())


def test_utf16_without_bom_is_rejected():
    xml = f"<worksheet xmlns='{MAIN_NS}'><sheetData><row r='1'/></sheetData></worksheet>".encode("utf-16-le")
    src = zipfile.ZipFile(io.BytesIO(build_xlsx("<row r='1'/>")))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            dst.writestr(info.filename, xml if info.filename == "xl/worksheets/sheet1.xml" else body)

    assert "UTF-8" in _reason(out.getvalue())


def _with_prefix(prefix: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(build_xlsx("<row r='1'><c t='inlineStr'><is><t>a</t></is></c></row>")))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            dst.writestr(info.filename, prefix + body if info.filename == "xl/worksheets/sheet1.xml" else body)
    return out.getvalue()


@pytest.mark.parametrize(
    "prefix",
    [
        b"<?foo " + (b"x" * 400_000 + b"<") * 5 + b"?>",  # PI at offset 0 padded with '<' to dodge the token cap
        b"   <?foo bar?>",  # PI at offset 3 without a BOM
        b"\xef\xbb\xbf<?foo bar?>",  # PI after a BOM
        b"<?xml " + b"x" * 2_000 + b"?>",  # over-long declaration
        b"<?xmlfoo?>",  # not a declaration
    ],
    ids=["pi-at-start", "pi-at-offset-3", "pi-after-bom", "long-declaration", "xml-prefixed-pi"],
)
def test_only_a_short_xml_declaration_may_start_a_part(prefix):
    assert "processing instructions" in _reason(_with_prefix(prefix))
