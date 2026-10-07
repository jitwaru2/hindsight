"""The deterministic suite's fixtures.

The engine's own fixtures do the heavy lifting: an embedded PostgreSQL through pg0 (``pg0_db_url``,
which also runs the engine's migrations), the local embedding and reranking models, and the
``mock`` LLM provider, so nothing here calls a model. pyproject.toml's ``env`` gives the suite its
own pg0 instance and sets the four extension variables before those fixtures run, so the
migration run applies our branch too, as it does at server startup.

On top of them: an engine with our extensions loaded the way ``hindsight_api.main`` loads them
(from the environment, through the engine's loader), and the engine's FastAPI app over it, which
loads our HTTP and MCP extensions from the environment as the server does.
"""

import httpx
import pytest_asyncio
from hindsight_api import MemoryEngine
from hindsight_api.api import create_app
from hindsight_api.engine.task_backend import SyncTaskBackend
from hindsight_api.extensions import OperationValidatorExtension, TenantExtension, load_extension

# The engine's fixtures, importable because pyproject.toml puts hindsight-api-slim on the path.
pytest_plugins = ["tests.conftest"]


@pytest_asyncio.fixture
async def cortana_memory(pg0_db_url, embeddings, cross_encoder, query_analyzer):
    """The engine as the server builds it, with the mock LLM and tasks run inline.

    SyncTaskBackend executes a submitted task immediately, so an asynchronous retain runs the
    worker's own ``batch_retain`` handler before the request that queued it returns.
    """
    from tests.conftest import _teardown_memory_engine

    memory = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider="mock",
        memory_llm_api_key="",
        memory_llm_model="mock",
        embeddings=embeddings,
        cross_encoder=cross_encoder,
        query_analyzer=query_analyzer,
        pool_min_size=1,
        pool_max_size=15,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
        operation_validator=load_extension("OPERATION_VALIDATOR", OperationValidatorExtension),
        tenant_extension=load_extension("TENANT", TenantExtension),
    )
    await memory.initialize()
    yield memory
    await _teardown_memory_engine(memory)


@pytest_asyncio.fixture
async def cortana_client(cortana_memory):
    """An HTTP client over the engine's app, with our HTTP and MCP extensions mounted."""
    app = create_app(cortana_memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
