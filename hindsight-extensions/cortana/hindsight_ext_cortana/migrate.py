"""The migration's structuring pass (specification 13.4 step 4): HSIGHT-8 rehearses it on a scratch
bank restored from the dump, and HSIGHT-10 runs it on the production bank.

``structure_bank`` structures every live world and experience fact that has no claims and that no
structuring call has answered (``reconcile.unstructured_facts``: never structured, or left
``structuring-pending`` by a failed call). It works document by document in statement-time order
(the document's event date from its retain parameters, else its facts' earliest ``mentioned_at``).
Each document goes through the HSIGHT-4 batches with run-level alignment and the source hints its
retain gave (event date, context, content), so a document is keyed as the hook would have keyed it
on retain. Legacy compound facts receive several claims each. Nothing is re-extracted, and
supersession is not applied here: the whole-bank reconciliation that follows
(``hindsight-cortana reconcile --all``) aligns pending keys and settles every key.

Resumable. Each call's ``structured`` ledger row is its progress, so a restart selects only what no
call has answered and continues where the last run stopped. The pass also writes
``structure-pass-started`` with its plan and ``structure-pass-finished`` or ``structure-pass-stopped``
with its counts; every row of one run carries the run's id.

Priority. Documents with a fact whose text or entity names match one of the given patterns
(case-insensitive regular expressions) are structured first, whole, then the rest, so an acceptance
run on the subjects that matter can start before the whole bank is done.

Concurrency. Up to ``concurrency`` documents are in flight at once; a document's batches run one
after another. Model calls overlap. Validating and writing an answer is serialised under one lock,
against the catalog read again at that moment, so a key another document created while this call
was out counts as existing: an answer naming it joins it, and a new name on a subject that now has
keys is stored unaligned for the alignment pass, as for any retain (HSIGHT-4's run-level alignment,
HSIGHT-5 decision 6). With one document in flight this is exactly the hook's behaviour.

Failures. A failed call is retried after a backoff that doubles each time; when its retries are spent
its facts are left ``structuring-pending`` and the pass goes on. A run of consecutive failed calls
stops the pass, and so does any failure that reads as the subscription's usage limit, at once and
without retries (a limit is a stop, not a loop). A stop is clean: calls in flight finish and write,
nothing new starts, and the summary records why.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table

from . import ledger
from .reconcile import unstructured_facts
from .structuring import VERSION, prompt
from .structuring.batching import Batch, make_batches, render
from .structuring.engine import EngineModel, PgStore, SourceHint, is_decision_item
from .structuring.records import Entity
from .structuring.runner import candidate_catalog, settle_answer

logger = logging.getLogger(__name__)

# Measured on the HSIGHT-8 rehearsal (the production dump of 2026-10-07 restored to scratch; the
# claude-code provider, one CLI subprocess per call): steady throughput 4.2 calls a minute at 1,
# 14.9 at 4 and 28.3 at 8 (about 25,600 facts an hour), with no failed call. It scales almost
# linearly because each call waits on the provider. 8 was not exceeded: at 8 the machine's load was
# 8.7 on 14 cores with 2 GB in model subprocesses, and a larger burst competes with every other use
# of the same subscription for no saving in its total usage.
DEFAULT_CONCURRENCY = 8
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# The provider's own words when the subscription's usage limit is reached (the claude-code provider
# passes the CLI's result text through, e.g. "You've hit your weekly limit · resets ...", with
# api_error_status 429), and the generic forms other providers use.
USAGE_LIMIT = re.compile(
    r"hit your [^.]*limit|usage limit|rate[ _-]?limit|too many requests|\b429\b|quota|limit\b[^.]*\bresets",
    re.IGNORECASE,
)


PASS_LOCK = "cortana-structure-pass:"


class PassRunning(RuntimeError):
    """Another structure pass is running on the bank."""


def is_usage_limit(error: BaseException) -> bool:
    return bool(USAGE_LIMIT.search(f"{type(error).__name__}: {error}"))


@dataclass(frozen=True)
class DocumentWork:
    """One document's facts for one phase, with the hint its retain gave."""

    document_id: str | None
    start: datetime
    fact_ids: tuple[UUID, ...]
    hint: SourceHint


