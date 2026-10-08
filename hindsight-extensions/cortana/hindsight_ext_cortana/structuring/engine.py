"""Structuring inside the engine: what ``on_retain_complete`` runs, and the entry point for claims
that arrive already structured (decision records, HSIGHT-6).

The engine offers extensions no public accessor for its database or its model provider
(specification 17 item 3, HSIGHT-2 report section 9). This module reaches them as the engine's own
code does, through private attributes pinned by ``tests/test_model_accessor.py`` and
``tests/test_structuring_engine.py``, so an upstream pull that renames them fails the suite:

- ``engine._get_pool()``: the asyncpg pool; tables are named with ``fq_table``, which follows the
  tenant schema the engine set for the operation;
- ``engine._config_resolver`` and ``engine._retain_llm_config``: the retain provider, bound to the
  bank as the retain path binds it. With the ``claude-code`` provider every call runs the Claude
  Agent SDK with a fresh ``CLAUDE_CONFIG_DIR`` and no tools;
- ``engine.entity_resolver``: the engine's resolver, used read-only (inside a transaction that is
  rolled back) to resolve a subject the model named that is not one of its fact's entities.

Structuring writes only our tables (``claims``, ``attributes``, ``ledger``). It reads the engine's
facts, chunks and entities and never writes them.
"""

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table

from . import VERSION
from .batching import Batch
from .names import names_overlap, normalize_name
from .records import BatchResult, Catalog, CatalogEntry, Chunk, Entity, FactInput, Source, SourceKind
from .runner import RunReport, structure, structure_batch
from .validation import ClaimAnswer, subject_names_to_resolve, validate

logger = logging.getLogger(__name__)

# Model-call retries for one structuring call. The provider's default retries a failing call for
# minutes, which would hold the retain's worker slot; a call that still fails leaves its facts
# pending for reconciliation (specification 6.4), which is the repair path.
STRUCTURING_MAX_RETRIES = 2

OPERATION = "cortana-structuring"
CORRECTION_PREFIX = "Correction:"


def source_kind(document_id: str | None, content: str) -> SourceKind:
    """A plugin session save, a correction document (specification 5.3), or a document."""
    if document_id and document_id.startswith("conversation:"):
        return "session"
    if content.lstrip().startswith(CORRECTION_PREFIX):
        return "correction"
    return "document"


class EngineModel:
    """The engine's retain provider, bound to a bank (or unbound, for the suite)."""

    def __init__(self, llm: Any):
        self._llm = llm
        self.name = f"{getattr(llm, 'provider', 'unknown')}/{getattr(llm, 'model', 'unknown')}"

    @classmethod
    async def for_bank(cls, engine: Any, bank_id: str, request_context: Any) -> "EngineModel":
        config = await engine._config_resolver.resolve_full_config(bank_id, request_context)
        return cls(engine._retain_llm_config.with_config(config, bank_id=bank_id, operation=OPERATION))

    async def answer(self, system: str, user: str) -> Any:
        from .validation import StructuringAnswer

        result = await self._llm.call(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=StructuringAnswer,
            skip_validation=True,
            scope="memory",
            max_retries=STRUCTURING_MAX_RETRIES,
        )
        return result.content


