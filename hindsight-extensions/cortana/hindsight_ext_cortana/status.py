"""What ``GET /ext/cortana/status`` and ``hindsight-cortana status`` report (specification 12).

One function, ``build_status``, so the route and the command print the same thing. It reads, for
one bank or (no bank given) every bank of the schema:

- the package and its migrations (HSIGHT-2);
- structuring: live world and experience facts without claims, and the facts pending structuring,
  which are those of them no structuring call has answered (``reconcile.unstructured_facts``'s
  definition: never structured, or left ``structuring-pending`` by a failed call; a fact the model
  answered with no claim, or a decision record's fact, is counted without claims but not pending, S8);
- supersession: unaligned claims, keys pending alignment, keys in conflict and stale-document
  markers (S9, S11);
- the last reconciliation and its counts: the newest ``reconciled`` ledger entry. A reconciliation
  that changed nothing writes no entry (criterion 7), so this is the last one that changed something;
- the last gate run and its table: the newest summary ``hindsight-cortana gate`` wrote to the gate folder
  (``gate.runner.gate_dir``, the operating folder's ``ranking/``), or ``null`` when there is none. The
  acceptance run uses a scratch bank restored from the dump, so the record lives beside the tables
  rather than in any one database. A failed gate is reported here, not listed in ``problems``: it
  blocks a release (specification 11), it is not a fault of the running server;
- retirements and restorations of facts in the last day, from the ledger;
- the worker's health: whether background work runs in this process or a separate
  ``hindsight-worker``, that worker's liveness probe, and the engine's operation queue;
- the retrieval log: its retention, row count and any rows past retention (a sign the sweep is not
  running);
- the home ``.env`` check: ``~/.env`` is loaded by the server at startup with override, so it must
  hold no ``HINDSIGHT_`` key (specification 13.1 and 17 item 7; design record section 7). Only key
  names are reported, never values.

``problems`` lists, in words, every check above that is out of order; an empty list means none.
"""

import json
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import dotenv_values
from hindsight_api.config import get_config
from hindsight_api.engine.schema import fq_table_explicit
from pydantic import BaseModel, Field

from . import retrievals
from .migrations import BRANCH, TABLES, branch_revisions, head_revision

DISTRIBUTION = "hindsight-ext-cortana"
# Long enough for a local liveness probe that answers without touching the database.
WORKER_PROBE_TIMEOUT_SECONDS = 2.0


class MigrationState(BaseModel):
    branch: str
    head: str
    applied: str | None
    current: bool


class Structuring(BaseModel):
    facts_without_claims: int
    pending_structuring: int


class Supersession(BaseModel):
    unaligned_claims: int
    keys_pending_alignment: int
    keys_in_conflict: int
    stale_document_markers: int


class Reconciliation(BaseModel):
    recorded_at: datetime
    bank_id: str
    run_id: str | None
    actor: str
    summary: dict[str, Any]


class GateRun(BaseModel):
    """The newest gate run (specification 11): when it ran, against what, whether it reached the
    target, where its table is in the operating folder's ``ranking/``, and its per-suite results (whether
    each ran and passed, its one-line result, and the acceptance run's counts)."""

    ran_at: datetime
    release: str | None = Field(description="the fork release or commit the run tested")
    passed: bool = Field(description="every suite met its target, or Josh accepted the shortfall")
    table_path: str = Field(description="the run's table beside the earlier scorer outputs in ranking/")
    suites: dict[str, Any] = Field(description="per suite (deterministic, structuring, acceptance): its result")


class LastDay(BaseModel):
    since: datetime
    facts_retired: int
    facts_restored: int
    retired_fact_ids: list[str]
    restored_fact_ids: list[str]
    claims_superseded: int
    claims_restored: int
    curation_failures: int


class WorkerProcess(BaseModel):
    url: str
    reachable: bool
    worker_id: str | None = None
    is_shutdown: bool | None = None
    seconds_since_last_poll: float | None = None
    uptime_seconds: float | None = None
    error: str | None = None


