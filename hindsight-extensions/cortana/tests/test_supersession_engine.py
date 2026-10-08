"""Supersession inside the engine, end to end, with no model calls (specification 6, 11.1, 15).

The engine runs with its ``mock`` provider and tasks run inline. Extraction is the mock's: each
sentence becomes a fact. Structuring and alignment calls are recognised by their system prompts and
answered here from the call's own message: a sentence of the form "<Subject>'s <attribute> is
<value>" is a claim, provisional when it says "proposed"; alignment answers come from each test.
So the whole path runs (retain, hook, structuring, alignment, rules, ledger, curation through
``update_memory_unit``) and the tests read the results from the engine's tables and endpoints.
All content is synthetic.
"""

import json
import re
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest
from hindsight_api import RequestContext
from hindsight_api.engine.providers.mock_llm import MockLLM
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage

from hindsight_ext_cortana import alignment
from hindsight_ext_cortana.merges import merge_key, reverse_merge
from hindsight_ext_cortana.reconcile import reconcile
from hindsight_ext_cortana.rules import is_rule_retirement
from hindsight_ext_cortana.structuring import prompt as structuring_prompt
from hindsight_ext_cortana.structuring.engine import PrestructuredClaim, record_prestructured
from hindsight_ext_cortana.supersession import keys_of_subject, settle

from test_retain_hook import _plugin_save

FACT = re.compile(r"^(F\d+) \(chunk (\d+)\)\nentities: (.*)\ntext: (.*)$", re.MULTILINE)
TURN = re.compile(r"^\[(T\d+) \| [^|]+ \| \w+\] (.*)$", re.MULTILINE)
CLAIM = re.compile(r"(?P<subject>[A-Z][a-z]+)'s (?P<attribute>[a-z]+) is (?P<value>[a-z0-9-]+)")
PENDING_KEY = re.compile(r"^SUBJECT (S\d+): .*$|^- ([a-z0-9-]+): .*$", re.MULTILINE)


def _structure(message: str) -> dict:
    turns = TURN.findall(message)
    facts = []
    for fact_id, _chunk, _entities, text in FACT.findall(message):
        statement = text.split(" | ")[0]
        claims = []
        for match in CLAIM.finditer(statement):
            said = match.group(0)
            turn = next((tid for tid, content in reversed(turns) if said in content), None)
            claims.append(
                {
                    "subject": match["subject"],
                    "attribute": match["attribute"],
                    "description": f"the {match['attribute']} of the subject",
                    "value": match["value"],
                    "provisional": "proposed" in statement.lower(),
                    "turn": turn,
                    "quote": said,
                }
            )
        facts.append({"fact": fact_id, "claims": claims})
    return {"facts": facts}


def _pending_keys(message: str) -> dict[str, list[str]]:
    """The pending keys per subject label in an alignment call's message."""
    keys: dict[str, list[str]] = {}
    for block in message.split("\n\n"):
        label = re.match(r"SUBJECT (S\d+):", block)
        if label and "new keys to align:" in block:
            pending = block.split("new keys to align:", 1)[1]
            keys[label.group(1)] = re.findall(r"^- ([a-z0-9-]+):", pending, re.MULTILINE)
    return keys


@pytest.fixture
def model(monkeypatch):
    """Answer structuring and alignment calls on the mock provider.

    ``align`` maps a pending key to the key it is the same as, or None for distinct; a pending key
    it does not name is left out of the answer. ``align_fail`` makes alignment calls raise."""
    state = {"structuring": 0, "structuring_messages": [], "alignment": [], "align": {}, "align_fail": None}
    original = MockLLM.call

    async def call(self, messages, response_format=None, **kwargs):
        system = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
        if system == structuring_prompt():
            state["structuring"] += 1
            state["structuring_messages"].append(messages[1]["content"])
            return LLMCallResult(content=_structure(messages[1]["content"]), usage=TokenUsage())
        if system == alignment.prompt():
            state["alignment"].append(messages[1]["content"])
            if state["align_fail"] is not None:
                raise state["align_fail"]
            subjects = [
                {
                    "subject": label,
                    "keys": [{"key": k, "same_as": state["align"][k]} for k in keys if k in state["align"]],
                }
                for label, keys in _pending_keys(messages[1]["content"]).items()
            ]
            return LLMCallResult(content={"subjects": subjects}, usage=TokenUsage())
        return await original(self, messages, response_format=response_format, **kwargs)

    monkeypatch.setattr(MockLLM, "call", call)
    return state


