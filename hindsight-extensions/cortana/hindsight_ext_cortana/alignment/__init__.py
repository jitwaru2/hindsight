"""Alignment of new keys (HSIGHT-5 decision 2; a proposed edit to specification 6.3).

A key created for a subject that already had keys before the retain starts ``pending`` on its
catalog row, and every claim on it is unaligned (rule S9). This pass resolves pending keys: one
bounded model call per batch of subjects, through the engine's retain provider (the path
structuring uses), shown each subject's established keys with their descriptions and example
values and the pending keys with the values stated under them. For each pending key the model
answers ``same_as <established key>`` or ``null`` (distinct). Code then decides:

- ``same_as`` naming an established (aligned) key of the subject, or a pending key the same answer
  marks distinct, is recorded as an attribute merge (``merges.merge_key``; reversible, in the
  ledger); the claims are re-keyed and the caller recomputes the key;
- ``null`` marks the key ``aligned`` as its own key (``merges.mark_distinct``), after which S1
  applies among its claims;
- anything else (a missing answer, an unknown target) leaves the key pending for the next run.

If the call fails the keys stay pending, the ledger records ``alignment-failed``, and
``reconcile`` retries. Alignment never calls recall. Keys of subjects that resolve to no engine
entity (HSIGHT-4's synthetic subject ids) are not aligned here: their uncertainty is the subject,
not the key, so they stay unaligned until an operator merge or subject resolution aligns them.

``prompt.md`` and ``render`` are versioned together by ``VERSION``.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Protocol
from uuid import UUID

from hindsight_api.engine.schema import fq_table
from pydantic import BaseModel, ConfigDict, ValidationError
from slugify import slugify

from .. import ledger
from ..merges import MergeRefused, mark_distinct, merge_key
from ..supersession import Key

logger = logging.getLogger(__name__)

VERSION = "1"
OPERATION = "cortana-alignment"
# Same reason as structuring's: the provider's default retries for minutes and would hold the
# retain's worker slot; a key whose call fails stays pending for reconciliation.
ALIGNMENT_MAX_RETRIES = 2
# Keys shown per call, established and pending together. A key line is a key, a one-line
# description and an example, so this keeps a call's message in the range structuring's calls
# measured (HSIGHT-4: 80 facts with chunk text). A subject with more keys than this still goes in
# one call of its own, because the model must see every established key to judge a duplicate.
KEYS_PER_CALL = 400
# Values shown per pending key: enough to show what the key holds without repeating its claims.
VALUES_PER_PENDING_KEY = 3


def prompt() -> str:
    """The alignment prompt, sent as the system prompt of every alignment call."""
    return files(__name__).joinpath("prompt.md").read_text(encoding="utf-8")


class KeyAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    same_as: str | None = None


class SubjectAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: str
    keys: list[KeyAnswer]


class AlignmentAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subjects: list[SubjectAnswer]


class AlignmentModel(Protocol):
    name: str

    async def answer(self, system: str, user: str) -> Any: ...


class EngineAligner:
    """The engine's retain provider bound to the bank, answering with ``AlignmentAnswer``."""

    def __init__(self, llm: Any):
        self._llm = llm
        self.name = f"{getattr(llm, 'provider', 'unknown')}/{getattr(llm, 'model', 'unknown')}"

    @classmethod
    async def for_bank(cls, engine: Any, bank_id: str, request_context: Any) -> "EngineAligner":
        config = await engine._config_resolver.resolve_full_config(bank_id, request_context)
        return cls(engine._retain_llm_config.with_config(config, bank_id=bank_id, operation=OPERATION))

    async def answer(self, system: str, user: str) -> Any:
        result = await self._llm.call(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=AlignmentAnswer,
            skip_validation=True,
            scope="memory",
            max_retries=ALIGNMENT_MAX_RETRIES,
        )
        return result.content


@dataclass(frozen=True)
class CatalogKey:
    key: str
    description: str
    example: str | None
    values: tuple[str, ...] = ()


@dataclass
class SubjectKeys:
    subject_id: UUID
    name: str
    established: list[CatalogKey] = field(default_factory=list)
    pending: list[CatalogKey] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.established) + len(self.pending)