@dataclass
class Plan:
    priority: list[DocumentWork]
    rest: list[DocumentWork]
    skipped_decision_facts: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "priority_documents": len(self.priority),
            "priority_facts": sum(len(d.fact_ids) for d in self.priority),
            "rest_documents": len(self.rest),
            "rest_facts": sum(len(d.fact_ids) for d in self.rest),
            "skipped_decision_facts": self.skipped_decision_facts,
        }


@dataclass
class PassReport:
    run_id: UUID
    bank_id: str
    started_at: datetime
    plan: dict[str, int]
    settings: dict[str, Any]
    phases_done: list[str] = field(default_factory=list)
    documents_done: int = 0
    calls: int = 0
    failed_calls: int = 0
    retries: int = 0
    facts_answered: int = 0
    facts_with_claims: int = 0
    compound_facts: int = 0
    facts_without_claims: int = 0
    facts_pending: int = 0
    claims: int = 0
    unaligned_claims: int = 0
    new_keys: int = 0
    call_seconds: list[float] = field(default_factory=list)
    stopped: str | None = None
    finished_at: datetime | None = None

    @property
    def planned_facts(self) -> int:
        """The facts of the phases this run takes on."""
        return self.plan["selected_facts"]

    def elapsed(self) -> float:
        return ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds()

    def facts_per_hour(self) -> float:
        elapsed = self.elapsed()
        return self.facts_answered / elapsed * 3600 if elapsed > 0 else 0.0

    def remaining_facts(self) -> int:
        return max(self.planned_facts - self.facts_answered - self.facts_pending, 0)

    def eta_seconds(self) -> float | None:
        rate = self.facts_per_hour()
        return self.remaining_facts() / rate * 3600 if rate > 0 else None

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        seconds = data.pop("call_seconds")
        data |= {
            "run_id": str(self.run_id),
            "elapsed_seconds": round(self.elapsed(), 1),
            "facts_per_hour": round(self.facts_per_hour(), 1),
            "remaining_facts": self.remaining_facts(),
            "eta_seconds": round(eta) if (eta := self.eta_seconds()) is not None else None,
            "call_seconds_median": round(median(seconds), 1) if seconds else None,
            "call_seconds_max": round(max(seconds), 1) if seconds else None,
        }
        return data


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


async def make_plan(conn: Any, bank_id: str, patterns: Sequence[re.Pattern[str]] = ()) -> Plan:
    """What the pass has left to do: the unstructured facts by document, in statement-time order, the
    documents with a fact that touches a priority pattern first. A priority document goes whole, so
    each document is still structured in one run, as the hook would have structured it on retain."""
    ids = await unstructured_facts(conn, bank_id)
    if not ids:
        return Plan(priority=[], rest=[])
    facts = await conn.fetch(
        f"""
        SELECT mu.id, mu.document_id, mu.text, COALESCE(mu.mentioned_at, mu.created_at) AS at,
               COALESCE(array_agg(e.canonical_name) FILTER (WHERE e.id IS NOT NULL), '{{}}') AS names
        FROM {fq_table("memory_units")} mu
        LEFT JOIN {fq_table("unit_entities")} ue ON ue.unit_id = mu.id
        LEFT JOIN {fq_table("entities")} e ON e.id = ue.entity_id
        WHERE mu.bank_id = $1 AND mu.id = ANY($2::uuid[])
        GROUP BY mu.id
        """,
        bank_id,
        ids,
    )
    documents = await conn.fetch(
        f"""
        SELECT d.id, d.retain_params, d.tags, left(d.original_text, 200) AS head,
               (SELECT min(COALESCE(mu.mentioned_at, mu.created_at)) FROM {fq_table("memory_units")} mu
                WHERE mu.bank_id = d.bank_id AND mu.document_id = d.id) AS first_at
        FROM {fq_table("documents")} d
        WHERE d.bank_id = $1 AND d.id = ANY($2::text[])
        """,
        bank_id,
        sorted({row["document_id"] for row in facts if row["document_id"]}),
    )
    hints: dict[str | None, SourceHint] = {}
    starts: dict[str | None, datetime] = {}
    decision_documents: set[str] = set()
    for row in documents:
        params = _json(row["retain_params"])
        if is_decision_item({"strategy": params.get("strategy"), "tags": list(row["tags"] or [])}):
            decision_documents.add(row["id"])
            continue
        event_date = _parse_time(params.get("event_date"))
        hints[row["id"]] = SourceHint(event_date=event_date, context=params.get("context") or "", content=row["head"])
        starts[row["id"]] = event_date or row["first_at"]

    by_document: dict[str | None, list[Any]] = {}
    priority_documents: set[str | None] = set()
    skipped = 0
    for row in facts:
        document = row["document_id"]
        if document in decision_documents:
            skipped += 1
            continue
        by_document.setdefault(document, []).append(row)
        haystack = [row["text"] or "", *row["names"]]
        if any(pattern.search(text) for pattern in patterns for text in haystack):
            priority_documents.add(document)

    def work(priority: bool) -> list[DocumentWork]:
        out = []
        for document, rows in by_document.items():
            if (document in priority_documents) != priority:
                continue
            start = starts.get(document) or min(row["at"] for row in rows)
            ordered = sorted(rows, key=lambda row: (row["at"], row["id"]))
            hint = hints.get(document) or SourceHint()
            out.append(DocumentWork(document, start, tuple(row["id"] for row in ordered), hint))
        return sorted(out, key=lambda d: (d.start, d.document_id or ""))

    return Plan(priority=work(True), rest=work(False), skipped_decision_facts=skipped)