class OperationQueue(BaseModel):
    pending: int = Field(description="queued and due now")
    waiting_to_retry: int = Field(description="queued with a retry time still in the future")
    processing: int
    oldest_pending_at: datetime | None
    oldest_processing_claimed_at: datetime | None
    processing_workers: list[str]
    completed_last_day: int
    failed_last_day: int
    last_completed_at: datetime | None


class Worker(BaseModel):
    mode: Literal["in-process", "separate"]
    healthy: bool | None = Field(
        description="separate: its liveness probe answered and it is not shutting down; in-process: null, "
        "since this process answering is the only liveness evidence"
    )
    process: WorkerProcess | None
    queue: OperationQueue


class RetrievalLog(BaseModel):
    retention_days: int
    rows: int
    oldest_recorded_at: datetime | None
    newest_recorded_at: datetime | None
    rows_past_retention: int


class HomeEnv(BaseModel):
    path: str
    exists: bool
    hindsight_keys: list[str]
    ok: bool


class Status(BaseModel):
    extension: str
    version: str
    database_schema: str
    bank_id: str | None = Field(description="the bank reported on, or null for every bank of the schema")
    generated_at: datetime
    migrations: MigrationState
    tables: dict[str, bool]
    structuring: Structuring | None
    supersession: Supersession | None
    last_reconciliation: Reconciliation | None
    gate: GateRun | None = None
    last_day: LastDay | None
    worker: Worker | None
    retrievals: RetrievalLog | None
    home_env: HomeEnv
    problems: list[str]


def last_gate_run(directory: Path | None = None) -> GateRun | None:
    """The newest gate run's summary from the gate folder, as ``GateRun``; None when there is none or
    it cannot be read."""
    from .gate.runner import newest_run

    run = newest_run(directory)
    if run is None:
        return None
    try:
        suites = {
            name: {"ran": suite.get("ran"), "passed": suite.get("passed"), "result": suite.get("note")}
            | ({"counts": suite.get("summary")} if name == "acceptance" else {})
            for name, suite in run["suites"].items()
        }
        return GateRun(
            ran_at=run["ran_at"],
            release=run.get("release"),
            passed=bool(run["passed"]),
            table_path=run["table_path"],
            suites=suites,
        )
    except (KeyError, TypeError, ValueError):
        return None


def home_env_check(home: Path | None = None) -> HomeEnv:
    path = (home or Path.home()) / ".env"
    keys = sorted(k for k in dotenv_values(path) if k.startswith("HINDSIGHT_")) if path.is_file() else []
    return HomeEnv(path=str(path), exists=path.is_file(), hindsight_keys=keys, ok=not keys)


