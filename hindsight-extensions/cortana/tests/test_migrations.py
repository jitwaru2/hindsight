"""Our migration branch runs in the engine's migration run and creates the four bank-scoped tables
(specification 4.2 and 13.1).

``pg0_db_url`` has already run the engine's ``run_migrations`` with our tenant extension
configured, exactly as server startup does, so these tests read the result.
"""

import uuid

import asyncpg
import pytest
from hindsight_api import RequestContext

from hindsight_ext_cortana.migrations import BRANCH, TABLES, VERSIONS_DIR, _scripts, head_revision
from hindsight_ext_cortana.tenant import CortanaTenantExtension

ENGINE_HEAD_AT_V0_10_2 = "e5b1c7d3a902"


@pytest.fixture
async def conn(pg0_db_url):
    connection = await asyncpg.connect(pg0_db_url)
    yield connection
    await connection.close()


def test_the_branch_is_its_own_tree_depending_on_the_engines_head():
    script = _scripts().get_revision(head_revision())
    base = script
    while base.down_revision is not None:
        base = _scripts().get_revision(base.down_revision)
    assert base.branch_labels == {BRANCH}
    assert base.dependencies == ENGINE_HEAD_AT_V0_10_2
    assert _scripts().get_revision(ENGINE_HEAD_AT_V0_10_2) is not None


def test_the_tenant_extension_names_the_branch_folder():
    assert CortanaTenantExtension.alembic_version_locations() == [str(VERSIONS_DIR)]


async def test_the_engine_run_recorded_our_head(conn):
    """Our head is in the engine's version table. Because it depends on the engine's head, Alembic
    records the engine's head through it rather than in a row of its own."""
    applied = {row["version_num"] for row in await conn.fetch("SELECT version_num FROM public.alembic_version")}
    assert head_revision() in applied
    assert ENGINE_HEAD_AT_V0_10_2 not in applied


async def test_the_four_tables_exist(conn):
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY($1)",
        list(TABLES),
    )
    assert {row["table_name"] for row in rows} == set(TABLES)


async def test_no_table_has_a_foreign_key_to_the_engine(conn):
    rows = await conn.fetch(
        """
        SELECT tc.table_name, ccu.table_name AS referenced
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu USING (constraint_schema, constraint_name)
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public' AND tc.table_name = ANY($1)
        """,
        list(TABLES),
    )
    assert [dict(row) for row in rows] == []


async def test_the_ledger_refuses_updates(conn):
    bank = f"cortana-ledger-{uuid.uuid4().hex[:8]}"
    row_id = await conn.fetchval(
        "INSERT INTO public.ledger (bank_id, event, actor) VALUES ($1, 'supersession', 'hook') RETURNING id", bank
    )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await conn.execute("UPDATE public.ledger SET reason = 'changed' WHERE id = $1", row_id)
    await conn.execute("DELETE FROM public.ledger WHERE bank_id = $1", bank)


async def test_the_engines_bank_deletion_sweeps_our_tables(cortana_memory, conn):
    """Declaring the tables bank-scoped is what makes ``delete_bank`` include them."""
    assert [table.name for table in cortana_memory.tenant_extension.extra_bank_tables()] == list(TABLES)

    doomed, kept = (f"cortana-scope-{uuid.uuid4().hex[:8]}" for _ in range(2))
    context = RequestContext()
    for bank in (doomed, kept):
        await cortana_memory.retain_async(bank, "Synthetic note for a bank-scope test.", request_context=context)
        await _insert_one_row_per_table(conn, bank)

    # The retain's own hook may have written rows too (structuring's claims or ledger entries).
    before = {
        table: await conn.fetchval(f"SELECT count(*) FROM public.{table} WHERE bank_id = $1", kept) for table in TABLES
    }
    assert all(count >= 1 for count in before.values())

    await cortana_memory.delete_bank(doomed, request_context=context)

    for table in TABLES:
        assert await conn.fetchval(f"SELECT count(*) FROM public.{table} WHERE bank_id = $1", doomed) == 0, table
        assert await conn.fetchval(f"SELECT count(*) FROM public.{table} WHERE bank_id = $1", kept) == before[table]
    await cortana_memory.delete_bank(kept, request_context=context)


async def _insert_one_row_per_table(conn, bank: str) -> None:
    await conn.execute(
        """
        INSERT INTO public.claims (bank_id, memory_unit_id, subject_entity_id, subject_text, attribute_key,
            value_text, stated_at, document_order, chunk_index, fact_ordinal, source_rank, source_kind, state,
            content_hash)
        VALUES ($1, gen_random_uuid(), gen_random_uuid(), 'Example subject', 'example-attribute', 'a value',
            now(), 0, 0, 0, 3, 'session', 'current', 'hash')
        """,
        bank,
    )
    await conn.execute(
        "INSERT INTO public.attributes (bank_id, subject_entity_id, attribute_key, description) "
        "VALUES ($1, gen_random_uuid(), 'example-attribute', 'An example attribute')",
        bank,
    )
    await conn.execute("INSERT INTO public.ledger (bank_id, event, actor) VALUES ($1, 'supersession', 'hook')", bank)
    await conn.execute("INSERT INTO public.retrievals (bank_id, kind, query) VALUES ($1, 'recall', 'q')", bank)
