"""Validates an uploaded dataset file and extracts sheet names, columns and row counts.

Runs in the Celery worker, never in the API process: files are untrusted and can be large. Every
parser is bounded in memory, rows visited and wall-clock time, because the threads pool enforces no
Celery time limits and a stuck task would hold a worker thread.

.xlsx is read with a small streaming expat scanner rather than openpyxl: openpyxl loads styles, themes
and shared strings whole and builds each row in full before any column cap applies, so its memory use
is driven by the file. The scanner keeps O(MAX_COLUMNS) state whatever the parts contain.
"""

import codecs
import csv
import io
import posixpath
import re
import time
import zipfile
from typing import BinaryIO
from xml.parsers import expat

import xlrd

CONTENT_TYPES = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}

_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_SHEETS = 50
MAX_COLUMNS = 500
MAX_COLUMN_NAME_CHARS = 200
# Data rows visited per sheet (empty ones included). Beyond this, row_count is a lower bound and
# rows_truncated is true.
ROW_SCAN_LIMIT = 200_000
TIME_LIMIT_S = 120.0
_DEADLINE_CHECK_EVERY = 1_000

# .xlsx zip guards. Python's zipfile never inflates a member past its declared size, so declared sizes
# bound the work; memory is bounded separately by streaming every part.
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_COMPRESSION_RATIO = 1_000
MAX_METADATA_PART_BYTES = 5 * 1024 * 1024  # workbook.xml and .rels
MAX_METADATA_ELEMENTS = 10_000  # <sheet>/<Relationship> entries collected from them
# expat holds one stack entry per open element and each tag whole: real parts nest ~10 deep, and Excel
# caps cell text at 32,767 chars, so legitimate files never come near these.
MAX_XML_DEPTH = 64
MAX_XML_TOKEN_BYTES = 1024 * 1024
_XML_CHUNK = 64 * 1024
# csv.Sniffer is quadratic in the sample size on adversarial input.
_CSV_SNIFF_CHARS = 16 * 1024

_CELL_REF = re.compile(r"([A-Za-z]{1,3})\d*$")


class DatasetRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Deadline:
    def __init__(self, seconds: float):
        self._at = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._at:
            raise DatasetRejected("File took too long to process")


def _column_names(values) -> list[str]:
    names = ["" if v is None else str(v)[:MAX_COLUMN_NAME_CHARS] for v in list(values)[:MAX_COLUMNS]]
    while names and names[-1] == "":
        names.pop()
    return names


def _sheet(name: str, columns: list[str], row_count: int, truncated: bool) -> dict:
    return {"name": name, "columns": columns, "row_count": row_count, "rows_truncated": truncated}


# --- .xlsx -------------------------------------------------------------------------------------------


class _StopScan(Exception):
    """Raised from an expat handler to end a scan early (not an error)."""


def _local(name: str) -> str:
    return name.rpartition("}")[2]


class _Collect:
    """Collects the attributes of every `element` (local name) in a small metadata part."""

    def __init__(self, element: str):
        self.items: list[dict[str, str]] = []
        self._element = element

    def start(self, name: str, attrs: dict) -> None:
        if name == self._element:
            if len(self.items) >= MAX_METADATA_ELEMENTS:
                raise DatasetRejected("Workbook metadata has too many entries")
            self.items.append({_local(k): v for k, v in attrs.items()})

    def end(self, name: str) -> None:
        pass

    def chars(self, data: str) -> None:
        pass


def _collect(zf: zipfile.ZipFile, part: str, element: str, deadline: _Deadline) -> list[dict[str, str]]:
    if zf.getinfo(part).file_size > MAX_METADATA_PART_BYTES:
        raise DatasetRejected("Workbook metadata part is too large")
    collector = _Collect(element)
    _stream_xml(zf, part, collector, deadline)
    return collector.items


def _relationships(
    zf: zipfile.ZipFile, names: set[str], part: str, deadline: _Deadline
) -> dict[str, tuple[str, str]]:
    """{rel id: (type, resolved target)} for `part`'s .rels file."""
    folder, base = posixpath.split(part)
    rels_name = posixpath.join(folder, "_rels", base + ".rels")
    if rels_name not in names:
        return {}
    rels = {}
    for rel in _collect(zf, rels_name, "Relationship", deadline):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        target = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        rels[rel.get("Id", "")] = (rel.get("Type", ""), target)
    return rels


