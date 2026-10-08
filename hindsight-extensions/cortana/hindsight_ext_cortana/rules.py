"""The supersession rules S1 to S11 as a pure function of a key's valid claims (specification 6.1).

Nothing here reads a database or calls a model. ``evaluate_key`` takes the valid claims of one key
(claims whose fact is live, or retired by these rules) and returns each claim's state, the claim it
was superseded by and the rule, plus the key's conflict and stale-document state; ``evaluate_facts``
turns those claim states into which facts are retired (S4, S8). The same inputs always give the same
outputs, so reconciliation can recompute any key at any time (principle 3, rule S10).

Order. A claim is later when its order tuple is greater: the statement timestamp, then the source
rank (S6: a decision record outranks a correction, a correction a document, a document a session
fact), then the document order, chunk index and fact ordinal (the order within one document), then
the engine's creation order. Specification 4.2 lists the rank last in the stored tuple; S6 and 4.3
say it breaks ties at equal *time*, so it is compared right after the timestamp here. Within one
document the rank is constant, so this only changes the order of claims from different documents
stated at the same instant, where positions inside different documents mean nothing.

Supersession forms a chain: a claim's ``superseded_by`` is the earliest later claim that replaced
it (specification 4.4), so the fact's invalidation reason names its direct successor and an
operator can follow the chain to the current claim.

S11 (memory against documents; decision 2026-10-07 "memory-document conflicts are raised, not
resolved by rule"). When a later non-provisional claim meets a standing earlier one with a
different value:

- a decision record or a correction supersedes anything; over a document claim it leaves the
  ``stale-document`` marker naming that document;
- a session claim supersedes a session claim, a decision or a correction, and conflicts with a
  document claim;
- a document claim supersedes a session claim (the edit is the resolution) and an earlier claim of
  the same document (a later dated entry), and conflicts with another document's claim and with a
  decision record or correction. The specification is silent on a document saved after a decision
  that disagrees with it; this reads it as the discrepancy ruling 11 says to raise, so it does not
  silently undo the owner's explicit words (HSIGHT-5 report, decisions).

A claim with the same value as a standing claim supersedes it as a restatement (S3), whatever the
kinds, so a session claim that agrees with a document restates it and a later save of an edited
document that agrees with the session closes the conflict. Conflicting claims all stay valid; while
more than one standing claim remains the key is in conflict and has no single current claim.

A stale-document marker stays on the key while the newest claim of that document on the key still
disagrees with every standing claim, so a later save of the document that agrees clears it.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

SourceKind = Literal["session", "document", "decision", "correction"]
ClaimState = Literal["current", "superseded", "unaligned", "provisional", "conflict"]
Rule = Literal["S1", "S2", "S3"]

AUTHORITATIVE: frozenset[str] = frozenset({"decision", "correction"})
VALID_STATES: frozenset[str] = frozenset({"current", "unaligned", "provisional", "conflict"})


@dataclass(frozen=True)
class RuleClaim:
    """One valid claim of a key, with what the rules need of it."""

    id: UUID
    fact_id: UUID
    value: str
    provisional: bool
    kind: SourceKind
    stated_at: datetime
    document_id: str | None = None
    document_order: int = 0
    chunk_index: int = 0
    fact_ordinal: int = 0
    source_rank: int = 0
    created_at: datetime | None = None
    subject: str = ""
    key: str = ""

    @property
    def order(self) -> tuple:
        created = self.created_at.timestamp() if self.created_at else 0.0
        return (
            self.stated_at,
            self.source_rank,
            self.document_order,
            self.chunk_index,
            self.fact_ordinal,
            created,
            str(self.fact_id),
            str(self.id),
        )


@dataclass(frozen=True)
class Decision:
    """A claim's state after the rules: ``superseded_by`` and ``rule`` only when superseded."""

    state: ClaimState
    superseded_by: UUID | None = None
    rule: Rule | None = None


@dataclass(frozen=True)
class KeyOutcome:
    """What the rules made of one key."""

    decisions: Mapping[UUID, Decision]
    current: UUID | None = None
    conflict: tuple[UUID, ...] = ()
    later_provisional: tuple[UUID, ...] = ()
    stale_documents: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactOutcome:
    """Whether a fact is retired (S4), and if so the reason in the fixed form of 4.4."""

    retired: bool
    reason: str | None = None
    superseding: tuple[tuple[UUID, UUID], ...] = field(default=())  # (claim id, superseding claim id)


def same_value(a: str, b: str) -> bool:
    """Values compare case-insensitively with whitespace collapsed and a final period ignored."""
    return _norm(a) == _norm(b)


def _norm(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".").strip()


def _supersedes(earlier: RuleClaim, later: RuleClaim) -> bool:
    """Whether a later non-provisional claim replaces a standing earlier one (S1, S3, S11)."""
    if same_value(earlier.value, later.value):
        return True
    if later.kind in AUTHORITATIVE:
        return True
    if later.kind == "session":
        return earlier.kind != "document"
    # later.kind == "document"
    if earlier.kind == "session":
        return True
    if earlier.kind == "document":
        return earlier.document_id is not None and earlier.document_id == later.document_id
    return False


