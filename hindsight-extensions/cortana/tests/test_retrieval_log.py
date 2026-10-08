"""The retrieval log (specification 12, criterion 11): every recall, including reflect's internal
ones, and every reflect leave a ``retrievals`` row from which the ids returned and cited can be read
without re-running the call. No model calls: reflect runs on the engine's mock provider, scripted to
recall and then answer citing what it found. All content is synthetic.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import asyncpg
import pytest
from hindsight_api import RequestContext
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import (
    LLMToolCall,
    LLMToolCallResult,
    MemoryFact,
    RecallResult,
    RecallScores,
    ReflectResult,
    ToolCallTrace,
)
from hindsight_api.engine.retain import embedding_utils
from hindsight_api.engine.schema import fq_table
from hindsight_api.extensions import RecallResult as RecallHookResult
from hindsight_api.extensions import ReflectResultContext

from hindsight_ext_cortana import retrievals
from hindsight_ext_cortana.reconcile import reconcile

FACTS = (
    "Kestrel Labs moved its standup to Tuesdays at nine.",
    "The Kestrel Labs standup is held in the Birch room.",
    "Priya Natarajan runs the Kestrel Labs standup.",
)


@pytest.fixture
async def conn(pg0_db_url):
    connection = await asyncpg.connect(pg0_db_url)
    yield connection
    await connection.close()


def _bank(name: str) -> str:
    return f"cortana-ret-{name}-{uuid.uuid4().hex[:8]}"


async def _seed(memory, bank: str) -> list[str]:
    """Facts inserted directly with real embeddings, so recall finds them without extraction."""
    await memory.ensure_bank_profile(bank, request_context=RequestContext())
    embeddings = await embedding_utils.generate_embeddings_batch(memory.embeddings, list(FACTS))
    ids = []
    now = datetime.now(UTC)
    async with (await memory._get_pool()).acquire() as c:
        for text, embedding in zip(FACTS, embeddings, strict=True):
            fact_id = str(uuid.uuid4())
            await c.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, event_date, fact_type, tags, embedding, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'world', '{{}}'::varchar[], $5::vector, $4, $4)
                """,
                fact_id,
                bank,
                text,
                now,
                "[" + ",".join(str(v) for v in embedding) + "]",
            )
            ids.append(fact_id)
    return ids


def _script_reflect(memory, monkeypatch) -> MockLLM:
    """Reflect's model, scripted: recall once, then answer citing the first id the recall returned."""
    mock = MockLLM(provider="mock", api_key="", base_url="", model="mock-model")

    def callback(messages, scope):
        if scope != "reflect_tool_call":
            return "summary"
        for message in messages:
            if message.get("role") == "tool":
                payload = json.loads(message.get("content") or "{}")
                ids = [m["id"] for m in payload.get("memories") or []]
                if ids:
                    return LLMToolCallResult(
                        tool_calls=[
                            LLMToolCall(
                                id="done-1", name="done", arguments={"answer": "Tuesdays.", "memory_ids": ids[:1]}
                            )
                        ],
                        finish_reason="tool_calls",
                    )
        return LLMToolCallResult(
            tool_calls=[LLMToolCall(id="recall-1", name="recall", arguments={"query": "Kestrel standup day"})],
            finish_reason="tool_calls",
        )

    mock.set_response_callback(callback)
    wrapper = MagicMock()
    wrapper.with_config.return_value = mock
    monkeypatch.setattr(memory, "_reflect_llm_config", wrapper)
    return mock


async def _rows(conn, bank: str) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM public.retrievals WHERE bank_id = $1 ORDER BY id", bank)
    out = []
    for row in rows:
        item = dict(row)
        for column in ("caller", "parameters", "results", "tool_calls"):
            if isinstance(item[column], str):
                item[column] = json.loads(item[column])
        out.append(item)
    return out


# Pure row building ----------------------------------------------------------------------------------


