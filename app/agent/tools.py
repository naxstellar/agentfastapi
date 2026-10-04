"""The agent's tools. Who and which conversation come from the run's AgentContext, never from the LLM."""

import asyncio

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage

from app.agent.context import AgentContext
from app.agent.workspace import RunPythonResult, SandboxDataset

MAX_LISTED_COLUMNS = 40
MAX_INSPECT_ROWS = 50

# Runs inside the sandbox. A function keeps its locals out of the agent's namespace, and the
# try/finally removes the function itself even when reading fails.
_INSPECT_CODE = """\
def _inspect_dataset(path, sheet, rows):
    import csv
    import pandas as pd

    if path.lower().endswith(".csv"):
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):  # latin-1 decodes any byte
            try:
                with open(path, encoding=encoding, newline="") as f:
                    sample = f.read(16384)
                break
            except UnicodeDecodeError:
                continue
        try:
            sep = csv.Sniffer().sniff(sample, delimiters=",;\\t|").delimiter
        except csv.Error:
            sep = ","
        df = pd.read_csv(path, sep=sep, encoding=encoding)
        print(f"{{path}} (CSV, separator {{sep!r}}, encoding {{encoding}})")
    else:
        with pd.ExcelFile(path) as book:
            names = book.sheet_names
            print(f"{{path}}: sheets {{names}}")
            if sheet is None:
                sheet = names[0]
            elif sheet not in names:
                raise ValueError(f"No sheet named {{sheet!r}}; the sheets are {{names}}")
            df = book.parse(sheet)
        print(f"sheet {{sheet!r}}")
    print(f"{{df.shape[0]}} rows x {{df.shape[1]}} columns")
    info = pd.DataFrame({{"dtype": df.dtypes.astype(str), "non_null": df.notna().sum(), "missing": df.isna().sum()}})
    with pd.option_context("display.width", 200, "display.max_columns", 50, "display.max_colwidth", 40):
        print("\\ncolumns:")
        print(info.to_string())
        print(f"\\nfirst {{rows}} rows:")
        print(df.head(rows).to_string())


try:
    _inspect_dataset({path!r}, {sheet!r}, {rows!r})
finally:
    del _inspect_dataset
"""


def inspect_code(path: str, sheet: str | None, rows: int) -> str:
    # repr() turns every value into a literal; path comes from the dataset mapping, not from the LLM.
    return _INSPECT_CODE.format(path=path, sheet=sheet, rows=rows)


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError


def describe_datasets(datasets: list[SandboxDataset]) -> str:
    if not datasets:
        return (
            "No datasets are attached to this conversation. Ask the user to upload a spreadsheet "
            "and attach it to this conversation."
        )
    lines = []
    for d in datasets:
        # Names and columns come from the user's file: repr() keeps them on one line, clearly quoted.
        lines.append(f"{d.path} (uploaded as {d.filename!r}, {_size(d.size_bytes)})")
        for sheet in d.sheets or []:
            columns = sheet.get("columns") or []
            shown = ", ".join(repr(c) for c in columns[:MAX_LISTED_COLUMNS])
            if len(columns) > MAX_LISTED_COLUMNS:
                shown += f", … ({len(columns) - MAX_LISTED_COLUMNS} more)"
            rows = sheet.get("row_count", 0)
            rows_text = f"at least {rows}" if sheet.get("rows_truncated") else str(rows)
            lines.append(f"  sheet {sheet.get('name')!r}: {rows_text} data rows; columns: {shown or '(none)'}")
    return "\n".join(lines)


def find_dataset(datasets: list[SandboxDataset], name: str) -> SandboxDataset | str:
    """The dataset `name` refers to (its sandbox path, file name or uploaded name), or an error message."""
    name = name.strip()
    for match in (
        lambda d: d.path == name,
        lambda d: d.path.rpartition("/")[2] == name,
        lambda d: d.filename == name,
    ):
        found = [d for d in datasets if match(d)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return f"Error: {name!r} is ambiguous; use one of the paths: {', '.join(d.path for d in found)}"
    available = ", ".join(d.path for d in datasets) or "none"
    return f"Error: no attached dataset named {name!r}. Attached datasets: {available}"


def _tool_message(result: RunPythonResult, name: str, tool_call_id: str) -> ToolMessage:
    # A returned ToolMessage is used as is: the tool node doesn't fill in its name.
    return ToolMessage(
        content=result.output,
        name=name,
        tool_call_id=tool_call_id,
        status="error" if result.is_error else "success",
        # Not sent to the LLM; kept for the display history.
        artifact={"artifacts": [{"id": str(a.id), "kind": a.kind, "filename": a.filename} for a in result.artifacts]},
    )


@tool(parse_docstring=True)
async def run_python(code: str, runtime: ToolRuntime[AgentContext]) -> ToolMessage:
    """Run Python code in this conversation's sandbox and return its stdout, stderr and saved files.

    Variables, imports and files persist between calls. Datasets are under data/; save results meant for
    the user under outputs/.

    Args:
        code: Python source to execute.
    """
    result = await runtime.context.workspace.run_python(code)
    return _tool_message(result, "run_python", runtime.tool_call_id)


@tool
async def list_datasets(runtime: ToolRuntime[AgentContext]) -> str:
    """List the datasets attached to this conversation: their paths in the sandbox, sheets, row counts and columns."""
    datasets = await asyncio.to_thread(runtime.context.workspace.load_datasets)
    return describe_datasets(datasets)


@tool(parse_docstring=True)
async def inspect_dataset(
    name: str, runtime: ToolRuntime[AgentContext], sheet: str | None = None, rows: int = 5
) -> ToolMessage:
    """Show a dataset's shape, column types, missing values and first rows.

    Args:
        name: The dataset's path as listed by list_datasets (e.g. data/sales.xlsx) or its file name.
        sheet: Excel sheet to inspect; defaults to the first sheet. Ignored for CSV files.
        rows: How many leading rows to show (1-50).
    """
    datasets = await asyncio.to_thread(runtime.context.workspace.load_datasets)
    dataset = find_dataset(datasets, name)
    if isinstance(dataset, str):
        return ToolMessage(dataset, name="inspect_dataset", tool_call_id=runtime.tool_call_id, status="error")
    rows = min(max(rows, 1), MAX_INSPECT_ROWS)
    result = await runtime.context.workspace.run_python(inspect_code(dataset.path, sheet, rows))
    return _tool_message(result, "inspect_dataset", runtime.tool_call_id)


TOOLS = [list_datasets, inspect_dataset, run_python]
