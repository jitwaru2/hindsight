"""Turning the model's answer into claim rows, in code (specification 5.2).

What the model proposes and what code decides:

- Subject. The model names one of the fact's entities, or another name. A listed entity is taken as
  is. Any other name must resolve to an existing entity of the bank (``Store.resolve_subjects``,
  which uses the engine's entity resolver without writing). A name that resolves to nothing cannot
  become an engine entity, because structuring writes only our tables; its claim is kept under a
  subject id derived from the name and stored ``unaligned``, so it retires nothing.
- Attribute. Normalized to a lowercase hyphenated key. An existing key of the subject (or an alias
  recorded for one) is used as is. A new key on a subject that had no keys becomes the subject's
  key. A new key on a subject that already had keys is ``unaligned`` unless ``same_as`` names one of
  the subject's keys, in which case the claim goes on that key and the new name is recorded as an
  alias of it (specification 3 principle 4, rule S9). New keys join the catalog so later calls see
  them.
- Provisional. The model's flag, or an earlier-state marker in the claim's own words: the fact's
  statement (the text before the engine's " | When:", " | Involving:" and reason parts) when the
  fact has one claim, or the claim's quote when the fact has several and the quote is found in the
  statement. A marker always wins over the model's flag (decision of 2026-10-07, HSIGHT-4).
- Statement time. The tuple of specification 4.3, with the document departure recorded in the
  HSIGHT-4 report: a session claim takes its turn's timestamp, else the first timestamp of its
  chunk, else the session's start; a document claim takes the date of the dated entry it comes
  from, else the document's stamped date; a decision record takes the moment it gives.
- Content hash. The SHA-256 of the fact text with whitespace collapsed, so a re-extracted fact can
  be matched to its predecessor.
"""

import hashlib
import re
from datetime import date, datetime, time
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slugify import slugify

from .batching import Batch
from .names import name_words, names_overlap, normalize_name
from .records import (
    SOURCE_RANKS,
    BatchResult,
    Catalog,
    CatalogEntry,
    ClaimRow,
    ClaimState,
    Entity,
    FactInput,
    StatedAtSource,
)