def _fact(ident: str, final: float, fact_type: str = "world") -> MemoryFact:
    return MemoryFact(id=ident, text="t", fact_type=fact_type, scores=RecallScores(final=final, semantic=0.5))


def test_a_recall_row_keeps_the_ranked_ids_scores_caller_and_parameters():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    hook = RecallHookResult(
        bank_id="b",
        query="when is standup",
        request_context=RequestContext(api_key="secret", api_key_id="key-1", internal=True),
        budget=None,
        max_tokens=2048,
        enable_trace=False,
        fact_types=["world", "experience"],
        question_date=None,
        include_entities=True,
        max_entity_tokens=500,
        include_chunks=False,
        max_chunk_tokens=1000,
        result=RecallResult(results=[_fact(a, 0.9), _fact(b, 0.4)]),
    )
    row = retrievals.recall_row(hook)
    assert row.kind == "recall" and row.query == "when is standup" and row.error is None
    assert [r["id"] for r in row.results] == [a, b]
    assert row.results[0]["scores"] == {"final": 0.9, "semantic": 0.5}
    assert row.caller == {"internal": True, "user_initiated": False, "api_key_id": "key-1"}
    assert "secret" not in json.dumps(row.caller)
    assert row.parameters["fact_types"] == ["world", "experience"] and row.parameters["max_tokens"] == 2048


def test_a_failed_recall_row_keeps_the_error_and_no_results():
    hook = RecallHookResult(
        bank_id="b",
        query="q",
        request_context=RequestContext(),
        budget=None,
        max_tokens=1,
        enable_trace=False,
        fact_types=["world"],
        question_date=None,
        include_entities=False,
        max_entity_tokens=0,
        include_chunks=False,
        max_chunk_tokens=0,
        result=None,
        success=False,
        error="boom",
    )
    row = retrievals.recall_row(hook)
    assert row.results == [] and row.error == "boom"


def test_each_reflect_tool_names_the_ids_it_returned_in_order():
    assert retrievals.ids_of("recall", {"memories": [{"id": "m1"}, {"id": "m2"}]}) == {"ids": ["m1", "m2"]}
    assert retrievals.ids_of("search_observations", {"observations": [{"id": "o1"}], "source_facts": {"f1": {}}}) == {
        "ids": ["o1"],
        "source_fact_ids": ["f1"],
    }
    assert retrievals.ids_of("search_mental_models", {"mental_models": [{"id": "mm"}]}) == {"ids": ["mm"]}
    assert retrievals.ids_of(
        "expand", {"results": [{"memory_id": "x", "memory": {"id": "x"}}, {"memory_id": "y", "error": "gone"}]}
    ) == {"ids": ["x", "y"]}
    assert retrievals.ids_of("done", {}) == {"ids": []}


def test_a_reflect_row_keeps_every_tool_call_in_order_and_the_cited_ids():
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    result = ReflectResult(
        text="answer",
        based_on={
            "world": [MemoryFact(id=b, text="t", fact_type="world")],
            "experience": [],
            "opinion": [],
            "observation": [MemoryFact(id=c, text="o", fact_type="observation")],
            "mental-models": [MemoryFact(id="team-rituals", text="m", fact_type="mental-models")],
            "directives": [],
        },
        tool_trace=[
            ToolCallTrace(
                tool="search_observations",
                input={"query": "q1"},
                output={"observations": [{"id": c}]},
                duration_ms=3,
                iteration=1,
            ),
            ToolCallTrace(
                tool="recall",
                input={"query": "q2"},
                output={"memories": [{"id": a}, {"id": b}]},
                duration_ms=5,
                iteration=2,
                reason="check facts",
            ),
        ],
    )
    reflect_id = uuid.uuid4()
    ctx = ReflectResultContext(
        bank_id="b", query="q", request_context=RequestContext(), budget=None, context=None, result=result
    )
    row = retrievals.reflect_row(ctx, reflect_id=reflect_id)
    assert row.kind == "reflect" and row.reflect_id == reflect_id and row.results == []
    assert [(t["iteration"], t["tool"], t["ids"]) for t in row.tool_calls] == [
        (1, "search_observations", [c]),
        (2, "recall", [a, b]),
    ]
    assert row.tool_calls[1]["input"] == {"query": "q2"} and row.tool_calls[1]["reason"] == "check facts"
    assert row.cited_ids == [uuid.UUID(b), uuid.UUID(c)]
    assert row.cited_mental_model_ids == ["team-rituals"]


