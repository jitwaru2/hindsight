"""The migration's structuring pass (specification 13.4 step 4): it structures every fact no call has
answered, document by document in statement-time order with the priority documents first; it resumes
where it stopped; a usage limit stops it at once and a run of failed calls stops it after retries;
and documents in flight at once do not create divergent keys on one subject.

Facts are seeded through the engine's own retain with the hook's structuring call failing, which
leaves them exactly as the migration finds legacy facts: stored, with no claims, and not answered.
The engine runs with its ``mock`` provider; structuring calls are answered from the call's message.
"""

import asyncio
import uuid

import pytest
from hindsight_api import RequestContext
from hindsight_ext_cortana import migrate
from hindsight_ext_cortana.reconcile import unstructured_facts
from test_retain_hook import _plugin_save
from test_structuring_engine import FACT, _answer, conn, structuring_calls  # noqa: F401 (fixtures)

EARLY = "2026-09-01T18:00:00Z"
LATE = "2026-09-20T18:00:00Z"


def _turns(name: str, thing: str) -> list[dict]:
    return [
        {"role": "user", "content": f"{name} said the {thing} should be painted green.", "timestamp": EARLY},
        {"role": "assistant", "content": f"Noted, {name}: the {thing} is to be green.", "timestamp": EARLY},
    ]


async def _seed(client, structuring_calls, bank: str, sessions: list[tuple[str, str, str, str]]) -> None:
    """Save sessions with the hook's structuring call failing, then let calls succeed again."""
    structuring_calls["fail"] = RuntimeError("provider unavailable while seeding")
    for session, start, name, thing in sessions:
        body = _plugin_save(bank, session, _turns(name, thing), start=start, append=False)
        response = await client.post(f"/v1/default/banks/{bank}/memories", json=body)
        assert response.status_code == 200, response.text
    structuring_calls["fail"] = None
    structuring_calls["calls"].clear()


def _pass(memory, bank: str, **kwargs) -> migrate.StructurePass:
    kwargs.setdefault("backoff_seconds", 0)
    return migrate.StructurePass(memory, bank, request_context=RequestContext(internal=True), **kwargs)


async def test_the_pass_structures_in_statement_time_order_and_resumes_with_nothing_left(
    structuring_calls, cortana_client, cortana_memory, conn
):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    later, earlier = str(uuid.uuid4()), str(uuid.uuid4())
    # Saved latest first, so creation order and statement order disagree.
    await _seed(
        cortana_client, structuring_calls, bank, [(later, LATE, "Jordan", "gate"), (earlier, EARLY, "Alex", "fence")]
    )
    todo = await unstructured_facts(conn, bank)
    assert todo

    report = await _pass(cortana_memory, bank, concurrency=1).run()

    assert report.stopped is None
    assert report.phases_done == ["priority", "rest"]
    assert report.facts_answered == len(todo) == report.planned_facts
    assert report.claims >= report.facts_with_claims > 0
    first, last = structuring_calls["calls"][0], structuring_calls["calls"][-1]
    assert "Alex" in first["messages"][1]["content"] and "Jordan" in last["messages"][1]["content"]
    assert await unstructured_facts(conn, bank) == []
    rows = await conn.fetch(
        "SELECT event, run_id FROM public.ledger WHERE bank_id = $1 AND run_id = $2 ORDER BY id", bank, report.run_id
    )
    events = [row["event"] for row in rows]
    assert events[0] == "structure-pass-started" and events[-1] == "structure-pass-finished"
    assert events.count("structured") == report.calls

    calls_before = len(structuring_calls["calls"])
    again = await _pass(cortana_memory, bank).run()
    assert again.planned_facts == 0 and again.calls == 0
    assert len(structuring_calls["calls"]) == calls_before


async def test_priority_documents_go_first_and_whole(structuring_calls, cortana_client, cortana_memory, conn):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    await _seed(
        cortana_client,
        structuring_calls,
        bank,
        [(str(uuid.uuid4()), EARLY, "Alex", "fence"), (str(uuid.uuid4()), LATE, "Jordan", "gate")],
    )
    async with (await cortana_memory._get_pool()).acquire() as c:
        plan = await migrate.make_plan(c, bank, migrate.compile_patterns(["JORDAN"]))
    assert len(plan.priority) == 1 and len(plan.rest) == 1
    jordan = plan.priority[0]
    rows = await conn.fetch(
        "SELECT id FROM public.memory_units WHERE bank_id = $1 AND document_id = $2", bank, jordan.document_id
    )
    unstructured = set(await unstructured_facts(conn, bank))
    assert set(jordan.fact_ids) == {row["id"] for row in rows} & unstructured

    report = await _pass(cortana_memory, bank, concurrency=1).run(patterns=migrate.compile_patterns(["jordan"]))
    assert report.stopped is None
    assert "Jordan" in structuring_calls["calls"][0]["messages"][1]["content"]
    assert "Alex" in structuring_calls["calls"][-1]["messages"][1]["content"]

    priority_only = await _pass(cortana_memory, bank).run(phases=("priority",))
    assert priority_only.planned_facts == 0


