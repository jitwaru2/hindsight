"""The gate's deterministic parts (specification 11): the strict scorer, the questions generated from
the ledger, the judge's verdict handling, the acceptance run's HTTP wiring against a fake server, the
run's table and summary, and ``status.gate``. Everything here is invented: no question, key or
answer from the real acceptance set is in the fork.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from hindsight_ext_cortana import status
from hindsight_ext_cortana.gate import acceptance, judge, questions, runner, strict

KESTREL = strict.AnswerKey(
    id="Q01",
    topic="kestrel",
    cur="moved to the north shed|2026-09-20",
    T="2026-09-20",
    pos="south shed|garage",
    c2="ladder",
)
FINCH = strict.AnswerKey(id="Q02", topic="finch", cur="weekly", o_re=r"\bmonthly\b")


def items(*texts: str, date: str | None = None) -> list[strict.Item]:
    return [strict.Item(text=text, event_date=date) for text in texts]


# The strict scorer.


def test_labels_follow_the_earlier_scorer():
    assert strict.label(KESTREL, "Lee painted the fence", None) == "-"
    assert strict.label(KESTREL, "The kestrel box moved to the north shed", None) == "C"
    assert strict.label(KESTREL, "The kestrel box is in the south shed (2026-09-01)", None) == "O"
    # pos with a horizon at or after T is not old, nor is one that says it was superseded.
    assert strict.label(KESTREL, "The kestrel box is in the south shed", "2026-09-25") == "N"
    assert strict.label(KESTREL, "Superseded: the kestrel box was in the south shed on August 3", None) == "N"
    assert strict.label(FINCH, "Finch feeding is monthly", None) == "O"
    assert strict.label(FINCH, "Finch feeding is twice a day", None) == "N"


def test_horizon_reads_iso_and_month_dates():
    assert strict.horizon("first on 2026-08-02, then September 14", None) == "2026-09-14"
    assert strict.horizon("no dates", "2026-07-01T10:00:00Z") == "2026-07-01"
    assert strict.horizon("no dates", None) is None


def test_strict_recall_rejects_a_first_result_that_also_states_the_old_position():
    loose_only = strict.score_recall(
        KESTREL, items("The kestrel box moved to the north shed from the south shed", "The kestrel ladder is red")
    )
    assert loose_only.loose and not loose_only.strict and loose_only.first_states_old_too
    assert loose_only.c2_rank == 2

    clean = strict.score_recall(KESTREL, items("The kestrel box moved to the north shed", "Lee likes tea"))
    assert clean.loose and clean.strict and clean.labels == "C-" and clean.first_c == 1

    buried = strict.score_recall(
        FINCH, items("Finch feeding is monthly", "Finch feeding is weekly now", "Finch seed is cheap")
    )
    assert not buried.loose and not buried.strict
    assert buried.first == "O" and buried.first_c == 2 and buried.old_above_first_c == 1

    assert strict.score_recall(FINCH, []).first is None


def test_the_read_passes_strictly_only_with_no_old_position_standing():
    current = strict.claim_item(
        "Kestrel box", "location", "north shed", "It moved.", datetime(2026, 9, 20, tzinfo=UTC), "a"
    )
    assert "(stated 2026-09-20)" in current.text and current.event_date == "2026-09-20"
    old = strict.claim_item("Kestrel box", "spare-location", "south shed", None, "2026-09-02T12:00:00Z", "b")
    other = strict.claim_item("Kestrel box", "color", "green", None, None, "c")

    both = strict.score_read(KESTREL, [current, old, other])
    assert both.spec and not both.strict and both.standing == 3
    assert [i.ref for i in both.current] == ["a"] and [i.ref for i in both.old] == ["b"]
    assert strict.score_read(KESTREL, [current, other]).strict
    assert not strict.score_read(KESTREL, [other]).spec


def test_keys_load_and_become_rubrics():
    keys = strict.load_keys({"_doc": "labels", "Q02": {"topic": "finch", "cur": "weekly", "o_re": "monthly"}})
    assert list(keys) == ["Q02"] and keys["Q02"].old == "monthly"
    rubric = strict.key_rubric(KESTREL)
    assert rubric.current == KESTREL.cur and rubric.second == "ladder" and "before 2026-09-20" in rubric.old
    assert strict.key_rubric(FINCH).old == r"\bmonthly\b"


# Questions generated from the ledger.

ALEX, BOX = uuid.uuid4(), uuid.uuid4()
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def claim(
    subject: uuid.UUID, attribute: str, value: str, state: str, days: int, name: str = "Alex"
) -> questions.KeyClaim:
    return questions.KeyClaim(
        uuid.uuid4(), uuid.uuid4(), subject, name, attribute, value, state, T0 + timedelta(days=days)
    )


def superseded(old: questions.KeyClaim, new: questions.KeyClaim, rule: str = "S1") -> questions.Supersession:
    return questions.Supersession(old.subject_id, old.attribute, old.claim_id, new.claim_id, rule, T0)


def test_one_question_per_superseded_key_with_the_current_claim_expected():
    green = claim(ALEX, "fence-color", "green", "superseded", 0)
    blue = claim(ALEX, "fence-color", "blue", "superseded", 1)
    red = claim(ALEX, "fence-color", "red", "current", 2)
    restated = claim(ALEX, "fence-color", "Red.", "superseded", 3)
    north = claim(BOX, "location", "north shed", "current", 1, "Kestrel box")
    north_again = claim(BOX, "location", "North shed", "superseded", 0, "Kestrel box")
    tea_a = claim(ALEX, "drink", "tea", "conflict", 1)
    tea_b = claim(ALEX, "drink", "coffee", "conflict", 2)
    tea_old = claim(ALEX, "drink", "water", "superseded", 0)
    late = claim(ALEX, "shoes", "boots", "provisional", 2)
    late_old = claim(ALEX, "shoes", "sandals", "superseded", 0)

    made = questions.generate(
        [
            superseded(green, blue),
            superseded(blue, red),
            superseded(restated, red, "S3"),
            superseded(north_again, north, "S3"),
            superseded(tea_old, tea_a),
            superseded(late_old, late),
        ],
        {
            (ALEX, "fence-color"): [green, blue, red, restated],
            (BOX, "location"): [north, north_again],
            (ALEX, "drink"): [tea_a, tea_b, tea_old],
            (ALEX, "shoes"): [late, late_old],
        },
    )

    assert made.supersessions == 6
    assert made.skipped == {"restatements only": 1, "key in conflict": 1, "no single current claim": 1}
    (only,) = made.questions
    assert only.question == "What is the current fence color of Alex?"
    assert only.expected is red
    assert [c.value for c in only.superseded] == ["blue", "green"]  # newest first; the restatement is not old
    assert only.rules == ["S1", "S3"] and only.id == f"G-{red.claim_id.hex[:8]}"


def test_the_reflect_sample_is_reproducible():
    made = [
        questions.GeneratedQuestion(f"G-{n}", ALEX, "Alex", "a", "q", claim(ALEX, "a", "v", "current", 0), [], [])
        for n in range(30)
    ]
    first = questions.sample(made, 5, seed="bank:2026-10-08")
    assert len(first) == 5 and first == questions.sample(made, 5, seed="bank:2026-10-08")
    assert questions.sample(made, None, seed="x") == made and questions.sample(made, 99, seed="x") == made


# The judge.


class FakeLLM:
    provider, model = "fake", "judge"

    def __init__(self, content):
        self.content, self.calls = content, []

    async def call(self, messages, response_format=None, **kwargs):
        self.calls.append(messages)

        class Result:
            content = self.content

        return Result()


async def test_the_judge_recomputes_correct_from_its_parts():
    rubric = strict.Rubric(current="north shed", old="south shed", second="ladder", patterns=False)
    lenient = FakeLLM(
        {"states_current": True, "old_as_current": True, "second_point": True, "correct": True, "reason": "x"}
    )
    verdict = await judge.Judge(lenient)("Where is the kestrel box?", "In the north or south shed.", rubric)
    assert not verdict.correct
    user = lenient.calls[0][1]["content"]
    assert "CURRENT: north shed" in user and "SECOND: ladder" in user and "as values" in user

    as_text = FakeLLM(
        json.dumps({"states_current": True, "old_as_current": False, "second_point": False, "correct": True})
    )
    assert not (await judge.Judge(as_text)("q", "a", rubric)).correct
    no_second = strict.Rubric(current="north shed")
    assert (await judge.Judge(as_text)("q", "a", no_second)).correct


# The acceptance run's wiring, against a fake server.

BANK = "bank-test"
FACT_NEW, FACT_OLD, FACT_OBS = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def fake_server(subject_id: str, claims_current: list[dict]) -> httpx.MockTransport:
    def view(value: str, state: str, fact: str, stated: str = "2026-09-20T12:00:00Z") -> dict:
        return {
            "claim_id": str(uuid.uuid4()),
            "fact_id": fact,
            "subject_id": subject_id,
            "subject": "Kestrel box",
            "attribute": "location",
            "value": value,
            "state": state,
            "stated_at": stated,
            "text": f"The kestrel box is in the {value}.",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/ext/cortana/subjects":
            return httpx.Response(200, json={"items": [{"id": subject_id, "name": "Kestrel box"}]})
        if path == "/ext/cortana/current":
            keys = [
                {"attribute": "location", "status": "current", "current": claims_current[0], "conflict": []},
                {"attribute": "spare", "status": "conflict", "current": None, "conflict": claims_current[1:]},
            ]
            if params.get("attribute"):
                keys = [k for k in keys if k["attribute"] == params["attribute"]]
            return httpx.Response(200, json={"keys": keys})
        if path.startswith("/ext/cortana/facts/"):
            fact = path.rsplit("/", 1)[1]
            if fact == FACT_NEW:
                return httpx.Response(200, json={"claims": [view("north shed", "current", FACT_NEW)]})
            if fact == FACT_OLD:
                return httpx.Response(200, json={"claims": [view("south shed", "superseded", FACT_OLD)]})
            return httpx.Response(404, json={})
        if path.endswith("/memories/recall"):
            body = json.loads(request.content)
            first = {"id": FACT_OLD, "type": "world", "text": "The kestrel box is in the south shed (2026-09-01)"}
            if body.get("tags"):
                first = {"id": FACT_NEW, "type": "world", "text": "The kestrel box moved to the north shed"}
            if "current location" in body["query"]:
                first = {"id": FACT_OBS, "type": "observation", "text": "Kestrel box: north shed now"}
            return httpx.Response(200, json={"results": [first]})
        if path.endswith("/reflect"):
            return httpx.Response(200, json={"text": "It moved to the north shed on 2026-09-20; the ladder stays."})
        return httpx.Response(404, json={"path": path})

    return httpx.MockTransport(handler)


class AlwaysRight:
    name = "fake/judge"

    async def __call__(self, question, answer, rubric):
        return judge.Verdict(states_current=True, old_as_current=False, second_point=True, correct=True)


async def test_the_ten_questions_are_read_recalled_and_reflected_and_scored_separately():
    subject_id = str(uuid.uuid4())
    north = {
        "claim_id": str(uuid.uuid4()),
        "subject": "Kestrel box",
        "attribute": "location",
        "value": "north shed",
        "text": "The kestrel box moved to the north shed.",
        "stated_at": "2026-09-20T12:00:00Z",
    }
    spare = north | {"claim_id": str(uuid.uuid4()), "attribute": "spare", "value": "the garage", "text": "Spare box."}
    spare["stated_at"] = "2026-09-02T12:00:00Z"
    transport = fake_server(subject_id, [north, spare])
    asked = acceptance.load_questions(
        [
            {"id": "Q01", "query": "Where is the kestrel box now?"},
            {"id": "Q01f", "query": "Where is the kestrel box now?", "tags": ["pool:work"], "tags_match": "any"},
        ]
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        (result,) = await acceptance.ask_ten(
            acceptance.Server(client, BANK), AlwaysRight(), asked, {"Q01": KESTREL}, {"Q01": ["kestrel"]}
        )
    assert result.subjects_read == ["Kestrel box"]
    assert result.read["spec"] and not result.read["strict"]  # the garage still stands on another key
    assert result.recall["first"] == "O" and not result.recall["strict"]
    assert result.recall_filtered["strict"]
    assert result.reflect.correct and result.reflect.answer.startswith("It moved")


async def test_generated_questions_class_recalls_first_result_by_its_facts_claims():
    subject_id = uuid.uuid4()
    north = {"claim_id": str(uuid.uuid4()), "subject": "Kestrel box", "attribute": "location", "value": "north shed"}
    transport = fake_server(str(subject_id), [north])
    expected = questions.KeyClaim(
        uuid.UUID(north["claim_id"]),
        uuid.UUID(FACT_NEW),
        subject_id,
        "Kestrel box",
        "location",
        "north shed",
        "current",
        T0,
    )
    old = questions.KeyClaim(
        uuid.uuid4(), uuid.UUID(FACT_OLD), subject_id, "Kestrel box", "location", "south shed", "superseded", T0
    )
    q = questions.GeneratedQuestion(
        "G-1",
        subject_id,
        "Kestrel box",
        "location",
        "What is the current location of Kestrel box?",
        expected,
        [old],
        ["S1"],
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        server = acceptance.Server(client, BANK)
        assert await acceptance.classify_first(server, q, {"id": FACT_NEW, "type": "world", "text": ""}) == (
            "C",
            f"fact {FACT_NEW} carries the current value",
        )
        assert (await acceptance.classify_first(server, q, {"id": FACT_OLD, "type": "world", "text": ""}))[0] == "O"
        both = {"id": FACT_OBS, "type": "observation", "text": "From the south shed to the north shed"}
        assert (await acceptance.classify_first(server, q, both))[0] == "C+O"
        assert (await acceptance.classify_first(server, q, None))[0] is None
        (result,) = await acceptance.ask_generated(server, AlwaysRight(), [q], {"G-1"})
    assert result.read and result.recall_first == "C" and result.recall_strict and result.reflect.correct


# The run's verdict, table, summary and status.


def ten_row(read: bool, recall: bool, reflect: bool) -> dict:
    return {
        "id": "Q01",
        "read": {"strict": read, "spec": True, "standing": 1, "current": [], "old": []},
        "recall": {"strict": recall, "loose": True, "labels": "C", "first_text": "x"},
        "recall_filtered": None,
        "reflect": {"verdict": {"correct": reflect, "reason": "r"}, "error": None},
        "errors": [],
    }


def test_the_hard_criteria_are_the_read_and_reflect_and_recall_is_measured():
    verdict = runner.acceptance_verdict([ten_row(True, False, True)], [])
    assert verdict["hard_criteria_pass"] and verdict["hsight_11_needed"]
    assert not runner.acceptance_verdict([ten_row(False, True, True)], [])["hard_criteria_pass"]
    assert not runner.acceptance_verdict([ten_row(True, True, False)], [])["hard_criteria_pass"]
    generated = [
        {"read": True, "recall_strict": True, "reflect": None},
        {"read": False, "recall_strict": True, "reflect": None},
    ]
    assert not runner.acceptance_verdict([ten_row(True, True, True)], generated)["hard_criteria_pass"]


def test_a_run_is_written_and_status_reports_the_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_CORTANA_GATE_DIR", str(tmp_path))
    assert status.last_gate_run() is None
    suites = {name: runner.SuiteResult(name, ran=True, passed=True, note="ok") for name in runner.SUITES}
    suites["acceptance"].summary = runner.acceptance_verdict([ten_row(True, False, True)], [])
    report = runner.GateReport(
        ran_at=datetime(2026, 10, 8, 4, 0, tzinfo=UTC),
        release="abc",
        bank=BANK,
        url="http://test",
        suites=suites,
        details={"ten": [ten_row(True, False, True)], "generated": []},
    )
    assert report.passed
    table, summary = runner.write(report, runner.gate_dir())
    text = table.read_text()
    assert "| Q01 | pass" in text and "Overall: **pass**" in text
    gate = status.last_gate_run()
    assert gate is not None and gate.passed and gate.table_path == str(table) and gate.release == "abc"
    assert gate.suites["acceptance"]["counts"]["hsight_11_needed"] is True

    suites["latency"] = runner.SuiteResult("latency", ran=False, note="not selected")
    later = runner.GateReport(datetime(2026, 10, 9, 4, 0, tzinfo=UTC), "abd", BANK, "http://test", suites)
    assert not later.passed
    runner.write(later, runner.gate_dir())
    assert status.last_gate_run().release == "abd" and not status.last_gate_run().passed


def test_the_deterministic_suite_reads_pytests_summary(monkeypatch):
    class Done:
        returncode = 1
        stdout = "FAILED tests/test_x.py::test_y - boom\n==== 1 failed, 200 passed, 3 warnings in 9.1s ====\n"

    seen = {}

    def run(*args, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "postgresql://somewhere/else")
    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "claude-code")
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setattr(runner.subprocess, "run", run)
    result = runner.run_deterministic()
    assert not any(k.startswith("HINDSIGHT_") for k in seen["env"]) and "PGPASSWORD" not in seen["env"]
    assert seen["env"]["PATH"]
    assert result.ran and not result.passed
    assert result.summary["failed"] == 1 and result.summary["passed"] == 200
    assert result.summary["failures"] == ["FAILED tests/test_x.py::test_y - boom"]


@pytest.mark.parametrize("name", ["questions", "keys", "subjects"])
def test_acceptance_inputs_can_be_moved_by_environment(tmp_path, monkeypatch, name):
    variable = {"questions": "QUESTIONS", "keys": "KEYS", "subjects": "SUBJECTS"}[name]
    monkeypatch.setenv(f"HINDSIGHT_CORTANA_ACCEPTANCE_{variable}", str(tmp_path / "x.json"))
    assert runner.default_inputs()[name] == tmp_path / "x.json"