class StructurePass:
    """One run of the pass over a bank. ``run`` structures the plan's phases in order."""

    def __init__(
        self,
        engine: Any,
        bank_id: str,
        *,
        request_context: Any,
        concurrency: int = DEFAULT_CONCURRENCY,
        retries: int = DEFAULT_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        model: Any = None,
        actor: str = "operator",
        on_progress: Callable[[PassReport], None] | None = None,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.engine = engine
        self.bank_id = bank_id
        self.request_context = request_context
        self.concurrency = concurrency
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.model = model
        self.actor = actor
        self.on_progress = on_progress
        self.store = PgStore(engine, bank_id, actor=actor)
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        self.report: PassReport | None = None

    async def run(
        self, *, patterns: Sequence[re.Pattern[str]] = (), phases: Sequence[str] = ("priority", "rest")
    ) -> PassReport:
        """Run the pass, holding the bank's pass lock for its whole length: a second pass on the same
        bank would structure the same facts twice, so it refuses to start (``PassRunning``)."""
        pool = await self.engine._get_pool()
        async with pool.acquire() as lock_conn:
            if not await lock_conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", PASS_LOCK + self.bank_id):
                raise PassRunning(f"another structure pass holds the lock for bank {self.bank_id}")
            try:
                return await self._run(pool, patterns, phases)
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(hashtext($1))", PASS_LOCK + self.bank_id)

    async def _run(self, pool: Any, patterns: Sequence[re.Pattern[str]], phases: Sequence[str]) -> PassReport:
        async with pool.acquire() as conn:
            plan = await make_plan(conn, self.bank_id, patterns)
        settings = {
            "concurrency": self.concurrency,
            "retries": self.retries,
            "backoff_seconds": self.backoff_seconds,
            "max_consecutive_failures": self.max_consecutive_failures,
            "phases": list(phases),
            "priority_patterns": len(patterns),
            "prompt_version": VERSION,
        }
        counts = plan.counts()
        counts["selected_facts"] = sum(counts[f"{phase}_facts"] for phase in ("priority", "rest") if phase in phases)
        report = self.report = PassReport(
            run_id=uuid.uuid4(), bank_id=self.bank_id, started_at=datetime.now(UTC), plan=counts, settings=settings
        )
        if self.model is None and counts["selected_facts"]:
            self.model = await EngineModel.for_bank(self.engine, self.bank_id, self.request_context)
        await self._ledger("structure-pass-started", f"plan {report.plan}", {"plan": report.plan, "settings": settings})
        for phase, documents in (("priority", plan.priority), ("rest", plan.rest)):
            if phase not in phases:
                continue
            queue = deque(documents)

            async def worker(queue: deque[DocumentWork] = queue) -> None:
                while queue and not self._stop.is_set():
                    await self._document(queue.popleft())

            await asyncio.gather(*(worker() for _ in range(self.concurrency)))
            if self._stop.is_set():
                break
            report.phases_done.append(phase)
        report.finished_at = datetime.now(UTC)
        event = "structure-pass-stopped" if report.stopped else "structure-pass-finished"
        reason = report.stopped or f"{report.facts_answered} facts answered, {report.claims} claims"
        await self._ledger(event, reason, report.summary())
        logger.info("cortana structure pass bank=%s %s", self.bank_id, report.summary())
        return report

    async def _document(self, work: DocumentWork) -> None:
        facts = await self.store.load_facts(list(work.fact_ids), {fact_id: work.hint for fact_id in work.fact_ids})
        facts = await self.store.rederive(facts)
        run_keys: set[tuple[UUID, str]] = set()
        run_subjects: dict[UUID, Entity] = {}
        for batch in make_batches(facts):
            if self._stop.is_set():
                return
            await self._batch(batch, run_keys, run_subjects)
        if not self._stop.is_set():
            assert self.report is not None
            self.report.documents_done += 1

    async def _batch(self, batch: Batch, run_keys: set[tuple[UUID, str]], run_subjects: dict[UUID, Entity]) -> None:
        report = self.report
        assert report is not None
        fact_ids = [fact.id for fact in batch.facts]
        details = {"run_id": str(report.run_id), "prompt_version": VERSION, "model": self.model.name, "pass": True}
        started = time.monotonic()
        raw, subjects, error = None, None, None
        for attempt in range(self.retries + 1):
            try:
                subjects, catalog = await candidate_catalog(batch, self.store, run_subjects)
                raw = await self.model.answer(prompt(), render(batch, catalog, subjects))
                error = None
                break
            except Exception as failure:
                error = failure
                if is_usage_limit(failure):
                    self._halt(f"usage limit reached: {type(failure).__name__}: {failure}")
                    break
                if attempt < self.retries:
                    report.retries += 1
                    delay = self.backoff_seconds * 2**attempt
                    logger.warning("cortana structure pass: call failed (%s); retrying in %.0f s", failure, delay)
                    await asyncio.sleep(delay)
        report.calls += 1
        if error is None:
            try:
                async with self._lock:
                    catalog_now = await self.store.load_catalog(subjects)
                    result = await settle_answer(
                        batch,
                        raw,
                        catalog_now,
                        self.store,
                        self.model,
                        prompt_version=VERSION,
                        run_keys=frozenset(run_keys),
                    )
                    await self.store.write(batch, result, {**details, "issues": result.issues})
                    if result.unstructured:
                        await self.store.record_pending(
                            result.unstructured, "the answer gave these facts no valid claim", details
                        )
            except Exception as failure:
                error = failure
        seconds = time.monotonic() - started
        report.call_seconds.append(seconds)
        if error is not None:
            reason = f"structuring call failed: {type(error).__name__}: {error}"
            report.failed_calls += 1
            report.facts_pending += len(fact_ids)
            await self.store.record_pending(fact_ids, reason, details)
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                self._halt(f"{self._consecutive_failures} consecutive failed calls; last: {reason}")
            self._progress()
            return
        self._consecutive_failures = 0
        run_keys.update((a.subject_id, a.key) for a in result.new_attributes if a.merged_into is None)
        for claim in result.claims:
            run_subjects.setdefault(claim.subject_entity_id, Entity(claim.subject_entity_id, claim.subject_text))
        per_fact: dict[UUID, int] = {}
        for claim in result.claims:
            per_fact[claim.memory_unit_id] = per_fact.get(claim.memory_unit_id, 0) + 1
        report.facts_answered += len(fact_ids)
        report.facts_with_claims += len(per_fact)
        report.compound_facts += sum(1 for count in per_fact.values() if count > 1)
        report.facts_without_claims += len(result.unstructured)
        report.claims += len(result.claims)
        report.unaligned_claims += sum(1 for claim in result.claims if claim.state == "unaligned")
        report.new_keys += sum(1 for a in result.new_attributes if a.merged_into is None)
        self._progress()

    def _halt(self, reason: str) -> None:
        assert self.report is not None
        if not self._stop.is_set():
            self.report.stopped = reason
            self._stop.set()
            logger.error("cortana structure pass stopping: %s", reason)

    def _progress(self) -> None:
        if self.on_progress is not None and self.report is not None:
            self.on_progress(self.report)

    async def _ledger(self, event: str, reason: str, details: dict[str, Any]) -> None:
        assert self.report is not None
        async with (await self.engine._get_pool()).acquire() as conn:
            await ledger.append(
                conn,
                self.bank_id,
                [ledger.Entry(event, reason=reason, details=json.loads(json.dumps(details, default=str)))],
                actor=self.actor,
                run_id=self.report.run_id,
            )


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return value if isinstance(value, datetime) else None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DocumentWork",
    "PassReport",
    "PassRunning",
    "Plan",
    "StructurePass",
    "compile_patterns",
    "is_usage_limit",
    "make_plan",
]
