"""The supersession rules on fixture claims, with no database and no model (specification 6.1, 11.1).

Every case builds a key's claims in code and checks ``evaluate_key`` and ``evaluate_facts``. The
claims are synthetic.
"""

import itertools
import uuid
from datetime import UTC, datetime, timedelta

from hindsight_ext_cortana.rules import (
    Decision,
    FactClaim,
    RuleClaim,
    evaluate_facts,
    evaluate_key,
    is_rule_retirement,
    retirement_reason,
    same_value,
)

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RANK = {"session": 0, "document": 1, "correction": 2, "decision": 3}


def claim(
    name: str,
    value: str,
    minutes: float,
    *,
    kind: str = "session",
    provisional: bool = False,
    document: str | None = None,
    fact: uuid.UUID | None = None,
    ordinal: int = 0,
    created: float = 0,
) -> RuleClaim:
    return RuleClaim(
        id=uuid.uuid5(uuid.NAMESPACE_OID, name),
        fact_id=fact or uuid.uuid5(uuid.NAMESPACE_URL, name),
        value=value,
        provisional=provisional,
        kind=kind,  # type: ignore[arg-type]
        stated_at=T0 + timedelta(minutes=minutes),
        document_id=document if document is not None else (f"conversation:{kind}" if kind == "session" else None),
        fact_ordinal=ordinal,
        source_rank=RANK[kind],
        created_at=T0 + timedelta(seconds=created),
        subject="Kestrel",
        key="status",
    )


def states(outcome, *claims: RuleClaim) -> list[str]:
    return [outcome.decisions[c.id].state for c in claims]


def by(outcome, c: RuleClaim) -> tuple:
    d = outcome.decisions[c.id]
    return (d.state, d.superseded_by, d.rule)


# S1 -----------------------------------------------------------------------------------------------


def test_the_latest_claim_is_current_and_each_earlier_one_superseded_by_its_successor():
    a, b, c = claim("a", "green", 1), claim("b", "amber", 2), claim("c", "red", 3)
    outcome = evaluate_key([c, a, b])
    assert outcome.current == c.id
    assert by(outcome, a) == ("superseded", b.id, "S1")
    assert by(outcome, b) == ("superseded", c.id, "S1")
    assert by(outcome, c) == ("current", None, None)
    assert outcome.conflict == () and outcome.stale_documents == ()


def test_the_calendar_day_a_fact_is_about_never_decides():
    """Criterion 3 at the rule level: the inputs carry the statement time only. A claim stated later
    about an earlier day still wins, because no field for the day a fact is about exists here."""
    stated_first_about_next_week = claim("first", "launch on the 20th", 1)
    stated_later_about_yesterday = claim("later", "launched on the 6th", 5)
    outcome = evaluate_key([stated_first_about_next_week, stated_later_about_yesterday])
    assert outcome.current == stated_later_about_yesterday.id


# S2 -----------------------------------------------------------------------------------------------


def test_a_provisional_claim_and_its_resolution_leave_one_current_claim():
    proposal = claim("proposal", "fork the engine", 1, provisional=True)
    resolution = claim("resolution", "fork the engine to our org", 2)
    outcome = evaluate_key([proposal, resolution])
    assert outcome.current == resolution.id
    assert by(outcome, proposal) == ("superseded", resolution.id, "S2")


def test_a_provisional_sequence_chains_to_the_resolution():
    p1 = claim("p1", "maybe green", 1, provisional=True)
    p2 = claim("p2", "maybe blue", 2, provisional=True)
    r = claim("r", "blue", 3)
    outcome = evaluate_key([p2, r, p1])
    assert by(outcome, p1) == ("superseded", p2.id, "S2")
    assert by(outcome, p2) == ("superseded", r.id, "S2")
    assert outcome.current == r.id and outcome.later_provisional == ()


def test_a_provisional_claim_never_supersedes_and_a_later_one_does_not_displace_the_current_claim():
    current = claim("current", "green", 1)
    later = claim("later", "maybe red", 2, provisional=True)
    outcome = evaluate_key([current, later])
    assert outcome.current == current.id
    assert by(outcome, current) == ("current", None, None)
    assert by(outcome, later) == ("provisional", None, None)
    assert outcome.later_provisional == (later.id,)


