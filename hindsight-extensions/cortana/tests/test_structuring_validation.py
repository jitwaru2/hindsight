"""Structuring's validation, in code and without a model (specification 5.2): key normalization,
the earlier-state markers, the content hash, the statement-time tuple, subject resolution, unaligned
keys and ``same_as``, and salvaging a partly malformed answer."""

import json
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from hindsight_ext_cortana.structuring.batching import Batch
from hindsight_ext_cortana.structuring.records import (
    CatalogEntry,
    Chunk,
    Entity,
    FactInput,
    Source,
    SourceRank,
    StatedAtSource,
)
from hindsight_ext_cortana.structuring.validation import (
    ClaimAnswer,
    content_hash,
    entry_timestamp,
    has_earlier_state_marker,
    names_overlap,
    normalize_key,
    parse_answer,
    statement_of,
    statement_time,
    subject_names_to_resolve,
    unresolved_subject_id,
    validate,
)

BANK = "bank-validation"
KESTREL = Entity(uuid4(), "Kestrel")
PRIYA = Entity(uuid4(), "Priya Natarajan")
PRIYA_SHORT = Entity(uuid4(), "Priya")


def _turn(role: str, stamp: str, content: str) -> str:
    return json.dumps({"role": role, "content": content, "timestamp": stamp})


SESSION_START = datetime(2025, 3, 3, 13, 0, tzinfo=UTC)
CHUNK_0 = Chunk(
    0,
    "\n".join(
        [
            _turn("user", "2025-03-03T13:00:00Z", "How often should Kestrel ship?"),
            _turn("assistant", "2025-03-03T13:01:30Z", "I recommend shipping weekly."),
        ]
    ),
    "bank_doc_0",
)
CHUNK_1 = Chunk(1, _turn("user", "2025-03-03T15:20:00Z", "We ship weekly from now on."), "bank_doc_1")
SESSION = Source(kind="session", document_id="conversation:s1", context="", date=SESSION_START)
DOCUMENT = Source(
    kind="document",
    document_id="docs/kestrel.md",
    context="",
    date=datetime(2025, 5, 21, 12, 0, tzinfo=entry_timestamp(date(2025, 5, 21)).tzinfo),
)


def fact(text: str, entities=(KESTREL,), chunk=CHUNK_0, ordinal=0, source=SESSION) -> FactInput:
    return FactInput(id=uuid4(), text=text, entities=tuple(entities), chunk=chunk, ordinal=ordinal, source=source)


def claim(**fields) -> ClaimAnswer:
    base = {"subject": "Kestrel", "attribute": "release-cadence", "value": "weekly", "provisional": False}
    return ClaimAnswer(**{**base, **fields})


def run(batch: Batch, answers, catalog=None, resolved=None):
    return validate(
        batch,
        answers,
        catalog or {},
        resolved or {},
        bank_id=BANK,
        prompt_version="1",
        model="mock/mock",
    )


# Keys, markers, hash


@pytest.mark.parametrize(
    ("given", "key"),
    [
        ("lesson-schedule", "lesson-schedule"),
        ("Lesson Schedule", "lesson-schedule"),
        ("  release_cadence!! ", "release-cadence"),
        ("Café hours", "cafe-hours"),
        ("release--days", "release-days"),
        ("!!!", ""),
    ],
)
def test_attribute_keys_are_lowercase_and_hyphenated(given, key):
    assert normalize_key(given) == key


def test_the_statement_is_the_text_before_the_engines_parts():
    text = "Lee chose to fork it. | When: 2026-10-07 | Involving: Lee | The fork supersedes the old build."
    assert statement_of(text) == "Lee chose to fork it."
    assert statement_of("No parts here.") == "No parts here."


@pytest.mark.parametrize(
    "text",
    [
        "Mara decided to pause sessions (later reversed on 2025-05-20).",
        "Her slot was Thursday 4pm (later superseded by Tuesdays).",
        "Chose to cancel (superseded in effect by the later plan).",
        "This was later contradicted when the plan dropped it.",
        "The slot was weekly; this later changed.",
        "The migration is pending review.",
        "The proposal is awaiting Mara's confirmation.",
        "The agent recommended moving to weekly releases.",
        "The agent's recommendation was not yet accepted.",
        "A move to Postgres was proposed.",
        "As of 2025-03-03, the boundary is not yet confirmed by Mara.",
        "Which position stands is an open question.",
    ],
)
def test_earlier_state_markers_are_found(text):
    assert has_earlier_state_marker(text)


