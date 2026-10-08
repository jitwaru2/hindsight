"""Applying the rules to the bank: claim states, key states, and fact retirement through the
engine's curation path (specification 6.1, 6.2, 8). The hook runs it after structuring; the
reconciliation runs it for a subject, a document or the whole bank.

``settle`` takes a set of keys and:

1. loads their claims (an indexed lookup in ``claims``; never recall, specification 14), the state
   of every fact they belong to (live in ``memory_units``, or in the engine's archive with its
   invalidation reason), and each key's catalog row;
2. computes every key with ``rules.evaluate_key`` over its valid claims: a claim whose fact is live,
   or was retired by these rules. A claim whose fact was deleted, or retired by anyone else, is not
   an input, which is how S5 restores what a vanished superseder had superseded;
3. writes the claim and key states that changed, each change with its ledger entry, in one
   transaction; unchanged inputs write nothing (S10);
4. compares each fact's computed state with its actual state and retires (``state: invalidated``
   with the fixed reason of 4.4), restores (``state: valid``) or rewrites the reason of a retired
   fact through ``MemoryEngine.update_memory_unit``, never by writing the engine's tables. That
   path deletes the observations that cite the fact, re-queues its siblings for consolidation and
   graph maintenance, and asks automatically refreshed mental models to refresh;
5. asks the engine to refresh every mental model whose stored grounding cites a fact it retired or
   an observation the retirement deleted (``submit_async_refresh_mental_model``), because the
   engine's own trigger covers only automatically refreshed models and defers while consolidation
   is pending.

One pass per bank runs at a time: a session-level advisory lock serialises the hook's passes on
concurrent retains and a reconciliation running beside them.
"""

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from hindsight_api.engine.reflect.retractions import based_on_fact_ids
from hindsight_api.engine.schema import fq_table

from . import ledger
from .rules import Decision, FactClaim, RuleClaim, evaluate_facts, evaluate_key, is_rule_retirement

logger = logging.getLogger(__name__)

Key = tuple[UUID, str]
LOCK_PREFIX = "cortana-supersession:"


@dataclass
class SettleReport:
    run_id: UUID
    keys: int = 0
    claims_changed: int = 0
    keys_changed: int = 0
    retired: list[UUID] = field(default_factory=list)
    restored: list[UUID] = field(default_factory=list)
    reasons_updated: list[UUID] = field(default_factory=list)
    refreshes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    ledger_entries: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.claims_changed
            or self.keys_changed
            or self.retired
            or self.restored
            or self.reasons_updated
            or self.refreshes
            or self.failures
        )

    def summary(self) -> dict[str, Any]:
        return {
            "keys": self.keys,
            "claims_changed": self.claims_changed,
            "keys_changed": self.keys_changed,
            "retired": len(self.retired),
            "restored": len(self.restored),
            "reasons_updated": len(self.reasons_updated),
            "refreshes": len(self.refreshes),
            "failures": self.failures,
        }


@dataclass(frozen=True)
class FactState:
    live: bool
    reason: str | None
    created_at: Any

    @property
    def ours(self) -> bool:
        """Retired by these rules (so its claims stay inputs and it may be restored)."""
        return not self.live and is_rule_retirement(self.reason)


# Scopes ------------------------------------------------------------------------------------------


async def keys_of_facts(conn: Any, bank_id: str, fact_ids: Iterable[UUID]) -> set[Key]:
    rows = await conn.fetch(
        f"SELECT DISTINCT subject_entity_id, attribute_key FROM {fq_table('claims')} "
        f"WHERE bank_id = $1 AND memory_unit_id = ANY($2::uuid[])",
        bank_id,
        list(fact_ids),
    )
    return {(row["subject_entity_id"], row["attribute_key"]) for row in rows}


