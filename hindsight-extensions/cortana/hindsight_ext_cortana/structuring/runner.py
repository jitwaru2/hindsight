"""Driving structuring calls: batch, ask, validate, write; a failed call leaves its facts pending.

``structure`` is the same for the hook and the suite. What differs is where things live (a
``Store``: the engine's database for the hook, memory for the suite) and who answers (a
``StructuringModel``: the engine's retain provider in both, reached differently).
"""

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from . import VERSION, prompt
from .batching import Batch, make_batches, render
from .records import BatchResult, Catalog, Entity, FactInput
from .validation import parse_answer, subject_names_to_resolve, validate

logger = logging.getLogger(__name__)


class StructuringModel(Protocol):
    """Answers one structuring call with the model's JSON. ``name`` is written on every claim."""

    name: str

    async def answer(self, system: str, user: str) -> Any: ...


class Store(Protocol):
    bank_id: str

    async def load_catalog(self, subject_ids: Iterable[UUID]) -> Catalog: ...

    async def related_subjects(self, subjects: dict[UUID, Entity]) -> dict[UUID, Entity]: ...

    async def resolve_subjects(self, names: set[str], batch: Batch) -> dict[str, Entity | None]: ...

    async def write(self, batch: Batch, result: BatchResult, details: dict[str, Any]) -> None: ...

    async def record_pending(self, fact_ids: list[UUID], reason: str, details: dict[str, Any]) -> None: ...


@dataclass
class CallRecord:
    facts: int
    seconds: float
    claims: int = 0
    unstructured: int = 0
    error: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    run_id: UUID
    calls: list[CallRecord] = field(default_factory=list)
    results: list[BatchResult] = field(default_factory=list)
    pending: list[UUID] = field(default_factory=list)

    @property
    def claims(self) -> int:
        return sum(len(result.claims) for result in self.results)


async def structure_batch(
    batch: Batch,
    store: Store,
    model: StructuringModel,
    *,
    prompt_version: str,
    run_keys: frozenset[tuple[UUID, str]] = frozenset(),
    run_subjects: dict[UUID, Entity] | None = None,
) -> BatchResult:
    """One call: render with the catalog as it stands, ask, validate. Raises on a failed call.

    The call is shown the catalog of the facts' entities, of existing subjects whose names are
    variants of theirs, and of the subjects earlier calls of the same retain gave claims to
    (``run_subjects``), so a retain split into several calls keys its facts as one call would."""
    subjects = batch.candidate_subjects() | (run_subjects or {})
    subjects |= await store.related_subjects(subjects)
    catalog = await store.load_catalog(subjects)
    raw = await model.answer(prompt(), render(batch, catalog, subjects))
    answers, issues = parse_answer(raw, batch)
    names = subject_names_to_resolve(batch, answers)
    resolved = await store.resolve_subjects(names, batch) if names else {}
    extra = {entity.id for entity in resolved.values() if entity is not None} - set(catalog)
    if extra:
        catalog = {**catalog, **(await store.load_catalog(extra))}
    result = validate(
        batch,
        answers,
        catalog,
        resolved,
        bank_id=store.bank_id,
        prompt_version=prompt_version,
        model=model.name,
        run_keys=run_keys,
    )
    result.issues[:0] = issues
    return result


async def structure(
    facts: list[FactInput], store: Store, model: StructuringModel, *, prompt_version: str = VERSION
) -> RunReport:
    """Structure facts in batches against the store's catalog; record what fails as pending."""
    report = RunReport(run_id=uuid.uuid4())
    run_keys: set[tuple[UUID, str]] = set()
    run_subjects: dict[UUID, Entity] = {}
    for batch in make_batches(facts):
        started = time.monotonic()
        details = {"run_id": str(report.run_id), "prompt_version": prompt_version, "model": model.name}
        fact_ids = [fact.id for fact in batch.facts]
        try:
            result = await structure_batch(
                batch,
                store,
                model,
                prompt_version=prompt_version,
                run_keys=frozenset(run_keys),
                run_subjects=dict(run_subjects),
            )
        except Exception as error:
            reason = f"structuring call failed: {type(error).__name__}: {error}"
            logger.warning("cortana structuring: %s (%d facts left pending)", reason, len(fact_ids))
            report.calls.append(CallRecord(facts=len(fact_ids), seconds=time.monotonic() - started, error=reason))
            report.pending.extend(fact_ids)
            await store.record_pending(fact_ids, reason, details)
            continue
        await store.write(batch, result, {**details, "issues": result.issues})
        run_keys.update((a.subject_id, a.key) for a in result.new_attributes if a.merged_into is None)
        for claim in result.claims:
            run_subjects.setdefault(claim.subject_entity_id, Entity(claim.subject_entity_id, claim.subject_text))
        if result.unstructured:
            report.pending.extend(result.unstructured)
            await store.record_pending(result.unstructured, "the answer gave these facts no valid claim", details)
        report.results.append(result)
        report.calls.append(
            CallRecord(
                facts=len(fact_ids),
                seconds=time.monotonic() - started,
                claims=len(result.claims),
                unstructured=len(result.unstructured),
                issues=result.issues,
            )
        )
    return report