@pytest.mark.parametrize(
    "text",
    [
        "Lee chose to fork Kestrel to his own account.",
        "Mara ruled that all notes live in Orchard.",
        "Rowan Ellis is a current coach of Mara Lindqvist.",
        "The release check still fails: 3 of 8 cases.",
        "Lee revised his position into a release rule.",
    ],
)
def test_resolutions_carry_no_marker(text):
    assert not has_earlier_state_marker(text)


def test_the_content_hash_ignores_whitespace_and_tracks_the_text():
    assert content_hash("Mara chose  weekly.\n") == content_hash("Mara chose weekly.")
    assert content_hash("Mara chose weekly.") != content_hash("Mara chose monthly.")
    assert len(content_hash("x")) == 64


# Statement time


def test_a_session_claim_takes_its_turns_timestamp():
    f = fact("The agent recommended shipping weekly.")
    batch = Batch(source=SESSION, facts=[f])
    assert statement_time(f, claim(turn="T2"), batch) == (
        datetime(2025, 3, 3, 13, 1, 30, tzinfo=UTC),
        StatedAtSource.TURN,
    )
    assert statement_time(f, claim(turn="t2"), batch)[1] is StatedAtSource.TURN


def test_a_turn_outside_the_facts_chunk_falls_back_to_the_chunks_first_turn():
    f0 = fact("The agent recommended shipping weekly.")
    f1 = fact("Lee chose weekly releases.", chunk=CHUNK_1)
    batch = Batch(source=SESSION, facts=[f0, f1])
    assert [t.id for t in batch.turns.values()] == ["T1", "T2", "T3"]
    stamp, origin = statement_time(f0, claim(turn="T3"), batch)
    assert (stamp, origin) == (datetime(2025, 3, 3, 13, 0, tzinfo=UTC), StatedAtSource.CHUNK_START)
    assert statement_time(f0, claim(turn=None), batch)[1] is StatedAtSource.CHUNK_START


def test_a_session_without_turn_timestamps_takes_its_start():
    untimed = Chunk(0, json.dumps({"role": "user", "content": "Ship weekly."}))
    f = fact("Ship weekly.", chunk=untimed)
    assert statement_time(f, claim(turn="T1"), Batch(source=SESSION, facts=[f])) == (
        SESSION_START,
        StatedAtSource.SESSION_START,
    )
    plain = fact("Ship weekly.", chunk=Chunk(0, "not json at all"))
    assert statement_time(plain, claim(), Batch(source=SESSION, facts=[plain]))[1] is StatedAtSource.SESSION_START


def test_a_document_claim_takes_its_entry_date_and_falls_back_to_the_document_date():
    f = fact("As of 2025-05-02, sessions were weekly; this later changed.", source=DOCUMENT)
    batch = Batch(source=DOCUMENT, facts=[f])
    assert statement_time(f, claim(as_of="2025-05-02"), batch) == (
        entry_timestamp(date(2025, 5, 2)),
        StatedAtSource.ENTRY_DATE,
    )
    assert entry_timestamp(date(2025, 5, 2)).isoformat() == "2025-05-02T12:00:00-04:00"
    for as_of in (None, "not a date", "2025-06-30"):
        assert statement_time(f, claim(as_of=as_of), batch) == (DOCUMENT.date, StatedAtSource.DOCUMENT_DATE)


def test_a_decision_claim_takes_the_moment_it_was_stated():
    stated = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    source = Source(kind="decision", document_id="decision:1", context="", date=stated)
    f = fact("Lee chose weekly releases.", source=source)
    assert statement_time(f, claim(), Batch(source=source, facts=[f])) == (stated, StatedAtSource.DECISION)


