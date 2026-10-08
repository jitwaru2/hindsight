"""Decision capture (specification 10.1): Josh's decision recorded in his words as the current claim.

``record_decision`` backs ``POST /ext/cortana/decisions`` and the ``cortana_record_decision`` tool:

1. It writes one document ``decision:<uuid>`` through the engine's retain path, synchronously, under the
   bank's ``decision`` strategy, whose extraction mode is ``chunks``: no model call, the words become
   the fact verbatim. The document is tagged ``source:decision``, ``domain:<d>`` and the domain's pool,
   with that pool as its observation scope, and names the subject as its entity so the engine's own
   entity resolution links the fact to it. The hook recognises the strategy and does not structure it.
2. When the retain returns (its facts are committed and the hook has run), it records the claim from
   the arguments with ``structuring.engine.record_prestructured``: the decision source kind and rank,
   the statement time given (now by default), and supersession settled before it returns. The claim
   counts at once even on a key new to the subject: the session chose the key with the subject's
   catalog in view (``cortana_subjects``), so the key is created aligned, or marked distinct if pending.
3. It returns the fact id, the key, and what the decision replaced, in one line a session can repeat
   to Josh ("Recorded as fact ... on Kestrel / region; it replaces the 2026-09-25 position ...").

Idempotence. The operation id is a uuid5 of the words, the subject (as compared), the key and the
statement time; the document id is ``decision:<operation id>``. A call whose document already holds
its claim answers from the stored record and writes nothing; a call interrupted after the retain
completes the claim on the stored fact. An advisory lock per operation id serialises identical calls.

The ``decision`` strategy. A bank with no ``decision`` strategy gets one (``retain_extraction_mode:
chunks``) through the engine's bank-config path the first time a decision is recorded, with a ledger
entry. A ``decision`` strategy configured with any other extraction mode is refused rather than
overwritten, because retaining under it would call the model or alter the words.

Pools play no part in supersession (7.3): the pool tag only scopes consolidation.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from hindsight_api.config_resolver import apply_strategy
from hindsight_api.engine.schema import fq_table
from pydantic import BaseModel, Field

from . import ledger
from .current import phrase, when
from .pools import DOMAIN_PREFIX, POOL_PREFIX, pool_for
from .structuring.engine import (
    DECISION_PREFIX,
    DECISION_STRATEGY,
    DECISION_TAG,
    PrestructuredClaim,
    record_prestructured,
)
from .structuring.names import normalize_name
from .structuring.validation import ENTRY_TIMEZONE, normalize_key

ACTOR = "decision-tool"
EXTRACTION_MODE = "chunks"
# The namespace of decision operation ids: fixed, so the same words, subject, key and time always
# derive the same id and document.
OPERATION_NAMESPACE = UUID("9a3c7e52-41d8-4f0b-b6a2-6d2e8f1c0b47")
LOCK_PREFIX = "cortana-decision:"
DEFAULT_DOMAIN = "general"


class DecisionError(ValueError):
    """A decision the tool cannot record as asked; ``status`` is the HTTP status the route answers."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class DecisionRequest(BaseModel):
    words: str = Field(description="Josh's words, verbatim, exactly as he said them")
    subject: str = Field(description="what the decision is about: an entity name from cortana_subjects, or a new one")
    attribute: str = Field(description="the attribute key: an existing key from cortana_subjects, or a new one")
    value: str = Field(description="the decided value, short, as the current-state read should show it")
    stated_at: datetime | None = Field(
        default=None,
        description="when Josh stated it (ISO 8601); now when omitted; a time without a zone is US Eastern",
    )
    session_id: str | None = Field(default=None, description="the session the decision was stated in")
    domain: str | None = Field(default=None, description="the session's domain: work, recovery or general")


class Superseded(BaseModel):
    claim_id: UUID
    fact_id: UUID
    value: str
    source: str
    stated_at: datetime
    rule: str | None
    document_id: str | None


class DecisionResult(BaseModel):
    message: str
    recorded: bool
    operation_id: UUID
    document_id: str
    fact_id: UUID
    claim_id: UUID
    subject_id: UUID
    subject: str
    attribute: str
    value: str
    stated_at: datetime
    state: str
    superseded: list[Superseded]
    superseded_by: Superseded | None
    conflict_with: list[Superseded]
    stale_documents: list[Superseded]


