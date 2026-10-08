"""The current-state read (specification 9): the position on a key, exactly, from the claims table.

A query over ``claims`` joined with the engine's facts (live in ``memory_units``, retired in
``invalidated_memory_units``) and the key's catalog row; no model, no ranking. The states it reports
are the ones the rules stored (``supersession.settle`` keeps them current after every retain and
reconciliation); this module only reads them and the facts' text.

- ``current``: for a subject and optionally one key, each key's current claim (with the fact's text,
  which for a decision record is Josh's words verbatim), its later provisional statements, the
  conflict it is in (both claims, newest first), the documents that still say otherwise, and the
  superseded claims newest first with their rules. A subject that matches nothing, or a key with no
  claim, is "no position recorded", never a guess.
- ``subjects``: the subjects whose name contains the text, or that the engine's entity resolution
  maps it to, with their keys and last statement time, so a caller finds the key before asking.
- ``fact``: the claims of one fact and their states.

Subjects resolve through the engine: an entity id; an entity or a claim's subject with exactly that
name (case-insensitive), which includes the synthetic subjects structuring creates for names that
resolve to no entity (their claims are unaligned, so they never carry a settled position); and,
when nothing matches by name, the engine's entity resolver, probed inside a transaction that is
rolled back so the read creates nothing.

A claim counts only while its fact is valid: live, or retired by the rules (its claims stay inputs,
so a superseded claim's text is still readable). A claim whose fact was retired by someone else is
listed in the history as ``withdrawn``; a claim whose fact no longer exists is an orphan the next
reconciliation sweeps, and is left out. Order is the rules' order (``rules.RuleClaim.order``):
statement time, then source rank, then the position inside the document.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from hindsight_api.engine.schema import fq_table_explicit
from pydantic import BaseModel

from .rules import is_rule_retirement
from .structuring.engine import PgStore
from .structuring.names import normalize_name
from .structuring.validation import normalize_key

# Statement times in summaries are shown in Josh's zone (root instructions: US Eastern).
DISPLAY_ZONE = ZoneInfo("America/New_York")

KeyStatus = Literal["current", "conflict", "unaligned", "no-position"]


class ClaimView(BaseModel):
    """One claim as the read reports it. ``text`` is the fact's text: for a decision record, Josh's
    words verbatim. ``state`` is the stored claim state, or ``withdrawn`` when its fact was retired
    outside the rules."""

    claim_id: UUID
    fact_id: UUID
    subject_id: UUID
    subject: str
    attribute: str
    value: str
    state: str
    provisional: bool
    source: str
    stated_at: datetime
    stated_at_source: str | None
    text: str | None
    document_id: str | None
    chunk_id: str | None
    fact_valid: bool
    superseded_by: UUID | None
    rule: str | None


class StaleDocument(BaseModel):
    """A document that still says otherwise, with its newest claim on the key."""

    document_id: str
    says: ClaimView | None


class KeyView(BaseModel):
    subject_id: UUID
    subject: str
    attribute: str
    description: str | None
    status: KeyStatus
    current: ClaimView | None
    conflict: list[ClaimView]
    unaligned: list[ClaimView]
    later_provisional: list[ClaimView]
    stale_documents: list[StaleDocument]
    history: list[ClaimView]
    summary: str


class SubjectRef(BaseModel):
    """A subject: an engine entity (``resolved``), or a synthetic subject for a name that resolves to
    no entity."""

    id: UUID
    name: str
    resolved: bool


class CurrentState(BaseModel):
    bank_id: str
    subject: str
    attribute: str | None
    subjects: list[SubjectRef]
    keys: list[KeyView]
    summary: str


class SubjectKey(BaseModel):
    attribute: str
    description: str | None
    status: KeyStatus
    value: str | None
    last_stated_at: datetime | None
    claims: int


class SubjectMatch(BaseModel):
    id: UUID
    name: str
    resolved: bool
    keys: list[SubjectKey]
    last_stated_at: datetime | None


class Subjects(BaseModel):
    bank_id: str
    q: str
    items: list[SubjectMatch]


class FactClaims(BaseModel):
    bank_id: str
    fact_id: UUID
    fact_valid: bool
    invalidation_reason: str | None
    text: str
    claims: list[ClaimView]


# Queries -----------------------------------------------------------------------------------------


async def _claim_rows(
    conn: Any,
    schema: str,
    bank_id: str,
    *,
    subject_ids: list[UUID] | None = None,
    attributes: list[str] | None = None,
    fact_id: UUID | None = None,
) -> list[Any]:
    claims = fq_table_explicit("claims", schema)
    live = fq_table_explicit("memory_units", schema)
    archive = fq_table_explicit("invalidated_memory_units", schema)
    return await conn.fetch(
        f"""
        SELECT c.*, COALESCE(m.text, i.text) AS fact_text, m.id IS NOT NULL AS fact_live,
               i.id IS NOT NULL AS fact_archived, i.invalidation_reason AS fact_reason, COALESCE(m.created_at, i.created_at) AS fact_created_at
        FROM {claims} c
        LEFT JOIN {live} m ON m.id = c.memory_unit_id AND m.bank_id = c.bank_id
        LEFT JOIN {archive} i ON i.id = c.memory_unit_id AND i.bank_id = c.bank_id
        WHERE c.bank_id = $1
          AND ($2::uuid[] IS NULL OR c.subject_entity_id = ANY($2::uuid[]))
          AND ($3::text[] IS NULL OR c.attribute_key = ANY($3::text[]))
          AND ($4::uuid IS NULL OR c.memory_unit_id = $4)
        """,
        bank_id,
        subject_ids,
        attributes,
        fact_id,
    )


async def _catalog(conn: Any, schema: str, bank_id: str, subject_ids: list[UUID]) -> dict[tuple[UUID, str], Any]:
    rows = await conn.fetch(
        f"""
        SELECT subject_entity_id, attribute_key, description, merged_into, alignment, conflict_claim_ids,
               stale_documents
        FROM {fq_table_explicit("attributes", schema)}
        WHERE bank_id = $1 AND subject_entity_id = ANY($2::uuid[])
        """,
        bank_id,
        subject_ids,
    )
    return {(row["subject_entity_id"], row["attribute_key"]): row for row in rows}


async def _entity_names(conn: Any, schema: str, bank_id: str, ids: list[UUID]) -> dict[UUID, str]:
    rows = await conn.fetch(
        f"SELECT id, canonical_name FROM {fq_table_explicit('entities', schema)} "
        f"WHERE bank_id = $1 AND id = ANY($2::uuid[]) AND entity_kind <> 'label'",
        bank_id,
        ids,
    )
    return {row["id"]: row["canonical_name"] for row in rows}


async def _claim_subject_names(conn: Any, schema: str, bank_id: str, ids: list[UUID]) -> dict[UUID, str]:
    """The name each subject was last stated under, for subjects that are not engine entities."""
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (subject_entity_id) subject_entity_id, subject_text
        FROM {fq_table_explicit("claims", schema)}
        WHERE bank_id = $1 AND subject_entity_id = ANY($2::uuid[])
        ORDER BY subject_entity_id, stated_at DESC, id
        """,
        bank_id,
        ids,
    )
    return {row["subject_entity_id"]: row["subject_text"] for row in rows}