def test_document_claims_order_by_entry_date_not_by_position():
    """A newest-first living document: the older entry sits in the later chunk, and must sort first."""
    newest = fact("Mara chose Tuesdays at 7am.", chunk=Chunk(0, "## 2025-05-20"), source=DOCUMENT)
    oldest = fact("Sessions were Thursdays; this later changed.", chunk=Chunk(1, "## 2025-05-02"), source=DOCUMENT)
    batch = Batch(source=DOCUMENT, facts=[newest, oldest])
    answers = {
        "F1": [claim(attribute="schedule", value="Tuesdays 7am", as_of="2025-05-20")],
        "F2": [claim(attribute="schedule", value="Thursdays", as_of="2025-05-02")],
    }
    rows = {r.memory_unit_id: r for r in run(batch, answers).claims}
    assert rows[oldest.id].statement_time < rows[newest.id].statement_time
    assert rows[oldest.id].provisional and not rows[newest.id].provisional


def test_session_claims_order_by_turn_then_chunk_and_ordinal():
    recommendation = fact("The agent recommended weekly releases.", ordinal=0)
    choice = fact("Lee chose weekly releases.", chunk=CHUNK_1, ordinal=0)
    batch = Batch(source=SESSION, facts=[choice, recommendation])
    answers = {"F1": [claim(turn="T3")], "F2": [claim(turn="T2", provisional=True)]}
    rows = {r.memory_unit_id: r for r in run(batch, answers).claims}
    assert rows[recommendation.id].statement_time < rows[choice.id].statement_time
    assert rows[choice.id].source_rank is SourceRank.SESSION
    assert rows[choice.id].statement_time == (datetime(2025, 3, 3, 15, 20, tzinfo=UTC), 0, 1, 0, 0)


# Claims: subjects, keys, provisional


def test_a_claim_row_carries_every_field():
    f = fact("Lee chose weekly releases. | When: 2025-03-03 | Involving: Lee", chunk=CHUNK_1, ordinal=4)
    (row,) = run(Batch(source=SESSION, facts=[f]), {"F1": [claim(subject="kestrel", turn="T1")]}).claims
    assert row.memory_unit_id == f.id
    assert (row.subject_entity_id, row.subject_text) == (KESTREL.id, "kestrel")
    assert (row.attribute_key, row.value_text, row.provisional, row.state) == (
        "release-cadence",
        "weekly",
        False,
        "current",
    )
    assert (row.chunk_index, row.fact_ordinal, row.document_order) == (1, 4, 0)
    assert (row.document_id, row.chunk_id, row.source_kind) == ("conversation:s1", "bank_doc_1", "session")
    assert (row.prompt_version, row.model) == ("1", "mock/mock")
    assert row.content_hash == content_hash(f.text)
    assert row.stated_at_source is StatedAtSource.TURN


def test_a_marker_makes_a_claim_provisional_whatever_the_model_says():
    f = fact("Lee decided to pause releases (later reversed on 2025-03-10).")
    (row,) = run(Batch(source=SESSION, facts=[f]), {"F1": [claim(provisional=False)]}).claims
    assert row.provisional


def test_a_marker_in_the_reason_part_does_not_count():
    f = fact("Lee chose to fork Kestrel. | When: 2025-03-03 | The fork supersedes and replaces the proposed build.")
    (row,) = run(Batch(source=SESSION, facts=[f]), {"F1": [claim(provisional=False)]}).claims
    assert not row.provisional


def test_a_compound_fact_scopes_markers_to_each_claims_quote():
    f = fact("Lee chose weekly releases, and the agent's proposal of a release owner is pending.")
    answers = {
        "F1": [
            claim(quote="Lee chose weekly releases"),
            claim(
                attribute="release-owner",
                value="agent's proposal",
                quote="the agent's proposal of a release owner is pending",
            ),
        ]
    }
    rows = {r.attribute_key: r for r in run(Batch(source=SESSION, facts=[f]), answers).claims}
    assert not rows["release-cadence"].provisional
    assert rows["release-owner"].provisional


def test_a_quote_not_found_in_the_statement_falls_back_to_the_whole_statement():
    f = fact("Lee chose weekly releases, and the release owner is pending.")
    answers = {"F1": [claim(quote="something else entirely"), claim(attribute="release-owner", value="pending")]}
    rows = run(Batch(source=SESSION, facts=[f]), answers).claims
    assert all(row.provisional for row in rows)


