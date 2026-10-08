"""The append-only ledger (specification 3 principle 5, 4.2): one row per supersession, restoration,
fact retirement and restoration, attribute merge and alignment, structuring failure, mental-model
refresh request and reconciliation outcome. The table's trigger refuses updates; nothing here
deletes. Rows carry the actor, the rule, the reason, the run id and the ids involved.

Events written by HSIGHT-5 (``event`` column):

- ``claim-superseded`` (rule S1, S2 or S3; S3 is a restatement), ``claim-restored`` (S5);
- ``conflict-opened``, ``conflict-changed``, ``conflict-closed`` (S11); ``stale-document-marked``,
  ``stale-document-cleared`` (S11);
- ``fact-retired``, ``fact-restored``, ``retirement-reason-updated``, ``curation-failed``;
- ``mental-model-refresh-requested``, ``mental-model-refresh-failed``;
- ``attribute-merged``, ``attribute-merge-reversed``, ``attribute-aligned``, ``alignment-failed``;
- ``orphan-claims-swept``, ``reconciled``.

HSIGHT-4 writes ``structured`` and ``structuring-pending``.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table


@dataclass(frozen=True)
class Entry:
    event: str
    reason: str | None = None
    rule: str | None = None
    claim_ids: Sequence[UUID] = ()
    memory_unit_ids: Sequence[UUID] = ()
    details: dict[str, Any] = field(default_factory=dict)


async def append(conn: Any, bank_id: str, entries: Iterable[Entry], *, actor: str, run_id: UUID | None) -> int:
    """Insert the entries in order on the caller's connection (inside its transaction, if any)."""
    rows = [
        (
            bank_id,
            entry.event,
            actor,
            entry.rule,
            entry.reason,
            run_id,
            list(entry.claim_ids),
            list(entry.memory_unit_ids),
            json.dumps(entry.details, default=str),
        )
        for entry in entries
    ]
    if rows:
        await conn.executemany(
            f"""
            INSERT INTO {fq_table("ledger")}
                (bank_id, event, actor, rule, reason, run_id, claim_ids, memory_unit_ids, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7::uuid[], $8::uuid[], $9::jsonb)
            """,
            rows,
        )
    return len(rows)