async def _refs(conn: Any, schema: str, bank_id: str, ids: list[UUID]) -> list[SubjectRef]:
    entities = await _entity_names(conn, schema, bank_id, ids)
    stated = await _claim_subject_names(conn, schema, bank_id, [i for i in ids if i not in entities])
    refs = [SubjectRef(id=i, name=entities[i], resolved=True) for i in ids if i in entities]
    refs += [SubjectRef(id=i, name=stated[i], resolved=False) for i in ids if i not in entities and i in stated]
    return sorted(refs, key=lambda ref: (not ref.resolved, normalize_name(ref.name), str(ref.id)))


async def _probe(memory: Any, bank_id: str, name: str) -> UUID | None:
    """The existing entity the engine's resolver maps ``name`` to, or None; creates nothing."""
    found = await PgStore(memory, bank_id).probe_entities([name])
    entity = found.get(name)
    return entity.id if entity is not None else None


async def resolve_subject(memory: Any, conn: Any, schema: str, bank_id: str, subject: str) -> list[SubjectRef]:
    """The subjects ``subject`` names: an id, the exact name, else the engine's entity resolution."""
    text = subject.strip()
    if not text:
        return []
    try:
        ids = [UUID(text)]
    except ValueError:
        rows = await conn.fetch(
            f"""
            SELECT id FROM {fq_table_explicit("entities", schema)}
            WHERE bank_id = $1 AND entity_kind <> 'label' AND LOWER(canonical_name) = LOWER($2)
            UNION
            SELECT DISTINCT subject_entity_id FROM {fq_table_explicit("claims", schema)}
            WHERE bank_id = $1 AND LOWER(subject_text) = LOWER($2)
            """,
            bank_id,
            text,
        )
        ids = [row["id"] for row in rows]
        if not ids:
            probed = await _probe(memory, bank_id, text)
            ids = [probed] if probed is not None else []
    return await _refs(conn, schema, bank_id, ids)