@pytest.fixture
async def conn(pg0_db_url):
    connection = await asyncpg.connect(pg0_db_url)
    yield connection
    await connection.close()


def _bank(name: str) -> str:
    return f"cortana-sup-{name}-{uuid.uuid4().hex[:8]}"


async def _session(
    client,
    bank: str,
    turns: list[tuple[str, str]],
    *,
    start: str,
    tags: list[str] | None = None,
    session: str | None = None,
) -> str:
    """A plugin-shaped save of ``turns`` (timestamp, user text); returns the session id."""
    session = session or str(uuid.uuid4())
    body = _plugin_save(
        bank,
        session,
        [{"role": "user", "content": text, "timestamp": ts} for ts, text in turns],
        start=start,
        append=False,
    )
    if tags:
        body["items"][0]["tags"] = [*body["items"][0]["tags"], *tags]
    response = await client.post(f"/v1/default/banks/{bank}/memories", json=body)
    assert response.status_code == 200, response.text
    return session


async def _document(client, bank: str, path: str, text: str, *, date: str, revision: int = 1) -> None:
    """A loader-shaped asynchronous retain of one vault document."""
    body = {
        "async": True,
        "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{bank}|{path}|{revision}")),
        "items": [
            {
                "content": f"# {path}\n\n{text}\n",
                "document_id": path,
                "timestamp": date,
                "tags": ["domain:work", "db:docs", "pool:work"],
                "strategy": "document",
                "observation_scopes": [["pool:work"]],
                "context": f"Vault file: {path}",
                "metadata": {"path": path},
                "update_mode": "replace",
            }
        ],
    }
    response = await client.post(f"/v1/default/banks/{bank}/memories", json=body)
    assert response.status_code == 200, response.text


async def _claims(conn, bank: str, key: str | None = None) -> dict[str, dict]:
    """Claims by value (values are unique within each test)."""
    rows = await conn.fetch(
        "SELECT * FROM public.claims WHERE bank_id = $1 AND ($2::text IS NULL OR attribute_key = $2)", bank, key
    )
    return {row["value_text"]: dict(row) for row in rows}


async def _live(conn, fact_id) -> bool:
    return await conn.fetchval("SELECT count(*) FROM public.memory_units WHERE id = $1", fact_id) == 1


async def _archived(conn, fact_id) -> str | None:
    return await conn.fetchval("SELECT invalidation_reason FROM public.invalidated_memory_units WHERE id = $1", fact_id)


async def _events(conn, bank: str) -> list[str]:
    return [
        row["event"] for row in await conn.fetch("SELECT event FROM public.ledger WHERE bank_id = $1 ORDER BY id", bank)
    ]


async def _ledger_count(conn, bank: str) -> int:
    return await conn.fetchval("SELECT count(*) FROM public.ledger WHERE bank_id = $1", bank)


# Criterion 1 ---------------------------------------------------------------------------------------


async def test_criterion_1_a_provisional_claim_and_its_resolution_retire_the_provisional_fact(
    model, cortana_client, conn
):
    bank = _bank("c1")
    await _session(
        cortana_client,
        bank,
        [
            ("2026-10-07T18:00:05Z", "I proposed that Kestrel's color is green."),
            ("2026-10-07T18:00:40Z", "Settled: Kestrel's color is blue."),
        ],
        start="2026-10-07T18:00:00Z",
    )
    claims = await _claims(conn, bank)
    green, blue = claims["green"], claims["blue"]
    assert green["provisional"] and not blue["provisional"]
    assert (blue["state"], green["state"]) == ("current", "superseded")
    assert (green["superseded_by"], green["superseded_rule"]) == (blue["id"], "S2")
    assert [c["state"] for c in claims.values()].count("current") == 1

    reason = await _archived(conn, green["memory_unit_id"])
    assert reason == f"superseded by {blue['memory_unit_id']} on Kestrel/color, rule S2"
    assert await _live(conn, blue["memory_unit_id"])

    listed = await cortana_client.get(f"/v1/default/banks/{bank}/memories/list", params={"state": "invalidated"})
    assert listed.status_code == 200, listed.text
    assert str(green["memory_unit_id"]) in {item["id"] for item in listed.json()["items"]}

    rows = await conn.fetch("SELECT * FROM public.ledger WHERE bank_id = $1 ORDER BY id", bank)
    superseded = next(r for r in rows if r["event"] == "claim-superseded")
    assert superseded["rule"] == "S2" and superseded["actor"] == "hook"
    assert list(superseded["claim_ids"]) == [green["id"], blue["id"]]
    retired = next(r for r in rows if r["event"] == "fact-retired")
    assert retired["reason"] == reason and retired["memory_unit_ids"][0] == green["memory_unit_id"]
    assert retired["run_id"] == superseded["run_id"]