def operation_id(words: str, subject: str, attribute: str, stated_at: datetime) -> UUID:
    """The decision's operation id: the words, the subject as compared, the key, and the time."""
    parts = [words, normalize_name(subject), normalize_key(attribute), stated_at.astimezone(UTC).isoformat()]
    return uuid.uuid5(OPERATION_NAMESPACE, json.dumps(parts))


def _normalized(request: DecisionRequest) -> tuple[str, str, str, str, datetime, str]:
    words, subject, value = request.words.strip(), request.subject.strip(), request.value.strip()
    key = normalize_key(request.attribute or "")
    missing = [name for name, text in (("words", words), ("subject", subject), ("value", value)) if not text]
    if not key:
        missing.append("attribute")
    if missing:
        raise DecisionError(f"missing {', '.join(missing)}: a decision needs Josh's words, the subject, key and value")
    stated_at = request.stated_at or datetime.now(UTC)
    if stated_at.tzinfo is None:
        stated_at = stated_at.replace(tzinfo=ENTRY_TIMEZONE)
    domain = normalize_key(request.domain or DEFAULT_DOMAIN)
    return words, subject, key, value, stated_at, domain


async def _ensure_strategy(memory: Any, bank_id: str, request_context: Any) -> bool:
    """Give the bank its ``decision`` strategy when it has none; True when one was added."""
    resolver = memory._config_resolver
    config = await resolver.get_bank_config(bank_id, request_context, cached=False)
    strategies = dict(config.get("retain_strategies") or {})
    existing = strategies.get(DECISION_STRATEGY)
    if existing is None:
        strategies[DECISION_STRATEGY] = {"retain_extraction_mode": EXTRACTION_MODE}
        await resolver.update_bank_config(bank_id, {"retain_strategies": strategies}, request_context)
        return True
    if not isinstance(existing, dict) or existing.get("retain_extraction_mode") != EXTRACTION_MODE:
        raise DecisionError(
            f"bank {bank_id!r} has a {DECISION_STRATEGY!r} retain strategy whose extraction mode is not "
            f"{EXTRACTION_MODE!r}; decision records must be stored verbatim, so set it to "
            f'{{"retain_extraction_mode": "{EXTRACTION_MODE}"}}',
            status=409,
        )
    return False


async def _check_one_chunk(memory: Any, bank_id: str, request_context: Any, words: str) -> None:
    """The words must fit one chunk, so the record is one fact carrying one claim."""
    config = apply_strategy(
        await memory._config_resolver.resolve_full_config(bank_id, request_context), DECISION_STRATEGY
    )
    if len(words) > config.retain_chunk_size:
        raise DecisionError(
            f"the words are {len(words)} characters; a decision record holds one chunk of at most "
            f"{config.retain_chunk_size}: record the decision itself, not the discussion around it"
        )


def _superseded(row: Any) -> Superseded:
    return Superseded(
        claim_id=row["id"],
        fact_id=row["memory_unit_id"],
        value=row["value_text"],
        source=row["source_kind"],
        stated_at=row["stated_at"],
        rule=row["superseded_rule"],
        document_id=row["document_id"],
    )


def _phrase(item: Superseded) -> str:
    return phrase(item.value, item.source, item.stated_at, item.document_id)


def message(result: DecisionResult) -> str:
    """The one line a session repeats to Josh."""
    head = "Recorded" if result.recorded else "Already recorded"
    line = f"{head} as fact {result.fact_id} on {result.subject} / {result.attribute}"
    replaced = [s for s in result.superseded if s.rule != "S2"]
    settled = [s for s in result.superseded if s.rule == "S2"]
    if result.state == "superseded" and result.superseded_by is not None:
        line += f"; a later statement remains the current position: {_phrase(result.superseded_by)}"
    elif result.state == "conflict":
        line += "; the key is now in conflict with " + ", ".join(_phrase(c) for c in result.conflict_with)
        line += ", so tell Josh which says what"
    if replaced:
        moments = sorted({when(s.stated_at)[:10] for s in replaced}, reverse=True)
        noun = "position" if len(moments) == 1 else "positions"
        line += (
            f"; it replaces the {' and '.join(moments)} {noun} (" + ", ".join(f'"{s.value}"' for s in replaced) + ")"
        )
    elif result.state in ("current", "conflict"):
        line += "; no earlier position on this key"
    if settled:
        line += "; it settles " + ", ".join(f'the proposal "{s.value}"' for s in settled)
    for stale in result.stale_documents:
        line += f'; {stale.document_id} still says "{stale.value}"'
    return line + "."