class PgStore:
    """Our tables in the engine's database, for one bank, in the operation's tenant schema."""

    def __init__(self, engine: Any, bank_id: str, *, actor: str = "hook"):
        self.engine = engine
        self.bank_id = bank_id
        self.actor = actor

    async def _pool(self) -> Any:
        return await self.engine._get_pool()

    async def load_catalog(self, subject_ids: Iterable[UUID]) -> Catalog:
        ids = list(subject_ids)
        catalog: Catalog = {subject: {} for subject in ids}
        if not ids:
            return catalog
        async with (await self._pool()).acquire() as conn:
            rows = await conn.fetch(
                f"SELECT subject_entity_id, attribute_key, description, example_value, merged_into "
                f"FROM {fq_table('attributes')} WHERE bank_id = $1 AND subject_entity_id = ANY($2::uuid[])",
                self.bank_id,
                ids,
            )
        for row in rows:
            entry = CatalogEntry(
                subject_id=row["subject_entity_id"],
                key=row["attribute_key"],
                description=row["description"],
                example_value=row["example_value"],
                merged_into=row["merged_into"],
            )
            catalog.setdefault(entry.subject_id, {})[entry.key] = entry
        return catalog

    async def related_subjects(self, subjects: dict[UUID, Entity]) -> dict[UUID, Entity]:
        """Existing subjects with keys whose names contain, or are contained in, a candidate's name."""
        if not subjects:
            return {}
        async with (await self._pool()).acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT e.id, e.canonical_name
                FROM {fq_table("entities")} e
                JOIN {fq_table("attributes")} a
                  ON a.bank_id = e.bank_id AND a.subject_entity_id = e.id AND a.merged_into IS NULL
                WHERE e.bank_id = $1 AND NOT (e.id = ANY($2::uuid[]))
                  AND EXISTS (SELECT 1 FROM unnest($3::text[]) AS n
                              WHERE strpos(LOWER(e.canonical_name), LOWER(n)) > 0
                                 OR strpos(LOWER(n), LOWER(e.canonical_name)) > 0)
                """,
                self.bank_id,
                list(subjects),
                [entity.name for entity in subjects.values()],
            )
        return {
            row["id"]: Entity(row["id"], row["canonical_name"])
            for row in rows
            if any(names_overlap(row["canonical_name"], entity.name) for entity in subjects.values())
        }

    async def resolve_subjects(self, names: set[str], batch: Batch) -> dict[str, Entity | None]:
        """Each name's existing entity, or ``None``. An exact name match first, then the engine's
        resolver inside a transaction that is rolled back, so a name it would create stays unknown."""
        resolved: dict[str, Entity | None] = {name: None for name in names}
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, canonical_name FROM {fq_table('entities')} "
                f"WHERE bank_id = $1 AND entity_kind <> 'label' "
                f"AND LOWER(canonical_name) = ANY(SELECT LOWER(n) FROM unnest($2::text[]) AS n)",
                self.bank_id,
                list(names),
            )
        by_name = {normalize_name(row["canonical_name"]): Entity(row["id"], row["canonical_name"]) for row in rows}
        for name in names:
            resolved[name] = by_name.get(normalize_name(name))
        remaining = sorted(name for name, entity in resolved.items() if entity is None)
        if remaining:
            nearby = [{"text": entity.name} for entity in batch.candidate_subjects().values()]
            # A task of its own, so the resolver's per-task pending statistics it accumulates are
            # this probe's alone and can be discarded without touching the retain's.
            probed = await asyncio.create_task(self._probe_resolver(remaining, nearby))
            resolved.update(probed)
        return resolved

    async def _probe_resolver(self, names: list[str], nearby: list[dict]) -> dict[str, Entity | None]:
        resolver = self.engine.entity_resolver
        mentions = [{"text": name, "type": "CONCEPT", "nearby_entities": nearby} for name in names]
        pool = await self._pool()
        async with pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            try:
                found = await resolver.resolve_entities_batch(
                    self.bank_id, mentions, context="", unit_event_date=datetime.now(UTC), conn=conn
                )
            finally:
                await transaction.rollback()
                resolver.discard_pending_stats()
            ids = [_as_uuid(entity.entity_id) for entity in found]
            existing = await conn.fetch(
                f"SELECT id, canonical_name FROM {fq_table('entities')} "
                f"WHERE id = ANY($1::uuid[]) AND entity_kind <> 'label'",
                ids,
            )
        known = {row["id"]: Entity(row["id"], row["canonical_name"]) for row in existing}
        return {name: known.get(_as_uuid(entity.entity_id)) for name, entity in zip(names, found, strict=True)}

    async def write(self, batch: Batch, result: BatchResult, details: dict[str, Any]) -> None:
        claim_ids = [uuid.uuid4() for _ in result.claims]
        async with (await self._pool()).acquire() as conn, conn.transaction():
            if result.claims:
                await conn.executemany(
                    f"""
                    INSERT INTO {fq_table("claims")} (
                        id, bank_id, memory_unit_id, subject_entity_id, subject_text, attribute_key,
                        value_text, provisional, stated_at, document_order, chunk_index, fact_ordinal,
                        source_rank, document_id, chunk_id, source_kind, state, prompt_version, model,
                        content_hash, stated_at_source
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                              $18, $19, $20, $21)
                    """,
                    [
                        (
                            claim_id,
                            self.bank_id,
                            c.memory_unit_id,
                            c.subject_entity_id,
                            c.subject_text,
                            c.attribute_key,
                            c.value_text,
                            c.provisional,
                            c.stated_at,
                            c.document_order,
                            c.chunk_index,
                            c.fact_ordinal,
                            int(c.source_rank),
                            c.document_id,
                            c.chunk_id,
                            c.source_kind,
                            c.state,
                            c.prompt_version,
                            c.model,
                            c.content_hash,
                            str(c.stated_at_source),
                        )
                        for claim_id, c in zip(claim_ids, result.claims, strict=True)
                    ],
                )
            if result.new_attributes:
                await conn.executemany(
                    f"""
                    INSERT INTO {fq_table("attributes")}
                        (bank_id, subject_entity_id, attribute_key, description, example_value, merged_into)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (bank_id, subject_entity_id, attribute_key) DO NOTHING
                    """,
                    [
                        (self.bank_id, a.subject_id, a.key, a.description, a.example_value, a.merged_into)
                        for a in result.new_attributes
                    ],
                )
            await self._ledger(
                conn,
                "structured",
                reason=f"{len(result.claims)} claims for {len(batch.facts) - len(result.unstructured)} facts",
                claim_ids=claim_ids,
                memory_unit_ids=[fact.id for fact in batch.facts],
                details={
                    **details,
                    "unaligned": sum(1 for c in result.claims if c.state == "unaligned"),
                    "new_attributes": [
                        f"{a.key}" + (f" -> {a.merged_into}" if a.merged_into else "") for a in result.new_attributes
                    ],
                },
            )

    async def record_pending(self, fact_ids: list[UUID], reason: str, details: dict[str, Any]) -> None:
        async with (await self._pool()).acquire() as conn:
            await self._ledger(conn, "structuring-pending", reason=reason, memory_unit_ids=fact_ids, details=details)

    async def _ledger(
        self,
        conn: Any,
        event: str,
        *,
        reason: str,
        memory_unit_ids: list[UUID],
        details: dict[str, Any],
        claim_ids: list[UUID] | None = None,
    ) -> None:
        run_id = details.get("run_id")
        await conn.execute(
            f"""
            INSERT INTO {fq_table("ledger")} (bank_id, event, actor, reason, run_id, claim_ids, memory_unit_ids, details)
            VALUES ($1, $2, $3, $4, $5, $6::uuid[], $7::uuid[], $8::jsonb)
            """,
            self.bank_id,
            event,
            self.actor,
            reason,
            UUID(run_id) if run_id else None,
            claim_ids or [],
            memory_unit_ids,
            json.dumps(details, default=str),
        )

    async def facts_with_claims(self, fact_ids: list[UUID]) -> set[UUID]:
        async with (await self._pool()).acquire() as conn:
            rows = await conn.fetch(
                f"SELECT DISTINCT memory_unit_id FROM {fq_table('claims')} "
                f"WHERE bank_id = $1 AND memory_unit_id = ANY($2::uuid[])",
                self.bank_id,
                fact_ids,
            )
        return {row["memory_unit_id"] for row in rows}

    async def load_facts(self, fact_ids: list[UUID], sources: dict[UUID, "SourceHint"]) -> list[FactInput]:
        """The facts with their chunks, entities and ordinals. ``sources`` gives, per fact, what the
        retain said about its document (event date, context, position); facts without a hint take
        their document's earliest ``mentioned_at`` as its date."""
        async with (await self._pool()).acquire() as conn:
            facts = await conn.fetch(
                f"""
                SELECT mu.id, mu.text, mu.chunk_id, mu.document_id, mu.mentioned_at, c.chunk_index, c.chunk_text
                FROM {fq_table("memory_units")} mu
                LEFT JOIN {fq_table("chunks")} c ON c.chunk_id = mu.chunk_id
                WHERE mu.bank_id = $1 AND mu.id = ANY($2::uuid[]) AND mu.fact_type IN ('world', 'experience')
                """,
                self.bank_id,
                fact_ids,
            )
            chunk_ids = sorted({row["chunk_id"] for row in facts if row["chunk_id"]})
            ordinals = await conn.fetch(
                f"""
                SELECT id, row_number() OVER (PARTITION BY chunk_id ORDER BY mentioned_at NULLS LAST, created_at, id) - 1
                       AS ordinal
                FROM {fq_table("memory_units")}
                WHERE bank_id = $1 AND chunk_id = ANY($2::text[]) AND fact_type IN ('world', 'experience')
                """,
                self.bank_id,
                chunk_ids,
            )
            entities = await conn.fetch(
                f"""
                SELECT ue.unit_id, e.id, e.canonical_name
                FROM {fq_table("unit_entities")} ue JOIN {fq_table("entities")} e ON e.id = ue.entity_id
                WHERE ue.unit_id = ANY($1::uuid[]) AND e.entity_kind <> 'label'
                ORDER BY e.canonical_name
                """,
                fact_ids,
            )
        ordinal_of = {row["id"]: row["ordinal"] for row in ordinals}
        entities_of: dict[UUID, list[Entity]] = defaultdict(list)
        for row in entities:
            entities_of[row["unit_id"]].append(Entity(row["id"], row["canonical_name"]))
        earliest: dict[str | None, datetime] = {}
        for row in facts:
            if row["mentioned_at"] is not None:
                current = earliest.get(row["document_id"])
                earliest[row["document_id"]] = min(current, row["mentioned_at"]) if current else row["mentioned_at"]
        inputs = []
        for row in facts:
            hint = sources.get(row["id"]) or SourceHint()
            date = hint.event_date or earliest.get(row["document_id"]) or datetime.now(UTC)
            chunk_text = row["chunk_text"] or ""
            source = Source(
                kind=source_kind(row["document_id"], hint.content if hint.content is not None else chunk_text),
                document_id=row["document_id"],
                context=hint.context,
                date=date,
                document_order=hint.document_order,
            )
            inputs.append(
                FactInput(
                    id=row["id"],
                    text=row["text"],
                    entities=tuple(entities_of.get(row["id"], [])),
                    chunk=Chunk(index=row["chunk_index"] or 0, text=chunk_text, chunk_id=row["chunk_id"]),
                    ordinal=ordinal_of.get(row["id"], 0),
                    source=source,
                )
            )
        return inputs