async def keys_of_documents(conn: Any, bank_id: str, document_ids: Iterable[str]) -> set[Key]:
    rows = await conn.fetch(
        f"SELECT DISTINCT subject_entity_id, attribute_key FROM {fq_table('claims')} "
        f"WHERE bank_id = $1 AND document_id = ANY($2::text[])",
        bank_id,
        list(document_ids),
    )
    return {(row["subject_entity_id"], row["attribute_key"]) for row in rows}


async def keys_of_subject(conn: Any, bank_id: str, subject_id: UUID) -> set[Key]:
    rows = await conn.fetch(
        f"SELECT DISTINCT subject_entity_id, attribute_key FROM {fq_table('claims')} "
        f"WHERE bank_id = $1 AND subject_entity_id = $2",
        bank_id,
        subject_id,
    )
    return {(row["subject_entity_id"], row["attribute_key"]) for row in rows}


async def keys_of_bank(conn: Any, bank_id: str) -> set[Key]:
    rows = await conn.fetch(
        f"SELECT DISTINCT subject_entity_id, attribute_key FROM {fq_table('claims')} WHERE bank_id = $1", bank_id
    )
    return {(row["subject_entity_id"], row["attribute_key"]) for row in rows}


async def fact_states(conn: Any, bank_id: str, fact_ids: Iterable[UUID]) -> dict[UUID, FactState]:
    """Each fact's state: live, or archived with its invalidation reason. Absent ids no longer exist."""
    ids = list(fact_ids)
    rows = await conn.fetch(
        f"""
        SELECT id, created_at, NULL::text AS reason, true AS live FROM {fq_table("memory_units")}
        WHERE bank_id = $1 AND id = ANY($2::uuid[])
        UNION ALL
        SELECT id, created_at, invalidation_reason, false FROM {fq_table("invalidated_memory_units")}
        WHERE bank_id = $1 AND id = ANY($2::uuid[])
        """,
        bank_id,
        ids,
    )
    return {row["id"]: FactState(row["live"], row["reason"], row["created_at"]) for row in rows}


# The pass ----------------------------------------------------------------------------------------


async def settle(
    engine: Any,
    bank_id: str,
    keys: Iterable[Key],
    *,
    request_context: Any,
    actor: str = "hook",
    run_id: UUID | None = None,
) -> SettleReport:
    """Recompute the keys and carry the result into claims, the catalog, the ledger and the engine."""
    scope = set(keys)
    report = SettleReport(run_id=run_id or uuid.uuid4(), keys=len(scope))
    if not scope:
        return report
    pool = await engine._get_pool()
    async with pool.acquire() as lock:
        await lock.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", LOCK_PREFIX + bank_id)
        try:
            await _settle(engine, pool, bank_id, scope, report, request_context=request_context, actor=actor)
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", LOCK_PREFIX + bank_id)
    if report.changed:
        logger.info("cortana supersession bank=%s run=%s %s", bank_id, report.run_id, report.summary())
    return report


