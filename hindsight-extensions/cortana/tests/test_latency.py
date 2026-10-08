"""The latency measurement and its budget (specification 14; criterion 13), with no model calls and no
running server: the percentile and budget arithmetic, the packaged budget, the synthetic text, the
command's exit codes, the load phase against a scripted server (``httpx.MockTransport``), and an idle
phase against the engine's own app on its mock provider. All content is synthetic.
"""

import json
import uuid
from collections import Counter
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from hindsight_ext_cortana.cli import app
from hindsight_ext_cortana.gate import latency, synthetic

from test_retrieval_log import _script_reflect, _seed


def _budget(**changes) -> latency.Budget:
    base = latency.load_budget().model_dump()
    base.update(recalls=4, reflects=2, warmup_recalls=1)
    base.update(changes)
    return latency.Budget.model_validate(base)


def _phase(name, recall, reflect=(1000.0,), present=None, errors=0, quiet=None) -> latency.Phase:
    return latency.Phase(
        name=name,
        started_at=datetime.now(UTC),
        recall=latency.stats(list(recall), errors),
        reflect=latency.stats(list(reflect)),
        load_present=present,
        quiet=quiet,
    )


def test_percentiles_are_inclusive_and_stay_within_the_sample():
    s = latency.stats([float(v) for v in range(1, 21)])
    assert (s.count, s.p50_ms, s.p95_ms, s.max_ms) == (20, 10.5, 19.1, 20.0)
    single = latency.stats([7.0])
    assert single.p50_ms == single.p95_ms == 7.0
    assert latency.stats([], errors=2).p95_ms is None


def test_breaches_name_every_way_a_run_misses_the_budget():
    budget = _budget(recall={"p50_ms": 500, "p95_ms": 600}, reflect={"p50_ms": 30000, "p95_ms": 60000})
    idle = _phase("idle", [300.0] * 20)
    assert latency.breaches([idle, _phase("load", [350.0] * 20, present=1.0)], budget) == []
    assert latency.breaches([idle], budget) == [], "an idle phase alone is checked only for failed calls"

    slow = latency.breaches([idle, _phase("load", [550.0] * 18 + [900.0, 900.0], present=1.0)], budget)
    assert [b.split(" over")[0] for b in slow] == [
        "load: recall p50 550.0 ms",
        "load: recall p95 900.0 ms",
        "load: recall p95 is 3.00 times idle,",
    ]
    lapsed = latency.breaches([idle, _phase("load", [350.0] * 20, present=0.5)], budget)
    assert len(lapsed) == 1 and "not valid" in lapsed[0]
    failed = latency.breaches([idle, _phase("load", [350.0] * 20, present=1.0, errors=1)], budget)
    assert failed == ["load: 1 recall calls failed"]
    busy = latency.breaches([_phase("idle", [300.0] * 20, quiet=0.75)], budget)
    assert busy == [
        "idle: an operation was queued or running on the server at 25% of the probes; the idle baseline is not valid"
    ]


def test_the_packaged_budget_records_its_measured_reasons():
    budget = latency.load_budget()
    assert budget.version >= 1 and budget.reason and budget.measurements
    assert budget.recalls >= 30 and budget.reflects >= 10
    assert budget.recall.p50_ms <= budget.recall.p95_ms and budget.reflect.p50_ms <= budget.reflect.p95_ms
    assert 0 < budget.load.min_present_fraction <= 1 and budget.recall_p95_over_idle > 1


def test_synthetic_text_is_repeatable_distinct_and_multi_chunk():
    facts = list(synthetic.facts(500, seed=1))
    assert facts == list(synthetic.facts(500, seed=1))
    assert len({text for text, _, _ in facts}) == 500 and all(names for _, names, _ in facts)
    document = synthetic.document(4, seed=7)
    assert document == synthetic.document(4, seed=7) and 4 * 3000 <= len(document) < 5 * 3000