# Criterion 3 ---------------------------------------------------------------------------------------


async def test_criterion_3_a_later_statement_wins_over_an_earlier_one_about_a_later_day(
    model, cortana_client, cortana_memory, conn
):
    bank = _bank("c3")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's launch is friday.")], start="2026-10-01T10:00:00Z"
    )
    earlier = (await _claims(conn, bank))["friday"]
    # The earlier statement is about a later day than the later one: its occurred_start is set
    # through the engine's own edit path to a date after everything else.
    await cortana_memory.update_memory_unit(
        bank, str(earlier["memory_unit_id"]), occurred_start="2026-12-31T00:00:00Z", request_context=RequestContext()
    )
    await _session(
        cortana_client, bank, [("2026-10-05T10:00:00Z", "Kestrel's launch is done.")], start="2026-10-05T10:00:00Z"
    )
    claims = await _claims(conn, bank)
    assert claims["done"]["state"] == "current" and claims["friday"]["state"] == "superseded"
    assert await _archived(conn, earlier["memory_unit_id"])


# Criterion 4 ---------------------------------------------------------------------------------------


async def test_criterion_4_supersession_crosses_pools(model, cortana_client, conn):
    bank = _bank("c4")
    await _session(
        cortana_client,
        bank,
        [("2026-10-01T10:00:00Z", "Kestrel's mood is calm.")],
        start="2026-10-01T10:00:00Z",
        tags=["pool:recovery", "domain:recovery"],
    )
    await _session(
        cortana_client,
        bank,
        [("2026-10-02T10:00:00Z", "Kestrel's mood is busy.")],
        start="2026-10-02T10:00:00Z",
        tags=["pool:work", "domain:work"],
    )
    claims = await _claims(conn, bank)
    assert claims["calm"]["state"] == "superseded" and claims["busy"]["state"] == "current"
    tags = await conn.fetchval(
        "SELECT tags FROM public.invalidated_memory_units WHERE id = $1", claims["calm"]["memory_unit_id"]
    )
    assert "pool:recovery" in tags


# Criterion 5 ---------------------------------------------------------------------------------------


async def test_criterion_5_a_fact_with_one_superseded_claim_stays_valid(model, cortana_client, conn):
    bank = _bank("c5")
    await _session(
        cortana_client,
        bank,
        [("2026-10-01T10:00:00Z", "Kestrel's status is green and Kestrel's owner is priya.")],
        start="2026-10-01T10:00:00Z",
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's status is red.")], start="2026-10-02T10:00:00Z"
    )
    claims = await _claims(conn, bank)
    shared = claims["green"]["memory_unit_id"]
    assert claims["priya"]["memory_unit_id"] == shared
    assert claims["green"]["state"] == "superseded" and claims["priya"]["state"] == "current"
    assert await _live(conn, shared), "a fact with a current claim is not retired"

    async def current(key: str):
        return await conn.fetch(
            "SELECT c.memory_unit_id FROM public.claims c JOIN public.memory_units mu ON mu.id = c.memory_unit_id "
            "WHERE c.bank_id = $1 AND c.attribute_key = $2 AND c.state = 'current'",
            bank,
            key,
        )

    assert [r["memory_unit_id"] for r in await current("status")] == [claims["red"]["memory_unit_id"]]
    assert [r["memory_unit_id"] for r in await current("owner")] == [shared]


# Criterion 6 and S5 --------------------------------------------------------------------------------


