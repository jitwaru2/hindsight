"""Reconciliation and the hook's work after structuring (specification 6.4).

``after_retain`` is what ``on_retain_complete`` runs once structuring has written the retain's
claims: it sweeps the orphaned claims of the retain's documents (a re-save deletes the facts of
changed chunks), aligns the pending keys among the retain's claims, and settles every key the
retain touched, which includes the keys of the retain's documents, so a re-save that deleted a
superseding fact restores what it had superseded (S5).

``reconcile`` recomputes supersession for a subject, a document or the whole bank from the claims and
the facts' current states. In order it:

1. structures the facts in scope that have no claims and that no structuring call has answered:
   facts never structured and facts left ``structuring-pending`` by a failed call. A fact the model
   answered with no claim is structured with no claims (S8) and is not asked about again;
2. aligns the pending keys in scope;
3. sweeps claim rows whose fact exists in neither ``memory_units`` nor the engine's archive
   (specification 4.2), recording each swept claim in the ledger;
4. settles every key in scope;
5. writes one ``reconciled`` ledger entry summarising the run, only when the run changed something,
   so a second run over unchanged inputs writes nothing (S10, criterion 7);
6. for a whole-bank run, deletes the bank's retrieval log rows older than the retention
   (``retrievals.sweep``, HSIGHT-7). The count is in the run's summary but does not by itself make
   the run one that changed something: the retrieval log is not supersession state.

The engine offers no hook after a document is deleted, so restoration after a document deletion
(criterion 6) happens at the next reconciliation of the document, its subjects or the bank.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table

from . import ledger, retrievals
from .alignment import AlignmentModel, AlignReport, align
from .structuring.engine import structure_facts
from .structuring.runner import RunReport
from .supersession import (
    Key,
    SettleReport,
    keys_of_bank,
    keys_of_documents,
    keys_of_facts,
    keys_of_subject,
    settle,
)

logger = logging.getLogger(__name__)


async def after_retain(engine: Any, result: Any, *, run_id: UUID | None = None) -> SettleReport:
    """Align and settle the keys a retain touched (``on_retain_complete``, after structuring)."""
    bank_id = result.bank_id
    fact_ids = [_uuid(unit) for units in result.unit_ids for unit in units]
    documents = {item.get("document_id") for item in result.contents} | {result.document_id}
    documents.discard(None)
    pool = await engine._get_pool()
    async with pool.acquire() as conn:
        keys = await keys_of_facts(conn, bank_id, fact_ids) if fact_ids else set()
        keys |= await keys_of_documents(conn, bank_id, documents) if documents else set()
    if not keys:
        return SettleReport(run_id=run_id or uuid.uuid4())
    for document in sorted(documents):
        await sweep_orphans(engine, bank_id, document=document, actor="hook", run_id=run_id)
    try:
        aligned = await align(
            engine, bank_id, keys=keys, request_context=result.request_context, actor="hook", run_id=run_id
        )
        keys |= aligned.affected
    except Exception:
        logger.exception("cortana alignment failed for bank %s; keys stay pending for reconciliation", bank_id)
    return await settle(engine, bank_id, keys, request_context=result.request_context, actor="hook", run_id=run_id)


@dataclass
class ReconcileReport:
    run_id: UUID
    scope: dict[str, Any]
    structured: RunReport | None
    aligned: AlignReport
    swept: int
    settled: SettleReport
    wrote_summary: bool
    retrievals_swept: int = 0

    def summary(self) -> dict[str, Any]:
        structured = self.structured
        return {
            "run_id": str(self.run_id),
            "scope": self.scope,
            "structured": {
                "facts": sum(call.facts for call in structured.calls) if structured else 0,
                "claims": structured.claims if structured else 0,
                "pending": len(structured.pending) if structured else 0,
            },
            "aligned": {
                "merged": len(self.aligned.merged),
                "distinct": len(self.aligned.distinct),
                "left_pending": len(self.aligned.left_pending),
                "failures": self.aligned.failures,
            },
            "swept_orphan_claims": self.swept,
            "settled": self.settled.summary(),
            "ledger_summary_written": self.wrote_summary,
            "retrievals_swept": self.retrievals_swept,
        }


async def reconcile(
    engine: Any,
    bank_id: str,
    *,
    subject: UUID | None = None,
    document: str | None = None,
    request_context: Any,
    actor: str = "reconciliation",
    structuring_model: Any = None,
    alignment_model: AlignmentModel | None = None,
) -> ReconcileReport:
    """Reconcile one subject, one document, or (neither given) the whole bank."""
    if subject is not None and document is not None:
        raise ValueError("reconcile a subject or a document, not both")
    run_id = uuid.uuid4()
    scope = {"subject": str(subject)} if subject else ({"document": document} if document else {"bank": bank_id})
    pool = await engine._get_pool()

    async with pool.acquire() as conn:
        todo = await unstructured_facts(conn, bank_id, subject=subject, document=document)
    structured = None
    if todo:
        structured = await structure_facts(engine, bank_id, todo, request_context, actor=actor, model=structuring_model)

    async with pool.acquire() as conn:
        keys = await _scope_keys(conn, bank_id, subject, document)
    aligned = await align(
        engine,
        bank_id,
        keys=keys,
        request_context=request_context,
        actor=actor,
        run_id=run_id,
        model=alignment_model,
    )
    keys |= aligned.affected

    swept = await sweep_orphans(engine, bank_id, subject=subject, document=document, actor=actor, run_id=run_id)
    settled = await settle(engine, bank_id, keys, request_context=request_context, actor=actor, run_id=run_id)

    report = ReconcileReport(run_id, scope, structured, aligned, swept, settled, wrote_summary=False)
    if subject is None and document is None:
        async with pool.acquire() as conn:
            report.retrievals_swept = await retrievals.sweep(conn, bank_id=bank_id)
    changed = bool(
        (structured and (structured.claims or structured.pending)) or aligned.changed or swept or settled.changed
    )
    if changed:
        async with pool.acquire() as conn:
            await ledger.append(
                conn,
                bank_id,
                [ledger.Entry("reconciled", reason=f"reconciled {scope}", details=report.summary())],
                actor=actor,
                run_id=run_id,
            )
        report.wrote_summary = True
    logger.info("cortana reconcile bank=%s %s", bank_id, report.summary())
    return report


async def _scope_keys(conn: Any, bank_id: str, subject: UUID | None, document: str | None) -> set[Key]:
    if subject is not None:
        return await keys_of_subject(conn, bank_id, subject)
    if document is not None:
        return await keys_of_documents(conn, bank_id, [document])
    return await keys_of_bank(conn, bank_id)


async def unstructured_facts(
    conn: Any, bank_id: str, *, subject: UUID | None = None, document: str | None = None
) -> list[UUID]:
    """Live world and experience facts in scope with no claims that no structuring call answered."""
    filters, args = [], [bank_id]
    if document is not None:
        args.append(document)
        filters.append(f"AND mu.document_id = ${len(args)}")
    if subject is not None:
        args.append(subject)
        filters.append(
            f"AND EXISTS (SELECT 1 FROM {fq_table('unit_entities')} ue "
            f"WHERE ue.unit_id = mu.id AND ue.entity_id = ${len(args)})"
        )
    rows = await conn.fetch(
        f"""
        WITH answered AS (
            SELECT DISTINCT unnest(memory_unit_ids) AS id FROM {fq_table("ledger")}
            WHERE bank_id = $1 AND event = 'structured'
        )
        SELECT mu.id FROM {fq_table("memory_units")} mu
        WHERE mu.bank_id = $1 AND mu.fact_type IN ('world', 'experience') {" ".join(filters)}
          AND NOT EXISTS (SELECT 1 FROM {fq_table("claims")} c WHERE c.bank_id = $1 AND c.memory_unit_id = mu.id)
          AND mu.id NOT IN (SELECT id FROM answered)
        ORDER BY mu.created_at, mu.id
        """,
        *args,
    )
    return [row["id"] for row in rows]


async def sweep_orphans(
    engine: Any,
    bank_id: str,
    *,
    subject: UUID | None = None,
    document: str | None = None,
    actor: str = "reconciliation",
    run_id: UUID | None = None,
) -> int:
    """Delete claim rows whose fact exists in neither the live table nor the archive, recording each
    swept claim's content in the ledger entry so its history survives the row."""
    filters, args = [], [bank_id]
    if subject is not None:
        args.append(subject)
        filters.append(f"AND c.subject_entity_id = ${len(args)}")
    if document is not None:
        args.append(document)
        filters.append(f"AND c.document_id = ${len(args)}")
    pool = await engine._get_pool()
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            f"""
            DELETE FROM {fq_table("claims")} c
            WHERE c.bank_id = $1 {" ".join(filters)}
              AND NOT EXISTS (SELECT 1 FROM {fq_table("memory_units")} mu WHERE mu.id = c.memory_unit_id)
              AND NOT EXISTS (SELECT 1 FROM {fq_table("invalidated_memory_units")} im WHERE im.id = c.memory_unit_id)
            RETURNING c.id, c.memory_unit_id, c.subject_entity_id, c.subject_text, c.attribute_key, c.value_text,
                      c.state, c.superseded_by, c.superseded_rule, c.document_id, c.source_kind, c.stated_at
            """,
            *args,
        )
        if rows:
            await ledger.append(
                conn,
                bank_id,
                [
                    ledger.Entry(
                        "orphan-claims-swept",
                        reason=f"{len(rows)} claims whose fact no longer exists",
                        claim_ids=[row["id"] for row in rows],
                        memory_unit_ids=list(dict.fromkeys(row["memory_unit_id"] for row in rows)),
                        details={"claims": [dict(row) for row in rows]},
                    )
                ],
                actor=actor,
                run_id=run_id,
            )
    return len(rows)


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