def test_retention_defaults_to_thirty_days_and_refuses_nonsense(monkeypatch):
    monkeypatch.delenv(retrievals.RETENTION_ENV, raising=False)
    assert retrievals.retention_days() == 30
    monkeypatch.setenv(retrievals.RETENTION_ENV, "45")
    assert retrievals.retention_days() == 45
    for bad in ("0", "-3", "a month"):
        monkeypatch.setenv(retrievals.RETENTION_ENV, bad)
        with pytest.raises(ValueError, match=retrievals.RETENTION_ENV):
            retrievals.retention_days()


# Through the engine -----------------------------------------------------------------------------------


async def test_a_recall_through_the_api_leaves_one_row_with_the_ids_it_returned(cortana_memory, cortana_client, conn):
    bank = _bank("recall")
    await _seed(cortana_memory, bank)
    response = await cortana_client.post(
        f"/v1/default/banks/{bank}/memories/recall", json={"query": "When is the Kestrel standup?", "types": ["world"]}
    )
    assert response.status_code == 200, response.text
    returned = [item["id"] for item in response.json()["results"]]
    assert returned

    (row,) = await _rows(conn, bank)
    assert row["kind"] == "recall" and row["query"] == "When is the Kestrel standup?"
    assert [r["id"] for r in row["results"]] == returned
    assert all(r["scores"]["final"] is not None for r in row["results"])
    assert row["caller"]["internal"] is False and row["reflect_id"] is None and row["error"] is None
    assert row["parameters"]["fact_types"] == ["world"]


