"""``on_retain_complete`` fires for both ways facts reach the bank (specification 17 item 6, 11.1).

Both arrive as asynchronous retains through the engine's HTTP API, so the hook runs inside the
worker's ``batch_retain`` handler, the path every save takes in production:

- the plugin's session save: newline-delimited JSON turns under ``conversation:<session id>``,
  strategy ``conversation``, a replace on the first save and ``update_mode: append`` after it,
  as ``writeSession`` in the plugin's ``claude-stop-hook.js`` sends them;
- the vault loader's retain: one markdown document under its vault path, strategy ``document``,
  ``update_mode: replace``, as ``submit`` in ``backfill/load.py`` sends it.

The content is synthetic.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from hindsight_api.extensions import RetainResult

from hindsight_ext_cortana import CortanaOperationHooks

PLUGIN_CONTEXT = (
    "conversation between the user and you (the coding agent): user turns are the user's words and "
    "decisions, assistant turns are yours."
)


@pytest.fixture
def hook_calls():
    """Every RetainResult our hook receives, recorded while the real hook still runs.

    Requested before the engine fixtures: the engine re-binds each hook on the instance when it is
    constructed (``instrument_operation_validator``), so the spy must be in place by then.
    """
    with patch.object(
        CortanaOperationHooks,
        "on_retain_complete",
        autospec=True,
        side_effect=CortanaOperationHooks.on_retain_complete,
    ) as spy:
        yield spy


def _results(spy, document_id: str) -> list[RetainResult]:
    return [call.args[1] for call in spy.await_args_list if call.args[1].document_id == document_id]


def _plugin_save(bank: str, session: str, turns: list[dict], *, start: str, append: bool) -> dict:
    ref_id = f"conversation:{session}"
    lines = turns if append else [{"role": "system", "content": f"REF-ID: {ref_id}", "timestamp": start}, *turns]
    content = "\n".join(json.dumps(turn) for turn in lines)
    item = {
        "content": content,
        "context": PLUGIN_CONTEXT,
        "document_id": ref_id,
        "tags": ["source:chat", "harness:claude-code"],
        "strategy": "conversation",
        "observation_scopes": "combined",
        "timestamp": start,
        "metadata": {"source": "chat", "session_id": session, "ref_id": ref_id, "harness": "claude-code"},
    }
    if append:
        item["update_mode"] = "append"
    mode = "append" if append else "replace"
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{bank}\n{ref_id}\n{mode}\n{content}"))
    return {"items": [item], "async": True, "operation_id": operation_id}


async def test_plugin_shaped_session_saves_fire_the_hook(hook_calls, cortana_client):
    bank = f"cortana-plugin-{uuid.uuid4().hex[:8]}"
    session = str(uuid.uuid4())
    start = "2026-10-07T18:00:00Z"
    first = [
        {"role": "user", "content": "Alex said the garden fence should be painted green this spring."},
        {"role": "assistant", "content": "Noted: the fence is to be painted green."},
    ]
    later = [
        {"role": "user", "content": "Alex changed his mind: the fence will be painted blue instead."},
        {"role": "assistant", "content": "Noted: blue, not green."},
    ]

    for turns, append in ((first, False), (later, True)):
        response = await cortana_client.post(
            f"/v1/default/banks/{bank}/memories", json=_plugin_save(bank, session, turns, start=start, append=append)
        )
        assert response.status_code == 200, response.text
        assert response.json()["async"] is True

    results = _results(hook_calls, f"conversation:{session}")
    assert len(results) == 2, "one hook call per save"
    for result in results:
        assert result.bank_id == bank
        assert result.success is True
        assert result.request_context.internal is True, "ran in the worker's batch_retain handler"
    assert all(len(ids) > 0 for ids in results[0].unit_ids), "the first save stored facts"
    assert all(len(ids) > 0 for ids in results[1].unit_ids), "the append stored facts"


async def test_loader_shaped_async_retain_fires_the_hook(hook_calls, cortana_client):
    bank = f"cortana-loader-{uuid.uuid4().hex[:8]}"
    path = "docs/example/garden.md"
    content = (
        "---\ntitle: Garden\nupdated: 2026-10-07\n---\n\n"
        "# Garden\n\nThe fence is painted blue. The gate is oak and was rehung in September.\n"
    )
    body = {
        "async": True,
        "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{bank}|{path}|1")),
        "items": [
            {
                "content": content,
                "document_id": path,
                "timestamp": "2026-10-07T12:00:00-04:00",
                "tags": ["domain:general", "db:docs", "pool:general"],
                "strategy": "document",
                "observation_scopes": [["pool:general"]],
                "context": f"Vault file: {path}",
                "metadata": {"path": path},
                "update_mode": "replace",
            }
        ],
    }

    response = await cortana_client.post(f"/v1/default/banks/{bank}/memories", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["async"] is True

    (result,) = _results(hook_calls, path)
    assert result.bank_id == bank
    assert result.success is True
    assert result.request_context.internal is True, "ran in the worker's batch_retain handler"
    assert len(result.unit_ids) == 1 and len(result.unit_ids[0]) > 0