# Building the views ------------------------------------------------------------------------------


def _order(row: Any) -> tuple:
    created = row["fact_created_at"].timestamp() if row["fact_created_at"] else 0.0
    return (
        row["stated_at"],
        row["source_rank"],
        row["document_order"],
        row["chunk_index"],
        row["fact_ordinal"],
        created,
        str(row["memory_unit_id"]),
        str(row["id"]),
    )


def _valid(row: Any) -> bool:
    return bool(row["fact_live"]) or (bool(row["fact_archived"]) and is_rule_retirement(row["fact_reason"]))


def _view(row: Any, subject: str) -> ClaimView:
    valid = _valid(row)
    return ClaimView(
        claim_id=row["id"],
        fact_id=row["memory_unit_id"],
        subject_id=row["subject_entity_id"],
        subject=subject,
        attribute=row["attribute_key"],
        value=row["value_text"],
        state=row["state"] if valid else "withdrawn",
        provisional=row["provisional"],
        source=row["source_kind"],
        stated_at=row["stated_at"],
        stated_at_source=row["stated_at_source"],
        text=row["fact_text"],
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        fact_valid=bool(row["fact_live"]),
        superseded_by=row["superseded_by"],
        rule=row["superseded_rule"],
    )


def when(moment: datetime) -> str:
    """A statement time as a summary shows it: minute precision in US Eastern."""
    return moment.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M %Z")


def phrase(value: str, source: str, stated_at: datetime, document_id: str | None) -> str:
    """One claim in a summary: the value, its source and time, and the document it came from."""
    origin = source if source in ("session", "decision") else f"{source} {document_id}"
    return f'"{value}" ({origin}, {when(stated_at)})'


def said(claim: ClaimView) -> str:
    return phrase(claim.value, claim.source, claim.stated_at, claim.document_id)


def _key_summary(view: KeyView) -> str:
    name = f"{view.subject} / {view.attribute}"
    if view.status == "current" and view.current is not None:
        line = f"{name}: {said(view.current)}"
        if view.current.source == "decision" and view.current.text:
            line += f'; Josh\'s words: "{view.current.text}"'
    elif view.status == "conflict":
        line = f"{name}: in conflict, " + " against ".join(said(c) for c in view.conflict)
        line += "; tell Josh which says what and ask which is right"
    elif view.status == "unaligned":
        line = f"{name}: no settled position (unaligned: the key or the subject is not yet aligned); newest statement "
        line += said(view.unaligned[0])
    else:
        line = f"{name}: no position recorded"
    if view.later_provisional:
        line += "; later provisional statement " + said(view.later_provisional[0])
    for stale in view.stale_documents:
        says = f' still says "{stale.says.value}"' if stale.says else " still says otherwise"
        line += f"; document {stale.document_id}{says}"
    replaced = [c for c in view.history if c.state == "superseded"]
    if replaced:
        line += f"; replaces {said(replaced[0])}"
        if len(replaced) > 1:
            line += f" and {len(replaced) - 1} earlier"
    return line


