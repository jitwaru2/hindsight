"""Packing a retain's new facts into structuring calls, and rendering one call's message.

One model call per retain batch (specification 5.2), split so a large retain cannot fail whole and
no call nears the model's output limit:

- ``MAX_FACTS_PER_CALL``: a call answers at most this many facts. The answer is about one to two
  claims of a few dozen tokens each per fact, so a full call stays near ten thousand output tokens
  in the worst case, well under the Claude Code CLI's output ceiling. The cap is set high on
  purpose: facts structured in one call are keyed against each other, and the structuring suite
  showed a session's recommendation and the choice that settled it split onto two keys in about one
  run in three when they fell in different calls (HSIGHT-4 report). One failed call leaves only its
  own facts pending for reconciliation.
- ``MAX_CHUNKS_PER_CALL``: a call shows at most this many source chunks. The engine's chunks hold
  up to about 12,000 characters, so the source text stays near twelve thousand input tokens.

Facts are taken in document order (chunk, then fact ordinal) and kept with their chunk: a call
holds whole chunks' facts where it can, and a chunk with more facts than one call holds is split
across calls that each show that chunk. Facts of different documents never share a call.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from .records import Catalog, Chunk, Entity, FactInput, Source, Turn
from .turns import parse_turns

MAX_FACTS_PER_CALL = 80
MAX_CHUNKS_PER_CALL = 4


@dataclass
class Batch:
    source: Source
    facts: list[FactInput]
    chunks: list[Chunk] = field(default_factory=list)
    # Facts are shown to the model as F1, F2, ...; turns as T1, T2, ... across the batch.
    fact_ids: dict[str, FactInput] = field(default_factory=dict)
    turns: dict[str, Turn] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seen: dict[int, Chunk] = {}
        for fact in self.facts:
            seen.setdefault(fact.chunk.index, fact.chunk)
        self.chunks = [seen[index] for index in sorted(seen)]
        self.fact_ids = {f"F{n}": fact for n, fact in enumerate(self.facts, start=1)}
        if self.source.kind == "session":
            number = 0
            for chunk in self.chunks:
                for role, stamp, content in parse_turns(chunk.text):
                    number += 1
                    turn = Turn(id=f"T{number}", chunk_index=chunk.index, role=role, timestamp=stamp, content=content)
                    self.turns[turn.id] = turn

    def candidate_subjects(self) -> dict[UUID, Entity]:
        """Every entity the batch's facts name, by id: the subjects whose catalog the call is shown."""
        return {entity.id: entity for fact in self.facts for entity in fact.entities}

    def chunk_turns(self, chunk_index: int) -> list[Turn]:
        return [turn for turn in self.turns.values() if turn.chunk_index == chunk_index]


def make_batches(facts: list[FactInput]) -> list[Batch]:
    """The calls for a retain's facts, in document order."""
    by_document: dict[tuple[int, str | None], list[FactInput]] = defaultdict(list)
    for fact in facts:
        by_document[(fact.source.document_order, fact.source.document_id)].append(fact)
    batches: list[Batch] = []
    for key in sorted(by_document, key=lambda k: (k[0], k[1] or "")):
        document_facts = sorted(by_document[key], key=lambda f: (f.chunk.index, f.ordinal))
        by_chunk: dict[int, list[FactInput]] = defaultdict(list)
        for fact in document_facts:
            by_chunk[fact.chunk.index].append(fact)
        current: list[FactInput] = []
        current_chunks: set[int] = set()
        for index in sorted(by_chunk):
            chunk_facts = by_chunk[index]
            if current and (
                len(current) + len(chunk_facts) > MAX_FACTS_PER_CALL or len(current_chunks) >= MAX_CHUNKS_PER_CALL
            ):
                batches.append(Batch(source=current[0].source, facts=current))
                current, current_chunks = [], set()
            for start in range(0, len(chunk_facts), MAX_FACTS_PER_CALL):
                part = chunk_facts[start : start + MAX_FACTS_PER_CALL]
                if current and len(current) + len(part) > MAX_FACTS_PER_CALL:
                    batches.append(Batch(source=current[0].source, facts=current))
                    current, current_chunks = [], set()
                current.extend(part)
                current_chunks.add(index)
        if current:
            batches.append(Batch(source=current[0].source, facts=current))
    return batches


def _entity_line(entities: tuple[Entity, ...]) -> str:
    return "; ".join(entity.name for entity in entities) if entities else "(none)"


def render(batch: Batch, catalog: Catalog, subjects: dict[UUID, Entity] | None = None) -> str:
    """The user message of one structuring call: the source, the subjects' keys, the facts.

    ``subjects`` are the facts' entities plus related existing subjects; a related subject is shown
    only when it has keys."""
    source = batch.source
    lines = ["SOURCE"]
    if source.kind == "session":
        lines += [
            "kind: conversation",
            f"document: {source.document_id}",
            f"started: {source.date.isoformat()}",
            f"context: {source.context}",
        ]
    else:
        lines += [
            "kind: document",
            f"document: {source.document_id}",
            f"document date: {source.date.date().isoformat()}",
            f"context: {source.context}",
        ]
    for chunk in batch.chunks:
        turns = batch.chunk_turns(chunk.index)
        lines.append("")
        if turns:
            lines.append(f"TURNS OF CHUNK {chunk.index}")
            for turn in turns:
                stamp = turn.timestamp.isoformat() if turn.timestamp else "no time"
                lines.append(f"[{turn.id} | {stamp} | {turn.role}] {turn.content}")
        else:
            lines.append(f"TEXT OF CHUNK {chunk.index}")
            lines.append(chunk.text)

    lines += ["", "SUBJECTS AND THEIR KEYS"]
    own = batch.candidate_subjects()
    shown = {**own, **(subjects or {})}
    for subject_id, entity in sorted(shown.items(), key=lambda item: item[1].name.casefold()):
        keys = [entry for entry in catalog.get(subject_id, {}).values() if entry.merged_into is None]
        if subject_id not in own and not keys:
            continue
        lines.append(entity.name)
        if not keys:
            lines.append("  (no keys yet)")
        for entry in sorted(keys, key=lambda e: e.key):
            example = f' Example: "{entry.example_value}"' if entry.example_value else ""
            lines.append(f"  {entry.key}: {entry.description}{example}")

    lines += ["", "FACTS"]
    for fact_id, fact in batch.fact_ids.items():
        lines += [
            "",
            f"{fact_id} (chunk {fact.chunk.index})",
            f"entities: {_entity_line(fact.entities)}",
            f"text: {fact.text}",
        ]
    return "\n".join(lines)
