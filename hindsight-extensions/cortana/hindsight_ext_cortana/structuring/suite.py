"""The structuring suite (specification 11 item 2): real model calls on the structuring fixtures,
scored for precision first and for key stability.

Each fixture's facts are structured exactly as the hook structures a retain (``runner.structure``:
the same batches, prompt, call and validation), with the catalog and the subjects kept in memory
(``MemoryStore``) instead of the engine's database. The model is the engine's retain provider,
built by the engine from the ``HINDSIGHT_API_*`` environment (``engine_retain_model``), so the call
carries the same provider, model and isolation as on the server.

Scoring, per expected key group of a fixture (``fixtures.ExpectedKey``):

- The group's key is the (subject, attribute) key that the most of its facts have a claim on;
  ties go to the key whose subject and attribute read most like the fixture's reference names.
  Two groups that end on one key collide, and every claim of both counts wrong, because a key
  shared by different properties is the worst failure (a wrong supersession).
- An expected claim counts correct when its fact has a claim on the group's key, every such claim
  carries the expected provisional flag, and the key does not collide.
- Precision is correct claims over expected claims whose fact received any claim; recall is
  correct claims over all expected claims. The target is ``PRECISION_TARGET`` on precision
  (HSIGHT-4 decision 1: a wrong key or a wrong provisional flag causes a wrong retirement, a missed
  claim only loses one supersession); recall is reported, not gated.
- Reported beside it: the end state each group would reach (the latest settled claim on the key by
  the statement-time tuple, against the fixture's ``current``), how often the statement time
  matches the fixture's (the turn for a session, the entry date for a document), and the claims of
  other facts that land on an expected key.

Key stability compares runs: ``cold`` runs start from an empty catalog each time; a ``warm`` run
starts from the catalog the first run built, as a later retain in the same bank would. A claim is
stable when its group's key has the same attribute (and subject) in both runs.
"""

import asyncio
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from . import VERSION
from .batching import Batch
from .engine import EngineModel
from .fixtures import ExpectedKey, StructuringFixture
from .names import names_overlap, normalize_name
from .records import BatchResult, Catalog, Chunk, ClaimRow, Entity, FactInput, Source
from .runner import RunReport, StructuringModel, structure

PRECISION_TARGET = 0.95

FIXTURE_NAMESPACE = UUID("2b0c8f53-49c7-4f0f-8d0f-5b8a0cc1c6e4")


def engine_retain_model() -> EngineModel:
    """The engine's retain provider, configured from the environment as the server configures it."""
    from hindsight_api import MemoryEngine

    engine = MemoryEngine(db_url="pg0://cortana-structuring-suite-unused", run_migrations=False)
    return EngineModel(engine._retain_llm_config)


def _is_label(name: str) -> bool:
    """The engine's label entities (``knowledge:decision``) are classifications, never subjects."""
    return ":" in name and not any(ch.isspace() for ch in name)


def entity_for(fixture: StructuringFixture, name: str) -> Entity:
    return Entity(id=uuid5(FIXTURE_NAMESPACE, f"{fixture.name}\n{normalize_name(name)}"), name=name)


def fixture_facts(fixture: StructuringFixture) -> tuple[list[FactInput], dict[UUID, str], dict[str, Entity]]:
    """The fixture's facts as the hook would load them; the fixture fact id of each; its entities.

    Entities are identified by name, case-insensitively, as one engine entity per name: the first
    spelling seen is the canonical one. Label entities are left out, as the hook leaves them out."""
    source = Source(
        kind=fixture.source.kind,
        document_id=fixture.source.document_id,
        context=fixture.source.context,
        date=fixture.source.timestamp,
    )
    chunks = {chunk.index: Chunk(index=chunk.index, text=chunk.text) for chunk in fixture.chunks}
    entities: dict[str, Entity] = {}
    ordinals: Counter[int] = Counter()
    facts, names = [], {}
    for fact in fixture.facts:
        fact_entities = []
        for name in fact.entities:
            if _is_label(name):
                continue
            entity = entities.setdefault(normalize_name(name), entity_for(fixture, name))
            if entity not in fact_entities:
                fact_entities.append(entity)
        fact_id = uuid5(FIXTURE_NAMESPACE, f"{fixture.name}\n{fact.id}")
        names[fact_id] = fact.id
        facts.append(
            FactInput(
                id=fact_id,
                text=fact.text,
                entities=tuple(fact_entities),
                chunk=chunks[fact.chunk_index],
                ordinal=ordinals[fact.chunk_index],
                source=source,
            )
        )
        ordinals[fact.chunk_index] += 1
    return facts, names, entities