def build_key(
    subject: SubjectRef, attribute: str, rows: list[Any], catalog_row: Any | None, document_rows: list[Any]
) -> KeyView:
    """One key's view from its claim rows (any state) and its catalog row."""
    ordered = sorted((row for row in rows if row["fact_live"] or row["fact_archived"]), key=_order, reverse=True)
    valid = [row for row in ordered if _valid(row)]

    def views(state: str) -> list[ClaimView]:
        return [_view(row, subject.name) for row in valid if row["state"] == state]

    current = views("current")
    conflict = views("conflict")
    unaligned = views("unaligned")
    provisional = views("provisional")
    history = [_view(row, subject.name) for row in ordered if not _valid(row) or row["state"] == "superseded"]
    head = current[0] if current else None
    # A claim still provisional has no later claim at all (S2), so each one is later than the current claim.
    status: KeyStatus
    if conflict:
        status = "conflict"
    elif head is not None:
        status = "current"
    elif unaligned:
        status = "unaligned"
    else:
        status = "no-position"

    stale: list[StaleDocument] = []
    for document in (catalog_row["stale_documents"] or []) if catalog_row else []:
        newest = next((row for row in document_rows if row["document_id"] == document), None)
        stale.append(StaleDocument(document_id=document, says=_view(newest, subject.name) if newest else None))

    view = KeyView(
        subject_id=subject.id,
        subject=subject.name,
        attribute=attribute,
        description=catalog_row["description"] if catalog_row else None,
        status=status,
        current=head if status == "current" else None,
        conflict=conflict,
        unaligned=unaligned,
        later_provisional=provisional,
        stale_documents=stale,
        history=history,
        summary="",
    )
    view.summary = _key_summary(view)
    return view


def _follow(catalog: dict[tuple[UUID, str], Any], subject_id: UUID, key: str) -> str:
    """A merged key's target: an alias names the key its claims were moved to (6.3)."""
    seen = {key}
    while (row := catalog.get((subject_id, key))) is not None and row["merged_into"] and row["merged_into"] not in seen:
        key = row["merged_into"]
        seen.add(key)
    return key


async def current(
    memory: Any, conn: Any, schema: str, bank_id: str, subject: str, attribute: str | None = None
) -> CurrentState:
    """The current position on one key of a subject, or on every key it has."""
    refs = await resolve_subject(memory, conn, schema, bank_id, subject)
    wanted = normalize_key(attribute) if attribute and attribute.strip() else None
    if not refs:
        missing = f"no subject matches {subject.strip()!r}"
        return CurrentState(
            bank_id=bank_id,
            subject=subject,
            attribute=wanted,
            subjects=[],
            keys=[],
            summary=f"No position recorded: {missing}.",
        )
    ids = [ref.id for ref in refs]
    catalog = await _catalog(conn, schema, bank_id, ids)
    targets = {ref.id: _follow(catalog, ref.id, wanted) for ref in refs} if wanted else None
    rows = await _claim_rows(
        conn, schema, bank_id, subject_ids=ids, attributes=sorted(set(targets.values())) if targets else None
    )
    by_key: dict[tuple[UUID, str], list[Any]] = defaultdict(list)
    for row in rows:
        by_key[(row["subject_entity_id"], row["attribute_key"])].append(row)

    keys: list[KeyView] = []
    for ref in refs:
        names = [targets[ref.id]] if targets else sorted(k for s, k in by_key if s == ref.id)
        for name in names:
            key_rows = by_key.get((ref.id, name), [])
            if targets and not key_rows and len(refs) > 1:
                continue  # several subjects matched the name: report the key where it has claims
            document_rows = sorted(
                (r for r in key_rows if r["document_id"] and (r["fact_live"] or r["fact_archived"])),
                key=_order,
                reverse=True,
            )
            keys.append(build_key(ref, name, key_rows, catalog.get((ref.id, name)), document_rows))
    if targets and not keys:
        first = refs[0]
        keys = [build_key(first, targets[first.id], [], catalog.get((first.id, targets[first.id])), [])]
    summary = "\n".join(view.summary for view in keys) if keys else f"{refs[0].name}: no position recorded on any key."
    return CurrentState(bank_id=bank_id, subject=subject, attribute=wanted, subjects=refs, keys=keys, summary=summary)


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def subjects(memory: Any, conn: Any, schema: str, bank_id: str, q: str, limit: int) -> Subjects:
    """Subjects whose name contains ``q``, or that the engine's entity resolution maps it to, with their keys.

    Subjects with claims are listed (an entity with no claims has no key to ask about), except the
    exact or resolved match, which is listed even with no keys so the caller learns it exists.
    Exact matches come first, then the most recently stated subjects."""
    text = q.strip()
    pattern = _like(text)
    rows = await conn.fetch(
        f"""
        SELECT id FROM {fq_table_explicit("entities", schema)}
        WHERE bank_id = $1 AND entity_kind <> 'label' AND canonical_name ILIKE $2
        UNION
        SELECT DISTINCT subject_entity_id FROM {fq_table_explicit("claims", schema)}
        WHERE bank_id = $1 AND subject_text ILIKE $2
        """,
        bank_id,
        pattern,
    )
    exact = {ref.id for ref in await resolve_subject(memory, conn, schema, bank_id, text)}
    ids = sorted({row["id"] for row in rows} | exact, key=str)
    if not ids:
        return Subjects(bank_id=bank_id, q=q, items=[])
    claim_rows = await _claim_rows(conn, schema, bank_id, subject_ids=ids)
    catalog = await _catalog(conn, schema, bank_id, ids)
    by_key: dict[tuple[UUID, str], list[Any]] = defaultdict(list)
    for row in claim_rows:
        by_key[(row["subject_entity_id"], row["attribute_key"])].append(row)

    items: list[SubjectMatch] = []
    for ref in await _refs(conn, schema, bank_id, ids):
        keys: list[SubjectKey] = []
        for (subject_id, name), key_rows in sorted(by_key.items(), key=lambda item: item[0][1]):
            if subject_id != ref.id:
                continue
            view = build_key(ref, name, key_rows, catalog.get((subject_id, name)), [])
            stated = [row["stated_at"] for row in key_rows if row["fact_live"] or row["fact_archived"]]
            if not stated:
                continue
            value = view.current.value if view.current else None
            keys.append(
                SubjectKey(
                    attribute=name,
                    description=view.description,
                    status=view.status,
                    value=value,
                    last_stated_at=max(stated),
                    claims=len(stated),
                )
            )
        if not keys and ref.id not in exact:
            continue
        last = max((k.last_stated_at for k in keys if k.last_stated_at), default=None)
        items.append(SubjectMatch(id=ref.id, name=ref.name, resolved=ref.resolved, keys=keys, last_stated_at=last))
    items.sort(key=lambda m: (m.id not in exact, -(m.last_stated_at.timestamp() if m.last_stated_at else 0), m.name))
    return Subjects(bank_id=bank_id, q=q, items=items[:limit])


