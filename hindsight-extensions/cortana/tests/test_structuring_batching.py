"""The structuring prompt is versioned and pinned; a retain's facts are packed into calls by chunk
and capped; a call's message carries the source, the catalog and the facts; and the suite scores
claims as HSIGHT-4 decided (precision on key and provisional flag, key stability). No model."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from hindsight_ext_cortana import structuring
from hindsight_ext_cortana.structuring.batching import MAX_CHUNKS_PER_CALL, MAX_FACTS_PER_CALL, make_batches, render
from hindsight_ext_cortana.structuring.fixtures import load_fixture
from hindsight_ext_cortana.structuring.records import CatalogEntry, Chunk, Entity, FactInput, Source
from hindsight_ext_cortana.structuring.suite import fixture_facts, key_stability, score_fixture
from hindsight_ext_cortana.structuring.validation import ClaimAnswer, validate

from test_structuring_fixtures import SYNTHETIC

# A change to prompt.md or to the message batching.render builds is a release (specification 11):
# raise VERSION, re-pin this hash, and run the structuring suite (hindsight-cortana suite
# structuring) before it ships.
PINNED_VERSION = "8"
PINNED_SHA256 = "e818b30718ce721e561ef8b1b8c137d00977d264bef05d69572dadfefd2df26e"

SOURCE = Source(kind="session", document_id="conversation:s", context="ctx", date=datetime(2025, 1, 1, tzinfo=UTC))
ORCA = Entity(uuid4(), "Orca")


def test_the_prompt_is_pinned_to_its_version():
    assert structuring.VERSION == PINNED_VERSION
    assert hashlib.sha256(structuring.prompt().encode()).hexdigest() == PINNED_SHA256


def test_the_prompt_names_no_fixture_people_or_projects():
    text = structuring.prompt()
    for name in ("Orchard", "Mara", "Rowan", "Postgres", "Hindsight"):
        assert name not in text


def _facts(per_chunk: dict[int, int], source: Source = SOURCE) -> list[FactInput]:
    facts = []
    for index, count in per_chunk.items():
        chunk = Chunk(
            index, json.dumps({"role": "user", "content": f"chunk {index}", "timestamp": "2025-01-01T00:00:00Z"})
        )
        facts += [FactInput(uuid4(), f"fact {index}.{n}", (ORCA,), chunk, n, source) for n in range(count)]
    return facts


def test_small_retains_make_one_call():
    (batch,) = make_batches(_facts({0: 5, 1: 7}))
    assert len(batch.facts) == 12 and [c.index for c in batch.chunks] == [0, 1]


def test_calls_are_capped_and_keep_chunks_whole_where_they_can():
    big = MAX_FACTS_PER_CALL * 2 // 3
    batches = make_batches(_facts({0: big, 1: big, 2: 5}))
    assert [len(b.facts) for b in batches] == [big, big + 5]
    assert [[c.index for c in b.chunks] for b in batches] == [[0], [1, 2]]
    assert all(len(b.facts) <= MAX_FACTS_PER_CALL for b in batches)


def test_a_chunk_larger_than_a_call_is_split():
    batches = make_batches(_facts({0: MAX_FACTS_PER_CALL * 2 + 3}))
    assert [len(b.facts) for b in batches] == [MAX_FACTS_PER_CALL, MAX_FACTS_PER_CALL, 3]
    assert all([c.index for c in b.chunks] == [0] for b in batches)


def test_a_call_shows_a_bounded_number_of_chunks():
    batches = make_batches(_facts(dict.fromkeys(range(MAX_CHUNKS_PER_CALL + 2), 1)))
    assert [len(b.chunks) for b in batches] == [MAX_CHUNKS_PER_CALL, 2]


def test_documents_never_share_a_call():
    other = Source(kind="document", document_id="docs/a.md", context="", date=SOURCE.date, document_order=1)
    batches = make_batches(_facts({0: 2}) + _facts({0: 2}, other))
    assert [b.source.document_id for b in batches] == ["conversation:s", "docs/a.md"]


def test_the_message_carries_turns_catalog_and_facts():
    facts = _facts({0: 2})
    (batch,) = make_batches(facts)
    catalog = {
        ORCA.id: {
            "pod-size": CatalogEntry(ORCA.id, "pod-size", "How many orcas travel together", "eleven"),
            "group-size": CatalogEntry(ORCA.id, "group-size", "alias of pod-size", merged_into="pod-size"),
        }
    }
    message = render(batch, catalog)
    assert "[T1 | 2025-01-01T00:00:00+00:00 | user] chunk 0" in message
    assert 'pod-size: How many orcas travel together Example: "eleven"' in message
    assert "group-size" not in message
    assert "F1 (chunk 0)\nentities: Orca\ntext: fact 0.0" in message
    document = make_batches(_facts({3: 1}, Source(kind="document", document_id="d", context="", date=SOURCE.date)))
    assert "TEXT OF CHUNK 3" in render(document[0], {})


# The suite's scoring, on the synthetic session fixture with hand-written answers


def _structure(answers_by_fact: dict[str, tuple[str, str, bool]]):
    fixture = load_fixture(SYNTHETIC / "synthetic-session.json")
    facts, names, _ = fixture_facts(fixture)
    by_name = {names[f.id]: f for f in facts}
    claims = []
    for fixture_id, (subject, attribute, provisional) in answers_by_fact.items():
        f = by_name[fixture_id]
        (batch,) = make_batches([f])
        answer = ClaimAnswer(subject=subject, attribute=attribute, value="v", provisional=provisional)
        claims += validate(batch, {"F1": [answer]}, {}, {}, bank_id="b", prompt_version="1", model="m").claims
    return fixture, claims, names


RIGHT = {
    "f1": ("Orchard", "storage-backend", True),
    "f5": ("Orchard", "storage-backend", False),
    "f2": ("Orchard", "notes-store", False),
    "f4": ("Orchard", "notes-store", True),
    "f6": ("Orchard", "postgres-database", False),
}


def test_the_suite_scores_a_right_answer_as_precise():
    fixture, claims, names = _structure(RIGHT)
    score = score_fixture(fixture, claims, names)
    assert (score.expected, score.produced, score.correct) == (5, 5, 5)
    assert score.precision == 1.0 and score.recall == 1.0
    assert [g.end_state for g in score.groups] == ["f5", "f2", "f6"]
    assert score.timed == 5


def test_a_wrong_provisional_flag_and_a_split_key_count_against_precision():
    answers = {**RIGHT, "f5": ("Orchard", "storage-backend", True), "f4": ("Orchard", "notes-confirmation", True)}
    fixture, claims, names = _structure(answers)
    score = score_fixture(fixture, claims, names)
    assert score.correct == 3
    assert score.precision == 3 / 5


def test_two_groups_on_one_key_collide_and_all_their_claims_count_wrong():
    answers = {**RIGHT, "f6": ("Orchard", "storage-backend", False)}
    fixture, claims, names = _structure(answers)
    score = score_fixture(fixture, claims, names)
    assert [g.collides for g in score.groups] == [True, False, True]
    assert score.correct == 2


def test_a_fact_without_claims_lowers_recall_not_precision():
    answers = {k: v for k, v in RIGHT.items() if k != "f6"}
    fixture, claims, names = _structure(answers)
    score = score_fixture(fixture, claims, names)
    assert (score.precision, score.recall) == (1.0, 4 / 5)


def test_key_stability_counts_claims_whose_group_key_is_unchanged():
    fixture, first, names = _structure(RIGHT)
    renamed = {**RIGHT, "f2": ("Orchard", "notes-home", False), "f4": ("Orchard", "notes-home", True)}
    _, second, _ = _structure(renamed)
    assert key_stability(fixture, first, first, names) == (5, 5)
    assert key_stability(fixture, first, second, names) == (3, 5)


def test_the_message_shows_related_subjects_only_when_they_have_keys():
    (batch,) = make_batches(_facts({0: 1}))
    pod = Entity(uuid4(), "Orca pod")
    bare = Entity(uuid4(), "Orca whale")
    catalog = {pod.id: {"size": CatalogEntry(pod.id, "size", "How many travel together")}}
    message = render(batch, catalog, {ORCA.id: ORCA, pod.id: pod, bare.id: bare})
    assert "Orca pod\n  size: How many travel together" in message
    assert "Orca whale" not in message
    assert "Orca\n  (no keys yet)" in message


async def test_the_suite_store_offers_name_variants_with_keys():
    from hindsight_ext_cortana.structuring.suite import MemoryStore

    full = Entity(uuid4(), "Priya Natarajan")
    store = MemoryStore("b", {"priya natarajan": full, "orca": ORCA})
    short = Entity(uuid4(), "Priya")
    assert await store.related_subjects({short.id: short}) == {}
    store.catalog[full.id] = {"lesson-schedule": CatalogEntry(full.id, "lesson-schedule", "d")}
    assert await store.related_subjects({short.id: short}) == {full.id: full}


class _ScriptedModel:
    """Answers each call with one claim per fact on (first entity, 'size'), recording the messages."""

    name = "scripted/model"

    def __init__(self):
        self.messages: list[str] = []

    async def answer(self, system: str, user: str):
        self.messages.append(user)
        ids = [line.split(" ")[0] for line in user.splitlines() if line.startswith("F") and "(chunk" in line]
        return {
            "facts": [
                {"fact": i, "claims": [{"subject": "Orca", "attribute": "size", "value": "11", "provisional": False}]}
                for i in ids
            ]
        }


async def test_later_calls_of_a_retain_see_the_subjects_earlier_calls_keyed():
    from hindsight_ext_cortana.structuring.runner import structure
    from hindsight_ext_cortana.structuring.suite import MemoryStore

    calf = Entity(uuid4(), "Calf")
    first = _facts({0: MAX_FACTS_PER_CALL})
    later_chunk = Chunk(1, "text")
    later = [FactInput(uuid4(), "The calf follows.", (calf,), later_chunk, 0, SOURCE)]
    model = _ScriptedModel()
    store = MemoryStore("b", {"orca": ORCA, "calf": calf})

    report = await structure(first + later, store, model)

    assert len(model.messages) == 2
    assert "Orca\n  size:" in model.messages[1], "the second call is shown the key the first call made"
    assert report.claims == MAX_FACTS_PER_CALL + 1 and not report.pending
    assert {claim.state for claim in store.claims} == {"current"}