def _workbook_layout(
    zf: zipfile.ZipFile, names: set[str], deadline: _Deadline
) -> tuple[list[tuple[str, str]], str | None]:
    """[(sheet name, worksheet part)] in workbook order, and the shared-strings part if any."""
    package = _relationships(zf, names, "", deadline)
    workbook = next((t for kind, t in package.values() if kind.endswith("/officeDocument") and t in names), None)
    if workbook is None:
        raise DatasetRejected("File is a zip archive but not an .xlsx workbook")
    rels = _relationships(zf, names, workbook, deadline)

    sheets = []
    for sheet in _collect(zf, workbook, "sheet", deadline):
        kind, target = rels.get(sheet.get("id", ""), ("", ""))  # r:id, namespace stripped
        if kind.endswith("/worksheet") and target in names:  # chartsheets/dialogsheets hold no data
            sheets.append((sheet.get("name", ""), target))
    if len(sheets) > MAX_SHEETS:
        raise DatasetRejected(f"Workbook has more than {MAX_SHEETS} sheets")
    shared = next((t for kind, t in rels.values() if kind.endswith("/sharedStrings") and t in names), None)
    return sheets, shared


def _check_markup(window: bytes, window_offset: int) -> None:
    """Rejects '<!' (comments, CDATA, DOCTYPE) and '<?' (processing instructions) anywhere but the XML
    declaration at the start. expat buffers those tokens whole, they may contain '<' and '>', and
    spreadsheet writers don't emit them."""
    i = window.find(b"<!")
    if i != -1:
        raise DatasetRejected("Workbook XML must not contain comments, CDATA or a DTD")
    i = window.find(b"<?")
    while i != -1:
        if not _is_xml_declaration(window, window_offset, i):
            raise DatasetRejected("Workbook XML must not contain processing instructions")
        i = window.find(b"<?", i + 1)


_XML_DECLARATION = re.compile(rb"<\?xml[ \t\r\n][^<>]{0,1000}\?>")


def _is_xml_declaration(window: bytes, window_offset: int, i: int) -> bool:
    """Only a short `<?xml ...?>` at the very start (or right after a UTF-8 BOM) is allowed: any other
    `<?` token would be buffered whole by expat, and the raw '<' scan can't bound it."""
    position = window_offset + i
    at_start = position == 0 or (position == 3 and window_offset == 0 and window.startswith(b"\xef\xbb\xbf"))
    return at_start and _XML_DECLARATION.match(window, i) is not None


def _stream_xml(zf: zipfile.ZipFile, part: str, handler, deadline: _Deadline) -> None:
    """Feeds `part` to `handler` through expat with bounded memory.

    expat keeps a stack entry per open element and buffers each tag whole before calling a handler,
    so nesting depth and the bytes between two '<' are capped here, before expat sees them.
    """
    # Forced UTF-8 (overrides an 8-bit encoding declaration; Excel always writes UTF-8): in UTF-8 the byte
    # 0x3C only ever means "<", which the raw-byte checks below rely on. UTF-16/32 is refused below.
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    depth = 0

    def reject_dtd(*_):
        raise DatasetRejected("Workbook XML must not contain a DTD")

    def start(name: str, attrs: dict) -> None:
        nonlocal depth
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise DatasetRejected("Workbook XML is nested too deeply")
        handler.start(_local(name), attrs)

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        handler.end(_local(name))

    parser.StartDoctypeDeclHandler = reject_dtd
    parser.EntityDeclHandler = reject_dtd
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = handler.chars
    since_lt = 0  # bytes since the last '<'
    offset = 0  # document offset of `chunk`
    prev_byte = b""
    try:
        with zf.open(part) as src:
            while chunk := src.read(_XML_CHUNK):
                # expat auto-detects UTF-16 (from a BOM or a '<\0' prefix) despite the forced encoding.
                # NUL is never legal in XML but always present in UTF-16/32 ('<' is 3C 00), so refuse it.
                if b"\x00" in chunk or (offset == 0 and chunk.startswith((b"\xff\xfe", b"\xfe\xff"))):
                    raise DatasetRejected("Workbook XML must be UTF-8")
                _check_markup(prev_byte + chunk, offset - len(prev_byte))
                # '<' can't appear inside a tag, an attribute value or a text run (unlike '>'), and
                # comments/CDATA (which may contain it) are rejected above, so this bounds every token.
                first = chunk.find(b"<")
                if first == -1:
                    since_lt += len(chunk)
                elif since_lt + first > MAX_XML_TOKEN_BYTES:
                    since_lt += first
                else:
                    since_lt = len(chunk) - chunk.rfind(b"<") - 1
                if since_lt > MAX_XML_TOKEN_BYTES:
                    raise DatasetRejected("Workbook XML contains an oversized tag or text run")
                parser.Parse(chunk, False)
                offset += len(chunk)
                prev_byte = chunk[-1:]
                deadline.check()  # per chunk, so even a single enormous row is time-bounded
            parser.Parse(b"", True)
    except _StopScan:
        pass
    except expat.ExpatError as exc:
        raise DatasetRejected("Workbook XML is malformed") from exc


