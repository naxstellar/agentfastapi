"""WP2 acceptance against the real Agent Engine Code Execution service.

Opt-in: `pytest -m external` (needs AGENT_ENGINE_NAME in env/.env and ADC, see CLAUDE.md). GCS and
Redis are faked here: the GCS round trip is covered by test_gcs_external.py.
"""

import asyncio
import inspect
import io
import uuid
from contextlib import contextmanager

import openpyxl
import pytest

import app.config
from app.agent.sandbox import (
    AgentEngineProvider,
    ConcurrentExecutionError,
    ExecTimeout,
    SandboxGone,
    SandboxManager,
    SandboxSlots,
)
from app.agent.tools import inspect_code
from app.agent.workspace import RESET_NOTE, ConversationWorkspace
from app.models_agent import Conversation, ConversationDataset, Dataset, Run
from tests.fakes import FakeStorage

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not app.config.AGENT_ENGINE_NAME, reason="AGENT_ENGINE_NAME is not set in env/.env"),
]


def _sales_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["month", "revenue"])
    for month, revenue in [("Jan", 100), ("Feb", 150), ("Mar", 125)]:
        ws.append([month, revenue])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TrackingProvider(AgentEngineProvider):
    """Remembers every sandbox it creates so the test can delete them all."""

    def __init__(self):
        super().__init__(app.config.AGENT_ENGINE_NAME, app.config.SANDBOX_MACHINE_CONFIG)
        self.names: list[str] = []

    async def create(self, ttl_s, display_name):
        handle = await super().create(ttl_s, display_name)
        self.names.append(handle.name)
        return handle


@pytest.fixture
async def provider():
    provider = TrackingProvider()
    yield provider
    for name in provider.names:
        await provider.delete(name)


@pytest.fixture
async def workspace_factory(db, user, async_redis_db, provider):
    storage = FakeStorage()
    conversation = Conversation(user_id=user.id, model="anthropic:claude-sonnet-5-5")
    db.add(conversation)
    db.flush()
    run = Run(conversation_id=conversation.id, user_id=user.id, status="running")
    dataset = Dataset(
        user_id=user.id,
        filename="sales.xlsx",
        gcs_key=f"users/{user.id}/datasets/{uuid.uuid4()}/sales.xlsx",
        size_bytes=0,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        status="ready",
    )
    db.add_all([run, dataset])
    db.flush()
    db.add(ConversationDataset(conversation_id=conversation.id, dataset_id=dataset.id))
    db.commit()
    storage.objects[dataset.gcs_key] = _sales_xlsx()

    @contextmanager
    def session_factory():
        yield db

    def make(**settings) -> ConversationWorkspace:
        manager = SandboxManager(provider, SandboxSlots(async_redis_db, 5), **settings)
        return ConversationWorkspace(
            user_id=user.id,
            conversation_id=conversation.id,
            run_id=run.id,
            manager=manager,
            storage=storage,
            session_factory=session_factory,
        )

    make.storage = storage
    return make


async def test_pandas_on_uploaded_xlsx_returns_chart_artifact(workspace_factory):
    workspace = workspace_factory()
    result = await workspace.run_python(
        "import pandas as pd, matplotlib.pyplot as plt\n"
        "df = pd.read_excel('data/sales.xlsx')\n"
        "print('total', df['revenue'].sum())\n"
        "df.plot(x='month', y='revenue')\n"
        "plt.savefig('outputs/revenue.png')\n"
        "plt.show()\n"
        "open('scratch.txt', 'w').write('not an artifact')\n"
    )
    assert not result.is_error, result.output
    assert "total 375" in result.output
    assert sorted((a.kind, a.filename) for a in result.artifacts) == [("image", "figure-1.png"), ("image", "revenue.png")]
    pngs = [v for k, v in workspace_factory.storage.objects.items() if "/artifacts/" in k]
    assert len(pngs) == 2 and all(v.startswith(b"\x89PNG") for v in pngs)

    # Files persist: the next call sees the dataset without it being sent again.
    again = await workspace.run_python("print(len(pd.read_excel('data/sales.xlsx')))")
    assert "3" in again.output and not again.artifacts


async def test_inspect_dataset_code_runs_in_the_sandbox(workspace_factory):
    """WP3's inspect_dataset snippet (pandas isn't installed locally, so this is its only real check)."""
    workspace = workspace_factory()
    xlsx = await workspace.run_python(inspect_code("data/sales.xlsx", None, 2))
    assert not xlsx.is_error, xlsx.output
    assert "sheets ['Sales']" in xlsx.output and "3 rows x 2 columns" in xlsx.output
    assert "revenue" in xlsx.output and "int64" in xlsx.output and "Feb" in xlsx.output and "Mar" not in xlsx.output

    # A semicolon-separated cp1252 CSV, as European Excel exports it.
    await workspace.run_python(r"open('eu.csv', 'wb').write('name;amount\nCaf\xe9;1,5\nB\xe4r;2\n'.encode('cp1252'))")
    csv = await workspace.run_python(inspect_code("eu.csv", None, 5))
    assert not csv.is_error, csv.output
    assert "separator ';'" in csv.output and "encoding cp1252" in csv.output and "Café" in csv.output

    missing = await workspace.run_python(inspect_code("data/sales.xlsx", "Nope", 5))
    assert missing.is_error and "No sheet named 'Nope'" in missing.output
    # The helper never stays in the agent's namespace, even after a failure.
    leaked = await workspace.run_python("print('_inspect_dataset' in globals())")
    assert "False" in leaked.output


