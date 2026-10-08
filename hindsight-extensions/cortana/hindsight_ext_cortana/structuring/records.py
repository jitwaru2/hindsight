"""The values structuring passes between its steps. Plain frozen dataclasses: they are built in
code from database rows or fixtures, never parsed from outside, so they need no validation."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal
from uuid import UUID

SourceKind = Literal["session", "document", "decision", "correction"]
ClaimState = Literal["current", "superseded", "unaligned"]


class SourceRank(IntEnum):
    """The last element of the statement-time tuple (specification 4.3, rule S6): at equal time the
    higher rank is the later statement, so a decision record outranks a correction, a correction
    outranks a document, and a document outranks a session fact."""

    SESSION = 0
    DOCUMENT = 1
    CORRECTION = 2
    DECISION = 3


SOURCE_RANKS: dict[SourceKind, SourceRank] = {
    "session": SourceRank.SESSION,
    "document": SourceRank.DOCUMENT,
    "correction": SourceRank.CORRECTION,
    "decision": SourceRank.DECISION,
}


class StatedAtSource(StrEnum):
    """Where a claim's statement time came from (``claims.stated_at_source``)."""

    TURN = "turn"
    CHUNK_START = "chunk-start"
    SESSION_START = "session-start"
    ENTRY_DATE = "entry-date"
    DOCUMENT_DATE = "document-date"
    DECISION = "decision"


@dataclass(frozen=True)
class Entity:
    id: UUID
    name: str


@dataclass(frozen=True)
class Turn:
    """One turn of a saved session, numbered across the batch it is shown in."""

    id: str
    chunk_index: int
    role: str
    timestamp: datetime | None
    content: str


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    chunk_id: str | None = None


@dataclass(frozen=True)
class Source:
    """The document a fact was extracted from, as the retain gave it.

    ``date`` is the retain's event date: the session's start for a plugin save, the stamped date for
    a vault document. ``document_order`` is the item's position in the retain call, the second
    element of the statement-time tuple; every save the plugin and the loader send has one item.
    """

    kind: SourceKind
    document_id: str | None
    context: str
    date: datetime
    document_order: int = 0


@dataclass(frozen=True)
class FactInput:
    """A fact to structure: an engine memory unit with its chunk, its entities and its position."""

    id: UUID
    text: str
    entities: tuple[Entity, ...]
    chunk: Chunk
    ordinal: int
    source: Source


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the attributes catalog."""

    subject_id: UUID
    key: str
    description: str
    example_value: str | None = None
    merged_into: str | None = None


Catalog = dict[UUID, dict[str, CatalogEntry]]


@dataclass(frozen=True)
class ClaimRow:
    """One row for ``claims`` (specification 4.2)."""

    memory_unit_id: UUID
    subject_entity_id: UUID
    subject_text: str
    attribute_key: str
    value_text: str
    provisional: bool
    stated_at: datetime
    document_order: int
    chunk_index: int
    fact_ordinal: int
    source_rank: SourceRank
    document_id: str | None
    chunk_id: str | None
    source_kind: SourceKind
    state: ClaimState
    prompt_version: str | None
    model: str | None
    content_hash: str
    stated_at_source: StatedAtSource

    @property
    def key(self) -> tuple[UUID, str]:
        return (self.subject_entity_id, self.attribute_key)

    @property
    def statement_time(self) -> tuple[datetime, int, int, int, int]:
        """The sortable tuple of specification 4.3; a greater tuple is the later statement."""
        return (self.stated_at, self.document_order, self.chunk_index, self.fact_ordinal, int(self.source_rank))


@dataclass
class BatchResult:
    """What validation made of one batch: the claims, the catalog additions, and what went wrong."""

    claims: list[ClaimRow] = field(default_factory=list)
    new_attributes: list[CatalogEntry] = field(default_factory=list)
    unstructured: list[UUID] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