def evaluate_key(claims: Iterable[RuleClaim], *, aligned: bool = True) -> KeyOutcome:
    """The state of every claim of one key (S1 to S3, S6, S9, S11).

    ``claims`` are the key's valid claims: claims whose fact is live or was retired by these rules.
    A claim whose fact was deleted, or retired by anyone else, is not an input; that is how S5
    restores what a vanished superseder had superseded. ``aligned`` is false while the key's
    catalog row is pending alignment: every claim is then unaligned and supersedes nothing (S9).
    """
    ordered = sorted(claims, key=lambda claim: claim.order)
    if not aligned:
        return KeyOutcome(decisions={claim.id: Decision("unaligned") for claim in ordered})

    decisions: dict[UUID, Decision] = {}
    standing: list[RuleClaim] = []
    marked: set[str] = set()
    for later in (claim for claim in ordered if not claim.provisional):
        kept: list[RuleClaim] = []
        for earlier in standing:
            if not _supersedes(earlier, later):
                kept.append(earlier)
                continue
            restated = same_value(earlier.value, later.value)
            decisions[earlier.id] = Decision("superseded", later.id, "S3" if restated else "S1")
            if not restated and later.kind in AUTHORITATIVE and earlier.kind == "document" and earlier.document_id:
                marked.add(earlier.document_id)
        standing = [*kept, later]

    # S2: a provisional claim is superseded by the earliest later claim of any kind.
    for index, claim in enumerate(ordered):
        if not claim.provisional:
            continue
        # Orders are unique (they end in the ids), so the next claim in order is the next later one.
        successor = ordered[index + 1] if index + 1 < len(ordered) else None
        decisions[claim.id] = Decision("superseded", successor.id, "S2") if successor else Decision("provisional")

    conflict = len(standing) > 1
    for claim in standing:
        decisions[claim.id] = Decision("conflict" if conflict else "current")
    current = standing[0] if len(standing) == 1 else None

    newest_of_document: dict[str, RuleClaim] = {}
    for claim in ordered:
        if claim.document_id is not None:
            newest_of_document[claim.document_id] = claim
    standing_values = [claim.value for claim in standing]
    stale = tuple(
        sorted(
            document
            for document in marked
            if not any(same_value(newest_of_document[document].value, value) for value in standing_values)
        )
    )
    later_provisional = tuple(
        claim.id
        for claim in ordered
        if decisions[claim.id].state == "provisional" and (current is None or claim.order > current.order)
    )
    return KeyOutcome(
        decisions=decisions,
        current=current.id if current else None,
        conflict=tuple(claim.id for claim in standing) if conflict else (),
        later_provisional=later_provisional,
        stale_documents=stale,
    )


@dataclass(frozen=True)
class FactClaim:
    """A claim as fact retirement sees it: its fact, its key's names, and its state after the rules."""

    id: UUID
    fact_id: UUID
    subject: str
    key: str
    decision: Decision


def retirement_reason(superseding_fact: UUID, subject: str, key: str, rule: str) -> str:
    """The fixed form of specification 4.4."""
    return f"superseded by {superseding_fact} on {subject}/{key}, rule {rule}"


REASON_PREFIX = "superseded by "


def is_rule_retirement(reason: str | None) -> bool:
    """Whether an archived fact's invalidation reason is one these rules wrote."""
    return bool(reason) and reason.startswith(REASON_PREFIX) and ", rule S" in reason


def evaluate_facts(claims: Iterable[FactClaim], fact_of_claim: Mapping[UUID, UUID]) -> dict[UUID, FactOutcome]:
    """Which facts are retired (S4): a fact is retired when every claim it carries is superseded.

    ``claims`` must hold every claim of each fact concerned, on every key, with its state after the
    rules; ``fact_of_claim`` maps a superseding claim to its fact for the reason. A fact with no
    claims is not an input and is never retired (S8). The reason joins one fixed-form clause per
    claim, ordered by subject and key, so a fact with several claims names each successor.
    """
    by_fact: dict[UUID, list[FactClaim]] = defaultdict(list)
    for claim in claims:
        by_fact[claim.fact_id].append(claim)
    outcomes: dict[UUID, FactOutcome] = {}
    for fact_id, fact_claims in by_fact.items():
        if not all(claim.decision.state == "superseded" for claim in fact_claims):
            outcomes[fact_id] = FactOutcome(retired=False)
            continue
        ordered = sorted(fact_claims, key=lambda claim: (claim.subject.casefold(), claim.key, str(claim.id)))
        clauses = [
            retirement_reason(
                fact_of_claim[claim.decision.superseded_by], claim.subject, claim.key, claim.decision.rule or "S1"
            )
            for claim in ordered
            if claim.decision.superseded_by is not None
        ]
        outcomes[fact_id] = FactOutcome(
            retired=True,
            reason="; ".join(clauses),
            superseding=tuple(
                (claim.id, claim.decision.superseded_by) for claim in ordered if claim.decision.superseded_by
            ),
        )
    return outcomes