async def _settle(
    engine: Any, pool: Any, bank_id: str, scope: set[Key], report: SettleReport, *, request_context: Any, actor: str
) -> None:
    subjects = [subject for subject, _ in scope]
    attributes = [key for _, key in scope]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.* FROM {fq_table("claims")} c
            JOIN unnest($2::uuid[], $3::text[]) AS k(subject, key)
              ON c.subject_entity_id = k.subject AND c.attribute_key = k.key
            WHERE c.bank_id = $1
            """,
            bank_id,
            subjects,
            attributes,
        )
        fact_ids = {row["memory_unit_id"] for row in rows}
        siblings = await conn.fetch(
            f"SELECT id, memory_unit_id, subject_text, attribute_key, subject_entity_id, state, superseded_by, "
            f"superseded_rule FROM {fq_table('claims')} WHERE bank_id = $1 AND memory_unit_id = ANY($2::uuid[])",
            bank_id,
            list(fact_ids),
        )
        states = await fact_states(conn, bank_id, fact_ids)
        catalog = {
            (row["subject_entity_id"], row["attribute_key"]): row
            for row in await conn.fetch(
                f"""
                SELECT a.subject_entity_id, a.attribute_key, a.alignment, a.conflict_claim_ids, a.stale_documents
                FROM {fq_table("attributes")} a
                JOIN unnest($2::uuid[], $3::text[]) AS k(subject, key)
                  ON a.subject_entity_id = k.subject AND a.attribute_key = k.key
                WHERE a.bank_id = $1
                """,
                bank_id,
                subjects,
                attributes,
            )
        }

    def valid(row: Any) -> bool:
        state = states.get(row["memory_unit_id"])
        return state is not None and (state.live or state.ours)

    by_key: dict[Key, list[Any]] = defaultdict(list)
    for row in rows:
        by_key[(row["subject_entity_id"], row["attribute_key"])].append(row)
    fact_of: dict[UUID, UUID] = {row["id"]: row["memory_unit_id"] for row in rows}

    entries: list[ledger.Entry] = []
    claim_updates: list[tuple[UUID, str, UUID | None, str | None]] = []
    key_updates: list[tuple[UUID, str, list[UUID], list[str]]] = []
    decisions: dict[UUID, Decision] = {}

    for key in sorted(scope, key=lambda k: (str(k[0]), k[1])):
        key_rows = [row for row in by_key.get(key, []) if valid(row)]
        entry = catalog.get(key)
        aligned = entry is None or entry["alignment"] == "aligned"
        outcome = evaluate_key((_rule_claim(row, states) for row in key_rows), aligned=aligned)
        valid_ids = {row["id"] for row in key_rows}
        for row in key_rows:
            new = outcome.decisions[row["id"]]
            decisions[row["id"]] = new
            if (row["state"], row["superseded_by"], row["superseded_rule"]) == (new.state, new.superseded_by, new.rule):
                continue
            claim_updates.append((row["id"], new.state, new.superseded_by, new.rule))
            change = _claim_entry(row, new, fact_of, valid_ids)
            if change is not None:
                entries.append(change)

        conflict = sorted(outcome.conflict, key=str)
        stale = list(outcome.stale_documents)
        old_conflict = sorted(entry["conflict_claim_ids"] or [], key=str) if entry else []
        old_stale = list(entry["stale_documents"] or []) if entry else []
        if entry is None or conflict != old_conflict or stale != old_stale:
            key_updates.append((key[0], key[1], conflict, stale))
        subject_text = (
            key_rows[0]["subject_text"] if key_rows else (by_key[key][0]["subject_text"] if by_key.get(key) else "")
        )
        entries.extend(_key_entries(key, subject_text, old_conflict, conflict, old_stale, stale, fact_of))

    if claim_updates or key_updates or entries:
        async with pool.acquire() as conn, conn.transaction():
            if claim_updates:
                await conn.executemany(
                    f"UPDATE {fq_table('claims')} SET state = $2, superseded_by = $3, superseded_rule = $4, "
                    f"updated_at = now() WHERE id = $1",
                    claim_updates,
                )
            if key_updates:
                await conn.executemany(
                    f"""
                    INSERT INTO {fq_table("attributes")}
                        (bank_id, subject_entity_id, attribute_key, description, conflict_claim_ids, stale_documents)
                    VALUES ($1, $2, $3, $3, $4::uuid[], $5::text[])
                    ON CONFLICT (bank_id, subject_entity_id, attribute_key) DO UPDATE
                    SET conflict_claim_ids = EXCLUDED.conflict_claim_ids, stale_documents = EXCLUDED.stale_documents,
                        updated_at = now()
                    """,
                    [(bank_id, subject, key, conflict, stale) for subject, key, conflict, stale in key_updates],
                )
            report.ledger_entries += await ledger.append(conn, bank_id, entries, actor=actor, run_id=report.run_id)
    report.claims_changed = len(claim_updates)
    report.keys_changed = len(key_updates)

    await _curate(engine, pool, bank_id, rows, siblings, scope, decisions, states, report, request_context, actor)


def _rule_claim(row: Any, states: dict[UUID, FactState]) -> RuleClaim:
    return RuleClaim(
        id=row["id"],
        fact_id=row["memory_unit_id"],
        value=row["value_text"],
        provisional=row["provisional"],
        kind=row["source_kind"],
        stated_at=row["stated_at"],
        document_id=row["document_id"],
        document_order=row["document_order"],
        chunk_index=row["chunk_index"],
        fact_ordinal=row["fact_ordinal"],
        source_rank=row["source_rank"],
        created_at=states[row["memory_unit_id"]].created_at,
        subject=row["subject_text"],
        key=row["attribute_key"],
    )


def _claim_entry(row: Any, new: Decision, fact_of: dict[UUID, UUID], valid_ids: set[UUID]) -> ledger.Entry | None:
    """The ledger entry for a claim whose state changed, or None for a change between valid states
    (a new claim taking its first state, a claim joining a conflict), which the key's entries cover."""
    where = f"{row['subject_text']}/{row['attribute_key']}"
    if new.state == "superseded":
        successor_fact = fact_of[new.superseded_by]
        kind = "restatement: " if new.rule == "S3" else ""
        reason = f"{kind}superseded by claim {new.superseded_by} (fact {successor_fact}) on {where}"
        details: dict[str, Any] = {"subject_entity_id": row["subject_entity_id"], "key": row["attribute_key"]}
        if row["state"] == "superseded":
            details["previous_superseded_by"] = row["superseded_by"]
            details["previous_rule"] = row["superseded_rule"]
        return ledger.Entry(
            "claim-superseded",
            reason=reason,
            rule=new.rule,
            claim_ids=[row["id"], new.superseded_by],
            memory_unit_ids=[row["memory_unit_id"], successor_fact],
            details=details,
        )
    if row["state"] == "superseded":
        gone = row["superseded_by"] not in valid_ids
        reason = (
            f"the claim that superseded it ({row['superseded_by']}) no longer exists; recomputed on {where}"
            if gone
            else f"recomputed on {where}: no longer superseded"
        )
        return ledger.Entry(
            "claim-restored",
            reason=reason,
            rule="S5",
            claim_ids=[row["id"]],
            memory_unit_ids=[row["memory_unit_id"]],
            details={"state": new.state, "previous_superseded_by": row["superseded_by"]},
        )
    return None


