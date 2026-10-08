"""Structuring inside the engine (specification 5.2 and 5.3): after a retain, the hook turns the new
facts into claim rows through the engine's retain provider and records the call in the ledger; a
failed call leaves the facts ``structuring-pending``; facts already structured are skipped;
pre-structured claims (decision records) skip the model; and resolving a subject never writes the
engine's entities.

The engine runs with its ``mock`` provider. Structuring calls are recognised by their system prompt
and answered from the call's own message, so the test checks the whole path (render, call, parse,
validate, write) without a model; every other call keeps the mock's own behaviour.
"""

import re
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from hindsight_api import RequestContext
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage

from hindsight_ext_cortana.structuring import VERSION, prompt
from hindsight_ext_cortana.structuring.engine import (
    PgStore,
    PrestructuredClaim,
    record_prestructured,
    structure_facts,
)
from hindsight_ext_cortana.structuring.batching import make_batches
from hindsight_ext_cortana.structuring.validation import content_hash

from test_retain_hook import _plugin_save

FACT = re.compile(r"^(F\d+) \(chunk (\d+)\)\nentities: (.*)\ntext: (.*)$", re.MULTILINE)
TURN = re.compile(r"^\[(T\d+) \|", re.MULTILINE)


def _answer(message: str) -> dict:
    """One claim per fact: the first entity's ``fence-color``, provisional when the text proposes,
    stated in the message's last turn."""
    turns = TURN.findall(message)
    facts = []
    for fact_id, _chunk, entities, text in FACT.findall(message):
        subject = entities.split("; ")[0] if entities != "(none)" else "Garden fence"
        facts.append(
            {
                "fact": fact_id,
                "claims": [
                    {
                        "subject": subject,
                        "attribute": "Fence Color",
                        "description": "The color the fence is painted",
                        "value": text.split(" | ")[0][:80],
                        "provisional": "suggest" in text.lower(),
                        "turn": turns[-1] if turns else None,
                    }
                ],
            }
        )
    return {"facts": facts}


@pytest.fixture
def structuring_calls(monkeypatch):
    """Answer structuring calls on the mock provider; record them; optionally fail them."""
    state = {"calls": [], "fail": None}
    original = MockLLM.call

    async def call(self, messages, response_format=None, **kwargs):
        if messages and messages[0].get("role") == "system" and messages[0].get("content") == prompt():
            state["calls"].append({"messages": messages, "scope": kwargs.get("scope"), "kwargs": kwargs})
            if state["fail"] is not None:
                raise state["fail"]
            return LLMCallResult(content=_answer(messages[1]["content"]), usage=TokenUsage())
        return await original(self, messages, response_format=response_format, **kwargs)

    monkeypatch.setattr(MockLLM, "call", call)
    return state


@pytest.fixture
async def conn(pg0_db_url):
    connection = await asyncpg.connect(pg0_db_url)
    yield connection
    await connection.close()


START = "2026-10-07T18:00:00Z"
TURNS = [
    {
        "role": "user",
        "content": "Alex said the garden fence should be painted green.",
        "timestamp": "2026-10-07T18:00:05Z",
    },
    {"role": "assistant", "content": "I suggest blue instead, Alex.", "timestamp": "2026-10-07T18:00:40Z"},
]


async def _save(client, bank: str, session: str) -> None:
    body = _plugin_save(bank, session, TURNS, start=START, append=False)
    response = await client.post(f"/v1/default/banks/{bank}/memories", json=body)
    assert response.status_code == 200, response.text


async def _facts(conn, bank: str) -> dict:
    rows = await conn.fetch(
        "SELECT id, text FROM public.memory_units WHERE bank_id = $1 AND fact_type IN ('world', 'experience')", bank
    )
    return {row["id"]: row["text"] for row in rows}


async def test_the_hook_structures_a_plugin_save_into_claims(structuring_calls, cortana_client, conn):
    bank = f"cortana-structuring-{uuid.uuid4().hex[:8]}"
    session = str(uuid.uuid4())
    await _save(cortana_client, bank, session)

    facts = await _facts(conn, bank)
    assert facts, "the save stored facts"
    (call,) = structuring_calls["calls"]
    assert call["scope"] == "memory"
    assert call["kwargs"]["max_retries"] == 2
    assert "TURNS OF CHUNK 0" in call["messages"][1]["content"]

    claims = await conn.fetch("SELECT * FROM public.claims WHERE bank_id = $1", bank)
    assert {row["memory_unit_id"] for row in claims} == set(facts)
    last_turn = datetime(2026, 10, 7, 18, 0, 40, tzinfo=UTC)
    for row in claims:
        assert row["attribute_key"] == "fence-color"
        assert row["prompt_version"] == VERSION and row["model"] == "mock/mock"
        assert row["content_hash"] == content_hash(facts[row["memory_unit_id"]])
        assert row["source_kind"] == "session" and row["source_rank"] == 0
        assert row["document_id"] == f"conversation:{session}"
        assert row["chunk_id"] is not None and row["chunk_index"] == 0
        assert row["stated_at"] == last_turn and row["stated_at_source"] == "turn"
        assert row["document_order"] == 0
    assert sorted(row["fact_ordinal"] for row in claims) == list(range(len(claims)))

    attributes = await conn.fetch("SELECT * FROM public.attributes WHERE bank_id = $1", bank)
    assert {row["attribute_key"] for row in attributes} == {"fence-color"}
    # Supersession (HSIGHT-5) writes its own entries after the structuring one.
    (ledger,) = await conn.fetch("SELECT * FROM public.ledger WHERE bank_id = $1 AND event = 'structured'", bank)
    assert (ledger["event"], ledger["actor"]) == ("structured", "hook")
    assert set(ledger["memory_unit_ids"]) == set(facts)
    assert set(ledger["claim_ids"]) == {row["id"] for row in claims}