def test_an_earlier_provisional_claim_is_superseded_by_the_next_claim_even_when_that_one_is_provisional():
    early = claim("early", "maybe amber", 1, provisional=True)
    settled = claim("settled", "green", 2)
    later = claim("later", "maybe red", 3, provisional=True)
    outcome = evaluate_key([later, early, settled])
    assert by(outcome, early) == ("superseded", settled.id, "S2")
    assert states(outcome, settled, later) == ["current", "provisional"]


def test_a_key_with_only_provisional_claims_keeps_the_newest_valid_and_has_no_current_claim():
    p1 = claim("p1", "maybe green", 1, provisional=True)
    p2 = claim("p2", "maybe red", 2, provisional=True)
    outcome = evaluate_key([p1, p2])
    assert outcome.current is None
    assert by(outcome, p1) == ("superseded", p2.id, "S2")
    assert outcome.later_provisional == (p2.id,)


# S3 -----------------------------------------------------------------------------------------------


def test_a_restatement_supersedes_and_is_recorded_as_s3():
    a = claim("a", "Green", 1)
    b = claim("b", " green. ", 2)
    outcome = evaluate_key([a, b])
    assert by(outcome, a) == ("superseded", b.id, "S3")
    assert same_value("Green", "green.") and not same_value("green", "greenish")


# S4, S8 -------------------------------------------------------------------------------------------


def _fact_claims(outcome, *claims: RuleClaim) -> list[FactClaim]:
    return [FactClaim(c.id, c.fact_id, c.subject, c.key, outcome.decisions[c.id]) for c in claims]


def test_a_fact_is_retired_only_when_every_claim_it_carries_is_superseded():
    shared = uuid.uuid4()
    status_old = claim("status-old", "green", 1, fact=shared)
    status_new = claim("status-new", "red", 2)
    status = evaluate_key([status_old, status_new])

    owner = RuleClaim(
        id=uuid.uuid4(),
        fact_id=shared,
        value="Priya",
        provisional=False,
        kind="session",
        stated_at=T0,
        subject="Kestrel",
        key="owner",
    )
    owner_outcome = evaluate_key([owner])
    fact_of = {c.id: c.fact_id for c in (status_old, status_new, owner)}

    claims = _fact_claims(status, status_old, status_new) + _fact_claims(owner_outcome, owner)
    outcomes = evaluate_facts(claims, fact_of)
    assert outcomes[shared].retired is False, "the owner claim is still current"
    assert outcomes[status_new.fact_id].retired is False

    later_owner = claim("owner-new", "Lee", 3)
    owner_outcome = evaluate_key([owner, later_owner])
    fact_of[later_owner.id] = later_owner.fact_id
    claims = _fact_claims(status, status_old) + _fact_claims(owner_outcome, owner)
    outcome = evaluate_facts(claims, fact_of)[shared]
    assert outcome.retired is True
    assert outcome.reason == (
        f"superseded by {later_owner.fact_id} on Kestrel/owner, rule S1; "
        f"superseded by {status_new.fact_id} on Kestrel/status, rule S1"
    )


def test_the_reason_has_the_fixed_form_and_is_recognised():
    fact = uuid.uuid4()
    reason = retirement_reason(fact, "Kestrel", "status", "S2")
    assert reason == f"superseded by {fact} on Kestrel/status, rule S2"
    assert is_rule_retirement(reason)
    assert not is_rule_retirement("operator: wrong extraction") and not is_rule_retirement(None)


def test_a_fact_with_no_claims_is_never_retired():
    assert evaluate_facts([], {}) == {}


# S5 -----------------------------------------------------------------------------------------------