async def test_criterion_11_a_reflect_and_its_internal_recall_are_readable_without_rerunning(
    cortana_memory, cortana_client, conn, monkeypatch
):
    bank = _bank("reflect")
    await _seed(cortana_memory, bank)
    _script_reflect(cortana_memory, monkeypatch)
    response = await cortana_client.post(
        f"/v1/default/banks/{bank}/reflect",
        json={"query": "Which day is the Kestrel standup?", "include": {"facts": {}, "tool_calls": {}}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    cited = [m["id"] for m in body["based_on"]["memories"]]
    (traced,) = [t for t in body["trace"]["tool_calls"] if t["tool"] == "recall"]
    traced_ids = [m["id"] for m in traced["output"]["memories"]]

    rows = await _rows(conn, bank)
    (reflect,) = [r for r in rows if r["kind"] == "reflect"]
    recalls = [r for r in rows if r["kind"] == "recall"]
    assert reflect["query"] == "Which day is the Kestrel standup?"
    assert [str(i) for i in reflect["cited_ids"]] == cited and len(cited) == 1
    (logged,) = [t for t in reflect["tool_calls"] if t["tool"] == "recall"]
    assert logged["ids"] == traced_ids and logged["input"]["query"] == "Kestrel standup day"

    # The reflect's own recall: linked by reflect_id, internal, with the scores behind the tool call.
    (inner,) = [r for r in recalls if r["reflect_id"] == reflect["reflect_id"]]
    assert inner["caller"]["internal"] is True and inner["query"] == "Kestrel standup day"
    assert [r["id"] for r in inner["results"]] == traced_ids
    assert reflect["reflect_id"] is not None

    # The route returns the same rows, and filters a reflect with the recalls it made.
    routed = await cortana_client.get(
        "/ext/cortana/retrievals", params={"bank_id": bank, "reflect_id": str(reflect["reflect_id"])}
    )
    assert routed.status_code == 200, routed.text
    items = routed.json()["items"]
    assert [i["kind"] for i in items] == ["reflect", "recall"]
    assert items[0]["cited_ids"] == cited and items[1]["results"][0]["id"] == traced_ids[0]


async def test_a_recall_after_a_reflect_in_the_same_task_is_not_linked(cortana_memory, conn, monkeypatch):
    bank = _bank("unlinked")
    await _seed(cortana_memory, bank)
    _script_reflect(cortana_memory, monkeypatch)
    await cortana_memory.reflect_async(bank, "Which day is the Kestrel standup?", request_context=RequestContext())
    await cortana_memory.recall_async(bank, "Birch room", request_context=RequestContext(internal=True))
    last = (await _rows(conn, bank))[-1]
    assert last["query"] == "Birch room" and last["reflect_id"] is None


async def test_a_failed_recall_leaves_a_row_with_the_error(cortana_memory, conn):
    bank = _bank("failed")
    await _seed(cortana_memory, bank)
    with patch.object(type(cortana_memory), "_search_with_retries", side_effect=RuntimeError("synthetic failure")):
        with pytest.raises(RuntimeError):
            await cortana_memory.recall_async(bank, "anything", request_context=RequestContext())
    (row,) = await _rows(conn, bank)
    assert row["error"] == "synthetic failure" and row["results"] == []


async def test_a_log_write_failure_does_not_fail_the_recall(cortana_memory, conn, caplog):
    bank = _bank("nowrite")
    await _seed(cortana_memory, bank)
    with patch.object(retrievals, "insert", side_effect=RuntimeError("disk full")):
        result = await cortana_memory.recall_async(bank, "Kestrel standup", request_context=RequestContext())
    assert result.results
    assert "retrieval log: recall row for bank" in caplog.text
    assert await _rows(conn, bank) == []


# Retention ---------------------------------------------------------------------------------------------


async def _aged(conn, bank: str, days: int) -> None:
    await conn.execute(
        "INSERT INTO public.retrievals (bank_id, kind, query, recorded_at) "
        "VALUES ($1, 'recall', $2, now() - make_interval(days => $3))",
        bank,
        f"{days} days old",
        days,
    )


async def test_the_sweep_deletes_rows_past_retention_for_one_bank_or_every_bank(conn):
    kept_bank, swept_bank = _bank("keep"), _bank("sweep")
    for bank in (kept_bank, swept_bank):
        await _aged(conn, bank, 31)
        await _aged(conn, bank, 29)
    assert await retrievals.sweep(conn, bank_id=swept_bank, days=30, schema="public") == 1
    assert [r["query"] for r in await _rows(conn, swept_bank)] == ["29 days old"]
    assert len(await _rows(conn, kept_bank)) == 2
    assert await retrievals.sweep(conn, days=30, schema="public") >= 1
    assert [r["query"] for r in await _rows(conn, kept_bank)] == ["29 days old"]


async def test_a_whole_bank_reconcile_sweeps_and_a_scoped_one_does_not(cortana_memory, conn, monkeypatch):
    monkeypatch.setenv(retrievals.RETENTION_ENV, "30")
    bank = _bank("reconcile")
    await cortana_memory.ensure_bank_profile(bank, request_context=RequestContext())
    await _aged(conn, bank, 40)
    scoped = await reconcile(cortana_memory, bank, document="none", request_context=RequestContext(internal=True))
    assert scoped.retrievals_swept == 0 and len(await _rows(conn, bank)) == 1
    first = await reconcile(cortana_memory, bank, request_context=RequestContext(internal=True))
    assert first.retrievals_swept == 1 and first.summary()["retrievals_swept"] == 1
    assert await _rows(conn, bank) == []
    assert not first.wrote_summary, "a sweep alone is not a supersession change"