@dataclass(frozen=True)
class SourceHint:
    """What a retain item says about its document."""

    event_date: datetime | None = None
    context: str = ""
    content: str | None = None
    document_order: int = 0


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _event_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


async def structure_facts(
    engine: Any,
    bank_id: str,
    fact_ids: list[UUID],
    request_context: Any,
    *,
    sources: dict[UUID, SourceHint] | None = None,
    actor: str = "hook",
    model: Any = None,
) -> RunReport | None:
    """Structure the facts that have no claims yet. The reconciliation (HSIGHT-5) calls this for
    facts left ``structuring-pending``; the hook calls it through ``structure_retain``."""
    store = PgStore(engine, bank_id, actor=actor)
    done = await store.facts_with_claims(fact_ids)
    todo = [fact_id for fact_id in fact_ids if fact_id not in done]
    if not todo:
        return None
    facts = await store.load_facts(todo, sources or {})
    if not facts:
        return None
    model = model or await EngineModel.for_bank(engine, bank_id, request_context)
    report = await structure(facts, store, model, prompt_version=VERSION)
    logger.info(
        "cortana structuring bank=%s facts=%d claims=%d calls=%d pending=%d run=%s",
        bank_id,
        len(facts),
        report.claims,
        len(report.calls),
        len(report.pending),
        report.run_id,
    )
    return report