def test_when_the_superseding_claim_disappears_the_superseded_claim_is_current_again():
    a, b, c = claim("a", "green", 1), claim("b", "amber", 2), claim("c", "red", 3)
    assert evaluate_key([a, b, c]).current == c.id
    without_c = evaluate_key([a, b])
    assert without_c.current == b.id and by(without_c, a) == ("superseded", b.id, "S1")
    without_b = evaluate_key([a, c])
    assert by(without_b, a) == ("superseded", c.id, "S1"), "the chain closes over the vanished claim"


# S6 -----------------------------------------------------------------------------------------------


def test_at_equal_time_a_decision_record_outranks_an_extracted_fact():
    extracted = claim("extracted", "fork", 1, kind="session", ordinal=9)
    decided = claim("decided", "fork to our org", 1, kind="decision")
    outcome = evaluate_key([decided, extracted])
    assert outcome.current == decided.id
    assert by(outcome, extracted) == ("superseded", decided.id, "S1")


def test_at_equal_time_a_correction_outranks_a_session_fact():
    session = claim("session", "Tuesday", 1)
    correction = claim("correction", "Wednesday", 1, kind="correction", document="corrections/day.md")
    assert evaluate_key([correction, session]).current == correction.id


def test_at_equal_time_and_rank_the_engines_creation_order_decides():
    first = claim("first", "green", 1, created=1, document="conversation:x")
    second = claim("second", "red", 1, created=2, document="conversation:y")
    assert evaluate_key([second, first]).current == second.id


# S9 -----------------------------------------------------------------------------------------------


def test_claims_on_a_key_pending_alignment_are_unaligned_and_supersede_nothing():
    a, b = claim("a", "green", 1), claim("b", "red", 2)
    outcome = evaluate_key([a, b], aligned=False)
    assert states(outcome, a, b) == ["unaligned", "unaligned"]
    assert outcome.current is None
    facts = evaluate_facts(_fact_claims(outcome, a, b), {})
    assert not any(f.retired for f in facts.values())


# S10 ----------------------------------------------------------------------------------------------


def test_the_rules_are_a_pure_function_of_the_claims_whatever_their_input_order():
    claims = [
        claim("p", "maybe amber", 0.5, provisional=True),
        claim("d1", "green", 1, kind="document", document="docs/kestrel.md"),
        claim("s1", "red", 2),
        claim("dec", "blue", 3, kind="decision"),
        claim("s2", "maybe blue", 4, provisional=True),
    ]
    expected = evaluate_key(claims)
    for permutation in itertools.permutations(claims):
        assert evaluate_key(permutation) == expected


# S11 ----------------------------------------------------------------------------------------------


def test_a_later_session_claim_that_disagrees_with_a_document_puts_the_key_in_conflict():
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    said = claim("said", "red", 2)
    outcome = evaluate_key([doc, said])
    assert states(outcome, doc, said) == ["conflict", "conflict"]
    assert outcome.current is None and outcome.conflict == (doc.id, said.id)
    assert not any(f.retired for f in evaluate_facts(_fact_claims(outcome, doc, said), {}).values())


def test_a_session_claim_that_agrees_with_the_document_is_a_restatement():
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    said = claim("said", "green", 2)
    outcome = evaluate_key([doc, said])
    assert by(outcome, doc) == ("superseded", said.id, "S3") and outcome.conflict == ()


def test_a_later_document_claim_supersedes_a_session_claim():
    said = claim("said", "red", 1)
    doc = claim("doc", "green", 2, kind="document", document="docs/kestrel.md")
    outcome = evaluate_key([said, doc])
    assert by(outcome, said) == ("superseded", doc.id, "S1") and outcome.current == doc.id


def test_two_documents_that_disagree_conflict_whatever_their_order():
    one = claim("one", "green", 1, kind="document", document="docs/a.md")
    two = claim("two", "red", 2, kind="document", document="docs/b.md")
    assert evaluate_key([one, two]).conflict == (one.id, two.id)
    reversed_times = [
        claim("one", "green", 2, kind="document", document="docs/a.md"),
        claim("two", "red", 1, kind="document", document="docs/b.md"),
    ]
    assert len(evaluate_key(reversed_times).conflict) == 2


