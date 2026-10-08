"""The current-state read, decision capture, pool scoping and the /mcp tool filter (HSIGHT-6), with no
model calls (specification 7.3, 9, 10.1; acceptance criteria 2, 16 and 17).

The engine runs with its ``mock`` provider and tasks inline, and structuring is answered by the
``model`` fixture of ``test_supersession_engine`` ("<Subject>'s <attribute> is <value>" is a claim).
Decision records go through ``POST /ext/cortana/decisions`` and the ``cortana_record_decision`` tool,
which retain under the ``chunks`` strategy, so a structuring call for a decision document would show
in the fixture's count. All content is synthetic.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastmcp import Client
from hindsight_api import RequestContext

from hindsight_ext_cortana import pools
from hindsight_ext_cortana.hooks import CortanaOperationHooks
from hindsight_ext_cortana.mcp import TOOLS
from hindsight_ext_cortana.structuring.engine import PrestructuredClaim, record_prestructured

from test_supersession_engine import (  # noqa: F401  (fixtures)
    _archived,
    _bank,
    _claims,
    _document,
    _events,
    _ledger_count,
    _live,
    _session,
    conn,
    model,
)

DOC = "docs/kestrel.md"


async def _decide(client, bank: str, **fields) -> dict:
    body = {"subject": "Kestrel", "attribute": "region", "domain": "work", "session_id": "s-1", **fields}
    response = await client.post("/ext/cortana/decisions", params={"bank_id": bank}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _current(client, bank: str, subject: str, attribute: str | None = None) -> dict:
    params = {"bank_id": bank, "subject": subject} | ({"attribute": attribute} if attribute else {})
    response = await client.get("/ext/cortana/current", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _tags(conn, bank: str, document: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT tags, observation_scopes FROM public.memory_units WHERE bank_id = $1 AND document_id = $2",
        bank,
        document,
    )
    assert rows, f"no facts for {document}"
    return sorted({tag for row in rows for tag in row["tags"]})


# Pool scoping (specification 7.3; COR-18) -------------------------------------------------------------


def test_a_document_without_a_pool_gets_its_domains_pool_or_the_default():
    assert pools.pool_for(["domain:recovery", "source:upload"]) == "recovery"
    assert pools.pool_for(["domain:finance"]) == "general"
    assert pools.pool_for([]) == "general"
    item = {"content": "x", "tags": ["domain:work"], "observation_scopes": "shared"}
    assert pools.scope_item(item) == {
        "content": "x",
        "tags": ["domain:work", "pool:work"],
        "observation_scopes": [["pool:work"]],
    }
    assert pools.scope_item({"content": "x", "tags": ["domain:work", "pool:recovery"]}) is None
    assert pools.scope_contents([{"content": "x", "tags": ["pool:work"]}]) is None


async def test_validate_retain_scopes_untagged_corrections_and_ingested_documents_and_leaves_plugin_saves(
    model, cortana_client, conn
):
    bank = _bank("pools")
    correction = {
        "content": "Correction: Kestrel's region is north, not south.",
        "document_id": "correction:kestrel-region",
        "tags": ["domain:recovery"],
    }
    ingested = {
        "content": "Kestrel's budget is 40k.",
        "document_id": "upload:kestrel-notes",
        "tags": ["source:upload"],
        "observation_scopes": "shared",
    }
    response = await cortana_client.post(f"/v1/default/banks/{bank}/memories", json={"items": [correction, ingested]})
    assert response.status_code == 200, response.text
    assert await _tags(conn, bank, "correction:kestrel-region") == ["domain:recovery", "pool:recovery"]
    assert await _tags(conn, bank, "upload:kestrel-notes") == ["pool:general", "source:upload"]
    scopes = await conn.fetchval(
        "SELECT observation_scopes FROM public.memory_units WHERE bank_id = $1 AND document_id = $2 LIMIT 1",
        bank,
        "upload:kestrel-notes",
    )
    assert "pool:general" in str(scopes)

    session = await _session(
        cortana_client,
        bank,
        [("2026-10-02T10:00:00Z", "Kestrel's host is beta.")],
        start="2026-10-02T10:00:00Z",
        tags=["domain:work", "pool:work"],
    )
    assert await _tags(conn, bank, f"conversation:{session}") == sorted(
        ["source:chat", "harness:claude-code", "domain:work", "pool:work"]
    ), "a save that carries its pool is left exactly as it came"


# Decision capture: criterion 2 --------------------------------------------------------------------------


async def test_criterion_2_a_decision_is_current_within_the_call_supersedes_and_survives_the_sessions_own_save(
    model, cortana_client, conn
):
    bank = _bank("c2")
    await _session(
        cortana_client, bank, [("2026-09-25T14:00:00Z", "Kestrel's region is east.")], start="2026-09-25T14:00:00Z"
    )
    east = (await _claims(conn, bank, "region"))["east"]
    calls = model["structuring"]
    words = "Kestrel's region is west. That is decided."

    first = await _decide(cortana_client, bank, words=words, value="west", stated_at="2026-10-07T15:00:00Z")
    assert model["structuring"] == calls, "a decision record is never structured by the model"
    assert first["recorded"] is True and first["state"] == "current" and first["document_id"].startswith("decision:")
    assert first["message"].startswith(f"Recorded as fact {first['fact_id']} on Kestrel / region")
    assert 'it replaces the 2026-09-25 position ("east")' in first["message"]
    assert [s["claim_id"] for s in first["superseded"]] == [str(east["id"])]

    claims = await _claims(conn, bank, "region")
    assert claims["west"]["source_kind"] == "decision" and claims["west"]["state"] == "current"
    assert claims["west"]["stated_at_source"] == "decision"
    assert (claims["east"]["state"], claims["east"]["superseded_rule"]) == ("superseded", "S1")
    assert await _archived(conn, east["memory_unit_id"])
    text = await conn.fetchval("SELECT text FROM public.memory_units WHERE id = $1", claims["west"]["memory_unit_id"])
    assert text == words, "the fact is Josh's words verbatim"
    assert await _tags(conn, bank, first["document_id"]) == ["domain:work", "pool:work", "source:decision"]
    events = await _events(conn, bank)
    assert "decision-strategy-configured" in events and events[-1] == "decision-recorded"

    # Idempotent: the same call records nothing new.
    before = (
        await _ledger_count(conn, bank),
        await conn.fetchval("SELECT count(*) FROM public.memory_units WHERE bank_id = $1", bank),
    )
    again = await _decide(cortana_client, bank, words=words, value="west", stated_at="2026-10-07T15:00:00Z")
    assert again["recorded"] is False and again["fact_id"] == first["fact_id"]
    assert again["message"].startswith("Already recorded")
    after = (
        await _ledger_count(conn, bank),
        await conn.fetchval("SELECT count(*) FROM public.memory_units WHERE bank_id = $1", bank),
    )
    assert after == before

    # The session's own save of the same words, in a turn later than the decision's time.
    await _session(
        cortana_client,
        bank,
        [("2026-10-07T15:02:00Z", "Kestrel's region is west.")],
        start="2026-10-07T14:55:00Z",
    )
    rows = await conn.fetch("SELECT * FROM public.claims WHERE bank_id = $1 AND attribute_key = 'region'", bank)
    restated = next(r for r in rows if r["source_kind"] == "session" and r["value_text"] == "west")
    assert (restated["state"], restated["superseded_by"], restated["superseded_rule"]) == (
        "superseded",
        claims["west"]["id"],
        "S3",
    )
    read = await _current(cortana_client, bank, "Kestrel", "region")
    (key,) = read["keys"]
    assert key["status"] == "current" and key["current"]["claim_id"] == str(claims["west"]["id"])
    assert key["current"]["text"] == words and key["current"]["source"] == "decision"
    assert [(h["value"], h["source"], h["rule"]) for h in key["history"]] == [
        ("west", "session", "S3"),
        ("east", "session", "S1"),
    ]
    assert f'Josh\'s words: "{words}"' in key["summary"]
    assert 'previous position "east" (session, 2026-09-25 10:00 EDT)' in key["summary"]


async def test_a_decision_on_a_new_key_of_a_known_subject_is_pending_alignment(model, cortana_client, conn):
    bank = _bank("newkey")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's region is east.")], start="2026-10-01T10:00:00Z"
    )
    result = await _decide(
        cortana_client,
        bank,
        words="We cap Kestrel at forty.",
        attribute="Funding cap",
        value="40k",
        stated_at="2026-10-02T10:00:00Z",
    )
    assert result["attribute"] == "funding-cap" and result["state"] == "unaligned"
    assert "a new key on this subject, pending alignment, so it replaces nothing yet" in result["message"]
    read = await _current(cortana_client, bank, "Kestrel", "funding cap")
    assert read["keys"][0]["status"] == "unaligned"


async def test_a_decision_strategy_that_is_not_verbatim_is_refused(cortana_memory, cortana_client):
    bank = _bank("strategy")
    await cortana_memory._ensure_bank_exists(bank, RequestContext())
    await cortana_memory._config_resolver.update_bank_config(
        bank, {"retain_strategies": {"decision": {"retain_extraction_mode": "concise"}}}, RequestContext()
    )
    response = await cortana_client.post(
        "/ext/cortana/decisions",
        params={"bank_id": bank},
        json={"words": "w", "subject": "Kestrel", "attribute": "region", "value": "west"},
    )
    assert response.status_code == 409 and "extraction mode" in response.json()["detail"]
    missing = await cortana_client.post(
        "/ext/cortana/decisions",
        params={"bank_id": bank},
        json={"words": " ", "subject": "K", "attribute": "r", "value": "v"},
    )
    assert missing.status_code == 422 and "missing words" in missing.json()["detail"]


# The current-state read: criteria 16 and 17 ---------------------------------------------------------------


async def test_criterion_16_through_the_read_a_conflict_reports_both_claims_until_the_edited_document_agrees(
    model, cortana_client, conn
):
    bank = _bank("c16r")
    await _document(cortana_client, bank, DOC, "Kestrel's host is alpha.", date="2026-10-01T12:00:00-04:00")
    await _session(
        cortana_client, bank, [("2026-10-02T10:00:00Z", "Kestrel's host is beta.")], start="2026-10-02T10:00:00Z"
    )
    (key,) = (await _current(cortana_client, bank, "Kestrel", "host"))["keys"]
    assert key["status"] == "conflict" and key["current"] is None
    assert [(c["value"], c["source"]) for c in key["conflict"]] == [("beta", "session"), ("alpha", "document")]
    assert all(c["text"] and c["stated_at"] for c in key["conflict"])
    assert key["conflict"][1]["document_id"] == DOC
    assert "in conflict" in key["summary"] and "tell Josh which says what" in key["summary"]

    await _document(cortana_client, bank, DOC, "Kestrel's host is beta.", date="2026-10-03T12:00:00-04:00", revision=2)
    (key,) = (await _current(cortana_client, bank, "Kestrel", "host"))["keys"]
    assert key["status"] == "current" and key["conflict"] == []
    assert (key["current"]["value"], key["current"]["source"]) == ("beta", "document")
    assert ("beta", "session", "S3") in [(h["value"], h["source"], h["rule"]) for h in key["history"]]


async def test_criterion_17_through_the_read_a_decision_leaves_the_stale_marker_until_the_document_agrees(
    model, cortana_client, conn
):
    bank = _bank("c17r")
    await _document(cortana_client, bank, DOC, "Kestrel's region is east.", date="2026-10-01T12:00:00-04:00")
    decided = await _decide(
        cortana_client, bank, words="Kestrel goes west.", value="west", stated_at="2026-10-02T15:00:00Z"
    )
    assert f'{DOC} still says "east"' in decided["message"]
    (key,) = (await _current(cortana_client, bank, "Kestrel", "region"))["keys"]
    assert key["status"] == "current" and key["current"]["value"] == "west"
    assert [(s["document_id"], s["says"]["value"]) for s in key["stale_documents"]] == [(DOC, "east")]
    assert f'document {DOC} still says "east"' in key["summary"]

    await _document(
        cortana_client, bank, DOC, "Kestrel's region is west.", date="2026-10-03T12:00:00-04:00", revision=2
    )
    (key,) = (await _current(cortana_client, bank, "Kestrel", "region"))["keys"]
    assert key["stale_documents"] == []
    assert key["current"]["source"] == "decision", "the S3 exception keeps the decision record current"
    assert ("west", "document", "S3") in [(h["value"], h["source"], h["rule"]) for h in key["history"]]
    assert "stale-document-cleared" in await _events(conn, bank)


# The read's other answers ------------------------------------------------------------------------------


async def test_no_position_recorded_is_explicit_and_provisional_statements_are_reported(model, cortana_client, conn):
    bank = _bank("read")
    await _session(
        cortana_client,
        bank,
        [
            ("2026-10-01T10:00:00Z", "Kestrel's budget is 40k."),
            ("2026-10-01T10:05:00Z", "Proposed: Kestrel's budget is 50k."),
        ],
        start="2026-10-01T10:00:00Z",
    )
    nobody = await _current(cortana_client, bank, "Nobody Atall")
    assert nobody["keys"] == [] and nobody["summary"] == "No position recorded: no subject matches 'Nobody Atall'."
    (key,) = (await _current(cortana_client, bank, "kestrel", "venue"))["keys"]
    assert key["status"] == "no-position" and key["summary"] == "Kestrel / venue: no position recorded"
    whole = await _current(cortana_client, bank, "Kestrel")
    (budget,) = [k for k in whole["keys"] if k["attribute"] == "budget"]
    assert budget["current"]["value"] == "40k"
    assert [p["value"] for p in budget["later_provisional"]] == ["50k"]
    assert "later provisional statement" in budget["summary"]


async def test_subjects_lists_keys_and_facts_lists_claims(model, cortana_client, cortana_memory, conn):
    bank = _bank("subjects")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's region is east.")], start="2026-10-01T10:00:00Z"
    )
    response = await cortana_client.get("/ext/cortana/subjects", params={"bank_id": bank, "q": "kest"})
    assert response.status_code == 200, response.text
    (match,) = [m for m in response.json()["items"] if m["name"] == "Kestrel"]
    assert match["resolved"] is True
    assert [(k["attribute"], k["status"], k["value"]) for k in match["keys"]] == [("region", "current", "east")]

    east = (await _claims(conn, bank, "region"))["east"]
    response = await cortana_client.get(f"/ext/cortana/facts/{east['memory_unit_id']}", params={"bank_id": bank})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fact_valid"] is True and [c["value"] for c in body["claims"]] == ["east"]
    missing = await cortana_client.get(f"/ext/cortana/facts/{uuid.uuid4()}", params={"bank_id": bank})
    assert missing.status_code == 404

    # A name that resolves to no entity is a synthetic subject: matched by name, never a settled position.
    (unit_ids,) = await cortana_memory.retain_batch_async(
        bank,
        [{"content": "A note about Zephyr Quill.", "document_id": "notes:zq", "strategy": "decision"}],
        request_context=RequestContext(),
        strategy="decision",
    )
    await record_prestructured(
        cortana_memory,
        bank,
        uuid.UUID(unit_ids[0]),
        [PrestructuredClaim(subject="Zephyr Quill", attribute="status", value="paused")],
        datetime(2026, 10, 2, tzinfo=UTC),
        request_context=RequestContext(),
    )
    read = await _current(cortana_client, bank, "zephyr quill")
    assert [s["resolved"] for s in read["subjects"]] == [False]
    assert read["keys"][0]["status"] == "unaligned" and "no settled position" in read["keys"][0]["summary"]


# The /mcp endpoint: exactly our three tools ------------------------------------------------------------


async def test_filter_mcp_tools_returns_exactly_our_tools():
    hooks = CortanaOperationHooks.__new__(CortanaOperationHooks)
    narrowed = await hooks.filter_mcp_tools("any", RequestContext(), frozenset({"retain", "delete_bank", "recall"}))
    assert narrowed == TOOLS == {"cortana_current", "cortana_subjects", "cortana_record_decision"}


@pytest.mark.parametrize("multi_bank", [False, True])
async def test_the_mcp_endpoint_lists_and_runs_only_our_tools(model, cortana_memory, cortana_client, multi_bank):
    from hindsight_api.api import mcp as engine_mcp

    bank = _bank("mcp")
    await _session(
        cortana_client, bank, [("2026-10-01T10:00:00Z", "Kestrel's region is east.")], start="2026-10-01T10:00:00Z"
    )
    server = engine_mcp.create_mcp_server(cortana_memory, multi_bank=multi_bank)
    token = engine_mcp._current_bank_id.set(bank)
    try:
        async with Client(server) as client:
            assert sorted(tool.name for tool in await client.list_tools()) == sorted(TOOLS)
            with pytest.raises(Exception, match="delete_bank|Unknown tool|not found"):
                await client.call_tool("delete_bank", {})
            read = await client.call_tool("cortana_current", {"subject": "Kestrel", "attribute": "region"})
            assert read.structured_content["keys"][0]["current"]["value"] == "east"
            line = await client.call_tool(
                "cortana_record_decision",
                {
                    "words": "Kestrel moves west.",
                    "subject": "Kestrel",
                    "attribute": "region",
                    "value": "west",
                    "stated_at": "2026-10-02T10:00:00Z",
                    "domain": "work",
                },
            )
            assert line.data.startswith("Recorded as fact") and '("east")' in line.data
            found = await client.call_tool("cortana_subjects", {"q": "Kestrel"})
            assert found.structured_content["items"][0]["keys"][0]["value"] == "west"
    finally:
        engine_mcp._current_bank_id.reset(token)