def _key_entries(
    key: Key,
    subject: str,
    old_conflict: list[UUID],
    conflict: list[UUID],
    old_stale: list[str],
    stale: list[str],
    fact_of: dict[UUID, UUID],
) -> list[ledger.Entry]:
    where = f"{subject}/{key[1]}"
    details = {"subject_entity_id": key[0], "key": key[1]}
    entries = []
    if conflict != old_conflict:
        event = "conflict-opened" if not old_conflict else ("conflict-closed" if not conflict else "conflict-changed")
        ids = conflict or old_conflict
        entries.append(
            ledger.Entry(
                event,
                rule="S11",
                reason=f"{where}: {len(conflict)} valid claims disagree" if conflict else f"{where}: resolved",
                claim_ids=ids,
                memory_unit_ids=[fact_of[i] for i in ids if i in fact_of],
                details={**details, "previous": old_conflict},
            )
        )
    for document in sorted(set(stale) - set(old_stale)):
        entries.append(
            ledger.Entry(
                "stale-document-marked",
                rule="S11",
                reason=f"{where}: {document} still states a superseded value",
                details={**details, "document_id": document},
            )
        )
    for document in sorted(set(old_stale) - set(stale)):
        entries.append(
            ledger.Entry(
                "stale-document-cleared",
                rule="S11",
                reason=f"{where}: {document} agrees again or no longer differs",
                details={**details, "document_id": document},
            )
        )
    return entries


