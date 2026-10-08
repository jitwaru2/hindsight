"""claims statement-time source

Adds ``claims.stated_at_source``: where a claim's statement time came from, so a wrong order is
diagnosable from the record (specification 3, principle 10). The values are the structuring step's
(``hindsight_ext_cortana.structuring.statement_time.StatedAtSource``): ``turn`` (the transcript turn's
timestamp), ``chunk-start`` (the first timestamped turn of the claim's chunk, when the turn could not
be identified), ``session-start`` (the session's start, when the save carries no turn timestamps),
``entry-date`` (the dated document entry the fact states), ``document-date`` (the document's stamped
date) and ``decision`` (the moment a decision record gives).

Revision ID: fafacc9c15dd
Revises: 8b07c49ffe8e
Create Date: 2026-10-07 18:07:37.967011

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from hindsight_api.alembic._dialect import run_for_dialect

# revision identifiers, used by Alembic.
revision: str = "fafacc9c15dd"
down_revision: str | Sequence[str] | None = "8b07c49ffe8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = ("turn", "chunk-start", "session-start", "entry-date", "document-date", "decision")


def _schema() -> str | None:
    """The tenant schema the engine is migrating, or None for its default."""
    return context.config.get_main_option("target_schema")


def _pg_upgrade() -> None:
    schema = _schema()
    op.add_column("claims", sa.Column("stated_at_source", sa.Text(), nullable=True), schema=schema)
    allowed = ", ".join(f"'{source}'" for source in SOURCES)
    op.create_check_constraint(
        "ck_claims_stated_at_source", "claims", f"stated_at_source IN ({allowed})", schema=schema
    )


def _pg_downgrade() -> None:
    schema = _schema()
    op.drop_constraint("ck_claims_stated_at_source", "claims", type_="check", schema=schema)
    op.drop_column("claims", "stated_at_source", schema=schema)


def _oracle_unsupported() -> None:
    raise RuntimeError("hindsight-ext-cortana supports PostgreSQL only; its tables cannot be created on Oracle.")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_unsupported)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_unsupported)