@dataclass
class AlignReport:
    merged: list[tuple[str, str, str]] = field(default_factory=list)  # (subject, key, into)
    distinct: list[tuple[str, str]] = field(default_factory=list)
    left_pending: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    affected: set[Key] = field(default_factory=set)
    calls: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.merged or self.distinct or self.failures)


async def load_pending(conn: Any, bank_id: str, keys: set[Key] | None) -> list[SubjectKeys]:
    """Subjects with pending keys (within ``keys`` when given), with their established keys."""
    rows = await conn.fetch(
        f"""
        SELECT a.subject_entity_id, a.attribute_key, a.description, a.example_value, e.canonical_name
        FROM {fq_table("attributes")} a
        JOIN {fq_table("entities")} e ON e.id = a.subject_entity_id AND e.bank_id = a.bank_id
        WHERE a.bank_id = $1 AND a.alignment = 'pending'
        ORDER BY e.canonical_name, a.attribute_key
        """,
        bank_id,
    )
    if keys is not None:
        rows = [row for row in rows if (row["subject_entity_id"], row["attribute_key"]) in keys]
    if not rows:
        return []
    subjects: dict[UUID, SubjectKeys] = {}
    for row in rows:
        subjects.setdefault(row["subject_entity_id"], SubjectKeys(row["subject_entity_id"], row["canonical_name"]))
    values = defaultdict(list)
    for row in await conn.fetch(
        f"""
        SELECT subject_entity_id, attribute_key, value_text FROM (
            SELECT subject_entity_id, attribute_key, value_text,
                   row_number() OVER (PARTITION BY subject_entity_id, attribute_key ORDER BY stated_at DESC, id) AS n
            FROM {fq_table("claims")}
            WHERE bank_id = $1 AND subject_entity_id = ANY($2::uuid[])
        ) ranked WHERE n <= $3
        """,
        bank_id,
        list(subjects),
        VALUES_PER_PENDING_KEY,
    ):
        values[(row["subject_entity_id"], row["attribute_key"])].append(row["value_text"])
    for row in rows:
        subjects[row["subject_entity_id"]].pending.append(
            CatalogKey(
                row["attribute_key"],
                row["description"],
                row["example_value"],
                tuple(values[(row["subject_entity_id"], row["attribute_key"])]),
            )
        )
    for row in await conn.fetch(
        f"SELECT subject_entity_id, attribute_key, description, example_value FROM {fq_table('attributes')} "
        f"WHERE bank_id = $1 AND subject_entity_id = ANY($2::uuid[]) AND alignment = 'aligned' "
        f"ORDER BY attribute_key",
        bank_id,
        list(subjects),
    ):
        subjects[row["subject_entity_id"]].established.append(
            CatalogKey(row["attribute_key"], row["description"], row["example_value"])
        )
    return list(subjects.values())


def batches(subjects: list[SubjectKeys]) -> list[list[SubjectKeys]]:
    """Subjects packed into calls of at most ``KEYS_PER_CALL`` keys; a larger subject goes alone."""
    calls: list[list[SubjectKeys]] = []
    current: list[SubjectKeys] = []
    size = 0
    for subject in subjects:
        if current and size + subject.size > KEYS_PER_CALL:
            calls.append(current)
            current, size = [], 0
        current.append(subject)
        size += subject.size
    if current:
        calls.append(current)
    return calls