class MemoryStore:
    """A bank held in memory: the catalog, the subjects (by exact name), the claims written."""

    def __init__(self, bank_id: str, entities: dict[str, Entity], catalog: Catalog | None = None):
        self.bank_id = bank_id
        self.entities = entities
        self.catalog: Catalog = {subject: dict(keys) for subject, keys in (catalog or {}).items()}
        self.claims: list[ClaimRow] = []
        self.pending: list[tuple[list[UUID], str]] = []

    async def load_catalog(self, subject_ids) -> Catalog:
        return {subject: dict(self.catalog.get(subject, {})) for subject in subject_ids}

    async def related_subjects(self, subjects: dict[UUID, Entity]) -> dict[UUID, Entity]:
        keyed = {e.id: e for e in self.entities.values() if e.id not in subjects and self.catalog.get(e.id)}
        return {
            subject_id: entity
            for subject_id, entity in keyed.items()
            if any(names_overlap(entity.name, candidate.name) for candidate in subjects.values())
        }

    async def resolve_subjects(self, names: set[str], batch: Batch) -> dict[str, Entity | None]:
        return {name: self.entities.get(normalize_name(name)) for name in names}

    async def write(self, batch: Batch, result: BatchResult, details: dict[str, Any]) -> None:
        self.claims.extend(result.claims)
        for entry in result.new_attributes:
            self.catalog.setdefault(entry.subject_id, {}).setdefault(entry.key, entry)

    async def record_pending(self, fact_ids: list[UUID], reason: str, details: dict[str, Any]) -> None:
        self.pending.append((fact_ids, reason))


@dataclass
class GroupScore:
    subject: str
    attribute: str
    key: str | None
    collides: bool
    claims: list[dict[str, Any]]
    end_state: str | None
    expected_current: str | None
    intrusions: list[str]


@dataclass
class FixtureScore:
    fixture: str
    expected: int
    produced: int
    on_key: int
    flag_ok: int
    correct: int
    timed_ok: int
    timed: int
    groups: list[GroupScore] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.correct / self.produced if self.produced else 0.0

    @property
    def recall(self) -> float:
        return self.correct / self.expected if self.expected else 0.0


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def _group_key(
    group: ExpectedKey, claims_by_fact: dict[str, list[ClaimRow]], subject_names: dict[UUID, str]
) -> tuple[UUID, str] | None:
    support: Counter[tuple[UUID, str]] = Counter()
    for expected in group.claims:
        for key in {claim.key for claim in claims_by_fact.get(expected.fact, [])}:
            support[key] += 1
    if not support:
        return None

    def rank(key: tuple[UUID, str]) -> tuple[int, float]:
        subject = subject_names.get(key[0], "")
        return (support[key], _similarity(subject, group.subject) + _similarity(key[1], group.attribute))

    return max(support, key=rank)


