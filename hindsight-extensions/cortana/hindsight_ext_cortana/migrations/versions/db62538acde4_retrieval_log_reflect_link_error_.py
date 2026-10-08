"""retrieval log: reflect link, error, parameters, cited mental models

What the retrieval log (HSIGHT-7, specification 12) keeps beyond HSIGHT-2's ``retrievals`` table:

- ``reflect_id``: on a reflect row, the reflect's own id; on a recall row, the id of the reflect it
  ran inside (reflect's tool recalls), so a reflect's internal recalls and their scores are one
  lookup away from the reflect. Null for a recall made outside a reflect.
- ``error``: the engine's error text for a recall that failed (the engine calls the post-recall hook
  for failures too); null on success.
- ``parameters``: the request's retrieval parameters as the hook receives them (fact types, budget,
  token limits, question date), so a row says what was asked for as well as what came back.
- ``cited_mental_model_ids``: the mental models a reflect's answer cited. Mental-model ids are text,
  not uuids, so they cannot share ``cited_ids``.

Indexes for the routes' filters and the retention sweep: (bank, kind, time), the reflect link, and
time alone for a sweep across every bank.

Revision ID: db62538acde4
Revises: 6e9bc963e6f1
Create Date: 2026-10-07 21:11:28.184576

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from hindsight_api.alembic._dialect import run_for_dialect
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "db62538acde4"
down_revision: str | Sequence[str] | None = "6e9bc963e6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _schema() -> str | None:
    """The tenant schema the engine is migrating, or None for its default."""
    return context.config.get_main_option("target_schema")


def _pg_upgrade() -> None:
    schema = _schema()
    op.add_column("retrievals", sa.Column("reflect_id", UUID, nullable=True), schema=schema)
    op.add_column("retrievals", sa.Column("error", sa.Text(), nullable=True), schema=schema)
    op.add_column(
        "retrievals",
        sa.Column("parameters", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        schema=schema,
    )
    op.add_column(
        "retrievals", sa.Column("cited_mental_model_ids", postgresql.ARRAY(sa.Text()), nullable=True), schema=schema
    )
    op.create_index("ix_retrievals_bank_kind_recorded", "retrievals", ["bank_id", "kind", "recorded_at"], schema=schema)
    op.create_index(
        "ix_retrievals_reflect",
        "retrievals",
        ["reflect_id"],
        schema=schema,
        postgresql_where=sa.text("reflect_id IS NOT NULL"),
    )
    op.create_index("ix_retrievals_recorded", "retrievals", ["recorded_at"], schema=schema)


def _pg_downgrade() -> None:
    schema = _schema()
    for index in ("ix_retrievals_recorded", "ix_retrievals_reflect", "ix_retrievals_bank_kind_recorded"):
        op.drop_index(index, "retrievals", schema=schema)
    for column in ("cited_mental_model_ids", "parameters", "error", "reflect_id"):
        op.drop_column("retrievals", column, schema=schema)


def _oracle_unsupported() -> None:
    raise RuntimeError("hindsight-ext-cortana supports PostgreSQL only; its tables cannot be created on Oracle.")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_unsupported)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_unsupported)