def test_a_later_dated_entry_of_the_same_document_supersedes_an_earlier_one():
    early = claim("early", "green", 1, kind="document", document="docs/kestrel.md")
    late = claim("late", "red", 2, kind="document", document="docs/kestrel.md")
    assert by(evaluate_key([early, late]), early) == ("superseded", late.id, "S1")


def test_criterion_16_a_conflict_closes_when_the_edited_document_agrees():
    """The document's old claim is gone after the edited save (its chunk was re-extracted), and the
    new document claim agrees with the session: the session claim is superseded as a restatement."""
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    said = claim("said", "red", 2)
    assert evaluate_key([doc, said]).conflict
    edited = claim("edited", "red", 3, kind="document", document="docs/kestrel.md")
    outcome = evaluate_key([said, edited])
    assert outcome.conflict == () and outcome.current == edited.id
    assert by(outcome, said) == ("superseded", edited.id, "S3")
    # Also when the old document claim survives (an unchanged chunk): the same document's later
    # entry supersedes it and the session claim is restated.
    outcome = evaluate_key([doc, said, edited])
    assert by(outcome, doc) == ("superseded", edited.id, "S1")
    assert by(outcome, said) == ("superseded", edited.id, "S3")
    assert outcome.conflict == ()


def test_a_conflict_closes_on_a_decision_record_and_leaves_the_stale_document_marker():
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    said = claim("said", "red", 2)
    decided = claim("decided", "red", 3, kind="decision")
    outcome = evaluate_key([doc, said, decided])
    assert outcome.current == decided.id and outcome.conflict == ()
    assert by(outcome, doc) == ("superseded", decided.id, "S1")
    assert by(outcome, said) == ("superseded", decided.id, "S3")
    assert outcome.stale_documents == ("docs/kestrel.md",)


def test_criterion_17_the_stale_document_marker_stays_until_the_document_agrees():
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    decided = claim("decided", "red", 2, kind="decision")
    outcome = evaluate_key([doc, decided])
    assert by(outcome, doc) == ("superseded", decided.id, "S1")
    assert outcome.stale_documents == ("docs/kestrel.md",)

    later_session = claim("later-session", "amber", 3)
    outcome = evaluate_key([doc, decided, later_session])
    assert outcome.current == later_session.id
    assert outcome.stale_documents == ("docs/kestrel.md",), "the document still says green"

    saved_agreeing = claim("saved", "red", 3, kind="document", document="docs/kestrel.md")
    outcome = evaluate_key([doc, decided, saved_agreeing])
    assert by(outcome, decided) == ("superseded", saved_agreeing.id, "S3")
    assert outcome.stale_documents == ()


def test_a_correction_supersedes_a_document_claim_with_the_marker():
    doc = claim("doc", "Tuesday", 1, kind="document", document="docs/schedule.md")
    fix = claim("fix", "Wednesday", 2, kind="correction", document="corrections/schedule.md")
    outcome = evaluate_key([doc, fix])
    assert outcome.current == fix.id and outcome.stale_documents == ("docs/schedule.md",)


def test_a_document_saved_after_a_decision_that_disagrees_is_raised_as_a_conflict():
    decided = claim("decided", "red", 1, kind="decision")
    doc = claim("doc", "green", 2, kind="document", document="docs/kestrel.md")
    outcome = evaluate_key([decided, doc])
    assert outcome.conflict == (decided.id, doc.id)


def test_session_statements_against_each_other_order_by_time_inside_a_conflict():
    doc = claim("doc", "green", 1, kind="document", document="docs/kestrel.md")
    s1 = claim("s1", "red", 2)
    s2 = claim("s2", "amber", 3)
    outcome = evaluate_key([doc, s1, s2])
    assert by(outcome, s1) == ("superseded", s2.id, "S1")
    assert outcome.conflict == (doc.id, s2.id)


def test_a_session_claim_supersedes_a_decision_record():
    decided = claim("decided", "red", 1, kind="decision")
    said = claim("said", "amber", 2)
    assert by(evaluate_key([decided, said]), decided) == ("superseded", said.id, "S1")


def test_decision_values_are_frozen_dataclasses():
    assert Decision("current") == Decision("current")