async def test_sandbox_has_no_secrets_no_internet_and_no_metadata_server(workspace_factory):
    workspace = workspace_factory()
    result = await workspace.run_python(
        "import os, urllib.request\n"
        "print('ENV', sorted(os.environ.items()))\n"
        "for url in ['https://example.com', 'http://metadata.google.internal/computeMetadata/v1/', "
        "'http://169.254.169.254/']:\n"
        "    try:\n"
        "        urllib.request.urlopen(urllib.request.Request(url, headers={'Metadata-Flavor': 'Google'}), timeout=5)\n"
        "        print('REACHED', url)\n"
        "    except Exception as exc:\n"
        "        print('BLOCKED', url, type(exc).__name__)\n"
    )
    assert "REACHED" not in result.output and result.output.count("BLOCKED") == 3, result.output
    secrets = [
        app.config.ANTHROPIC_API_KEY,
        app.config.OPENAI_API_KEY,
        app.config.AUTH0_CLIENT_SECRET,
        app.config.SESSION_SECRET_KEY,
        app.config.DATABASE_URL,
    ]
    for secret in filter(None, secrets):
        assert secret not in result.output
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in result.output


async def test_runaway_code_hits_the_timeout_and_state_is_rolled_back(workspace_factory):
    workspace = workspace_factory(exec_timeout_s=10)
    await workspace.run_python("x = 'before'")

    result = await workspace.run_python("x = 'changed'\nopen('outputs/late.txt', 'w').write('x')\nwhile True:\n    pass\n")
    assert result.is_error and "exceeded the 10 s time limit and was rolled back" in result.output

    after = await workspace.run_python("import os\nprint(x, os.path.exists('outputs/late.txt'))")
    assert not after.is_error and "before False" in after.output, after.output


async def test_turn_after_ttl_expiry_rehydrates_and_still_answers(workspace_factory):
    workspace = workspace_factory(ttl_s=60, exec_timeout_s=30, min_remaining_s=5)
    await workspace.run_python("x = 41")
    await asyncio.sleep(75)  # the TTL counts from creation; executions don't extend it

    result = await workspace.run_python("import pandas as pd\nprint('rows', len(pd.read_excel('data/sales.xlsx')))")
    assert result.output.startswith(RESET_NOTE), result.output
    assert "rows 3" in result.output
    gone = await workspace.run_python("print(x)")
    assert "NameError" in gone.output  # state really was reset


async def test_execute_code_adapter_contract(provider):
    """The private async method the adapter depends on still exists, takes the same arguments,
    aborts and rolls back on cancellation, and errors map as the spike measured."""
    signature = inspect.signature(provider._client.aio.sandboxes._execute_code)
    assert {"name", "inputs", "config"} <= set(signature.parameters)

    handle = await provider.create(600, "contract-test")
    result = await provider.execute(handle.name, "z = 'before'\nprint('ok')", [], 30)
    assert (result.exit_status, result.stdout) == (0, "ok\n")

    task = asyncio.create_task(
        provider.execute(handle.name, "z = 'changed'\nimport time\ntime.sleep(30)\nopen('late.txt','w').write('x')", [], 60)
    )
    await asyncio.sleep(5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2)
    after = await provider.execute(handle.name, "import os\nprint(z, os.path.exists('late.txt'))", [], 30)
    assert after.stdout == "before False\n"

    with pytest.raises(ExecTimeout):
        await provider.execute(handle.name, "import time\ntime.sleep(30)", [], 3)

    # Concurrency is optimistic: both run, the first to finish commits, the later finisher is rejected.
    slow = asyncio.create_task(provider.execute(handle.name, "z = 'slow'\nimport time\ntime.sleep(10)", [], 30))
    await asyncio.sleep(3)
    quick = await provider.execute(handle.name, "print('quick')", [], 30)
    assert quick.stdout == "quick\n"
    with pytest.raises(ConcurrentExecutionError):
        await slow
    assert (await provider.execute(handle.name, "print(z)", [], 30)).stdout == "before\n"

    await provider.delete(handle.name)
    with pytest.raises(SandboxGone):
        await provider.execute(handle.name, "print(1)", [], 30)
    assert await provider.expires_at(handle.name) is None