async def test_criterion_6_deleting_the_superseding_document_restores_on_reconciliation(
    model, cortana_client, cortana_memory, conn
):
    bank = _bank("c6")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's phase is design.")], start="2026-10-01T10:00:00Z"
    )
    later = await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's phase is build.")], start="2026-10-02T10:00:00Z"
    )
    old = (await _claims(conn, bank))["design"]
    assert await _archived(conn, old["memory_unit_id"])

    response = await cortana_client.delete(f"/v1/default/banks/{bank}/documents/conversation:{later}")
    assert response.status_code == 200, response.text
    report = await reconcile(cortana_memory, bank, request_context=RequestContext())

    restored = (await _claims(conn, bank))["design"]
    assert restored["state"] == "current" and restored["superseded_by"] is None
    assert await _live(conn, old["memory_unit_id"])
    assert report.swept == 1 and "build" not in await _claims(conn, bank)
    events = await _events(conn, bank)
    assert {"orphan-claims-swept", "claim-restored", "fact-restored", "reconciled"} <= set(events)
    restored_row = await conn.fetchrow(
        "SELECT * FROM public.ledger WHERE bank_id = $1 AND event = 'claim-restored'", bank
    )
    assert restored_row["rule"] == "S5" and "no longer exists" in restored_row["reason"]


async def test_a_resave_that_drops_the_superseding_statement_restores_through_the_hook(model, cortana_client, conn):
    bank = _bank("s5")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's phase is design.")], start="2026-10-01T10:00:00Z"
    )
    later = await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's phase is build.")], start="2026-10-02T10:00:00Z"
    )
    assert (await _claims(conn, bank))["design"]["state"] == "superseded"
    await _session(
        cortana_client,
        bank,
        [("2026-10-02T10:00:00Z", "Nothing more was said about the project.")],
        start="2026-10-02T10:00:00Z",
        session=later,
    )
    design = (await _claims(conn, bank))["design"]
    assert design["state"] == "current"
    assert await _live(conn, design["memory_unit_id"])


async def test_a_resave_rederives_claims_for_reextracted_facts_by_content_hash(model, cortana_client, conn):
    bank = _bank("hash")
    session = await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's phase is design.")], start="2026-10-01T10:00:00Z"
    )
    before = (await _claims(conn, bank))["design"]
    await _session(
        cortana_client,
        bank,
        [("2026-10-01T10:00:00Z", "Kestrel's phase is design."), ("2026-10-01T10:05:00Z", "Lunch was soup today.")],
        start="2026-10-01T10:00:00Z",
        session=session,
    )
    after = (await _claims(conn, bank))["design"]
    assert after["memory_unit_id"] != before["memory_unit_id"], "the chunk was re-extracted under a new id"
    assert await _live(conn, after["memory_unit_id"])
    assert (after["attribute_key"], after["stated_at"], after["state"]) == ("phase", before["stated_at"], "current")
    facts_asked = model["structuring_messages"][-1].split("\nFACTS\n", 1)[1]
    assert "Kestrel's phase is design" not in facts_asked, "the model was not asked about it again"
    row = await conn.fetchrow(
        "SELECT details FROM public.ledger WHERE bank_id = $1 AND event = 'structured' AND details ? 'rederived_from'",
        bank,
    )
    assert json.loads(row["details"])["rederived_from"] == {str(after["memory_unit_id"]): str(before["memory_unit_id"])}


# Criterion 7 and S10 -------------------------------------------------------------------------------


async def test_criterion_7_reconciling_twice_writes_nothing_the_second_time(
    model, cortana_client, cortana_memory, conn
):
    bank = _bank("c7")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's phase is design.")], start="2026-10-01T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's phase is build.")], start="2026-10-02T10:00:00Z"
    )
    first = await reconcile(cortana_memory, bank, request_context=RequestContext())
    before = await _ledger_count(conn, bank)
    calls = model["structuring"]
    second = await reconcile(cortana_memory, bank, request_context=RequestContext())
    assert await _ledger_count(conn, bank) == before
    assert not second.wrote_summary and not second.settled.changed
    assert model["structuring"] == calls, "facts the model answered are not structured again"
    assert first.settled.keys == second.settled.keys >= 1