def _column_index(ref: str | None) -> int | None:
    match = _CELL_REF.match(ref or "")
    if not match:
        return None
    index = 0
    for ch in match.group(1).upper():
        index = index * 26 + ord(ch) - 64
    return index


class _SheetScan:
    """Counts rows with a value and captures row 1 (the header), keeping O(MAX_COLUMNS) state."""

    def __init__(self):
        self.count = 0
        self.truncated = False
        self.header: dict[int, str] = {}  # column -> text
        self.header_shared: dict[int, int] = {}  # column -> shared-string index
        self._visited = 0
        self._row = 0
        self._row_has_value = False
        self._col = 0
        self._cell_type: str | None = None
        self._in_cell = False
        self._capture = False
        self._cell_has_value = False
        self._text: list[str] = []
        self._text_len = 0

    def _keeping_text(self) -> bool:
        return self._row == 1 and self._col <= MAX_COLUMNS

    def start(self, name: str, attrs: dict) -> None:
        if name == "row":
            self._visited += 1
            if self._visited - 1 > ROW_SCAN_LIMIT:
                self.truncated = True
                raise _StopScan
            r = attrs.get("r", "")
            self._row = int(r) if r.isdigit() else self._row + 1
            self._col = 0
            self._row_has_value = False
        elif name == "c":
            self._col = _column_index(attrs.get("r")) or self._col + 1
            self._cell_type = attrs.get("t")
            self._in_cell = True
            self._cell_has_value = False
            self._text, self._text_len = [], 0
        elif name in ("v", "t") and self._in_cell:
            self._capture = True

    def chars(self, data: str) -> None:
        if not self._capture or not data:
            return
        self._cell_has_value = True
        if self._keeping_text() and self._text_len < MAX_COLUMN_NAME_CHARS:
            piece = data[: MAX_COLUMN_NAME_CHARS - self._text_len]
            self._text.append(piece)
            self._text_len += len(piece)

    def end(self, name: str) -> None:
        if name in ("v", "t"):
            self._capture = False
        elif name == "c":
            self._in_cell = False
            if self._cell_has_value:
                self._row_has_value = True
                if self._keeping_text():
                    text = "".join(self._text)
                    if self._cell_type == "s":
                        if text.strip().isdigit():
                            self.header_shared[self._col] = int(text)
                    else:
                        self.header[self._col] = text
        elif name == "row" and self._row != 1 and self._row_has_value:
            self.count += 1


class _SharedStringsScan:
    """Collects only the shared strings at `wanted` indices, stopping after the last one."""

    def __init__(self, wanted: set[int]):
        self.found: dict[int, str] = {}
        self._wanted = wanted
        self._last = max(wanted)
        self._index = -1
        self._phonetic = 0
        self._capture = False
        self._text: list[str] = []
        self._text_len = 0

    def start(self, name: str, attrs: dict) -> None:
        if name == "si":
            self._index += 1
            if self._index > self._last:
                raise _StopScan
            self._text, self._text_len = [], 0
        elif name == "rPh":  # phonetic hints, not part of the string
            self._phonetic += 1
        elif name == "t" and not self._phonetic and self._index in self._wanted:
            self._capture = True

    def chars(self, data: str) -> None:
        if self._capture and self._text_len < MAX_COLUMN_NAME_CHARS:
            piece = data[: MAX_COLUMN_NAME_CHARS - self._text_len]
            self._text.append(piece)
            self._text_len += len(piece)

    def end(self, name: str) -> None:
        if name == "t":
            self._capture = False
        elif name == "rPh":
            self._phonetic -= 1
        elif name == "si" and self._index in self._wanted:
            self.found[self._index] = "".join(self._text)


def _check_zip(zf: zipfile.ZipFile) -> set[str]:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise DatasetRejected("Workbook has too many internal parts")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise DatasetRejected("Workbook is too large when uncompressed")
    for info in infos:
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise DatasetRejected("Workbook has a suspicious compression ratio")
    return {i.filename for i in infos}


def _inspect_xlsx(fileobj: BinaryIO, deadline: _Deadline) -> list[dict]:
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as exc:
        raise DatasetRejected("File is not a valid .xlsx workbook") from exc
    with zf:
        names = _check_zip(zf)
        sheets, shared_part = _workbook_layout(zf, names, deadline)

        scans = []
        for name, part in sheets:
            scan = _SheetScan()
            _stream_xml(zf, part, scan, deadline)
            scans.append((name, scan))

        wanted = {i for _, scan in scans for i in scan.header_shared.values()}
        shared: dict[int, str] = {}
        if wanted and shared_part:
            shared_scan = _SharedStringsScan(wanted)
            _stream_xml(zf, shared_part, shared_scan, deadline)
            shared = shared_scan.found

    result = []
    for name, scan in scans:
        cells = scan.header | {col: shared.get(i, "") for col, i in scan.header_shared.items()}
        width = max(cells, default=0)
        columns = _column_names(cells.get(col, "") for col in range(1, width + 1))
        result.append(_sheet(name, columns, scan.count, scan.truncated))
    return result