async def fact(conn: Any, schema: str, bank_id: str, fact_id: UUID) -> FactClaims | None:
    """The claims of one fact and their states, or None when the fact exists in neither table."""
    unit = await conn.fetchrow(
        f"""
        SELECT text, true AS live, NULL::text AS reason FROM {fq_table_explicit("memory_units", schema)}
        WHERE bank_id = $1 AND id = $2
        UNION ALL
        SELECT text, false, invalidation_reason FROM {fq_table_explicit("invalidated_memory_units", schema)}
        WHERE bank_id = $1 AND id = $2
        """,
        bank_id,
        fact_id,
    )
    if unit is None:
        return None
    rows = await _claim_rows(conn, schema, bank_id, fact_id=fact_id)
    names = await _entity_names(conn, schema, bank_id, sorted({row["subject_entity_id"] for row in rows}, key=str))
    claims = [
        _view(row, names.get(row["subject_entity_id"], row["subject_text"]))
        for row in sorted(rows, key=lambda r: (normalize_name(r["subject_text"]), r["attribute_key"], str(r["id"])))
    ]
    return FactClaims(
        bank_id=bank_id,
        fact_id=fact_id,
        fact_valid=bool(unit["live"]),
        invalidation_reason=unit["reason"],
        text=unit["text"],
        claims=claims,
    )


async def schema_for(memory: Any, request_context: Any) -> str:
    """Authenticate the caller as the engine does and return its schema (also set for ``fq_table``)."""
    return await memory._authenticate_tenant(request_context)


__all__ = [
    "ClaimView",
    "CurrentState",
    "FactClaims",
    "KeyView",
    "SubjectMatch",
    "SubjectRef",
    "Subjects",
    "current",
    "fact",
    "resolve_subject",
    "schema_for",
    "subjects",
]
