"""cortana tables: claims, attributes, ledger, retrievals (specification section 4.2)

The base of the ``cortana`` branch. It depends on the engine's head at v0.10.2 so it runs after
the engine's own tables exist, without making the engine's tree its parent.

Revision ID: 8b07c49ffe8e
Revises:
Create Date: 2026-10-07 16:18:52.820639

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from hindsight_api.alembic._dialect import run_for_dialect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8b07c49ffe8e"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = ("cortana",)
depends_on: str | Sequence[str] | None = "e5b1c7d3a902"

UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")


def _schema() -> str | None:
    """The tenant schema the engine is migrating, or None for its default."""
    return context.config.get_main_option("target_schema")


def _qualified(name: str) -> str:
    schema = _schema()
    return f'"{schema}".{name}' if schema else name


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
    ]


def _pg_upgrade() -> None:
    schema = _schema()

    # One row per claim a fact makes. memory_unit_id names the engine fact without a foreign key,
    # because retirement moves the fact to invalidated_memory_units; reconciliation sweeps rows
    # whose fact exists in neither table. The statement time is the sortable tuple
    # (stated_at, document_order, chunk_index, fact_ordinal, source_rank) of section 4.3.
    op.create_table(
        "claims",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("bank_id", sa.Text(), nullable=False),
        sa.Column("memory_unit_id", UUID, nullable=False),
        sa.Column("subject_entity_id", UUID, nullable=False),
        sa.Column("subject_text", sa.Text(), nullable=False),
        sa.Column("attribute_key", sa.Text(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("provisional", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("document_order", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("fact_ordinal", sa.Integer(), nullable=False),
        sa.Column("source_rank", sa.SmallInteger(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=True),
        sa.Column("chunk_id", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("superseded_by", UUID, nullable=True),
        sa.Column("superseded_rule", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "source_kind IN ('session', 'document', 'decision', 'correction')", name="ck_claims_source_kind"
        ),
        sa.CheckConstraint("state IN ('current', 'superseded', 'unaligned')", name="ck_claims_state"),
        schema=schema,
    )
    op.create_index("ix_claims_key", "claims", ["bank_id", "subject_entity_id", "attribute_key"], schema=schema)
    op.create_index("ix_claims_memory_unit", "claims", ["bank_id", "memory_unit_id"], schema=schema)
    op.create_index("ix_claims_document", "claims", ["bank_id", "document_id"], schema=schema)
    op.create_index("ix_claims_content_hash", "claims", ["bank_id", "content_hash"], schema=schema)
    op.create_index("ix_claims_superseded_by", "claims", ["superseded_by"], schema=schema)

    # The catalog of attribute keys per subject, shown to the structuring step so keys stay
    # stable. merged_into records an attribute merge (section 6.3).
    op.create_table(
        "attributes",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("bank_id", sa.Text(), nullable=False),
        sa.Column("subject_entity_id", UUID, nullable=False),
        sa.Column("attribute_key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("example_value", sa.Text(), nullable=True),
        sa.Column("merged_into", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("bank_id", "subject_entity_id", "attribute_key", name="uq_attributes_key"),
        schema=schema,
    )

    # Append-only: every supersession, retirement, restoration, attribute merge, structuring
    # failure and reconciliation outcome. The trigger below refuses UPDATE; DELETE stays possible
    # because the engine's bank deletion sweeps bank-scoped tables.
    op.create_table(
        "ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("bank_id", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("run_id", UUID, nullable=True),
        sa.Column("claim_ids", postgresql.ARRAY(UUID), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("memory_unit_ids", postgresql.ARRAY(UUID), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("actor IN ('hook', 'reconciliation', 'decision-tool', 'operator')", name="ck_ledger_actor"),
        schema=schema,
    )
    op.create_index("ix_ledger_bank_recorded", "ledger", ["bank_id", "recorded_at"], schema=schema)
    op.execute(
        f"""
        CREATE FUNCTION {_qualified("cortana_ledger_append_only")}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'cortana ledger is append-only: % refused', TG_OP;
        END
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER ledger_append_only BEFORE UPDATE ON {_qualified('ledger')} "
        f"FOR EACH ROW EXECUTE FUNCTION {_qualified('cortana_ledger_append_only')}()"
    )

    # What every recall and reflect returned (section 12): ranked ids with scores, and for
    # reflect its tool calls and the ids its answer cited.
    op.create_table(
        "retrievals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("bank_id", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("caller", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("results", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("cited_ids", postgresql.ARRAY(UUID), nullable=True),
        sa.CheckConstraint("kind IN ('recall', 'reflect')", name="ck_retrievals_kind"),
        schema=schema,
    )
    op.create_index("ix_retrievals_bank_recorded", "retrievals", ["bank_id", "recorded_at"], schema=schema)


def _pg_downgrade() -> None:
    schema = _schema()
    for table in ("retrievals", "ledger", "attributes", "claims"):
        op.drop_table(table, schema=schema)
    op.execute(f"DROP FUNCTION IF EXISTS {_qualified('cortana_ledger_append_only')}()")


def _oracle_unsupported() -> None:
    raise RuntimeError("hindsight-ext-cortana supports PostgreSQL only; its tables cannot be created on Oracle.")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_unsupported)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_unsupported)