async def test_settling_unchanged_keys_writes_nothing(model, cortana_client, cortana_memory, conn):
    bank = _bank("s10")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's phase is design.")], start="2026-10-01T10:00:00Z"
    )
    subject = (await _claims(conn, bank))["design"]["subject_entity_id"]
    keys = await keys_of_subject(conn, bank, subject)
    before = await _ledger_count(conn, bank)
    report = await settle(cortana_memory, bank, keys, request_context=RequestContext(), actor="reconciliation")
    assert not report.changed and await _ledger_count(conn, bank) == before


# Criterion 8, merges and alignment -----------------------------------------------------------------


async def test_criterion_8_an_unaligned_claim_retires_nothing_until_merged_and_the_merge_reverses(
    model, cortana_client, cortana_memory, conn
):
    bank = _bank("c8")
    model["align_fail"] = RuntimeError("alignment unavailable")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's status is green.")], start="2026-10-01T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's state is red.")], start="2026-10-02T10:00:00Z"
    )
    claims = await _claims(conn, bank)
    green, red = claims["green"], claims["red"]
    assert red["state"] == "unaligned" and green["state"] == "current"
    assert await _live(conn, green["memory_unit_id"])
    alignment_row = await conn.fetchrow(
        "SELECT alignment FROM public.attributes WHERE bank_id = $1 AND attribute_key = 'state'", bank
    )
    assert alignment_row["alignment"] == "pending"
    assert "alignment-failed" in await _events(conn, bank)

    subject = green["subject_entity_id"]
    merged = await merge_key(cortana_memory, bank, subject, "state", "status", actor="operator", reason="test")
    await settle(cortana_memory, bank, merged.keys, request_context=RequestContext(), actor="operator")
    claims = await _claims(conn, bank)
    assert claims["red"]["attribute_key"] == "status" and claims["red"]["keyed_as"] == "state"
    assert claims["green"]["state"] == "superseded" and claims["red"]["state"] == "current"
    assert await _archived(conn, green["memory_unit_id"])
    events = await _events(conn, bank)
    assert events[-3:] == ["attribute-merged", "claim-superseded", "fact-retired"]

    reversed_ = await reverse_merge(cortana_memory, bank, subject, "state", actor="operator", reason="test")
    await settle(cortana_memory, bank, reversed_.keys, request_context=RequestContext(), actor="operator")
    claims = await _claims(conn, bank)
    assert claims["red"]["attribute_key"] == "state" and claims["red"]["state"] == "current"
    assert claims["green"]["state"] == "current"
    assert await _live(conn, green["memory_unit_id"])
    assert (await _events(conn, bank))[-3:] == ["attribute-merge-reversed", "claim-restored", "fact-restored"]
    row = await conn.fetchrow(
        "SELECT alignment, merged_into FROM public.attributes WHERE bank_id = $1 AND attribute_key = 'state'", bank
    )
    assert (row["alignment"], row["merged_into"]) == ("aligned", None)


async def test_the_alignment_pass_merges_a_same_as_key_in_the_hook(model, cortana_client, conn):
    bank = _bank("align")
    model["align"] = {"state": "status"}
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's status is green.")], start="2026-10-01T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's state is red.")], start="2026-10-02T10:00:00Z"
    )
    (message,) = model["alignment"]
    assert "established keys:\n- status:" in message and "new keys to align:\n- state:" in message
    assert '"red"' in message
    claims = await _claims(conn, bank)
    assert claims["red"]["attribute_key"] == "status" and claims["green"]["state"] == "superseded"
    assert await _archived(conn, claims["green"]["memory_unit_id"])
    row = await conn.fetchrow("SELECT * FROM public.ledger WHERE bank_id = $1 AND event = 'attribute-merged'", bank)
    assert row["actor"] == "hook" and "alignment prompt" in row["reason"]


async def test_a_distinct_answer_aligns_the_key_and_s1_applies_among_its_claims(model, cortana_client, conn):
    bank = _bank("distinct")
    model["align"] = {"budget": None}
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's status is green.")], start="2026-10-01T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's budget is small.")], start="2026-10-02T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-03T10:00:00Z", "Kestrel's budget is large.")], start="2026-10-03T10:00:00Z"
    )
    claims = await _claims(conn, bank)
    assert claims["small"]["state"] == "superseded" and claims["large"]["state"] == "current"
    assert claims["green"]["state"] == "current"
    assert "attribute-aligned" in await _events(conn, bank)
    assert len(model["alignment"]) == 1, "an aligned key is not asked about again"


