"""Questions generated from the ledger (specification 11 item 3).

For every key with a supersession in the run's window, one question, "What is the current
<attribute> of <subject>?", whose expected answer is the key's current claim (the newest standing
claim on the key, as the rules ordered it). The key's superseded claims with a different value are
the old positions the answer must not give as current. A key is skipped, and counted, when every
supersession in the window was a restatement (S3: same value), when the key stands in conflict
(S11: the read reports both claims, so there is no single expected answer), or when it has no
current claim (its newest claim is provisional or unaligned).

``generate`` is pure; ``load`` reads the ledger and the claims (read-only).
"""

import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table

from ..rules import same_value

Key = tuple[UUID, str]


@dataclass(frozen=True)
class KeyClaim:
    claim_id: UUID
    fact_id: UUID
    subject_id: UUID
    subject: str
    attribute: str
    value: str
    state: str
    stated_at: datetime


@dataclass(frozen=True)
class Supersession:
    """One ``claim-superseded`` ledger entry: ``old`` was superseded by ``new`` on a key."""

    subject_id: UUID
    attribute: str
    old_claim_id: UUID
    new_claim_id: UUID
    rule: str | None
    recorded_at: datetime


@dataclass
class GeneratedQuestion:
    id: str
    subject_id: UUID
    subject: str
    attribute: str
    question: str
    expected: KeyClaim
    superseded: list[KeyClaim]
    rules: list[str]


@dataclass
class Generated:
    questions: list[GeneratedQuestion] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    supersessions: int = 0


def question_text(subject: str, attribute: str) -> str:
    return f"What is the current {attribute.replace('-', ' ').replace('_', ' ')} of {subject}?"


def generate(supersessions: Iterable[Supersession], claims: dict[Key, Sequence[KeyClaim]]) -> Generated:
    """One question per key superseded in the window, expected answer the key's current claim."""
    out = Generated()
    by_key: dict[Key, list[Supersession]] = defaultdict(list)
    for entry in supersessions:
        out.supersessions += 1
        by_key[(entry.subject_id, entry.attribute)].append(entry)

    def skip(reason: str) -> None:
        out.skipped[reason] = out.skipped.get(reason, 0) + 1

    for key in sorted(by_key, key=lambda k: (str(k[0]), k[1])):
        entries = by_key[key]
        if all(entry.rule == "S3" for entry in entries):
            skip("restatements only")
            continue
        on_key = list(claims.get(key, ()))
        if any(claim.state == "conflict" for claim in on_key):
            skip("key in conflict")
            continue
        current = [claim for claim in on_key if claim.state == "current"]
        if len(current) != 1:
            skip("no single current claim")
            continue
        expected = current[0]
        old = [c for c in on_key if c.state == "superseded" and not same_value(c.value, expected.value)]
        if not old:
            skip("no superseded claim with another value")
            continue
        old.sort(key=lambda c: c.stated_at, reverse=True)
        out.questions.append(
            GeneratedQuestion(
                id=f"G-{expected.claim_id.hex[:8]}",
                subject_id=key[0],
                subject=expected.subject,
                attribute=key[1],
                question=question_text(expected.subject, key[1]),
                expected=expected,
                superseded=old,
                rules=sorted({entry.rule or "" for entry in entries}),
            )
        )
    return out


def sample(questions: Sequence[GeneratedQuestion], size: int | None, *, seed: str) -> list[GeneratedQuestion]:
    """A reproducible sample (the same seed and questions give the same sample); all when ``size`` is
    None or covers them."""
    if size is None or size >= len(questions):
        return list(questions)
    chosen = random.Random(seed).sample(range(len(questions)), size)
    return [questions[i] for i in sorted(chosen)]


async def load(conn: Any, bank_id: str, since: datetime | None) -> tuple[list[Supersession], dict[Key, list[KeyClaim]]]:
    """The window's supersessions and every claim on the keys they touch."""
    rows = await conn.fetch(
        f"""
        SELECT recorded_at, rule, claim_ids, details FROM {fq_table("ledger")}
        WHERE bank_id = $1 AND event = 'claim-superseded' AND ($2::timestamptz IS NULL OR recorded_at >= $2)
        ORDER BY id
        """,
        bank_id,
        since,
    )
    claim_ids = list({row["claim_ids"][0] for row in rows} | {row["claim_ids"][1] for row in rows})
    keyed = await conn.fetch(
        f"SELECT id, subject_entity_id, attribute_key FROM {fq_table('claims')} WHERE bank_id = $1 AND id = ANY($2::uuid[])",
        bank_id,
        claim_ids,
    )
    key_of = {row["id"]: (row["subject_entity_id"], row["attribute_key"]) for row in keyed}
    supersessions = []
    for row in rows:
        old, new = row["claim_ids"][0], row["claim_ids"][1]
        key = key_of.get(old) or key_of.get(new)
        if key is None:
            continue  # both claims swept since; nothing to ask
        supersessions.append(Supersession(key[0], key[1], old, new, row["rule"], row["recorded_at"]))
    keys = sorted({(s.subject_id, s.attribute) for s in supersessions}, key=lambda k: (str(k[0]), k[1]))
    claims: dict[Key, list[KeyClaim]] = defaultdict(list)
    if keys:
        found = await conn.fetch(
            f"""
            SELECT c.id, c.memory_unit_id, c.subject_entity_id, c.subject_text, c.attribute_key, c.value_text,
                   c.state, c.stated_at
            FROM {fq_table("claims")} c
            JOIN unnest($2::uuid[], $3::text[]) AS k(subject, attribute)
              ON c.subject_entity_id = k.subject AND c.attribute_key = k.attribute
            WHERE c.bank_id = $1
            """,
            bank_id,
            [k[0] for k in keys],
            [k[1] for k in keys],
        )
        for row in found:
            claims[(row["subject_entity_id"], row["attribute_key"])].append(
                KeyClaim(
                    claim_id=row["id"],
                    fact_id=row["memory_unit_id"],
                    subject_id=row["subject_entity_id"],
                    subject=row["subject_text"],
                    attribute=row["attribute_key"],
                    value=row["value_text"],
                    state=row["state"],
                    stated_at=row["stated_at"],
                )
            )
    return supersessions, dict(claims)


__all__ = [
    "Generated",
    "GeneratedQuestion",
    "KeyClaim",
    "Supersession",
    "generate",
    "load",
    "question_text",
    "sample",
]