def test_a_short_name_and_a_resolved_name_are_one_subject():
    f_short = fact("Priya chose Tuesdays.", entities=(PRIYA_SHORT,))
    f_full = fact("Priya Natarajan chose Tuesdays.", entities=(PRIYA,))
    batch = Batch(source=SESSION, facts=[f_short, f_full])
    answers = {
        "F1": [claim(subject="Priya Natarajan", attribute="lesson-schedule", value="Tuesdays")],
        "F2": [claim(subject="Priya Natarajan", attribute="lesson-schedule", value="Tuesdays")],
    }
    assert subject_names_to_resolve(batch, answers) == {"Priya Natarajan"}
    rows = run(batch, answers, resolved={"Priya Natarajan": PRIYA}).claims
    assert {row.subject_entity_id for row in rows} == {PRIYA.id}
    assert all(row.state == "current" for row in rows)


def test_a_subject_that_resolves_to_nothing_is_kept_unaligned_under_a_stable_id():
    f = fact("The release train runs weekly.")
    answers = {"F1": [claim(subject="release train")]}
    (row,) = run(Batch(source=SESSION, facts=[f]), answers, resolved={"release train": None}).claims
    assert row.state == "unaligned"
    assert row.subject_entity_id == unresolved_subject_id(BANK, "Release  Train")
    assert row.subject_text == "release train"


def test_a_new_key_on_a_subject_without_keys_becomes_its_key():
    f = fact("Lee chose weekly releases.")
    result = run(Batch(source=SESSION, facts=[f]), {"F1": [claim(description="How often Kestrel ships")]})
    assert result.claims[0].state == "current"
    (entry,) = result.new_attributes
    assert (entry.subject_id, entry.key, entry.description, entry.example_value, entry.merged_into) == (
        KESTREL.id,
        "release-cadence",
        "How often Kestrel ships",
        "weekly",
        None,
    )


def _catalog(*keys: str, merged: dict[str, str] | None = None):
    entries = {k: CatalogEntry(KESTREL.id, k, f"{k} of Kestrel") for k in keys}
    for alias, target in (merged or {}).items():
        entries[alias] = CatalogEntry(KESTREL.id, alias, f"alias of {target}", merged_into=target)
    return {KESTREL.id: entries}


def test_an_existing_key_is_aligned_and_adds_nothing():
    f = fact("Lee chose weekly releases.")
    result = run(
        Batch(source=SESSION, facts=[f]), {"F1": [claim(attribute="Release Cadence")]}, _catalog("release-cadence")
    )
    assert result.claims[0].attribute_key == "release-cadence"
    assert result.claims[0].state == "current"
    assert result.new_attributes == []


def test_a_new_key_on_a_subject_with_keys_is_unaligned_and_joins_the_catalog():
    f1 = fact("Lee chose weekly releases.")
    f2 = fact("Kestrel ships weekly now.")
    batch = Batch(source=SESSION, facts=[f1, f2])
    answers = {"F1": [claim(attribute="ship-frequency")], "F2": [claim(attribute="ship-frequency")]}
    result = run(batch, answers, _catalog("release-owner"))
    assert [row.state for row in result.claims] == ["unaligned", "unaligned"]
    assert [e.key for e in result.new_attributes] == ["ship-frequency"]


def test_same_as_puts_a_new_key_on_the_existing_one_and_records_the_alias():
    f = fact("Lee chose weekly releases.")
    answers = {"F1": [claim(attribute="ship-frequency", same_as="release-cadence")]}
    result = run(Batch(source=SESSION, facts=[f]), answers, _catalog("release-cadence"))
    assert (result.claims[0].attribute_key, result.claims[0].state) == ("release-cadence", "current")
    (alias,) = result.new_attributes
    assert (alias.key, alias.merged_into) == ("ship-frequency", "release-cadence")


def test_same_as_naming_no_existing_key_leaves_the_claim_unaligned():
    f = fact("Lee chose weekly releases.")
    answers = {"F1": [claim(attribute="ship-frequency", same_as="no-such-key")]}
    result = run(Batch(source=SESSION, facts=[f]), answers, _catalog("release-owner"))
    assert (result.claims[0].attribute_key, result.claims[0].state) == ("ship-frequency", "unaligned")


def test_a_recorded_alias_maps_to_its_key():
    f = fact("Lee chose weekly releases.")
    answers = {"F1": [claim(attribute="ship-frequency")]}
    result = run(
        Batch(source=SESSION, facts=[f]),
        answers,
        _catalog("release-cadence", merged={"ship-frequency": "release-cadence"}),
    )
    assert (result.claims[0].attribute_key, result.claims[0].state) == ("release-cadence", "current")


