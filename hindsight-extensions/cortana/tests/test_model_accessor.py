"""How an extension calls the engine's configured model provider (specification 17 item 3).

The engine offers no public accessor. An extension reaches the memory engine through its context
(``self.context.get_memory_engine()``); the providers the engine itself uses are private attributes
of that object, ``_retain_llm_config`` for retain, bound to a bank with
``with_config(await _config_resolver.resolve_full_config(bank, context), bank_id=..., operation=...)``
exactly as the engine's retain path binds it. These tests pin that path, so an upstream pull that
renames it fails here rather than in structuring (HSIGHT-4), and show that a call made through it
with the ``claude-code`` provider carries the provider's isolated Claude configuration.
"""

from pathlib import Path

import claude_agent_sdk
import pytest
from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.engine.task_backend import SyncTaskBackend
from hindsight_api.extensions import OperationValidatorExtension, load_extension


async def test_an_extension_calls_the_engines_retain_provider(cortana_memory):
    engine = cortana_memory._operation_validator.context.get_memory_engine()
    provider = engine._retain_llm_config
    bank = "cortana-model-accessor"
    config = await engine._config_resolver.resolve_full_config(bank, RequestContext())
    llm = provider.with_config(config, bank_id=bank, operation="cortana-structuring")

    provider.clear_mock_calls()
    await llm.call(messages=[{"role": "user", "content": "accessor probe"}], scope="memory")
    # The call's trace row is written in the background; let it land before the engine closes,
    # as the engine's own tests do (tests/test_llm_trace.py).
    await engine._llm_recorder._flush_pending(llm.trace_context().trace_id)

    (call,) = provider.get_mock_calls()
    assert call["messages"] == [{"role": "user", "content": "accessor probe"}]


class _Captured(Exception):
    """Raised by the stand-in SDK once it has seen the options, so no model is called."""


async def test_claude_code_calls_through_that_provider_are_isolated(
    pg0_db_url, embeddings, cross_encoder, query_analyzer, monkeypatch
):
    seen = {}

    async def query(*, prompt, options):
        seen["options"] = options
        raise _Captured
        yield  # an async generator, as the SDK's query is

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    engine = MemoryEngine(
        db_url=pg0_db_url,
        memory_llm_provider="claude-code",
        memory_llm_api_key="",
        memory_llm_model="claude-sonnet-5-5",
        embeddings=embeddings,
        cross_encoder=cross_encoder,
        query_analyzer=query_analyzer,
        run_migrations=False,
        task_backend=SyncTaskBackend(),
        operation_validator=load_extension("OPERATION_VALIDATOR", OperationValidatorExtension),
    )
    provider = engine._operation_validator.context.get_memory_engine()._retain_llm_config

    with pytest.raises(_Captured):
        await provider.call(messages=[{"role": "user", "content": "isolation probe"}], max_retries=0)

    options = seen["options"]
    config_dir = Path(options.env["CLAUDE_CONFIG_DIR"])
    assert config_dir.name.startswith("hindsight-claude-code-"), "a fresh configuration folder"
    assert config_dir != Path.home() / ".claude"
    assert options.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""
    assert options.tools == [] and options.allowed_tools == []
    assert options.model == "claude-sonnet-5-5"