def render(call: list[SubjectKeys]) -> str:
    """The message of one alignment call. Labels S1, S2... name the subjects in the answer."""
    parts = []
    for index, subject in enumerate(call, start=1):
        lines = [f"SUBJECT S{index}: {subject.name}", "established keys:"]
        lines += [_line(k) for k in subject.established] or ["(none)"]
        lines.append("new keys to align:")
        for k in subject.pending:
            stated = "; ".join(f'"{v}"' for v in k.values)
            lines.append(_line(k) + (f"; values stated: {stated}" if stated else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _line(k: CatalogKey) -> str:
    example = f' (example: "{k.example}")' if k.example else ""
    return f"- {k.key}: {k.description}{example}"


def decide(call: list[SubjectKeys], raw: Any) -> tuple[dict[UUID, dict[str, str | None]], list[str]]:
    """Per subject, each answered pending key's target (None for distinct); and issues. Code
    accepts a target only when it is an established key of the subject or a pending key the same
    answer marks distinct; any other answer leaves the key out, so it stays pending."""
    issues: list[str] = []
    try:
        answer = AlignmentAnswer.model_validate(raw)
    except ValidationError as error:
        raise ValueError(f"the alignment answer does not validate ({error.error_count()} errors)") from error
    labels = {f"S{index}": subject for index, subject in enumerate(call, start=1)}
    decided: dict[UUID, dict[str, str | None]] = {}
    for item in answer.subjects:
        subject = labels.get(item.subject.strip().upper())
        if subject is None:
            issues.append(f"the answer names subject {item.subject!r}, which the call lacks")
            continue
        pending = {k.key for k in subject.pending}
        established = {k.key for k in subject.established}
        raw_answers = {slugify(k.key): (slugify(k.same_as) if k.same_as else None) for k in item.keys}
        distinct = {key for key, target in raw_answers.items() if key in pending and target is None}
        choices: dict[str, str | None] = {}
        for key, target in raw_answers.items():
            if key not in pending:
                issues.append(f"{subject.name}: the answer names {key!r}, which is not pending")
            elif target is None:
                choices[key] = None
            elif target != key and (target in established or target in distinct):
                choices[key] = target
            else:
                issues.append(f"{subject.name}/{key}: same_as {target!r} is not an established key; left pending")
        decided[subject.subject_id] = choices
    return decided, issues


async def align(
    engine: Any,
    bank_id: str,
    *,
    keys: set[Key] | None,
    request_context: Any,
    actor: str,
    run_id: UUID | None,
    model: AlignmentModel | None = None,
) -> AlignReport:
    """Resolve the pending keys among ``keys`` (every pending key of the bank when None)."""
    report = AlignReport()
    pool = await engine._get_pool()
    async with pool.acquire() as conn:
        subjects = await load_pending(conn, bank_id, keys)
    if not subjects:
        return report
    if model is None:
        model = await EngineAligner.for_bank(engine, bank_id, request_context)
    for call in batches(subjects):
        report.calls += 1
        try:
            decided, issues = decide(call, await model.answer(prompt(), render(call)))
        except Exception as error:
            reason = f"alignment call failed: {type(error).__name__}: {error}"
            logger.warning("cortana alignment: %s", reason)
            report.failures.append(reason)
            async with pool.acquire() as conn:
                await ledger.append(
                    conn,
                    bank_id,
                    [
                        ledger.Entry(
                            "alignment-failed",
                            reason=reason,
                            details={
                                "keys": [f"{s.name}/{k.key}" for s in call for k in s.pending],
                                "prompt_version": VERSION,
                                "model": model.name,
                            },
                        )
                    ],
                    actor=actor,
                    run_id=run_id,
                )
            continue
        for issue in issues:
            logger.info("cortana alignment: %s", issue)
        for subject in call:
            choices = decided.get(subject.subject_id, {})
            # Distinct keys first, so a pending key may be merged into another the answer keeps.
            ordered = sorted(subject.pending, key=lambda k: choices.get(k.key, "") is not None)
            for pending in ordered:
                if pending.key not in choices:
                    report.left_pending.append((subject.name, pending.key))
                    continue
                target = choices[pending.key]
                note = f"alignment prompt {VERSION}, {model.name}"
                if target is None:
                    if await mark_distinct(
                        engine,
                        bank_id,
                        subject.subject_id,
                        pending.key,
                        actor=actor,
                        run_id=run_id,
                        reason=f"distinct ({note})",
                        subject_text=subject.name,
                    ):
                        report.distinct.append((subject.name, pending.key))
                        report.affected.add((subject.subject_id, pending.key))
                    continue
                try:
                    merged = await merge_key(
                        engine,
                        bank_id,
                        subject.subject_id,
                        pending.key,
                        target,
                        actor=actor,
                        run_id=run_id,
                        reason=f"same attribute ({note})",
                        subject_text=subject.name,
                    )
                except MergeRefused as refused:
                    report.left_pending.append((subject.name, pending.key))
                    logger.info("cortana alignment: %s", refused)
                    continue
                report.merged.append((subject.name, pending.key, target))
                report.affected |= merged.keys
    return report
