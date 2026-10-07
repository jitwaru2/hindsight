"""Structuring fixtures: source chunks, the facts the engine extracted from them under our
extraction instructions, and the claims structuring must end with (specification 11, item 2).

A fixture is one JSON file validated by ``StructuringFixture``. Expected claims are grouped by key:
the claims listed under one ``ExpectedKey`` must land on one (subject, attribute) key, with the
given provisional status, and ``current`` names the claim that must end current (none when every
claim on the key is provisional). ``attribute`` is a reference name; the structuring step may name
the key differently, so a suite scores the grouping, the subject and the provisional status, and
treats claims on different expected keys sharing a key as the worst failure.

Real fixtures are cut from real sessions and vault documents and never enter this repository: the
repository is public. They live in a folder on the operator's machine, ``REAL_FIXTURES_ENV`` names
it, and ``real_fixtures_dir`` returns ``None`` when it is absent so a suite can skip with a reason.
Synthetic fixtures of the same shape, with invented names and facts, ship in ``tests/fixtures``.
"""

import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

REAL_FIXTURES_ENV = "HINDSIGHT_CORTANA_REAL_FIXTURES"
DEFAULT_REAL_FIXTURES = Path.home() / ".cortana-legacy" / "hindsight" / "fixtures" / "structuring"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Model):
    """The retain the dry run imitated: what the plugin or the loader sends for this document."""

    kind: Literal["session", "document"]
    document_id: str
    strategy: str
    context: str
    timestamp: datetime
    content_sha256: str


class Extraction(_Model):
    """How the facts were produced: the engine's dry-run extraction under our instructions."""

    instructions_version: str
    instructions_sha256: str
    model: str
    run: str


class Chunk(_Model):
    index: int
    text: str


class Fact(_Model):
    id: str
    chunk_index: int
    text: str
    fact_type: Literal["world", "experience"]
    entities: list[str]
    occurred_start: str | None = None
    occurred_end: str | None = None


class ExpectedClaim(_Model):
    fact: str
    value: str
    provisional: bool
    # Statement time as specification 4.3 defines it: the turn's timestamp for a session claim, the
    # document's stamped date for a document claim.
    stated_at: datetime
    # The date the claim's own text gives, where it differs from stated_at: a document entry's date.
    as_of: date | None = None


class ExpectedKey(_Model):
    subject: str
    attribute: str
    description: str
    claims: list[ExpectedClaim]
    current: str | None

    @model_validator(mode="after")
    def _current_is_a_settled_claim(self) -> Self:
        if self.current is not None and self.current not in {c.fact for c in self.claims if not c.provisional}:
            raise ValueError(f"{self.subject}/{self.attribute}: current {self.current} is not a non-provisional claim")
        return self


class StructuringFixture(_Model):
    name: str
    description: str
    source: Source
    extraction: Extraction
    chunks: list[Chunk]
    facts: list[Fact]
    expected: list[ExpectedKey]

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        chunk_indices = {c.index for c in self.chunks}
        fact_ids = [f.id for f in self.facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("fact ids repeat")
        for fact in self.facts:
            if fact.chunk_index not in chunk_indices:
                raise ValueError(f"fact {fact.id} names chunk {fact.chunk_index}, which the fixture lacks")
        for key in self.expected:
            for claim in key.claims:
                if claim.fact not in fact_ids:
                    raise ValueError(f"{key.subject}/{key.attribute} names fact {claim.fact}, which the fixture lacks")
        return self


def load_fixture(path: Path) -> StructuringFixture:
    return StructuringFixture.model_validate_json(path.read_text(encoding="utf-8"))


def load_fixtures(directory: Path) -> list[StructuringFixture]:
    return [load_fixture(path) for path in sorted(directory.glob("*.json"))]


def real_fixtures_dir() -> Path | None:
    """The real fixtures' folder: ``$HINDSIGHT_CORTANA_REAL_FIXTURES``, else the operating folder's
    ``fixtures/structuring``; ``None`` when that folder does not exist."""
    configured = os.environ.get(REAL_FIXTURES_ENV)
    directory = Path(configured).expanduser() if configured else DEFAULT_REAL_FIXTURES
    return directory if directory.is_dir() else None