async def test_a_failed_call_leaves_the_facts_structuring_pending(structuring_calls, cortana_client, conn):
    structuring_calls["fail"] = RuntimeError("the subscription refused the call")
    bank = f"cortana-pending-{uuid.uuid4().hex[:8]}"
    await _save(cortana_client, bank, str(uuid.uuid4()))

    facts = await _facts(conn, bank)
    assert facts, "the retain itself succeeded"
    assert await conn.fetchval("SELECT count(*) FROM public.claims WHERE bank_id = $1", bank) == 0
    (ledger,) = await conn.fetch("SELECT * FROM public.ledger WHERE bank_id = $1", bank)
    assert (ledger["event"], ledger["actor"]) == ("structuring-pending", "hook")
    assert set(ledger["memory_unit_ids"]) == set(facts)
    assert "the subscription refused the call" in ledger["reason"]


async def test_structured_facts_are_not_structured_again(structuring_calls, cortana_client, cortana_memory, conn):
    bank = f"cortana-again-{uuid.uuid4().hex[:8]}"
    await _save(cortana_client, bank, str(uuid.uuid4()))
    facts = list(await _facts(conn, bank))
    calls = len(structuring_calls["calls"])

    assert await structure_facts(cortana_memory, bank, facts, RequestContext()) is None
    assert len(structuring_calls["calls"]) == calls
    assert (
        await conn.fetchval("SELECT count(*) FROM public.ledger WHERE bank_id = $1 AND event = 'structured'", bank) == 1
    )


async def test_reconciliation_can_structure_pending_facts(structuring_calls, cortana_client, cortana_memory, conn):
    """The entry point reconciliation uses: facts left pending are structured on a later run."""
    structuring_calls["fail"] = RuntimeError("down")
    bank = f"cortana-retry-{uuid.uuid4().hex[:8]}"
    await _save(cortana_client, bank, str(uuid.uuid4()))
    facts = list(await _facts(conn, bank))

    structuring_calls["fail"] = None
    report = await structure_facts(cortana_memory, bank, facts, RequestContext(), actor="reconciliation")
    assert report is not None and report.claims == len(facts) and not report.pending
    claims = await conn.fetch("SELECT * FROM public.claims WHERE bank_id = $1", bank)
    # Without the retain's event date, the date is the document's earliest mentioned_at; the turn
    # timestamps still give the statement time.
    assert {row["stated_at_source"] for row in claims} == {"turn"}
    events = await conn.fetch("SELECT event, actor FROM public.ledger WHERE bank_id = $1 ORDER BY id", bank)
    assert [(r["event"], r["actor"]) for r in events] == [
        ("structuring-pending", "hook"),
        ("structured", "reconciliation"),
    ]


async def test_decision_records_skip_the_model(structuring_calls, cortana_memory, conn):
    bank = f"cortana-decision-{uuid.uuid4().hex[:8]}"
    (unit_ids,) = await cortana_memory.retain_batch_async(
        bank,
        [{"content": "Alex decided the fence is painted blue.", "document_id": "decision:fence"}],
        request_context=RequestContext(),
    )
    structuring_calls["calls"].clear()
    await conn.execute("DELETE FROM public.claims WHERE bank_id = $1", bank)
    stated = datetime(2026, 10, 7, 19, 30, tzinfo=UTC)

    result = await record_prestructured(
        cortana_memory,
        bank,
        uuid.UUID(unit_ids[0]),
        [PrestructuredClaim(subject="Alex", attribute="Fence Color", value="blue", description="The fence's color")],
        stated,
    )

    assert structuring_calls["calls"] == []
    (row,) = await conn.fetch("SELECT * FROM public.claims WHERE bank_id = $1", bank)
    assert (row["source_kind"], row["source_rank"], row["stated_at_source"]) == ("decision", 3, "decision")
    assert row["stated_at"] == stated
    assert (row["attribute_key"], row["value_text"], row["provisional"]) == ("fence-color", "blue", False)
    assert row["prompt_version"] is None and row["model"] is None
    assert result.claims[0].memory_unit_id == uuid.UUID(unit_ids[0])
    events = await conn.fetch("SELECT event, actor FROM public.ledger WHERE bank_id = $1 ORDER BY id", bank)
    assert (events[-1]["event"], events[-1]["actor"]) == ("structured", "decision-tool")


async def test_resolving_a_subject_never_writes_the_engines_entities(
    structuring_calls, cortana_client, cortana_memory, conn
):
    bank = f"cortana-resolve-{uuid.uuid4().hex[:8]}"
    await _save(cortana_client, bank, str(uuid.uuid4()))
    store = PgStore(cortana_memory, bank)
    facts = await store.load_facts(list(await _facts(conn, bank)), {})
    (batch,) = make_batches(facts)
    before = await conn.fetchval("SELECT count(*) FROM public.entities WHERE bank_id = $1", bank)
    known = await conn.fetchval("SELECT canonical_name FROM public.entities WHERE bank_id = $1 LIMIT 1", bank)

    names = {"A name no fact mentions", known.upper()} if known else {"A name no fact mentions"}
    resolved = await store.resolve_subjects(names, batch)

    assert await conn.fetchval("SELECT count(*) FROM public.entities WHERE bank_id = $1", bank) == before
    assert resolved["A name no fact mentions"] is None
    if known:
        assert resolved[known.upper()].name == known