# --- .xls / .csv -------------------------------------------------------------------------------------


def _count_rows(rows, is_empty, deadline: _Deadline) -> tuple[list[str], int, bool]:
    """Header columns, non-empty data rows, and whether the scan stopped at ROW_SCAN_LIMIT."""
    columns: list[str] = []
    count = 0
    for visited, row in enumerate(rows, start=1):
        if visited % _DEADLINE_CHECK_EVERY == 0:
            deadline.check()
        if visited == 1:
            columns = _column_names(row)
            continue
        if visited - 1 > ROW_SCAN_LIMIT:  # data rows visited, header excluded
            return columns, count, True
        if not is_empty(row):
            count += 1
    return columns, count, False


def _inspect_xls(fileobj: BinaryIO, deadline: _Deadline) -> list[dict]:
    # xlrd reads the whole file and each sheet into memory; bounded only by MAX_UPLOAD_BYTES.
    book = xlrd.open_workbook(file_contents=fileobj.read(), on_demand=True)
    try:
        if book.nsheets > MAX_SHEETS:
            raise DatasetRejected(f"Workbook has more than {MAX_SHEETS} sheets")
        sheets = []
        for i in range(book.nsheets):
            deadline.check()
            sh = book.sheet_by_index(i)
            columns = _column_names(sh.row_values(0, 0, MAX_COLUMNS)) if sh.nrows else []
            rows = max(sh.nrows - 1, 0)
            sheets.append(_sheet(sh.name, columns, min(rows, ROW_SCAN_LIMIT), rows > ROW_SCAN_LIMIT))
            book.unload_sheet(i)
        return sheets
    finally:
        book.release_resources()


def _csv_encoding(fileobj: BinaryIO) -> str:
    """utf-8 (with or without BOM), else cp1252. Rejects anything containing NUL bytes (binary)."""
    utf8_ok = True
    utf8 = codecs.getincrementaldecoder("utf-8-sig")()
    while chunk := fileobj.read(1024 * 1024):
        if b"\x00" in chunk:
            raise DatasetRejected("File looks binary, not a CSV text file")
        if utf8_ok:
            try:
                utf8.decode(chunk)
            except UnicodeDecodeError:
                utf8_ok = False
    if utf8_ok:
        try:
            utf8.decode(b"", final=True)  # a multi-byte sequence cut off at EOF
        except UnicodeDecodeError:
            utf8_ok = False
    fileobj.seek(0)
    if utf8_ok:
        return "utf-8-sig"
    try:
        fileobj.read().decode("cp1252")
    except UnicodeDecodeError as exc:
        raise DatasetRejected("CSV is not valid UTF-8 or Windows-1252 text") from exc
    fileobj.seek(0)
    return "cp1252"


def _inspect_csv(fileobj: BinaryIO, name: str, deadline: _Deadline) -> list[dict]:
    encoding = _csv_encoding(fileobj)
    text = io.TextIOWrapper(fileobj, encoding=encoding, newline="")
    try:
        sample = text.read(_CSV_SNIFF_CHARS)
        text.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(text, dialect)
        columns, count, truncated = _count_rows(reader, lambda r: not any(c.strip() for c in r), deadline)
        return [_sheet(name, columns, count, truncated)]
    except csv.Error as exc:
        raise DatasetRejected(f"Malformed CSV: {exc}") from exc
    finally:
        text.detach()


def inspect_dataset(fileobj: BinaryIO, extension: str, name: str) -> list[dict]:
    """Returns per-sheet metadata or raises DatasetRejected. `fileobj` must be seekable."""
    deadline = _Deadline(TIME_LIMIT_S)
    head = fileobj.read(8)
    fileobj.seek(0)
    if not head:
        raise DatasetRejected("File is empty")
    if extension == ".xlsx":
        if not head.startswith(_ZIP_MAGIC):
            raise DatasetRejected("File content is not an .xlsx workbook")
        parse = _inspect_xlsx
    elif extension == ".xls":
        if head != _OLE2_MAGIC:
            raise DatasetRejected("File content is not an .xls workbook")
        parse = _inspect_xls
    elif extension == ".csv":
        if head.startswith((_ZIP_MAGIC, _OLE2_MAGIC)):
            raise DatasetRejected("File is a spreadsheet workbook, not a CSV text file")
        return _inspect_csv(fileobj, name, deadline)
    else:
        raise DatasetRejected(f"Unsupported file type: {extension}")

    try:
        return parse(fileobj, deadline)
    except DatasetRejected:
        raise
    except Exception as exc:  # malformed input can make parsers raise almost anything
        raise DatasetRejected(f"File could not be read as {extension}") from exc