async def probe_worker(url: str) -> WorkerProcess:
    try:
        async with httpx.AsyncClient(timeout=WORKER_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return WorkerProcess(url=url, reachable=False, error=f"{type(error).__name__}: {error}")
    return WorkerProcess(
        url=url,
        reachable=True,
        worker_id=body.get("worker_id"),
        is_shutdown=body.get("is_shutdown"),
        seconds_since_last_poll=body.get("seconds_since_last_poll"),
        uptime_seconds=body.get("uptime_seconds"),
    )


def worker_mode() -> Literal["in-process", "separate"]:
    """Where background work runs: in this process, or (``HINDSIGHT_API_WORKER_ENABLED=false``, as
    specification 14 runs production) in a separate ``hindsight-worker``."""
    return "in-process" if get_config().worker_enabled else "separate"


def worker_probe_url() -> str:
    """The separate worker's liveness probe. It reads the same profile, so the same port."""
    return f"http://127.0.0.1:{get_config().worker_http_port}/health/live"


async def build_status(pool: Any, schema: str, *, bank_id: str | None = None) -> Status:
    """The full status, reading through ``pool`` in ``schema``."""

    def t(name: str) -> str:
        return fq_table_explicit(name, schema)

    bank = "AND bank_id = $1" if bank_id is not None else ""
    bank_args = [bank_id] if bank_id is not None else []
    home_env = home_env_check()
    problems: list[str] = []
    if not home_env.ok:
        problems.append(f"{home_env.path} defines {', '.join(home_env.hindsight_keys)}; it must hold no HINDSIGHT_ key")

    async with pool.acquire() as conn:
        versions = await conn.fetch(f"SELECT version_num FROM {t('alembic_version')}")
        present = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1 AND table_name = ANY($2::text[])",
            schema,
            list(TABLES),
        )
    ours = branch_revisions()
    applied = next((row["version_num"] for row in versions if row["version_num"] in ours), None)
    head = head_revision()
    found = {row["table_name"] for row in present}
    migrations = MigrationState(branch=BRANCH, head=head, applied=applied, current=applied == head)
    tables = {table: table in found for table in TABLES}
    base = dict(
        extension=DISTRIBUTION,
        version=version(DISTRIBUTION),
        database_schema=schema,
        bank_id=bank_id,
        generated_at=datetime.now(UTC),
        migrations=migrations,
        tables=tables,
        home_env=home_env,
        gate=last_gate_run(),
    )
    if not migrations.current or not all(tables.values()):
        problems.append(f"migration branch {BRANCH} is at {applied}, head is {head}; missing tables are not read")
        return Status(
            **base,
            structuring=None,
            supersession=None,
            last_reconciliation=None,
            last_day=None,
            worker=None,
            retrievals=None,
            problems=problems,
        )

    since = datetime.now(UTC) - timedelta(days=1)
    async with pool.acquire() as conn:
        facts = await conn.fetchrow(
            f"""
            WITH answered AS (
                SELECT DISTINCT unnest(memory_unit_ids) AS id FROM {t("ledger")}
                WHERE event = 'structured' {bank}
            ),
            without AS (
                SELECT mu.id FROM {t("memory_units")} mu
                WHERE mu.fact_type IN ('world', 'experience') {bank.replace("bank_id", "mu.bank_id")}
                  AND NOT EXISTS (SELECT 1 FROM {t("claims")} c WHERE c.bank_id = mu.bank_id AND c.memory_unit_id = mu.id)
            )
            SELECT (SELECT count(*) FROM without) AS without_claims,
                   (SELECT count(*) FROM without WHERE id NOT IN (SELECT id FROM answered)
                      AND id NOT IN (SELECT id FROM {t("memory_units")} WHERE document_id LIKE 'decision:%')) AS pending
            """,
            *bank_args,
        )
        keys = await conn.fetchrow(
            f"""
            SELECT (SELECT count(*) FROM {t("claims")} WHERE state = 'unaligned' {bank}) AS unaligned,
                   count(*) FILTER (WHERE alignment = 'pending') AS pending,
                   count(*) FILTER (WHERE conflict_claim_ids <> '{{}}') AS conflicts,
                   COALESCE(sum(cardinality(stale_documents)), 0) AS stale
            FROM {t("attributes")} WHERE true {bank}
            """,
            *bank_args,
        )
        reconciled = await conn.fetchrow(
            f"""
            SELECT recorded_at, bank_id, run_id, actor, details FROM {t("ledger")}
            WHERE event = 'reconciled' {bank} ORDER BY recorded_at DESC, id DESC LIMIT 1
            """,
            *bank_args,
        )
        day_args = [since, *bank_args]
        day_bank = bank.replace("$1", "$2")
        events = await conn.fetch(
            f"""
            SELECT event, count(*) AS entries, array_agg(DISTINCT u.id) FILTER (WHERE u.id IS NOT NULL) AS facts
            FROM {t("ledger")} l LEFT JOIN LATERAL unnest(l.memory_unit_ids) u(id)
                 ON l.event IN ('fact-retired', 'fact-restored')
            WHERE l.recorded_at >= $1 {day_bank}
              AND l.event IN ('fact-retired', 'fact-restored', 'claim-superseded', 'claim-restored', 'curation-failed')
            GROUP BY event
            """,
            *day_args,
        )
        queue = await conn.fetchrow(
            f"""
            SELECT count(*) FILTER (WHERE status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= now()))
                       AS pending,
                   count(*) FILTER (WHERE status = 'pending' AND next_retry_at > now()) AS waiting,
                   count(*) FILTER (WHERE status = 'processing') AS processing,
                   min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending,
                   min(claimed_at) FILTER (WHERE status = 'processing') AS oldest_claimed,
                   COALESCE(array_agg(DISTINCT worker_id) FILTER (WHERE status = 'processing' AND worker_id IS NOT NULL),
                            '{{}}') AS workers,
                   count(*) FILTER (WHERE status = 'completed' AND completed_at >= $1) AS completed_day,
                   count(*) FILTER (WHERE status = 'failed' AND updated_at >= $1) AS failed_day,
                   max(completed_at) FILTER (WHERE status = 'completed') AS last_completed
            FROM {t("async_operations")} WHERE true {day_bank}
            """,
            *day_args,
        )
        days = retrievals.retention_days()
        log = await conn.fetchrow(
            f"""
            SELECT count(*) AS rows, min(recorded_at) AS oldest, max(recorded_at) AS newest,
                   count(*) FILTER (WHERE recorded_at < now() - make_interval(days => $1)) AS past
            FROM {t("retrievals")} WHERE true {day_bank}
            """,
            days,
            *bank_args,
        )

    by_event = {row["event"]: row for row in events}

    def count(event: str) -> int:
        return by_event[event]["entries"] if event in by_event else 0

    def fact_ids(event: str) -> list[str]:
        return sorted(str(i) for i in (by_event[event]["facts"] or [])) if event in by_event else []

    mode = worker_mode()
    process, healthy = None, None
    if mode == "separate":
        process = await probe_worker(worker_probe_url())
        healthy = process.reachable and not process.is_shutdown
        if not healthy:
            problems.append(f"the separate worker did not answer its liveness probe at {process.url}")
    if log["past"]:
        problems.append(f"{log['past']} retrieval log rows are older than {days} days; the sweep has not run")

    return Status(
        **base,
        structuring=Structuring(facts_without_claims=facts["without_claims"], pending_structuring=facts["pending"]),
        supersession=Supersession(
            unaligned_claims=keys["unaligned"],
            keys_pending_alignment=keys["pending"],
            keys_in_conflict=keys["conflicts"],
            stale_document_markers=keys["stale"],
        ),
        last_reconciliation=Reconciliation(
            recorded_at=reconciled["recorded_at"],
            bank_id=reconciled["bank_id"],
            run_id=str(reconciled["run_id"]) if reconciled["run_id"] else None,
            actor=reconciled["actor"],
            summary=_json(reconciled["details"]),
        )
        if reconciled
        else None,
        last_day=LastDay(
            since=since,
            facts_retired=len(fact_ids("fact-retired")),
            facts_restored=len(fact_ids("fact-restored")),
            retired_fact_ids=fact_ids("fact-retired"),
            restored_fact_ids=fact_ids("fact-restored"),
            claims_superseded=count("claim-superseded"),
            claims_restored=count("claim-restored"),
            curation_failures=count("curation-failed"),
        ),
        worker=Worker(
            mode=mode,
            healthy=healthy,
            process=process,
            queue=OperationQueue(
                pending=queue["pending"],
                waiting_to_retry=queue["waiting"],
                processing=queue["processing"],
                oldest_pending_at=queue["oldest_pending"],
                oldest_processing_claimed_at=queue["oldest_claimed"],
                processing_workers=list(queue["workers"]),
                completed_last_day=queue["completed_day"],
                failed_last_day=queue["failed_day"],
                last_completed_at=queue["last_completed"],
            ),
        ),
        retrievals=RetrievalLog(
            retention_days=days,
            rows=log["rows"],
            oldest_recorded_at=log["oldest"],
            newest_recorded_at=log["newest"],
            rows_past_retention=log["past"],
        ),
        problems=problems,
    )


def _json(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})