# Words that mark a claim as a state in the moment or an earlier state (specification 2,
# "Provisional claim"; HSIGHT-3's extraction instructions write them; HSIGHT-4 decision 4).
EARLIER_STATE_MARKERS = re.compile(
    r"""
    \blater\s+(?:reversed|superseded|contradicted|changed|withdrawn|replaced|overturned|corrected|revised
               |cancell?ed|dropped|undone|rescinded)\b
    | \bthis\s+later\s+changed\b
    | \bsuperseded\b
    | \bnot\s+(?:yet\s+)?(?:accepted|confirmed|decided|approved|agreed|adopted|settled|resolved|final)\b
    | \bpending\b
    | \bawait(?:s|ed|ing)?\b
    | \brecommend(?:s|ed|ing|ations?)?\b
    | \bpropos(?:e|es|ed|ing|als?)\b
    | \bsuggest(?:s|ed|ing|ions?)?\b
    | \btentative(?:ly)?\b
    | \bopen\s+question\b
    | \bun(?:resolved|decided|confirmed)\b
    | \bunder\s+consideration\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The separator between the parts of an engine fact's text: what | When: ... | Involving: ... | why
# (``fact_extraction.py``, "Build combined fact text from the 4 dimensions").
FACT_PART_SEPARATOR = " | "

# A dated document entry is stamped at noon US Eastern, as the vault loader stamps a document's date
# (the structuring fixtures' document timestamps), so an entry and a document of the same day compare
# by the tuple's later elements.
ENTRY_TIMEZONE = ZoneInfo("America/New_York")
ENTRY_TIME = time(12, 0)

# Namespace for the subject id of a name that resolves to no engine entity.
UNRESOLVED_SUBJECT_NAMESPACE = UUID("6f1c2b8e-5d1e-4c55-9a0e-3b6f0f6c2a71")


class ClaimAnswer(BaseModel):
    """One claim as the model returns it. The schema is appended to the call's message by the
    engine's provider, so the descriptions are terse and the prompt carries the rules."""

    model_config = ConfigDict(extra="ignore")

    subject: str = Field(description="one of the fact's entities, spelled as listed")
    attribute: str = Field(description="lowercase hyphenated key")
    description: str | None = Field(default=None, description="for a new key: what it holds")
    same_as: str | None = Field(default=None, description="existing key a new key duplicates")
    value: str
    provisional: bool
    turn: str | None = Field(default=None, description="conversation: turn id such as T12")
    as_of: str | None = Field(default=None, description="document: YYYY-MM-DD of the entry")
    quote: str | None = Field(default=None, description="only when the fact has several claims")


class FactAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact: str = Field(description="the fact id, such as F1")
    claims: list[ClaimAnswer]


class StructuringAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facts: list[FactAnswer]


def normalize_key(attribute: str) -> str:
    """An attribute key: lowercase, ASCII, hyphenated (specification 2)."""
    return slugify(attribute)


def statement_of(fact_text: str) -> str:
    """The fact's statement: its text before the engine's When, Involving and reason parts."""
    return fact_text.split(FACT_PART_SEPARATOR, 1)[0]


def has_earlier_state_marker(text: str) -> bool:
    return EARLIER_STATE_MARKERS.search(text) is not None


def content_hash(fact_text: str) -> str:
    """SHA-256 of the fact text, whitespace collapsed (specification 4.2)."""
    return hashlib.sha256(" ".join(fact_text.split()).encode("utf-8")).hexdigest()


def unresolved_subject_id(bank_id: str, name: str) -> UUID:
    return uuid5(UNRESOLVED_SUBJECT_NAMESPACE, f"{bank_id}\n{normalize_name(name)}")


def entry_timestamp(day: date) -> datetime:
    return datetime.combine(day, ENTRY_TIME, tzinfo=ENTRY_TIMEZONE)


def parse_answer(raw: object, batch: Batch) -> tuple[dict[str, list[ClaimAnswer]], list[str]]:
    """The model's claims per batch fact id, salvaging what validates; and the issues found.

    A malformed answer as a whole raises ``ValueError`` (the batch is then pending); a malformed
    claim is dropped and noted, so one bad claim does not lose its neighbours.
    """
    issues: list[str] = []
    try:
        return _claims_by_fact(StructuringAnswer.model_validate(raw), batch, issues), issues
    except ValidationError:
        pass
    if not isinstance(raw, dict) or not isinstance(raw.get("facts"), list):
        raise ValueError("the answer has no list of facts")
    facts: list[FactAnswer] = []
    for item in raw["facts"]:
        if not isinstance(item, dict) or not isinstance(item.get("claims"), list):
            issues.append(f"dropped a malformed fact entry: {str(item)[:200]}")
            continue
        claims = []
        for claim in item["claims"]:
            try:
                claims.append(ClaimAnswer.model_validate(claim))
            except ValidationError as error:
                issues.append(f"{item.get('fact')}: dropped a malformed claim ({error.error_count()} errors)")
        facts.append(FactAnswer(fact=str(item.get("fact", "")), claims=claims))
    return _claims_by_fact(StructuringAnswer(facts=facts), batch, issues), issues


def _claims_by_fact(answer: StructuringAnswer, batch: Batch, issues: list[str]) -> dict[str, list[ClaimAnswer]]:
    claims: dict[str, list[ClaimAnswer]] = {}
    for item in answer.facts:
        fact_id = item.fact.strip()
        if fact_id not in batch.fact_ids:
            issues.append(f"the answer names fact {fact_id!r}, which the batch lacks")
            continue
        claims.setdefault(fact_id, []).extend(item.claims)
    return claims


def subject_names_to_resolve(batch: Batch, answers: dict[str, list[ClaimAnswer]]) -> set[str]:
    """Subjects the model named that are not among their fact's entities."""
    names: set[str] = set()
    for fact_id, claims in answers.items():
        listed = {normalize_name(entity.name) for entity in batch.fact_ids[fact_id].entities}
        names.update(claim.subject.strip() for claim in claims if normalize_name(claim.subject) not in listed)
    names.discard("")
    return names


def statement_time(fact: FactInput, claim: ClaimAnswer, batch: Batch) -> tuple[datetime, StatedAtSource]:
    """The claim's timestamp and where it came from (the tuple's first element)."""
    source = fact.source
    if source.kind == "decision":
        return source.date, StatedAtSource.DECISION
    if source.kind == "session":
        turn = batch.turns.get((claim.turn or "").strip().upper())
        if turn is not None and turn.chunk_index == fact.chunk.index and turn.timestamp is not None:
            return turn.timestamp, StatedAtSource.TURN
        stamps = [t.timestamp for t in batch.chunk_turns(fact.chunk.index) if t.timestamp is not None]
        if stamps:
            return min(stamps), StatedAtSource.CHUNK_START
        return source.date, StatedAtSource.SESSION_START
    if claim.as_of:
        try:
            day = date.fromisoformat(claim.as_of.strip()[:10])
        except ValueError:
            day = None
        if day is not None and day <= source.date.astimezone(ENTRY_TIMEZONE).date():
            return entry_timestamp(day), StatedAtSource.ENTRY_DATE
    return source.date, StatedAtSource.DOCUMENT_DATE


def _provisional(claim: ClaimAnswer, statement: str, several: bool) -> bool:
    if claim.provisional:
        return True
    scope = statement
    if several and claim.quote and normalize_name(claim.quote) in normalize_name(statement):
        scope = claim.quote
    return has_earlier_state_marker(scope)


def _resolve_subject(
    claim: ClaimAnswer, fact: FactInput, resolved: dict[str, Entity | None], bank_id: str
) -> tuple[Entity, bool]:
    """The claim's subject entity, and whether it is an engine entity."""
    wanted = normalize_name(claim.subject)
    for entity in fact.entities:
        if normalize_name(entity.name) == wanted:
            return entity, True
    match = resolved.get(claim.subject.strip())
    if match is not None:
        return match, True
    name = claim.subject.strip()
    return Entity(id=unresolved_subject_id(bank_id, name), name=name), False


def _align_key(
    claim: ClaimAnswer,
    key: str,
    subject: Entity,
    catalog: Catalog,
    new_keys: dict[tuple[UUID, str], CatalogEntry],
    had_keys: set[UUID],
    value: str,
    run_keys: frozenset[tuple[UUID, str]],
) -> tuple[str, ClaimState]:
    """The key a claim lands on and whether it is aligned; records new keys and aliases in
    ``new_keys``. ``had_keys`` holds the subjects that had keys before the retain; ``run_keys`` the
    keys earlier calls of the same retain created."""
    known = {**catalog.get(subject.id, {}), **{k: e for (s, k), e in new_keys.items() if s == subject.id}}

    def landing(name: str) -> tuple[str, ClaimState]:
        made = new_keys.get((subject.id, name))
        is_new = (made is not None and made.merged_into is None) or (subject.id, name) in run_keys
        return name, "unaligned" if is_new and subject.id in had_keys else "current"

    entry = known.get(key)
    if entry is not None:
        return landing(entry.merged_into or key)
    target = known.get(normalize_key(claim.same_as)) if claim.same_as else None
    if target is not None:
        canonical = target.merged_into or target.key
        new_keys[(subject.id, key)] = CatalogEntry(
            subject_id=subject.id,
            key=key,
            description=f"alias of {canonical}",
            example_value=value,
            merged_into=canonical,
        )
        return landing(canonical)
    new_keys[(subject.id, key)] = CatalogEntry(
        subject_id=subject.id,
        key=key,
        description=(claim.description or "").strip() or f"{key} of {subject.name}",
        example_value=value,
    )
    return key, "unaligned" if subject.id in had_keys else "current"


def validate(
    batch: Batch,
    answers: dict[str, list[ClaimAnswer]],
    catalog: Catalog,
    resolved: dict[str, Entity | None],
    *,
    bank_id: str,
    prompt_version: str | None,
    model: str | None,
    run_keys: frozenset[tuple[UUID, str]] = frozenset(),
) -> BatchResult:
    """The claim rows and catalog additions for one batch. ``catalog`` is the catalog as it stood
    before the call, for every subject the claims may land on; ``run_keys`` the keys earlier calls
    of the same retain created."""
    result = BatchResult()
    # Subjects with keys from before the retain: keys earlier calls of this retain created do not
    # count, so a retain split into several calls aligns as one call would (the batch is the retain).
    had_keys = {
        subject
        for subject, keys in catalog.items()
        if any(e.merged_into is None and (subject, k) not in run_keys for k, e in keys.items())
    }
    new_keys: dict[tuple[UUID, str], CatalogEntry] = {}

    for fact_id, fact in batch.fact_ids.items():
        claims = answers.get(fact_id, [])
        statement = statement_of(fact.text)
        several = len(claims) > 1
        rows: list[ClaimRow] = []
        for claim in claims:
            value = claim.value.strip()
            key = normalize_key(claim.attribute)
            if not value or not key or not claim.subject.strip():
                result.issues.append(f"{fact_id}: dropped a claim without subject, attribute or value")
                continue
            subject, is_entity = _resolve_subject(claim, fact, resolved, bank_id)
            key, state = _align_key(claim, key, subject, catalog, new_keys, had_keys, value, run_keys)
            if not is_entity:
                state = "unaligned"
                result.issues.append(f"{fact_id}: subject {subject.name!r} resolves to no entity; stored unaligned")
            if any(row.key == (subject.id, key) for row in rows):
                result.issues.append(f"{fact_id}: dropped a second claim on {subject.name}/{key}")
                continue
            stated_at, stated_source = statement_time(fact, claim, batch)
            rows.append(
                ClaimRow(
                    memory_unit_id=fact.id,
                    subject_entity_id=subject.id,
                    subject_text=claim.subject.strip(),
                    attribute_key=key,
                    value_text=value,
                    provisional=_provisional(claim, statement, several),
                    stated_at=stated_at,
                    document_order=fact.source.document_order,
                    chunk_index=fact.chunk.index,
                    fact_ordinal=fact.ordinal,
                    source_rank=SOURCE_RANKS[fact.source.kind],
                    document_id=fact.source.document_id,
                    chunk_id=fact.chunk.chunk_id,
                    source_kind=fact.source.kind,
                    state=state,
                    prompt_version=prompt_version,
                    model=model,
                    content_hash=content_hash(fact.text),
                    stated_at_source=stated_source,
                )
            )
        if rows:
            result.claims.extend(rows)
        else:
            result.unstructured.append(fact.id)
    result.new_attributes = list(new_keys.values())
    return result