# Facts -------------------------------------------------------------------------------------------


async def _curate(
    engine: Any,
    pool: Any,
    bank_id: str,
    rows: list[Any],
    siblings: list[Any],
    scope: set[Key],
    decisions: dict[UUID, Decision],
    states: dict[UUID, FactState],
    report: SettleReport,
    request_context: Any,
    actor: str,
) -> None:
    """Carry each fact's computed state into the engine through its curation path."""
    concerned = {row["memory_unit_id"] for row in rows if row["id"] in decisions}
    fact_claims: list[FactClaim] = []
    outside_successors: set[UUID] = set()
    for row in siblings:
        if row["memory_unit_id"] not in concerned:
            continue
        if (row["subject_entity_id"], row["attribute_key"]) in scope:
            decision = decisions.get(row["id"])
            if decision is None:
                continue
        else:
            decision = Decision(row["state"], row["superseded_by"], row["superseded_rule"])
            if row["superseded_by"] is not None:
                outside_successors.add(row["superseded_by"])
        fact_claims.append(
            FactClaim(row["id"], row["memory_unit_id"], row["subject_text"], row["attribute_key"], decision)
        )
    fact_of = {row["id"]: row["memory_unit_id"] for row in rows}
    missing = outside_successors - set(fact_of)
    if missing:
        async with pool.acquire() as conn:
            for row in await conn.fetch(
                f"SELECT id, memory_unit_id FROM {fq_table('claims')} WHERE id = ANY($1::uuid[])", list(missing)
            ):
                fact_of[row["id"]] = row["memory_unit_id"]
    # A sibling superseded by a claim that no longer exists keeps its fact valid until its own key
    # is recomputed; never retire on a dangling reference.
    fact_claims = [c for c in fact_claims if c.decision.superseded_by is None or c.decision.superseded_by in fact_of]
    outcomes = evaluate_facts(fact_claims, fact_of)

    to_retire, to_restore, to_reason = [], [], []
    for fact_id, outcome in sorted(outcomes.items(), key=lambda item: str(item[0])):
        state = states.get(fact_id)
        if state is None:
            continue
        if outcome.retired and state.live:
            to_retire.append((fact_id, outcome))
        elif not outcome.retired and state.ours:
            to_restore.append(fact_id)
        elif outcome.retired and state.ours and outcome.reason != state.reason:
            to_reason.append((fact_id, outcome))

    citing: list[UUID] = []
    if to_retire:
        async with pool.acquire() as conn:
            citing = [
                row["id"]
                for row in await conn.fetch(
                    f"SELECT id FROM {fq_table('memory_units')} WHERE bank_id = $1 AND fact_type = 'observation' "
                    f"AND source_memory_ids && $2::uuid[]",
                    bank_id,
                    [fact_id for fact_id, _ in to_retire],
                )
            ]

    for fact_id in to_restore:
        if await _update(
            engine,
            pool,
            bank_id,
            fact_id,
            "valid",
            None,
            report,
            request_context,
            actor,
            ledger.Entry(
                "fact-restored",
                rule="S5",
                reason="a claim it carries is valid again",
                claim_ids=[c.id for c in fact_claims if c.fact_id == fact_id],
                memory_unit_ids=[fact_id],
                details={"previous_reason": states[fact_id].reason},
            ),
        ):
            report.restored.append(fact_id)
    for fact_id, outcome in to_retire:
        if await _update(
            engine,
            pool,
            bank_id,
            fact_id,
            "invalidated",
            outcome.reason,
            report,
            request_context,
            actor,
            _retirement_entry("fact-retired", fact_id, outcome, fact_of),
        ):
            report.retired.append(fact_id)
    for fact_id, outcome in to_reason:
        entry = _retirement_entry("retirement-reason-updated", fact_id, outcome, fact_of)
        entry.details["previous_reason"] = states[fact_id].reason
        if await _update(
            engine, pool, bank_id, fact_id, "invalidated", outcome.reason, report, request_context, actor, entry
        ):
            report.reasons_updated.append(fact_id)

    if report.retired:
        await _refresh_mental_models(
            engine, pool, bank_id, set(report.retired) | set(citing), report, request_context, actor
        )


