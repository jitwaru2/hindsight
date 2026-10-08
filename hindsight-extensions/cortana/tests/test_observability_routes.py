"""``GET /ext/cortana/ledger`` and ``GET /ext/cortana/status`` answer their contracts (specification
9 and 12). Rows are written directly, so nothing calls a model. All content is synthetic.
"""

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from hindsight_api import RequestContext

from hindsight_ext_cortana import ledger, status
from hindsight_ext_cortana.migrations import TABLES, head_revision
from hindsight_ext_cortana.reconcile import unstructured_facts


@pytest.fixture
async def conn(pg0_db_url):
    connection = await asyncpg.connect(pg0_db_url)
    yield connection
    await connection.close()


@pytest.fixture(autouse=True)
def clean_home(tmp_path, monkeypatch):
    """A home folder with no .env, unless a test writes one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _bank(name: str) -> str:
    return f"cortana-obs-{name}-{uuid.uuid4().hex[:8]}"


async def _fact(conn, bank: str, text: str, *, document: str | None = None) -> uuid.UUID:
    fact_id = uuid.uuid4()
    if document:
        await conn.execute(
            "INSERT INTO public.documents (id, bank_id, original_text) VALUES ($1, $2, '') ON CONFLICT DO NOTHING",
            document,
            bank,
        )
    await conn.execute(
        "INSERT INTO public.memory_units (id, bank_id, text, event_date, fact_type, document_id) "
        "VALUES ($1, $2, $3, now(), 'world', $4)",
        fact_id,
        bank,
        text,
        document,
    )
    return fact_id


async def _claim(conn, bank: str, fact_id, subject, subject_text: str, key: str, *, state="current", document=None):
    return await conn.fetchval(
        """
        INSERT INTO public.claims (bank_id, memory_unit_id, subject_entity_id, subject_text, attribute_key, keyed_as,
            value_text, stated_at, document_order, chunk_index, fact_ordinal, source_rank, source_kind, state,
            content_hash, document_id)
        VALUES ($1, $2, $3, $4, $5, $5, 'v', now(), 0, 0, 0, 3, 'session', $6, 'h', $7)
        RETURNING id
        """,
        bank,
        fact_id,
        subject,
        subject_text,
        key,
        state,
        document,
    )


async def _entity(conn, bank: str, name: str) -> uuid.UUID:
    entity_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO public.entities (id, bank_id, canonical_name) VALUES ($1, $2, $3)", entity_id, bank, name
    )
    return entity_id


# Ledger -------------------------------------------------------------------------------------------------


async def test_the_ledger_route_filters_by_subject_attribute_fact_document_and_time(cortana_client, conn):
    bank = _bank("ledger")
    lee = await _entity(conn, bank, "Lee")
    other = await _entity(conn, bank, "Kestrel")
    f1 = await _fact(conn, bank, "Lee's editor is vim.", document="notes/lee.md")
    f2 = await _fact(conn, bank, "Kestrel's office is Birch.")
    f3 = await _fact(conn, bank, "Unstructured fact.", document="notes/other.md")
    c1 = await _claim(conn, bank, f1, lee, "Lee", "editor", document="notes/lee.md")
    c2 = await _claim(conn, bank, f2, other, "Kestrel", "office")
    swept_subject = uuid.uuid4()
    entries = [
        ledger.Entry("structured", reason="claims", claim_ids=[c1], memory_unit_ids=[f1]),
        ledger.Entry("fact-retired", reason="retired", memory_unit_ids=[f2]),
        ledger.Entry("conflict-opened", rule="S11", details={"subject_entity_id": str(lee), "key": "editor"}),
        ledger.Entry("structuring-pending", reason="call failed", memory_unit_ids=[f3]),
        ledger.Entry(
            "orphan-claims-swept",
            details={
                "claims": [
                    {"subject_entity_id": str(swept_subject), "attribute_key": "desk", "document_id": "notes/gone.md"}
                ]
            },
        ),
    ]
    await ledger.append(conn, bank, entries, actor="hook", run_id=None)
    _ = c2

    async def events(**params) -> list[str]:
        response = await cortana_client.get("/ext/cortana/ledger", params={"bank_id": bank, **params})
        assert response.status_code == 200, response.text
        return [item["event"] for item in response.json()["items"]]

    assert await events() == [e.event for e in reversed(entries)]
    assert await events(subject="lee") == ["conflict-opened", "structured"]
    assert await events(subject=str(lee), attribute="editor") == ["conflict-opened", "structured"]
    assert await events(attribute="office") == ["fact-retired"]
    assert await events(fact_id=str(f2)) == ["fact-retired"]
    assert await events(document="notes/lee.md") == ["structured"]
    assert await events(document="notes/other.md") == ["structuring-pending"]
    assert await events(document="notes/gone.md") == ["orphan-claims-swept"]
    assert await events(subject=str(swept_subject), attribute="desk") == ["orphan-claims-swept"]
    assert await events(limit=2) == ["orphan-claims-swept", "structuring-pending"]
    assert await events(since=(datetime.now(UTC) + timedelta(minutes=1)).isoformat()) == []

    unknown = await cortana_client.get("/ext/cortana/ledger", params={"bank_id": bank, "subject": "Nobody"})
    assert unknown.json() == {"subject_ids": [], "items": []}


# Status -------------------------------------------------------------------------------------------------


async def test_status_reports_every_section_for_a_bank(cortana_client, cortana_memory, conn, monkeypatch):
    bank = _bank("status")
    subject = await _entity(conn, bank, "Lee")
    claimed = await _fact(conn, bank, "Lee's editor is vim.")
    answered = await _fact(conn, bank, "A fact the model answered with no claim.")
    await _fact(conn, bank, "A fact never structured.")
    await _claim(conn, bank, claimed, subject, "Lee", "editor", state="unaligned")
    await conn.execute(
        "INSERT INTO public.attributes (bank_id, subject_entity_id, attribute_key, description, alignment, "
        "conflict_claim_ids, stale_documents) VALUES ($1, $2, 'editor', 'd', 'pending', ARRAY[gen_random_uuid()], "
        "ARRAY['notes/a.md', 'notes/b.md'])",
        bank,
        subject,
    )
    retired = uuid.uuid4()
    await ledger.append(
        conn,
        bank,
        [
            ledger.Entry("structured", memory_unit_ids=[answered]),
            ledger.Entry("fact-retired", memory_unit_ids=[retired]),
            ledger.Entry("fact-restored", memory_unit_ids=[uuid.uuid4()]),
            ledger.Entry("claim-superseded", rule="S1"),
            ledger.Entry("reconciled", reason="reconciled", details={"settled": {"keys": 3}}),
        ],
        actor="reconciliation",
        run_id=uuid.uuid4(),
    )
    await conn.execute("INSERT INTO public.retrievals (bank_id, kind, query) VALUES ($1, 'recall', 'q')", bank)

    response = await cortana_client.get("/ext/cortana/status", params={"bank_id": bank})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bank_id"] == bank and body["problems"] == []
    assert body["migrations"]["current"] is True and body["migrations"]["applied"] == head_revision()
    assert body["tables"] == {table: True for table in TABLES}
    async with (await cortana_memory._get_pool()).acquire() as c:
        pending = await unstructured_facts(c, bank)
    assert body["structuring"] == {"facts_without_claims": 2, "pending_structuring": len(pending)} and len(pending) == 1
    assert body["supersession"] == {
        "unaligned_claims": 1,
        "keys_pending_alignment": 1,
        "keys_in_conflict": 1,
        "stale_document_markers": 2,
    }
    assert body["last_reconciliation"]["summary"] == {"settled": {"keys": 3}}
    assert body["last_reconciliation"]["actor"] == "reconciliation"
    assert body["gate"] is None
    day = body["last_day"]
    assert (day["facts_retired"], day["facts_restored"], day["claims_superseded"]) == (1, 1, 1)
    assert day["retired_fact_ids"] == [str(retired)]
    assert body["worker"]["mode"] == "in-process" and body["worker"]["healthy"] is None
    assert body["worker"]["queue"]["pending"] >= 0
    assert body["retrievals"]["retention_days"] == 30 and body["retrievals"]["rows"] == 1
    assert body["home_env"]["ok"] is True and body["home_env"]["exists"] is False


async def test_status_names_a_hindsight_key_in_the_home_env_without_its_value(cortana_client, clean_home):
    (clean_home / ".env").write_text("OTHER=1\nexport HINDSIGHT_API_PORT=9999\nHINDSIGHT_API_LLM_API_KEY=secret\n")
    body = (await cortana_client.get("/ext/cortana/status")).json()
    assert body["home_env"]["hindsight_keys"] == ["HINDSIGHT_API_LLM_API_KEY", "HINDSIGHT_API_PORT"]
    assert body["home_env"]["ok"] is False
    assert any(".env defines HINDSIGHT_API_LLM_API_KEY" in p for p in body["problems"])
    assert "secret" not in str(body)


async def test_status_reports_an_unreachable_separate_worker(cortana_client, monkeypatch):
    monkeypatch.setattr(status, "worker_mode", lambda: "separate")
    monkeypatch.setattr(status, "worker_probe_url", lambda: "http://127.0.0.1:9/health/live")
    body = (await cortana_client.get("/ext/cortana/status")).json()
    worker = body["worker"]
    assert worker["mode"] == "separate" and worker["healthy"] is False
    assert worker["process"]["reachable"] is False and worker["process"]["error"]
    assert any("separate worker did not answer" in p for p in body["problems"])


async def test_status_flags_retrieval_rows_past_retention(cortana_client, conn):
    bank = _bank("overdue")
    await conn.execute(
        "INSERT INTO public.retrievals (bank_id, kind, query, recorded_at) "
        "VALUES ($1, 'recall', 'old', now() - interval '45 days')",
        bank,
    )
    body = (await cortana_client.get("/ext/cortana/status", params={"bank_id": bank})).json()
    assert body["retrievals"]["rows_past_retention"] == 1
    assert any("the sweep has not run" in p for p in body["problems"])
    await conn.execute("DELETE FROM public.retrievals WHERE bank_id = $1", bank)


async def test_the_cli_and_the_route_build_the_same_status(cortana_client, cortana_memory):
    """``hindsight-cortana status`` prints ``build_status``'s model; the route returns the same one."""
    schema = (await cortana_memory.tenant_extension.authenticate(RequestContext())).schema_name
    built = await status.build_status(await cortana_memory._get_pool(), schema)
    routed = (await cortana_client.get("/ext/cortana/status")).json()
    assert set(routed) == set(built.model_dump(mode="json"))
    assert routed["database_schema"] == built.database_schema