def test_a_fact_with_no_valid_claim_is_left_unstructured():
    f1 = fact("Lee chose weekly releases.")
    f2 = fact("Something with no usable claim.")
    f3 = fact("Missing from the answer.")
    batch = Batch(source=SESSION, facts=[f1, f2, f3])
    answers = {"F1": [claim()], "F2": [claim(value="  "), claim(attribute="!!!")]}
    result = run(batch, answers)
    assert [row.memory_unit_id for row in result.claims] == [f1.id]
    assert result.unstructured == [f2.id, f3.id]
    assert len(result.issues) == 2


def test_a_second_claim_on_one_key_in_one_fact_is_dropped():
    f = fact("Lee chose weekly releases.")
    answers = {"F1": [claim(value="weekly"), claim(value="every week")]}
    result = run(Batch(source=SESSION, facts=[f]), answers)
    assert [row.value_text for row in result.claims] == ["weekly"]
    assert result.issues


# The answer


def test_a_well_formed_answer_parses():
    f = fact("Lee chose weekly releases.")
    batch = Batch(source=SESSION, facts=[f])
    answers, issues = parse_answer({"facts": [{"fact": "F1", "claims": [claim().model_dump()]}]}, batch)
    assert list(answers) == ["F1"] and issues == []


def test_a_malformed_claim_is_dropped_and_its_neighbours_kept():
    batch = Batch(source=SESSION, facts=[fact("a"), fact("b")])
    raw = {
        "facts": [
            {"fact": "F1", "claims": [claim().model_dump(), {"subject": "Kestrel"}]},
            {"fact": "F9", "claims": [claim().model_dump()]},
            "not an entry",
        ]
    }
    answers, issues = parse_answer(raw, batch)
    assert len(answers["F1"]) == 1
    assert "F9" not in answers
    assert len(issues) == 3


@pytest.mark.parametrize("raw", [None, "text", {"facts": "no"}, {"other": []}])
def test_an_answer_without_facts_fails_the_call(raw):
    with pytest.raises(ValueError):
        parse_answer(raw, Batch(source=SESSION, facts=[fact("a")]))


def test_unresolved_subject_ids_are_per_bank():
    assert unresolved_subject_id("a", "x") != unresolved_subject_id("b", "x")
    assert isinstance(unresolved_subject_id("a", "x"), UUID)


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("Priya", "Priya Natarajan", True),
        ("Priya Natarajan", "priya", True),
        ("Kestrel", "Kestrel fork", True),
        ("Priya", "Priyanka Natarajan", False),
        ("Kestrel", "Lumen", False),
        ("!!!", "Kestrel", False),
    ],
)
def test_name_variants_are_names_whose_words_one_contains(a, b, overlap):
    assert names_overlap(a, b) is overlap


def test_keys_an_earlier_call_of_the_same_retain_made_align_as_in_one_call():
    """A retain split into several calls: the second call sees the first call's new key in the
    catalog, and a subject that had no keys before the retain still aligns on it."""
    f = fact("Kestrel ships weekly now.")
    catalog = {KESTREL.id: {"release-cadence": CatalogEntry(KESTREL.id, "release-cadence", "made by call 1")}}
    run_keys = frozenset({(KESTREL.id, "release-cadence")})
    batch = Batch(source=SESSION, facts=[f])
    same = validate(
        batch, {"F1": [claim()]}, catalog, {}, bank_id=BANK, prompt_version="1", model="m", run_keys=run_keys
    )
    assert same.claims[0].state == "current"
    other = validate(
        batch,
        {"F1": [claim(attribute="release-owner", value="Lee")]},
        catalog,
        {},
        bank_id=BANK,
        prompt_version="1",
        model="m",
        run_keys=run_keys,
    )
    assert other.claims[0].state == "current", "the subject had no keys before the retain"
    before = {KESTREL.id: {**catalog[KESTREL.id], "release-owner": CatalogEntry(KESTREL.id, "release-owner", "older")}}
    owner_existed = validate(
        batch, {"F1": [claim()]}, before, {}, bank_id=BANK, prompt_version="1", model="m", run_keys=run_keys
    )
    assert owner_existed.claims[0].state == "unaligned", "a key new in this retain, on a subject with older keys"