async def test_a_usage_limit_stops_the_pass_at_once_and_a_rerun_continues(
    structuring_calls, cortana_client, cortana_memory, conn
):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    await _seed(
        cortana_client,
        structuring_calls,
        bank,
        [(str(uuid.uuid4()), EARLY, "Alex", "fence"), (str(uuid.uuid4()), LATE, "Jordan", "gate")],
    )
    structuring_calls["fail"] = RuntimeError("Claude Code reported an error: You've hit your weekly limit · resets 3am")

    stopped = await _pass(cortana_memory, bank, concurrency=1, retries=3).run()

    assert stopped.stopped and "usage limit" in stopped.stopped
    assert len(structuring_calls["calls"]) == 1  # no retries, no further documents
    assert stopped.facts_answered == 0 and stopped.facts_pending > 0
    events = await conn.fetch(
        "SELECT event FROM public.ledger WHERE bank_id = $1 AND run_id = $2", bank, stopped.run_id
    )
    assert {"structuring-pending", "structure-pass-stopped"} <= {row["event"] for row in events}

    structuring_calls["fail"] = None
    resumed = await _pass(cortana_memory, bank).run()
    assert resumed.stopped is None and resumed.planned_facts == stopped.planned_facts
    assert await unstructured_facts(conn, bank) == []


async def test_failed_calls_are_retried_then_left_pending_and_a_run_of_failures_stops_the_pass(
    structuring_calls, cortana_client, cortana_memory, conn
):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    await _seed(
        cortana_client,
        structuring_calls,
        bank,
        [(str(uuid.uuid4()), EARLY, "Alex", "fence"), (str(uuid.uuid4()), LATE, "Jordan", "gate")],
    )
    structuring_calls["fail"] = RuntimeError("Claude Code call failed after all retries")

    report = await _pass(cortana_memory, bank, concurrency=1, retries=2, max_consecutive_failures=2).run()

    assert report.failed_calls == 2 and report.retries == 4
    assert len(structuring_calls["calls"]) == 6
    assert report.stopped and "2 consecutive failed calls" in report.stopped
    status = await unstructured_facts(conn, bank)
    assert len(status) == report.planned_facts  # pending facts stay selectable for the next run


def test_usage_limit_recognition():
    assert migrate.is_usage_limit(RuntimeError("Claude Code reported an error: You've hit your weekly limit · resets"))
    assert migrate.is_usage_limit(RuntimeError("API error 429: rate_limit_error"))
    assert not migrate.is_usage_limit(RuntimeError("Claude Code call failed after all retries"))
    assert not migrate.is_usage_limit(ValueError("the answer gave these facts no valid claim"))


class _RacingModel:
    """Answers structuring calls like the mock, but holds every answer until two calls are out, and
    names the attribute after the call's own session, so two documents answered from the same empty
    catalog would create two different keys on one subject (Alex, an entity of both documents)."""

    name = "test/racing"

    def __init__(self) -> None:
        self.out = 0
        self.both_out = asyncio.Event()

    async def answer(self, system: str, user: str) -> dict:
        self.out += 1
        if self.out >= 2:
            self.both_out.set()
        await asyncio.wait_for(self.both_out.wait(), timeout=30)
        answer = _answer(user)
        attribute = "Fence Color" if "fence" in user else "Paint Shade"
        for fact in answer["facts"]:
            for claim in fact["claims"]:
                claim["subject"] = "Alex"
                claim["attribute"] = attribute
        return answer


async def test_documents_in_flight_at_once_do_not_fork_keys(structuring_calls, cortana_client, cortana_memory, conn):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    await _seed(
        cortana_client,
        structuring_calls,
        bank,
        [(str(uuid.uuid4()), EARLY, "Alex", "fence"), (str(uuid.uuid4()), LATE, "Alex", "gate")],
    )

    report = await _pass(cortana_memory, bank, concurrency=2, model=_RacingModel()).run()

    assert report.stopped is None and report.calls == 2
    keys = await conn.fetch(
        "SELECT attribute_key, alignment FROM public.attributes WHERE bank_id = $1 ORDER BY attribute_key", bank
    )
    alignment = {row["attribute_key"]: row["alignment"] for row in keys}
    # Both calls saw Alex with no keys. Whichever wrote second found the other's key on the subject
    # and stored its own new key unaligned, for the alignment pass; without the lock and the re-read
    # of the catalog, both would be aligned and never compared.
    assert sorted(alignment.values()) == ["aligned", "pending"]


@pytest.mark.parametrize("value", ["2026-09-01T18:00:00Z", "2026-09-01T18:00:00+00:00", "2026-09-01T18:00:00"])
def test_event_dates_from_retain_parameters(value):
    parsed = migrate._parse_time(value)
    assert parsed is not None and parsed.tzinfo is not None and parsed.hour == 18


async def test_a_second_pass_on_the_same_bank_refuses_to_start(structuring_calls, cortana_client, cortana_memory):
    bank = f"cortana-migrate-{uuid.uuid4().hex[:8]}"
    await _seed(cortana_client, structuring_calls, bank, [(str(uuid.uuid4()), EARLY, "Alex", "fence")])
    async with (await cortana_memory._get_pool()).acquire() as holder:
        await holder.execute("SELECT pg_advisory_lock(hashtext($1))", migrate.PASS_LOCK + bank)
        with pytest.raises(migrate.PassRunning):
            await _pass(cortana_memory, bank).run()
        await holder.execute("SELECT pg_advisory_unlock(hashtext($1))", migrate.PASS_LOCK + bank)
    assert structuring_calls["calls"] == []
    assert (await _pass(cortana_memory, bank).run()).stopped is None
