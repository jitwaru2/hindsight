"""Live recall and reflect latency, idle and while a retain and a consolidation run, checked against
the recorded budget (specification 14; acceptance criterion 13).

``measure`` talks to a running server over HTTP, as a session does, so the wall times include
everything a caller waits for: admission, the recall semaphore, the reranker's threads and the CPU
that background work in the same process (or the same machine) takes from them.

A phase is a fixed number of recalls and reflects against one bank, one at a time, after a few
unrecorded warm-up recalls. Reflects are spread evenly among the recalls so both meet the same load.
The idle phase waits for the server's operation queue to empty and records at every probe whether it
still is, so leftover background work cannot pass for an idle baseline. Queries are drawn from the bank itself (fact texts for recall, its most-mentioned entities for
reflect) unless a query file is given, so the same command serves a synthetic bank and a bank
restored from the daily dump.

The load phase runs background work on a separate synthetic bank (``<bank>-load`` by default), so
the measured bank's contents are never changed. The contention it measures is the server's, not the
bank's: the API process's reranker threads, recall semaphore and database pool, and the machine's
CPU, which a second bank on the same server exercises as fully. The load bank gets the package's
atomic extraction instructions (what production runs after cut-over) and auto-consolidation; the
command triggers a consolidation and keeps one asynchronous retain of a multi-chunk synthetic
document in flight (as the plugin and the loader send them), each retain's facts feeding the next
consolidation. Probes start once a retain and a consolidation are both processing, and every probe
records whether they still were, so a run whose load lapsed is reported, not passed.
"""

import asyncio
import json
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from .. import extraction
from . import synthetic

BUDGET_FILE = "latency_budget.json"


class Ceiling(BaseModel):
    p50_ms: float
    p95_ms: float


class LoadSettings(BaseModel):
    document_chunks: int = Field(description="extraction chunks per synthetic document retained under load")
    wait_timeout_s: float = Field(
        description="how long to wait for the load to start, for it to stop, and for the server to fall idle"
    )
    min_present_fraction: float = Field(description="share of probes that must find both running")


class Budget(BaseModel):
    """``latency_budget.json``: the measured limits and why they are what they are."""

    version: int
    recorded: date
    reason: str
    recalls: int
    reflects: int
    warmup_recalls: int
    recall: Ceiling
    reflect: Ceiling
    recall_p95_over_idle: float = Field(description="load p95 over idle p95, recall, same run")
    load: LoadSettings
    measurements: dict[str, Any] = Field(default_factory=dict, description="the runs the budget was set from")


def load_budget(path: Path | None = None) -> Budget:
    text = path.read_text() if path else files(__package__).joinpath(BUDGET_FILE).read_text()
    return Budget.model_validate_json(text)


class Stats(BaseModel):
    count: int
    errors: int
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    samples_ms: list[float]


def stats(samples: list[float], errors: int = 0) -> Stats:
    """p50 and p95 by the inclusive method (linear between order statistics, within the sample)."""
    if not samples:
        return Stats(count=0, errors=errors, p50_ms=None, p95_ms=None, max_ms=None, samples_ms=[])
    cuts = statistics.quantiles(samples, n=20, method="inclusive") if len(samples) > 1 else [samples[0]] * 19
    return Stats(
        count=len(samples),
        errors=errors,
        p50_ms=round(cuts[9], 1),
        p95_ms=round(cuts[18], 1),
        max_ms=round(max(samples), 1),
        samples_ms=[round(s, 1) for s in samples],
    )


class Phase(BaseModel):
    name: Literal["idle", "load"]
    started_at: datetime
    recall: Stats
    reflect: Stats
    load_present: float | None = Field(default=None, description="share of probes that found the load running")
    quiet: float | None = Field(
        default=None, description="idle: share of probes that found no operation queued or running on the server"
    )
    worker: dict[str, Any] | None = Field(default=None, description="the status route's worker section, before")
    load_operations: dict[str, int] = Field(default_factory=dict, description="load-bank operations by outcome")


class Report(BaseModel):
    bank: str
    url: str
    load_bank: str | None
    budget_version: int
    phases: list[Phase]
    breaches: list[str]
    ok: bool


class Queries(BaseModel):
    recall: list[str]
    reflect: list[str]


