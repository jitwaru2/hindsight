"""supersession state: claim states, keyed_as, key alignment and conflicts

What the supersession rules (HSIGHT-5, specification 6) keep in our tables:

- ``claims.state`` gains ``provisional`` (a valid provisional claim no later claim has superseded,
  rule S2) and ``conflict`` (a valid claim in an S11 conflict). ``current`` is the key's one current
  claim; ``unaligned`` a claim on a key pending alignment (S9); ``superseded`` as before.
- ``claims.keyed_as``: the key the structuring step named for the claim, before any attribute merge
  moved it, so a merge can be reversed exactly (specification 6.3). Existing rows take their key.
- ``attributes.alignment``: ``pending`` (a key created for a subject that already had keys, or for a
  subject that resolves to no entity; its claims are unaligned until the alignment pass or a merge
  resolves it), ``aligned`` or ``merged`` (``merged_into`` names the key). Alias rows the structuring
  step recorded become ``merged``; keys with unaligned claims become ``pending``.
- ``attributes.conflict_claim_ids`` and ``attributes.stale_documents``: the key's S11 state as the
  rules last computed it, so the current-state read (HSIGHT-6) can report it and the rules write a
  ledger entry only when it changes (S10).

Revision ID: 6e9bc963e6f1
Revises: fafacc9c15dd
Create Date: 2026-10-07 20:30:30.514096

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from hindsight_api.alembic._dialect import run_for_dialect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "6e9bc963e6f1"
down_revision: str | Sequence[str] | None = "fafacc9c15dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
CLAIM_STATES = ("current", "superseded", "unaligned", "provisional", "conflict")
OLD_CLAIM_STATES = ("current", "superseded", "unaligned")
ALIGNMENTS = ("pending", "aligned", "merged")


def _schema() -> str | None:
    """The tenant schema the engine is migrating, or None for its default."""
    return context.config.get_main_option("target_schema")


def _qualified(name: str) -> str:
    schema = _schema()
    return f'"{schema}".{name}' if schema else name


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _pg_upgrade() -> None:
    schema = _schema()
    op.drop_constraint("ck_claims_state", "claims", type_="check", schema=schema)
    op.create_check_constraint("ck_claims_state", "claims", _in("state", CLAIM_STATES), schema=schema)
    op.add_column("claims", sa.Column("keyed_as", sa.Text(), nullable=True), schema=schema)
    op.execute(f"UPDATE {_qualified('claims')} SET keyed_as = attribute_key WHERE keyed_as IS NULL")

    op.add_column(
        "attributes",
        sa.Column("alignment", sa.Text(), server_default="aligned", nullable=False),
        schema=schema,
    )
    op.create_check_constraint("ck_attributes_alignment", "attributes", _in("alignment", ALIGNMENTS), schema=schema)
    op.add_column(
        "attributes",
        sa.Column("conflict_claim_ids", postgresql.ARRAY(UUID), server_default=sa.text("'{}'"), nullable=False),
        schema=schema,
    )
    op.add_column(
        "attributes",
        sa.Column("stale_documents", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        schema=schema,
    )
    op.execute(f"UPDATE {_qualified('attributes')} SET alignment = 'merged' WHERE merged_into IS NOT NULL")
    op.execute(
        f"""
        UPDATE {_qualified("attributes")} a SET alignment = 'pending'
        WHERE a.merged_into IS NULL AND EXISTS (
            SELECT 1 FROM {_qualified("claims")} c
            WHERE c.bank_id = a.bank_id AND c.subject_entity_id = a.subject_entity_id
              AND c.attribute_key = a.attribute_key AND c.state = 'unaligned')
        """
    )
    op.create_index(
        "ix_attributes_pending",
        "attributes",
        ["bank_id", "subject_entity_id"],
        schema=schema,
        postgresql_where=sa.text("alignment = 'pending'"),
    )


def _pg_downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_attributes_pending", "attributes", schema=schema)
    for column in ("stale_documents", "conflict_claim_ids"):
        op.drop_column("attributes", column, schema=schema)
    op.drop_constraint("ck_attributes_alignment", "attributes", type_="check", schema=schema)
    op.drop_column("attributes", "alignment", schema=schema)
    op.drop_column("claims", "keyed_as", schema=schema)
    op.execute(f"UPDATE {_qualified('claims')} SET state = 'current' WHERE state IN ('provisional', 'conflict')")
    op.drop_constraint("ck_claims_state", "claims", type_="check", schema=schema)
    op.create_check_constraint("ck_claims_state", "claims", _in("state", OLD_CLAIM_STATES), schema=schema)


def _oracle_unsupported() -> None:
    raise RuntimeError("hindsight-ext-cortana supports PostgreSQL only; its tables cannot be created on Oracle.")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_unsupported)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_unsupported)