async def test_reconciliation_retries_a_failed_alignment(model, cortana_client, cortana_memory, conn):
    bank = _bank("retry")
    model["align_fail"] = RuntimeError("down")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's status is green.")], start="2026-10-01T10:00:00Z"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's state is red.")], start="2026-10-02T10:00:00Z"
    )
    model["align_fail"], model["align"] = None, {"state": "status"}
    report = await reconcile(cortana_memory, bank, request_context=RequestContext())
    assert report.aligned.merged == [("Kestrel", "state", "status")]
    assert (await _claims(conn, bank))["green"]["state"] == "superseded"


# Criteria 9 and 10 ---------------------------------------------------------------------------------


async def test_criteria_9_and_10_observations_mental_models_recall_and_the_archive(
    model, cortana_client, cortana_memory, conn, monkeypatch
):
    bank = _bank("c9")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's venue is harbor.")], start="2026-10-01T10:00:00Z"
    )
    old = (await _claims(conn, bank))["harbor"]["memory_unit_id"]
    await cortana_memory.run_consolidation(bank, request_context=RequestContext())
    citing = await conn.fetch(
        "SELECT id FROM public.memory_units WHERE bank_id = $1 AND fact_type = 'observation' AND $2 = ANY(source_memory_ids)",
        bank,
        old,
    )
    assert citing, "consolidation built an observation on the fact"

    created = await cortana_client.post(
        f"/v1/default/banks/{bank}/mental-models", json={"name": "Kestrel venue", "source_query": "Where is Kestrel?"}
    )
    assert created.status_code == 200, created.text
    model_id = await conn.fetchval("SELECT id FROM public.mental_models WHERE bank_id = $1", bank)
    # Test arrangement: give the model a grounding that cites the fact and one observation on it,
    # in the shape the engine stores (``based_on`` by type).
    based_on = {"world": [{"id": str(old)}], "observation": [{"id": str(citing[0]["id"])}]}
    await conn.execute(
        "UPDATE public.mental_models SET reflect_response = $2::jsonb WHERE id = $1",
        model_id,
        json.dumps({"based_on": based_on}),
    )
    requested = []
    original = type(cortana_memory).submit_async_refresh_mental_model

    async def spy(self, bank_id, mental_model_id, **kwargs):
        requested.append(mental_model_id)
        return await original(self, bank_id, mental_model_id, **kwargs)

    monkeypatch.setattr(type(cortana_memory), "submit_async_refresh_mental_model", spy)

    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's venue is library.")], start="2026-10-02T10:00:00Z"
    )
    assert is_rule_retirement(await _archived(conn, old))
    still_citing = await conn.fetchval(
        "SELECT count(*) FROM public.memory_units WHERE bank_id = $1 AND $2 = ANY(source_memory_ids)", bank, old
    )
    assert still_citing == 0, "no valid observation cites the retired fact"
    assert str(model_id) in requested
    row = await conn.fetchrow(
        "SELECT * FROM public.ledger WHERE bank_id = $1 AND event = 'mental-model-refresh-requested'", bank
    )
    assert row["details"] and str(model_id) in row["details"]

    recalled = await cortana_client.post(
        f"/v1/default/banks/{bank}/memories/recall", json={"query": "Kestrel venue harbor", "budget": "low"}
    )
    assert recalled.status_code == 200, recalled.text
    assert str(old) not in {item["id"] for item in recalled.json()["results"]}
    got = await cortana_client.get(f"/v1/default/banks/{bank}/memories/{old}")
    assert got.status_code == 200, got.text
    assert got.json().get("state") == "invalidated"


# Criteria 16 and 17 (S11, the rule side) -----------------------------------------------------------