async def _result(
    conn: Any, bank_id: str, op: UUID, document_id: str, fact_id: UUID, *, recorded: bool
) -> DecisionResult:
    claim = await conn.fetchrow(
        f"SELECT * FROM {fq_table('claims')} WHERE bank_id = $1 AND memory_unit_id = $2 AND source_kind = 'decision' "
        f"ORDER BY created_at, id LIMIT 1",
        bank_id,
        fact_id,
    )
    if claim is None:
        raise DecisionError(f"the decision record {document_id} has no claim", status=500)
    superseded = await conn.fetch(
        f"SELECT * FROM {fq_table('claims')} WHERE bank_id = $1 AND superseded_by = $2 AND id <> $2 "
        f"AND memory_unit_id <> $3 AND stated_at <= $4 ORDER BY stated_at DESC, source_rank DESC, id",
        bank_id,
        claim["id"],
        fact_id,
        claim["stated_at"],
    )
    later = None
    if claim["state"] == "superseded" and claim["superseded_by"] is not None:
        later = await conn.fetchrow(f"SELECT * FROM {fq_table('claims')} WHERE id = $1", claim["superseded_by"])
    key = await conn.fetchrow(
        f"SELECT conflict_claim_ids, stale_documents FROM {fq_table('attributes')} "
        f"WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3",
        bank_id,
        claim["subject_entity_id"],
        claim["attribute_key"],
    )
    conflict_ids = [c for c in (key["conflict_claim_ids"] if key else []) if c != claim["id"]]
    conflict = await conn.fetch(
        f"SELECT * FROM {fq_table('claims')} WHERE id = ANY($1::uuid[]) ORDER BY stated_at DESC, id", conflict_ids
    )
    stale: list[Superseded] = []
    for document in key["stale_documents"] if key else []:
        newest = await conn.fetchrow(
            f"SELECT * FROM {fq_table('claims')} WHERE bank_id = $1 AND subject_entity_id = $2 AND attribute_key = $3 "
            f"AND document_id = $4 ORDER BY stated_at DESC, document_order DESC, chunk_index DESC, fact_ordinal DESC "
            f"LIMIT 1",
            bank_id,
            claim["subject_entity_id"],
            claim["attribute_key"],
            document,
        )
        if newest is not None:
            stale.append(_superseded(newest))
    result = DecisionResult(
        message="",
        recorded=recorded,
        operation_id=op,
        document_id=document_id,
        fact_id=fact_id,
        claim_id=claim["id"],
        subject_id=claim["subject_entity_id"],
        subject=claim["subject_text"],
        attribute=claim["attribute_key"],
        value=claim["value_text"],
        stated_at=claim["stated_at"],
        state=claim["state"],
        superseded=[_superseded(row) for row in superseded],
        superseded_by=_superseded(later) if later is not None else None,
        conflict_with=[_superseded(row) for row in conflict],
        stale_documents=stale,
    )
    result.message = message(result)
    return result