def _retirement_entry(event: str, fact_id: UUID, outcome: Any, fact_of: dict[UUID, UUID]) -> ledger.Entry:
    claim_ids = [claim for pair in outcome.superseding for claim in pair]
    rules = sorted({clause.rsplit("rule ", 1)[-1] for clause in (outcome.reason or "").split("; ") if clause})
    return ledger.Entry(
        event,
        rule=",".join(rules) or None,
        reason=outcome.reason,
        claim_ids=claim_ids,
        memory_unit_ids=[fact_id, *dict.fromkeys(fact_of[s] for _, s in outcome.superseding)],
        details={},
    )


async def _update(
    engine: Any,
    pool: Any,
    bank_id: str,
    fact_id: UUID,
    state: str,
    reason: str | None,
    report: SettleReport,
    request_context: Any,
    actor: str,
    entry: ledger.Entry,
) -> bool:
    """One curation call; the ledger records what happened, success or failure."""
    try:
        result = await engine.update_memory_unit(
            bank_id, str(fact_id), state=state, reason=reason, request_context=request_context
        )
    except Exception as error:
        message = f"update_memory_unit({fact_id}, state={state}) failed: {type(error).__name__}: {error}"
        logger.warning("cortana supersession: %s", message)
        report.failures.append(message)
        entry = ledger.Entry(
            "curation-failed", reason=message, memory_unit_ids=[fact_id], details={"wanted": entry.event}
        )
        result = None
    else:
        if result is None:
            entry = ledger.Entry(
                "curation-failed",
                reason="the fact no longer exists",
                memory_unit_ids=[fact_id],
                details={"wanted": entry.event},
            )
            report.failures.append(f"{fact_id}: no longer exists")
    async with pool.acquire() as conn:
        report.ledger_entries += await ledger.append(conn, bank_id, [entry], actor=actor, run_id=report.run_id)
    return result is not None


async def _refresh_mental_models(
    engine: Any, pool: Any, bank_id: str, gone: set[UUID], report: SettleReport, request_context: Any, actor: str
) -> None:
    """Request a refresh of every mental model whose grounding cites a retired fact or an observation
    the retirement deleted (specification 8)."""
    gone_text = {str(i) for i in gone}
    async with pool.acquire() as conn:
        models = await conn.fetch(
            f"SELECT id, name, reflect_response FROM {fq_table('mental_models')} WHERE bank_id = $1", bank_id
        )
    entries = []
    for model in models:
        response = model["reflect_response"]
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError:
                continue
        cited = set(based_on_fact_ids((response or {}).get("based_on"))) & gone_text
        if not cited:
            continue
        model_id = str(model["id"])
        try:
            operation = await engine.submit_async_refresh_mental_model(
                bank_id, model_id, request_context=request_context
            )
        except Exception as error:
            message = f"refresh of mental model {model_id} failed: {type(error).__name__}: {error}"
            report.failures.append(message)
            entries.append(
                ledger.Entry("mental-model-refresh-failed", reason=message, details={"mental_model_id": model_id})
            )
            continue
        report.refreshes.append(model_id)
        entries.append(
            ledger.Entry(
                "mental-model-refresh-requested",
                reason=f"mental model {model['name']!r} cites {len(cited)} retired or swept memories",
                memory_unit_ids=sorted(UUID(i) for i in cited),
                details={"mental_model_id": model_id, "operation": (operation or {}).get("operation_id")},
            )
        )
    if entries:
        async with pool.acquire() as conn:
            report.ledger_entries += await ledger.append(conn, bank_id, entries, actor=actor, run_id=report.run_id)