async def test_criterion_16_a_session_against_a_document_conflicts_until_the_edited_document_agrees(
    model, cortana_client, conn
):
    bank = _bank("c16")
    await _document(
        cortana_client, bank, "docs/kestrel.md", "Kestrel's host is alpha.", date="2026-10-01T12:00:00-04:00"
    )
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's host is beta.")], start="2026-10-02T10:00:00Z"
    )
    claims = await _claims(conn, bank)
    alpha, beta = claims["alpha"], claims["beta"]
    assert (alpha["state"], beta["state"]) == ("conflict", "conflict")
    assert await _live(conn, alpha["memory_unit_id"]) and await _live(conn, beta["memory_unit_id"])
    key = await conn.fetchrow(
        "SELECT conflict_claim_ids FROM public.attributes WHERE bank_id = $1 AND attribute_key = 'host'", bank
    )
    assert set(key["conflict_claim_ids"]) == {alpha["id"], beta["id"]}
    opened = await conn.fetchrow("SELECT * FROM public.ledger WHERE bank_id = $1 AND event = 'conflict-opened'", bank)
    assert opened["rule"] == "S11" and set(opened["claim_ids"]) == {alpha["id"], beta["id"]}

    await _document(
        cortana_client, bank, "docs/kestrel.md", "Kestrel's host is beta.", date="2026-10-03T12:00:00-04:00", revision=2
    )
    rows = await conn.fetch("SELECT * FROM public.claims WHERE bank_id = $1 AND attribute_key = 'host'", bank)
    claims = {(r["value_text"], r["source_kind"]): dict(r) for r in rows}
    assert set(claims) == {("beta", "session"), ("beta", "document")}, "the re-save's orphaned claim was swept"
    edited = claims[("beta", "document")]
    session_beta = next(c for c in claims.values() if c["source_kind"] == "session")
    assert (session_beta["state"], session_beta["superseded_by"], session_beta["superseded_rule"]) == (
        "superseded",
        edited["id"],
        "S3",
    )
    assert edited["state"] == "current"
    events = await _events(conn, bank)
    assert "conflict-closed" in events and "orphan-claims-swept" in events


async def test_criterion_17_a_decision_over_a_document_leaves_the_stale_marker_until_the_document_agrees(
    model, cortana_client, cortana_memory, conn
):
    bank = _bank("c17")
    await _document(
        cortana_client, bank, "docs/kestrel.md", "Kestrel's region is east.", date="2026-10-01T12:00:00-04:00"
    )
    east = (await _claims(conn, bank))["east"]
    (unit_ids,) = await cortana_memory.retain_batch_async(
        bank,
        [{"content": "Decision recorded about the Kestrel region.", "document_id": "decision:kestrel-region"}],
        request_context=RequestContext(),
    )
    await record_prestructured(
        cortana_memory,
        bank,
        uuid.UUID(unit_ids[0]),
        [PrestructuredClaim(subject="Kestrel", attribute="region", value="west")],
        datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
        request_context=RequestContext(),
    )
    claims = await _claims(conn, bank, "region")
    assert claims["west"]["state"] == "current" and claims["west"]["source_kind"] == "decision"
    assert claims["east"]["state"] == "superseded" and claims["east"]["superseded_rule"] == "S1"
    assert await _archived(conn, east["memory_unit_id"])
    stale = await conn.fetchval(
        "SELECT stale_documents FROM public.attributes WHERE bank_id = $1 AND attribute_key = 'region'", bank
    )
    assert stale == ["docs/kestrel.md"]
    assert "stale-document-marked" in await _events(conn, bank)

    await _document(
        cortana_client,
        bank,
        "docs/kestrel.md",
        "Kestrel's region is west.",
        date="2026-10-03T12:00:00-04:00",
        revision=2,
    )
    stale = await conn.fetchval(
        "SELECT stale_documents FROM public.attributes WHERE bank_id = $1 AND attribute_key = 'region'", bank
    )
    assert stale == []
    assert "stale-document-cleared" in await _events(conn, bank)


# Migration -----------------------------------------------------------------------------------------


async def test_the_migration_adds_the_supersession_columns_and_states(conn):
    columns = {
        (row["table_name"], row["column_name"])
        for row in await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('claims', 'attributes')"
        )
    }
    assert {
        ("claims", "keyed_as"),
        ("attributes", "alignment"),
        ("attributes", "conflict_claim_ids"),
        ("attributes", "stale_documents"),
    } <= columns
    check = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_claims_state'")
    assert "provisional" in check and "conflict" in check