async def record_decision(memory: Any, bank_id: str, request: DecisionRequest, request_context: Any) -> DecisionResult:
    """Record Josh's decision as the current claim on its key (specification 10.1; criterion 2)."""
    words, subject, key, value, stated_at, domain = _normalized(request)
    op = operation_id(words, subject, key, stated_at)
    document_id = f"{DECISION_PREFIX}{op}"
    await memory._authenticate_tenant(request_context)
    pool = await memory._get_pool()
    async with pool.acquire() as lock:
        await lock.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", f"{LOCK_PREFIX}{bank_id}:{op}")
        try:
            facts = await lock.fetch(
                f"SELECT id FROM {fq_table('memory_units')} WHERE bank_id = $1 AND document_id = $2 "
                f"ORDER BY created_at, id",
                bank_id,
                document_id,
            )
            if facts:
                fact_id = facts[0]["id"]
                done = await lock.fetchval(
                    f"SELECT count(*) FROM {fq_table('claims')} WHERE bank_id = $1 AND memory_unit_id = $2",
                    bank_id,
                    fact_id,
                )
                if done:
                    return await _result(lock, bank_id, op, document_id, fact_id, recorded=False)
            else:
                fact_id = await _retain(
                    memory,
                    bank_id,
                    request_context,
                    words,
                    subject,
                    key,
                    value,
                    stated_at,
                    domain,
                    request.session_id,
                    op,
                    document_id,
                )
            run_id = uuid.uuid4()
            await record_prestructured(
                memory,
                bank_id,
                fact_id,
                [
                    PrestructuredClaim(
                        subject=subject, attribute=key, value=value, description=request.attribute.strip()
                    )
                ],
                stated_at,
                kind="decision",
                actor=ACTOR,
                request_context=request_context,
                run_id=run_id,
            )
            result = await _result(lock, bank_id, op, document_id, fact_id, recorded=True)
            await ledger.append(
                lock,
                bank_id,
                [
                    ledger.Entry(
                        event="decision-recorded",
                        reason=result.message,
                        claim_ids=[result.claim_id],
                        memory_unit_ids=[fact_id],
                        details={
                            "operation_id": str(op),
                            "document_id": document_id,
                            "session_id": request.session_id,
                            "domain": domain,
                            "superseded": [str(s.claim_id) for s in result.superseded],
                        },
                    )
                ],
                actor=ACTOR,
                run_id=run_id,
            )
            return result
        finally:
            await lock.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", f"{LOCK_PREFIX}{bank_id}:{op}")


async def _retain(
    memory: Any,
    bank_id: str,
    request_context: Any,
    words: str,
    subject: str,
    key: str,
    value: str,
    stated_at: datetime,
    domain: str,
    session_id: str | None,
    op: UUID,
    document_id: str,
) -> UUID:
    """Store the words as one verbatim fact through the engine's retain path; its id."""
    await memory._ensure_bank_exists(bank_id, request_context)
    if await _ensure_strategy(memory, bank_id, request_context):
        async with (await memory._get_pool()).acquire() as conn:
            await ledger.append(
                conn,
                bank_id,
                [
                    ledger.Entry(
                        event="decision-strategy-configured",
                        reason=f"bank retain strategy {DECISION_STRATEGY!r} set to extraction mode {EXTRACTION_MODE!r}",
                        details={"strategy": DECISION_STRATEGY, "retain_extraction_mode": EXTRACTION_MODE},
                    )
                ],
                actor=ACTOR,
                run_id=None,
            )
    await _check_one_chunk(memory, bank_id, request_context, words)
    pool_tag = POOL_PREFIX + pool_for([DOMAIN_PREFIX + domain])
    item = {
        "content": words,
        "context": "A decision Josh stated, recorded verbatim by cortana_record_decision",
        "event_date": stated_at,
        "document_id": document_id,
        "tags": [DECISION_TAG, DOMAIN_PREFIX + domain, pool_tag],
        "observation_scopes": [[pool_tag]],
        "strategy": DECISION_STRATEGY,
        "entities": [{"text": subject, "type": "CONCEPT"}],
        "metadata": {
            "source": "decision",
            "operation_id": str(op),
            "subject": subject,
            "attribute": key,
            "value": value,
            "stated_at": stated_at.isoformat(),
            "session_id": session_id or "",
        },
    }
    unit_ids = await memory.retain_batch_async(
        bank_id, [item], request_context=request_context, strategy=DECISION_STRATEGY
    )
    ids = [UUID(str(unit)) for units in unit_ids for unit in units]
    if len(ids) != 1:
        raise DecisionError(f"the decision record {document_id} produced {len(ids)} facts, not one", status=500)
    return ids[0]