async def bank_queries(client: httpx.AsyncClient, bank: str, recalls: int, reflects: int) -> Queries:
    """Recall queries from facts spread evenly through the bank's list, and reflect questions about
    its most-mentioned entities."""
    api = f"/v1/default/banks/{bank}"
    total = (await _get(client, f"{api}/memories/list", limit=1))["total"]
    if not total:
        raise ValueError(f"bank {bank} has no memories to draw queries from")
    recall = []
    for i in range(recalls):
        items = (await _get(client, f"{api}/memories/list", limit=1, offset=i * total // recalls))["items"]
        if items:
            recall.append(" ".join(items[0]["text"].split(" | ")[0].split()[:20]))
    entities = (await _get(client, f"{api}/entities", limit=reflects))["items"]
    names = [e["canonical_name"] for e in entities] or ["this bank"]
    reflect = [
        f"What is the current state of {names[i % len(names)]}, and what changed most recently?"
        for i in range(reflects)
    ]
    return Queries(recall=recall, reflect=reflect)


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params)
    response.raise_for_status()
    return response.json()


async def _timed(call: Callable[[], Awaitable[httpx.Response]]) -> tuple[float, bool]:
    start = time.perf_counter()
    try:
        response = await call()
        ok = response.status_code == 200
    except httpx.HTTPError:
        ok = False
    return (time.perf_counter() - start) * 1000, ok


class Load:
    """Background work on the load bank for the duration of a phase."""

    def __init__(self, client: httpx.AsyncClient, bank: str, settings: LoadSettings, *, run: str):
        self.client, self.bank, self.settings, self.run = client, bank, settings, run
        self.api = f"/v1/default/banks/{bank}"
        self.outcomes: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    async def running(self) -> set[str]:
        """The operation types processing on the load bank now."""
        body = await _get(self.client, f"{self.api}/operations", status="processing", exclude_parents="true", limit=100)
        return {op["task_type"] for op in body["operations"]}

    async def present(self) -> bool:
        kinds = await self.running()
        return any("retain" in k for k in kinds) and "consolidation" in kinds

    async def start(self) -> None:
        updates = {**extraction.bank_config_updates(None), "enable_auto_consolidation": True}
        (await self.client.patch(f"{self.api}/config", json={"updates": updates})).raise_for_status()
        self._task = asyncio.create_task(self._retain_loop())
        await self._consolidate()
        deadline = time.monotonic() + self.settings.wait_timeout_s
        while not await self.present():
            if self._task.done():
                self._task.result()
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"no retain and consolidation running together on {self.bank} within "
                    f"{self.settings.wait_timeout_s:.0f}s"
                )
            await self._consolidate()
            await asyncio.sleep(2)

    async def _consolidate(self) -> None:
        (await self.client.post(f"{self.api}/consolidate", json={})).raise_for_status()

    async def _retain_loop(self) -> None:
        n = 0
        while True:
            n += 1
            seed = uuid.uuid5(uuid.NAMESPACE_URL, f"{self.run}:{n}").int % 1_000_000
            item = {
                "content": synthetic.document(self.settings.document_chunks, seed=seed),
                "document_id": f"latency-load:{self.run}:{n}",
                "context": "synthetic project notes for the latency measurement",
                "tags": ["source:synthetic", "domain:work", "pool:work"],
                "observation_scopes": [["pool:work"]],
            }
            response = await self.client.post(f"{self.api}/memories", json={"items": [item], "async": True})
            response.raise_for_status()
            op = response.json()["operation_id"]
            while True:
                await asyncio.sleep(2)
                status = (await _get(self.client, f"{self.api}/operations/{op}"))["status"]
                if status in ("completed", "failed", "cancelled"):
                    self.outcomes[f"retain_{status}"] = self.outcomes.get(f"retain_{status}", 0) + 1
                    break

    async def stop(self) -> None:
        """Stop submitting and cancel what is queued or running on the load bank until it stays
        empty, so the server is left idle (the load bank is the command's own; its unconsolidated
        facts stay). Auto-consolidation goes off first, because a retain finishing after the cancel
        would queue a consolidation, and a consolidation round that ends as it is cancelled queues
        the next round; polling until two consecutive looks find nothing catches both."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        updates = {"updates": {"enable_auto_consolidation": False}}
        (await self.client.patch(f"{self.api}/config", json=updates)).raise_for_status()
        deadline = time.monotonic() + self.settings.wait_timeout_s
        empty = 0
        while empty < 2:
            empty = 0 if await self._cancel_all() else empty + 1
            if time.monotonic() > deadline:
                raise TimeoutError(f"operations kept arriving on {self.bank} after the load stopped")
            await asyncio.sleep(2)

    async def _cancel_all(self) -> int:
        cancelled = 0
        for status in ("pending", "processing"):
            for op in (await _get(self.client, f"{self.api}/operations", status=status, limit=100))["operations"]:
                response = await self.client.delete(f"{self.api}/operations/{op['id']}")
                if response.status_code not in (200, 409):
                    response.raise_for_status()
                kind = "retain" if "retain" in op["task_type"] else op["task_type"]
                self.outcomes[f"{kind}_cancelled_at_end"] = self.outcomes.get(f"{kind}_cancelled_at_end", 0) + 1
                cancelled += 1
        return cancelled


async def server_busy(client: httpx.AsyncClient) -> bool | None:
    """Whether any operation is due or running anywhere on the server (the status route's
    server-wide queue); None when the route does not answer."""
    response = await client.get("/ext/cortana/status")
    if response.status_code != 200:
        return None
    queue = response.json()["worker"]["queue"]
    return bool(queue["pending"] or queue["processing"])


async def run_phase(
    client: httpx.AsyncClient,
    bank: str,
    queries: Queries,
    budget: Budget,
    name: Literal["idle", "load"],
    load: Load | None = None,
) -> Phase:
    started = datetime.now().astimezone()
    worker = None
    status = await client.get("/ext/cortana/status", params={"bank_id": bank})
    if status.status_code == 200:
        worker = status.json().get("worker")
    if load is not None:
        await load.start()
    else:
        deadline = time.monotonic() + budget.load.wait_timeout_s
        while await server_busy(client) and time.monotonic() < deadline:
            await asyncio.sleep(5)
    api = f"/v1/default/banks/{bank}"

    async def recall(query: str) -> tuple[float, bool]:
        return await _timed(lambda: client.post(f"{api}/memories/recall", json={"query": query}))

    async def reflect(query: str) -> tuple[float, bool]:
        return await _timed(lambda: client.post(f"{api}/reflect", json={"query": query}))

    try:
        for i in range(budget.warmup_recalls):
            await recall(queries.recall[i % len(queries.recall)])
        # Reflects spread evenly among the recalls.
        every = max(1, len(queries.recall) // max(1, len(queries.reflect)))
        plan: list[tuple[str, str]] = []
        reflects = iter(queries.reflect)
        for i, query in enumerate(queries.recall):
            plan.append(("recall", query))
            if (i + 1) % every == 0 and (r := next(reflects, None)) is not None:
                plan.append(("reflect", r))
        plan.extend(("reflect", r) for r in reflects)
        times: dict[str, list[float]] = {"recall": [], "reflect": []}
        errors = {"recall": 0, "reflect": 0}
        present: list[bool] = []
        quiet: list[bool] = []
        for kind, query in plan:
            if load is not None:
                present.append(await load.present())
            elif (busy := await server_busy(client)) is not None:
                quiet.append(not busy)
            elapsed, ok = await (recall(query) if kind == "recall" else reflect(query))
            if ok:
                times[kind].append(elapsed)
            else:
                errors[kind] += 1
    finally:
        if load is not None:
            await load.stop()
    return Phase(
        name=name,
        started_at=started,
        recall=stats(times["recall"], errors["recall"]),
        reflect=stats(times["reflect"], errors["reflect"]),
        load_present=round(sum(present) / len(present), 3) if present else None,
        quiet=round(sum(quiet) / len(quiet), 3) if quiet else None,
        worker=worker,
        load_operations=load.outcomes if load is not None else {},
    )


def breaches(phases: list[Phase], budget: Budget) -> list[str]:
    """Every way the run misses the budget; empty when it passes."""
    found: list[str] = []
    by_name = {p.name: p for p in phases}
    for phase in phases:
        for kind in ("recall", "reflect"):
            s: Stats = getattr(phase, kind)
            if s.errors:
                found.append(f"{phase.name}: {s.errors} {kind} calls failed")
    idle = by_name.get("idle")
    if idle is not None and idle.quiet is not None and idle.quiet < 1:
        found.append(
            f"idle: an operation was queued or running on the server at {1 - idle.quiet:.0%} of the probes; "
            "the idle baseline is not valid"
        )
    load = by_name.get("load")
    if load is None:
        return found
    if load.load_present is None or load.load_present < budget.load.min_present_fraction:
        found.append(
            f"load: a retain and a consolidation were running for {load.load_present} of the probes, "
            f"under the required {budget.load.min_present_fraction}; the measurement is not valid"
        )
    for kind in ("recall", "reflect"):
        s, ceiling = getattr(load, kind), getattr(budget, kind)
        for q in ("p50", "p95"):
            value, limit = getattr(s, f"{q}_ms"), getattr(ceiling, f"{q}_ms")
            if value is None or value > limit:
                found.append(f"load: {kind} {q} {value} ms over the budget's {limit} ms")
    if idle is not None and idle.recall.p95_ms and load.recall.p95_ms:
        ratio = load.recall.p95_ms / idle.recall.p95_ms
        if ratio > budget.recall_p95_over_idle:
            found.append(f"load: recall p95 is {ratio:.2f} times idle, over the budget's {budget.recall_p95_over_idle}")
    return found


async def measure(
    client: httpx.AsyncClient,
    bank: str,
    budget: Budget,
    *,
    idle: bool = True,
    load_bank: str | None = None,
    queries: Queries | None = None,
) -> Report:
    """Run the idle phase (when ``idle``) and the load phase (when ``load_bank``) and check them."""
    queries = queries or await bank_queries(client, bank, budget.recalls, budget.reflects)
    phases = []
    if idle:
        phases.append(await run_phase(client, bank, queries, budget, "idle"))
    if load_bank is not None:
        load = Load(client, load_bank, budget.load, run=datetime.now().strftime("%Y%m%dT%H%M%S"))
        phases.append(await run_phase(client, bank, queries, budget, "load", load))
    found = breaches(phases, budget)
    return Report(
        bank=bank,
        url=str(client.base_url),
        load_bank=load_bank,
        budget_version=budget.version,
        phases=phases,
        breaches=found,
        ok=not found,
    )


def read_queries(path: Path) -> Queries:
    return Queries.model_validate(json.loads(path.read_text()))