def score_fixture(fixture: StructuringFixture, claims: list[ClaimRow], fact_names: dict[UUID, str]) -> FixtureScore:
    claims_by_fact: dict[str, list[ClaimRow]] = {}
    for claim in claims:
        claims_by_fact.setdefault(fact_names[claim.memory_unit_id], []).append(claim)
    subject_names = {claim.subject_entity_id: claim.subject_text for claim in claims}
    keys = [_group_key(group, claims_by_fact, subject_names) for group in fixture.expected]
    chosen = Counter(key for key in keys if key is not None)
    score = FixtureScore(fixture.name, 0, 0, 0, 0, 0, 0, 0)

    for group, key in zip(fixture.expected, keys, strict=True):
        collides = key is not None and chosen[key] > 1
        rows = []
        for expected in group.claims:
            score.expected += 1
            fact_claims = claims_by_fact.get(expected.fact, [])
            on_key = [c for c in fact_claims if key is not None and c.key == key]
            flag_ok = bool(on_key) and all(c.provisional == expected.provisional for c in on_key)
            correct = bool(on_key) and flag_ok and not collides
            timed_ok = None
            if on_key:
                claim = on_key[0]
                if fixture.source.kind == "session":
                    timed_ok = claim.stated_at == expected.stated_at
                else:
                    want = expected.as_of or fixture.source.timestamp.date()
                    timed_ok = claim.stated_at.date() == want
                score.timed += 1
                score.timed_ok += int(timed_ok)
            score.produced += int(bool(fact_claims))
            score.on_key += int(bool(on_key) and not collides)
            score.flag_ok += int(flag_ok)
            score.correct += int(correct)
            rows.append(
                {
                    "fact": expected.fact,
                    "expected_provisional": expected.provisional,
                    "claims": [
                        {
                            "subject": c.subject_text,
                            "attribute": c.attribute_key,
                            "value": c.value_text,
                            "provisional": c.provisional,
                            "state": c.state,
                            "stated_at": c.stated_at.isoformat(),
                            "stated_at_source": str(c.stated_at_source),
                        }
                        for c in fact_claims
                    ],
                    "on_key": bool(on_key),
                    "flag_ok": flag_ok,
                    "correct": correct,
                    "time_ok": timed_ok,
                }
            )
        on_group_key = [c for c in claims if key is not None and c.key == key]
        settled = [c for c in on_group_key if not c.provisional and c.state != "unaligned"]
        latest = max(settled, key=lambda c: c.statement_time) if settled else None
        end_state = fact_names[latest.memory_unit_id] if latest else None
        own = {e.fact for e in group.claims}
        intrusions = sorted(
            {
                fact_names[c.memory_unit_id] + ("" if c.provisional else " (settled)")
                for c in on_group_key
                if fact_names[c.memory_unit_id] not in own
            }
        )
        score.groups.append(
            GroupScore(
                subject=group.subject,
                attribute=group.attribute,
                key=f"{subject_names.get(key[0], '?')}/{key[1]}" if key else None,
                collides=collides,
                claims=rows,
                end_state=end_state,
                expected_current=group.current,
                intrusions=intrusions,
            )
        )
    return score


def key_stability(
    fixture: StructuringFixture,
    first: list[ClaimRow],
    second: list[ClaimRow],
    fact_names: dict[UUID, str],
) -> tuple[int, int]:
    """Expected claims whose group key is the same in both runs, and how many were compared."""

    def keys(claims: list[ClaimRow]) -> list[tuple[str, str] | None]:
        by_fact: dict[str, list[ClaimRow]] = {}
        for claim in claims:
            by_fact.setdefault(fact_names[claim.memory_unit_id], []).append(claim)
        names = {claim.subject_entity_id: claim.subject_text for claim in claims}
        out = []
        for group in fixture.expected:
            key = _group_key(group, by_fact, names)
            out.append((normalize_name(names[key[0]]), key[1]) if key else None)
        return out

    same = total = 0
    for group, a, b in zip(fixture.expected, keys(first), keys(second), strict=True):
        if a is None or b is None:
            continue
        total += len(group.claims)
        same += len(group.claims) if a == b else 0
    return same, total


@dataclass
class FixtureRun:
    claims: list[ClaimRow]
    report: RunReport
    catalog: Catalog