class FakeServer:
    """The routes the measurement calls. The load bank's retain and consolidation run until cancelled,
    unless ``idle_load`` keeps the load bank empty."""

    def __init__(self, idle_load: bool = False):
        self.idle_load = idle_load
        self.calls: Counter[str] = Counter()
        self.config: list[dict] = []
        self.retains: list[dict] = []
        self.cancelled: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        query = dict(request.url.params)
        self.calls[f"{method} {path.rsplit('/', 1)[-1]}"] += 1
        if path.endswith("/memories/list"):
            offset = int(query.get("offset", 0))
            return httpx.Response(200, json={"total": 40, "items": [{"text": f"fact {offset} | When: today"}]})
        if path.endswith("/entities"):
            return httpx.Response(200, json={"items": [{"canonical_name": "Kestrel"}]})
        if path == "/ext/cortana/status":
            queue = {"pending": 0, "processing": 0}
            return httpx.Response(200, json={"worker": {"mode": "separate", "healthy": True, "queue": queue}})
        if path.endswith("/memories/recall") or path.endswith("/reflect"):
            return httpx.Response(200, json={})
        if path.endswith("/config"):
            self.config.append(json.loads(request.content)["updates"])
            return httpx.Response(200, json={})
        if path.endswith("/consolidate"):
            return httpx.Response(200, json={"operation_id": "consolidation-1"})
        if path.endswith("/memories") and method == "POST":
            self.retains.append(json.loads(request.content))
            return httpx.Response(200, json={"operation_id": f"retain-{len(self.retains)}"})
        if method == "DELETE":
            self.cancelled.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        if path.endswith("/operations"):
            running = (
                []
                if self.idle_load or self.cancelled or not self.retains
                else [
                    {"id": "retain-1", "task_type": "batch_retain"},
                    {"id": "consolidation-1", "task_type": "consolidation"},
                ]
            )
            return httpx.Response(200, json={"operations": running if query.get("status") == "processing" else []})
        if "/operations/" in path:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(404, json={"path": path})


async def test_a_load_phase_drives_a_retain_and_a_consolidation_and_cancels_them_at_the_end():
    server = FakeServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://scratch") as client:
        # The fake answers in microseconds, so the idle-to-load ratio is noise here.
        report = await latency.measure(client, "measured", _budget(recall_p95_over_idle=1e6), load_bank="measured-load")
    idle, load = report.phases
    assert report.breaches == [] and report.load_bank == "measured-load"
    assert (load.recall.count, load.reflect.count, load.load_present) == (4, 2, 1.0)
    assert idle.load_present is None and idle.quiet == 1.0 and idle.worker["mode"] == "separate"
    assert server.calls["POST recall"] == 2 * (1 + 4) and server.calls["POST reflect"] == 2 * 2
    first, last = server.config[0], server.config[-1]
    assert first["retain_extraction_mode"] == "custom" and first["enable_auto_consolidation"] is True
    assert last == {"enable_auto_consolidation": False}, "auto-consolidation goes off before the cancel"
    assert server.calls["POST consolidate"] >= 1
    (body,) = server.retains[:1]
    assert body["async"] is True and len(body["items"][0]["content"]) >= _budget().load.document_chunks * 3000
    assert sorted(server.cancelled) == ["consolidation-1", "retain-1"]
    assert load.load_operations == {"retain_cancelled_at_end": 1, "consolidation_cancelled_at_end": 1}


async def test_a_load_that_never_starts_fails_rather_than_measuring_idle():
    budget = _budget(load={**_budget().load.model_dump(), "wait_timeout_s": 0.1})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(FakeServer(idle_load=True)), base_url="http://s"
    ) as client:
        with pytest.raises(TimeoutError, match="no retain and consolidation running together"):
            await latency.measure(client, "measured", budget, idle=False, load_bank="measured-load")


async def test_an_idle_phase_against_the_engine_reads_the_bank_and_times_every_call(
    cortana_memory, cortana_client, monkeypatch
):
    bank = f"cortana-lat-{uuid.uuid4().hex[:8]}"
    await _seed(cortana_memory, bank)
    _script_reflect(cortana_memory, monkeypatch)
    report = await latency.measure(cortana_client, bank, _budget())
    (idle,) = report.phases
    assert (idle.recall.count, idle.recall.errors, idle.reflect.count, idle.reflect.errors) == (4, 0, 2, 0)
    assert idle.worker["mode"] == "in-process" and report.ok


def test_the_command_exits_one_on_a_breach_and_zero_within_the_budget(monkeypatch, tmp_path):
    def fake(ok: bool):
        async def measure(client, bank, budget, **options):
            assert options["load_bank"] == "b-load" and options["idle"] is True
            return latency.Report(
                bank=bank,
                url=str(client.base_url),
                load_bank=options["load_bank"],
                budget_version=budget.version,
                phases=[_phase("load", [100.0], present=1.0)],
                breaches=[] if ok else ["load: recall p95 900.0 ms over the budget's 600 ms"],
                ok=ok,
            )

        return measure

    for ok, code in ((True, 0), (False, 1)):
        monkeypatch.setattr(latency, "measure", fake(ok))
        out = tmp_path / f"report-{ok}.json"
        result = CliRunner().invoke(app, ["latency", "--bank", "b", "--url", "http://127.0.0.1:1", "--out", str(out)])
        assert result.exit_code == code, result.output
        assert json.loads(out.read_text())["ok"] is ok and ("BREACH" in result.output) is (not ok)
