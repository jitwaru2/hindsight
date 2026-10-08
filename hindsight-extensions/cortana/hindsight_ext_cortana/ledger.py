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

``fetch`` reads it for ``GET /ext/cortana/ledger`` (HSIGHT-7). An entry concerns a subject, key or
document when a claim it names (by claim id, or by fact id) has it, when its details name it (the
key-level S11 entries and merges carry ``subject_entity_id``, ``key`` and ``document_id``), when a
claim recorded in its details has it (an orphan sweep keeps the swept claims there, since their
rows are gone), or, for a document, when a fact it names belongs to the document (a structuring
failure names facts that have no claims).
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table, fq_table_explicit


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


async def fetch(
    conn: Any,
    *,
    schema: str,
    bank_id: str | None = None,
    subject_ids: Sequence[UUID] | None = None,
    attribute: str | None = None,
    fact_id: UUID | None = None,
    document: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Entries newest first. Subject, attribute and document must hold together for one claim."""

    def t(name: str) -> str:
        return fq_table_explicit(name, schema)

    args: list[Any] = []

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    where = []
    if bank_id is not None:
        where.append(f"l.bank_id = {arg(bank_id)}")
    if since is not None:
        where.append(f"l.recorded_at >= {arg(since)}")
    if fact_id is not None:
        where.append(f"{arg(fact_id)}::uuid = ANY(l.memory_unit_ids)")
    if subject_ids is not None or attribute is not None or document is not None:
        claim, detail, swept = [], [], []
        if subject_ids is not None:
            subjects = arg(list(subject_ids))
            claim.append(f"c.subject_entity_id = ANY({subjects}::uuid[])")
            detail.append(f"l.details->>'subject_entity_id' = ANY({subjects}::uuid[]::text[])")
            swept.append(f"e->>'subject_entity_id' = ANY({subjects}::uuid[]::text[])")
        if attribute is not None:
            key = arg(attribute)
            claim.append(f"(c.attribute_key = {key} OR c.keyed_as = {key})")
            detail.append(f"(l.details->>'key' = {key} OR l.details->>'into' = {key})")
            swept.append(f"e->>'attribute_key' = {key}")
        if document is not None:
            doc = arg(document)
            claim.append(f"c.document_id = {doc}")
            detail.append(f"l.details->>'document_id' = {doc}")
            swept.append(f"e->>'document_id' = {doc}")
        scope = [
            f"""EXISTS (SELECT 1 FROM {t("claims")} c WHERE c.bank_id = l.bank_id
                AND (c.id = ANY(l.claim_ids) OR c.memory_unit_id = ANY(l.memory_unit_ids))
                AND {" AND ".join(claim)})""",
            f"({' AND '.join(detail)})",
            f"""EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(l.details->'claims') = 'array'
                THEN l.details->'claims' ELSE '[]'::jsonb END) e WHERE {" AND ".join(swept)})""",
        ]
        if document is not None and subject_ids is None and attribute is None:
            for table in ("memory_units", "invalidated_memory_units"):
                scope.append(
                    f"EXISTS (SELECT 1 FROM {t(table)} mu WHERE mu.id = ANY(l.memory_unit_ids) "
                    f"AND mu.document_id = {doc})"
                )
        where.append(f"({' OR '.join(scope)})")
    rows = await conn.fetch(
        f"""
        SELECT l.id, l.bank_id, l.recorded_at, l.event, l.actor, l.rule, l.reason, l.run_id, l.claim_ids,
               l.memory_unit_ids, l.details
        FROM {t("ledger")} l
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY l.recorded_at DESC, l.id DESC
        LIMIT {arg(limit)}
        """,
        *args,
    )
    out = []
    for row in rows:
        item = dict(row)
        if isinstance(item["details"], str):
            item["details"] = json.loads(item["details"])
        out.append(item)
    return out


async def subject_ids(conn: Any, *, schema: str, subject: str, bank_id: str | None = None) -> list[UUID]:
    """A subject given as an entity id, or by name: the entities with that exact name (case-insensitive)
    and the subjects of claims stated about that name (unresolved subjects have synthetic ids)."""
    try:
        return [UUID(subject)]
    except ValueError:
        pass
    bank_filter = "AND bank_id = $2" if bank_id is not None else ""
    args = [subject] + ([bank_id] if bank_id is not None else [])
    rows = await conn.fetch(
        f"""
        SELECT id FROM {fq_table_explicit("entities", schema)} WHERE LOWER(canonical_name) = LOWER($1) {bank_filter}
        UNION
        SELECT DISTINCT subject_entity_id FROM {fq_table_explicit("claims", schema)}
        WHERE LOWER(subject_text) = LOWER($1) {bank_filter}
        """,
        *args,
    )
    return [row["id"] for row in rows]