async def run_fixture(
    fixture: StructuringFixture, model: StructuringModel, catalog: Catalog | None = None
) -> tuple[FixtureRun, dict[UUID, str]]:
    facts, names, entities = fixture_facts(fixture)
    store = MemoryStore(f"suite-{fixture.name}", entities, catalog)
    report = await structure(facts, store, model, prompt_version=VERSION)
    return FixtureRun(claims=store.claims, report=report, catalog=store.catalog), names


def _claim_json(claim: ClaimRow, names: dict[UUID, str]) -> dict[str, Any]:
    row = asdict(claim)
    row["memory_unit_id"] = names[claim.memory_unit_id]
    row["subject_entity_id"] = str(claim.subject_entity_id)
    row["stated_at"] = claim.stated_at.isoformat()
    row["source_rank"] = int(claim.source_rank)
    row["stated_at_source"] = str(claim.stated_at_source)
    return row


async def run_suite(
    fixtures: list[StructuringFixture], model: StructuringModel, *, cold_runs: int, warm: bool, out: Path | None
) -> dict[str, Any]:
    """Run every fixture ``cold_runs`` times from an empty catalog, then once warm; score each run.

    Fixtures run concurrently (each is its own bank); within a fixture, calls run in order so each
    sees the catalog the previous one left, as in a retain."""

    async def one(fixture: StructuringFixture) -> dict[str, Any]:
        runs: list[tuple[str, FixtureRun]] = []
        names: dict[UUID, str] = {}
        for n in range(cold_runs):
            run, names = await run_fixture(fixture, model)
            runs.append((f"cold-{n + 1}", run))
        if warm and runs:
            run, names = await run_fixture(fixture, model, catalog=runs[0][1].catalog)
            runs.append(("warm", run))
        result: dict[str, Any] = {"fixture": fixture.name, "runs": []}
        for label, run in runs:
            score = score_fixture(fixture, run.claims, names)
            result["runs"].append(
                {
                    "run": label,
                    "precision": score.precision,
                    "recall": score.recall,
                    "score": asdict(score),
                    "calls": [asdict(call) for call in run.report.calls],
                    "pending": [names[fact_id] for fact_id in run.report.pending],
                    "claims": [_claim_json(claim, names) for claim in run.claims],
                }
            )
        first = runs[0][1].claims if runs else []
        stability = {}
        for label, run in runs[1:]:
            stability[label] = key_stability(fixture, first, run.claims, names)
        result["stability"] = stability
        return result

    results = await asyncio.gather(*(one(fixture) for fixture in fixtures))
    summary = summarize(results)
    report = {"prompt_version": VERSION, "model": model.name, "summary": summary, "fixtures": results}
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "structuring-suite.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals per run label across fixtures, and stability per comparison."""
    totals: dict[str, Counter[str]] = {}
    for fixture in results:
        for run in fixture["runs"]:
            counter = totals.setdefault(run["run"], Counter())
            for field_name in ("expected", "produced", "on_key", "flag_ok", "correct", "timed", "timed_ok"):
                counter[field_name] += run["score"][field_name]
    runs = {}
    for label, c in totals.items():
        runs[label] = {
            "precision": c["correct"] / c["produced"] if c["produced"] else 0.0,
            "recall": c["correct"] / c["expected"] if c["expected"] else 0.0,
            "key_precision": c["on_key"] / c["produced"] if c["produced"] else 0.0,
            "provisional_precision": c["flag_ok"] / c["on_key"] if c["on_key"] else 0.0,
            "statement_time_match": c["timed_ok"] / c["timed"] if c["timed"] else 0.0,
            **dict(c),
        }
    stability: dict[str, list[int]] = {}
    for fixture in results:
        for label, (same, total) in fixture["stability"].items():
            pair = stability.setdefault(label, [0, 0])
            pair[0] += same
            pair[1] += total
    return {
        "target": PRECISION_TARGET,
        "runs": runs,
        "meets_target": all(run["precision"] >= PRECISION_TARGET for run in runs.values()),
        "stability": {
            label: {"same": s, "compared": t, "rate": s / t if t else 0.0} for label, (s, t) in stability.items()
        },
    }