async def structure_retain(engine: Any, result: Any) -> RunReport | None:
    """``on_retain_complete``'s work: structure the retain's new facts (specification 5.2)."""
    sources: dict[UUID, SourceHint] = {}
    for index, (item, unit_ids) in enumerate(zip(result.contents, result.unit_ids, strict=False)):
        hint = SourceHint(
            event_date=_event_date(item.get("event_date")),
            context=item.get("context") or "",
            content=item.get("content"),
            document_order=index,
        )
        for unit_id in unit_ids:
            sources[_as_uuid(unit_id)] = hint
    if not sources:
        return None
    return await structure_facts(engine, result.bank_id, list(sources), result.request_context, sources=sources)


@dataclass(frozen=True)
class PrestructuredClaim:
    """A claim given whole by its caller, such as the decision tool's arguments (HSIGHT-6)."""

    subject: str
    attribute: str
    value: str
    provisional: bool = False
    description: str | None = None
    same_as: str | None = None


async def record_prestructured(
    engine: Any,
    bank_id: str,
    fact_id: UUID,
    claims: list[PrestructuredClaim],
    stated_at: datetime,
    *,
    kind: SourceKind = "decision",
    actor: str = "decision-tool",
) -> BatchResult:
    """Write claims that arrive already structured, without a model call (specification 5.3).

    The decision record's fact is stored by the engine first (chunks mode, no extraction); this
    validates the given claims exactly as a model's would be (subject resolution, key
    normalization, unaligned keys, ``same_as``), times them at ``stated_at`` with the decision
    rank, and writes them. Supersession (HSIGHT-5) runs after it as after the hook.
    """
    store = PgStore(engine, bank_id, actor=actor)
    (loaded,) = await store.load_facts([fact_id], {fact_id: SourceHint(event_date=stated_at)})
    fact = FactInput(
        id=loaded.id,
        text=loaded.text,
        entities=loaded.entities,
        chunk=loaded.chunk,
        ordinal=loaded.ordinal,
        source=Source(kind=kind, document_id=loaded.source.document_id, context="", date=stated_at),
    )
    batch = Batch(source=fact.source, facts=[fact])
    answers = {
        "F1": [
            ClaimAnswer(
                subject=c.subject,
                attribute=c.attribute,
                value=c.value,
                provisional=c.provisional,
                description=c.description,
                same_as=c.same_as,
            )
            for c in claims
        ]
    }
    catalog = await store.load_catalog(batch.candidate_subjects())
    names = subject_names_to_resolve(batch, answers)
    resolved = await store.resolve_subjects(names, batch) if names else {}
    extra = {entity.id for entity in resolved.values() if entity is not None} - set(catalog)
    if extra:
        catalog = {**catalog, **(await store.load_catalog(extra))}
    result = validate(batch, answers, catalog, resolved, bank_id=bank_id, prompt_version=None, model=None)
    await store.write(batch, result, {"run_id": str(uuid.uuid4()), "source": kind})
    return result


__all__ = [
    "EngineModel",
    "PgStore",
    "PrestructuredClaim",
    "SourceHint",
    "record_prestructured",
    "structure_batch",
    "structure_facts",
    "structure_retain",
]
