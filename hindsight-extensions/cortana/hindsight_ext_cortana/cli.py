"""``hindsight-cortana``: the operator's command for this package.

``verify-base``, ``suite structuring`` (HSIGHT-4), ``reconcile``, ``merge`` and ``unmerge``
(HSIGHT-5), ``status`` and ``retrievals sweep`` (HSIGHT-7), ``latency`` (HSIGHT-9), and
``migrate structure`` and ``gate`` (HSIGHT-8) are here.

``latency`` talks to a running server over HTTP; it does not open the engine.

``reconcile``, ``merge``, ``unmerge``, ``status``, ``retrievals sweep`` and ``migrate structure`` open the engine in this process the way the engine's
``hindsight-worker`` does: from the ``HINDSIGHT_API_*`` environment (source the profile first),
with the tenant and operation-hooks extensions loaded and no migrations run. Work the engine queues
(consolidation, graph maintenance, mental-model refreshes) is written to its operations table for
the server's worker, not run here.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from .verify_base import format_report, verify_base

app = typer.Typer(help="Operate Cortana's extension to the Hindsight engine.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Operate Cortana's extension to the Hindsight engine."""


@app.command("verify-base")
def verify_base_command(
    record: Annotated[Path, typer.Argument(help="RECORD of the installed wheel, <dist>.dist-info/RECORD")],
    package_dir: Annotated[
        Path, typer.Argument(help="the checkout's package directory, e.g. hindsight-api-slim/hindsight_api")
    ],
) -> None:
    """Check that a checkout's package directory is byte-identical to an installed wheel.

    Exits 0 when every file matches, 1 on any mismatched, missing or extra file.
    """
    try:
        result = verify_base(record, package_dir)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(format_report(result, record, package_dir))
    raise typer.Exit(0 if result.ok else 1)


suite_app = typer.Typer(help="Run the evaluation suites that call a model (specification 11).", no_args_is_help=True)
app.add_typer(suite_app, name="suite")


@suite_app.command("structuring")
def suite_structuring_command(
    fixtures: Annotated[
        list[Path] | None,
        typer.Option(
            "--fixtures",
            help="a fixture file or folder; repeatable. Default: the real fixtures' folder "
            "($HINDSIGHT_CORTANA_REAL_FIXTURES, else ~/.cortana-legacy/hindsight/fixtures/structuring)",
        ),
    ] = None,
    cold_runs: Annotated[int, typer.Option(help="runs from an empty catalog per fixture")] = 2,
    warm: Annotated[bool, typer.Option(help="one more run starting from the first run's catalog")] = True,
    out: Annotated[Path | None, typer.Option(help="folder for structuring-suite.json")] = None,
) -> None:
    """Structure the fixtures' facts with the engine's retain provider and score the claims.

    The provider and model come from the HINDSIGHT_API_* environment, as on the server (source the
    profile first). Exits 0 when every run meets the precision target, 1 otherwise.
    """
    from rich.console import Console
    from rich.table import Table

    from .structuring.fixtures import load_fixture, load_fixtures, real_fixtures_dir
    from .structuring.suite import PRECISION_TARGET, engine_retain_model, run_suite

    paths = fixtures or ([real_fixtures_dir()] if real_fixtures_dir() else [])
    if not paths:
        raise typer.BadParameter("no fixtures: pass --fixtures or set HINDSIGHT_CORTANA_REAL_FIXTURES")
    loaded = []
    for path in paths:
        loaded.extend(load_fixtures(path) if path.is_dir() else [load_fixture(path)])
    report = asyncio.run(run_suite(loaded, engine_retain_model(), cold_runs=cold_runs, warm=warm, out=out))

    console = Console()
    table = Table(title=f"structuring suite, prompt {report['prompt_version']}, {report['model']}")
    for column in ("fixture", "run", "precision", "recall", "key", "provisional", "time", "pending"):
        table.add_column(column)
    for fixture in report["fixtures"]:
        for run in fixture["runs"]:
            s = run["score"]
            table.add_row(
                fixture["fixture"],
                run["run"],
                f"{run['precision']:.3f} ({s['correct']}/{s['produced']})",
                f"{run['recall']:.3f} ({s['correct']}/{s['expected']})",
                f"{s['on_key']}/{s['produced']}",
                f"{s['flag_ok']}/{s['on_key'] or 0}",
                f"{s['timed_ok']}/{s['timed']}",
                str(len(run["pending"])),
            )
    console.print(table)
    summary = report["summary"]
    for label, run in summary["runs"].items():
        console.print(f"{label}: precision {run['precision']:.3f}, recall {run['recall']:.3f}")
    for label, stability in summary["stability"].items():
        console.print(f"key stability first run vs {label}: {stability['same']}/{stability['compared']}")
    met = summary["meets_target"]
    console.print(f"target {PRECISION_TARGET}: {'met' if met else 'NOT met'}")
    raise typer.Exit(0 if met else 1)


async def _with_engine(work: Callable[[Any], Awaitable[Any]]) -> Any:
    from hindsight_api import MemoryEngine
    from hindsight_api.engine.task_backend import WorkerTaskBackend
    from hindsight_api.extensions import OperationValidatorExtension, TenantExtension, load_extension

    engine = MemoryEngine(
        run_migrations=False,
        task_backend=WorkerTaskBackend(),
        tenant_extension=load_extension("TENANT", TenantExtension),
        operation_validator=load_extension("OPERATION_VALIDATOR", OperationValidatorExtension),
    )
    await engine.initialize()
    try:
        return await work(engine)
    finally:
        await engine.close()


async def _subject_id(engine: Any, bank: str, subject: str) -> UUID:
    """An entity id given as a UUID or as the entity's exact name (case-insensitive)."""
    try:
        return UUID(subject)
    except ValueError:
        pass
    from hindsight_api.engine.schema import fq_table

    async with (await engine._get_pool()).acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id FROM {fq_table('entities')} WHERE bank_id = $1 AND LOWER(canonical_name) = LOWER($2)",
            bank,
            subject,
        )
    if len(rows) != 1:
        raise typer.BadParameter(f"{subject!r} names {len(rows)} entities of bank {bank}; pass the entity id")
    return rows[0]["id"]


def _context() -> Any:
    from hindsight_api import RequestContext

    return RequestContext(internal=True)


@app.command("reconcile")
def reconcile_command(
    bank: Annotated[str, typer.Option(help="the bank to reconcile")],
    subject: Annotated[str | None, typer.Option(help="one subject: entity id or exact name")] = None,
    document: Annotated[str | None, typer.Option(help="one document id")] = None,
    all_: Annotated[bool, typer.Option("--all", help="the whole bank")] = False,
    skip_structuring: Annotated[
        bool, typer.Option(help="align and settle only; leave facts without claims to `migrate structure`")
    ] = False,
) -> None:
    """Recompute supersession from the claims and the facts' states (specification 6.4).

    Structures facts that have no claims, aligns pending keys, sweeps orphaned claims, settles every
    key in scope, and writes a ledger summary when anything changed. Prints the run's summary.
    """
    if sum(bool(x) for x in (subject, document, all_)) != 1:
        raise typer.BadParameter("give exactly one of --subject, --document or --all")
    from .reconcile import reconcile

    async def work(engine: Any) -> dict:
        subject_id = await _subject_id(engine, bank, subject) if subject else None
        report = await reconcile(
            engine,
            bank,
            subject=subject_id,
            document=document,
            request_context=_context(),
            structure=not skip_structuring,
        )
        return report.summary()

    typer.echo(json.dumps(asyncio.run(_with_engine(work)), indent=1, default=str))


@app.command("merge")
def merge_command(
    bank: Annotated[str, typer.Option(help="the bank")],
    subject: Annotated[str, typer.Option(help="entity id or exact name")],
    key: Annotated[str, typer.Option(help="the key to merge")],
    into: Annotated[str, typer.Option(help="the key it is the same attribute as")],
    reason: Annotated[str, typer.Option(help="why, for the ledger")] = "operator merge",
) -> None:
    """Merge one attribute key of a subject into another (specification 6.3), then recompute both."""
    asyncio.run(_with_engine(lambda engine: _merge(engine, bank, subject, key, into, reason, reverse=False)))


@app.command("unmerge")
def unmerge_command(
    bank: Annotated[str, typer.Option(help="the bank")],
    subject: Annotated[str, typer.Option(help="entity id or exact name")],
    key: Annotated[str, typer.Option(help="the merged key to split back out")],
    reason: Annotated[str, typer.Option(help="why, for the ledger")] = "operator reversal",
) -> None:
    """Reverse a merge by recording its inverse, then recompute both keys."""
    asyncio.run(_with_engine(lambda engine: _merge(engine, bank, subject, key, None, reason, reverse=True)))


async def _merge(
    engine: Any, bank: str, subject: str, key: str, into: str | None, reason: str, *, reverse: bool
) -> None:
    import uuid

    from .merges import MergeRefused, merge_key, reverse_merge
    from .supersession import settle

    subject_id = await _subject_id(engine, bank, subject)
    run_id = uuid.uuid4()
    try:
        if reverse:
            result = await reverse_merge(engine, bank, subject_id, key, actor="operator", reason=reason, run_id=run_id)
        else:
            result = await merge_key(
                engine, bank, subject_id, key, into or "", actor="operator", reason=reason, run_id=run_id
            )
    except MergeRefused as refused:
        raise typer.BadParameter(str(refused)) from refused
    settled = await settle(engine, bank, result.keys, request_context=_context(), actor="operator", run_id=run_id)
    typer.echo(json.dumps({"moved_claims": len(result.claim_ids), "settled": settled.summary()}, indent=1, default=str))


@app.command("status")
def status_command(
    bank: Annotated[str | None, typer.Option(help="one bank; every bank of the schema when omitted")] = None,
) -> None:
    """Print what GET /ext/cortana/status answers (specification 12), as JSON.

    Exits 0 when the status lists no problems, 1 otherwise.
    """
    from .status import build_status

    async def work(engine: Any) -> Any:
        from hindsight_api import RequestContext

        schema = (await engine.tenant_extension.authenticate(RequestContext())).schema_name
        return await build_status(await engine._get_pool(), schema, bank_id=bank)

    status = asyncio.run(_with_engine(work))
    typer.echo(status.model_dump_json(indent=1))
    raise typer.Exit(1 if status.problems else 0)


retrievals_app = typer.Typer(help="The retrieval log (specification 12).", no_args_is_help=True)
app.add_typer(retrievals_app, name="retrievals")


@retrievals_app.command("sweep")
def retrievals_sweep_command(
    bank: Annotated[str | None, typer.Option(help="one bank; every bank when omitted")] = None,
    days: Annotated[
        int | None,
        typer.Option(min=1, help="retention in days; default $HINDSIGHT_CORTANA_RETRIEVALS_RETENTION_DAYS, else 30"),
    ] = None,
) -> None:
    """Delete retrieval log rows older than the retention. A whole-bank reconcile does this too."""
    from . import retrievals

    async def work(engine: Any) -> dict:
        retention = days or retrievals.retention_days()
        async with (await engine._get_pool()).acquire() as conn:
            deleted = await retrievals.sweep(conn, bank_id=bank, days=retention)
        return {"bank": bank, "retention_days": retention, "deleted": deleted}

    typer.echo(json.dumps(asyncio.run(_with_engine(work)), indent=1))


@app.command("latency")
def latency_command(
    bank: Annotated[str, typer.Option(help="the bank whose recall and reflect are timed")],
    url: Annotated[
        str | None, typer.Option(help="the server; default http://127.0.0.1:$HINDSIGHT_API_PORT (8888 when unset)")
    ] = None,
    load_bank: Annotated[
        str | None, typer.Option(help="the synthetic bank that carries the background load; default <bank>-load")
    ] = None,
    idle: Annotated[bool, typer.Option(help="measure with no background load first")] = True,
    load: Annotated[bool, typer.Option(help="measure while a retain and a consolidation run")] = True,
    queries: Annotated[
        Path | None, typer.Option(help='JSON {"recall": [...], "reflect": [...]}; default: drawn from the bank')
    ] = None,
    budget: Annotated[
        Path | None, typer.Option(help="a budget file; default the package's latency_budget.json")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="write the full report, every sample included, as JSON")] = None,
) -> None:
    """Time live recall and reflect, idle and while a retain and a consolidation run, and check the
    50th and 95th percentiles against the recorded budget (specification 14; criterion 13).

    The load runs on a separate synthetic bank, so the measured bank is not changed. Exits 0 within
    the budget, 1 on any breach, including a failed call or load that was not running throughout.
    """
    import os

    import httpx
    from rich.console import Console
    from rich.table import Table

    from .gate import latency

    spec = latency.load_budget(budget)
    base = url or f"http://127.0.0.1:{os.environ.get('HINDSIGHT_API_PORT', '8888')}"

    async def work() -> latency.Report:
        async with httpx.AsyncClient(base_url=base, timeout=600) as client:
            return await latency.measure(
                client,
                bank,
                spec,
                idle=idle,
                load_bank=(load_bank or f"{bank}-load") if load else None,
                queries=latency.read_queries(queries) if queries else None,
            )

    report = asyncio.run(work())
    if out:
        out.write_text(report.model_dump_json(indent=1))
    table = Table(title=f"latency on {bank} at {base}, budget version {spec.version}")
    for column in ("phase", "call", "n", "errors", "p50 ms", "p95 ms", "max ms", "load running"):
        table.add_column(column)
    for phase in report.phases:
        for kind in ("recall", "reflect"):
            s = getattr(phase, kind)
            share = "" if phase.load_present is None else f"{phase.load_present:.0%}"
            table.add_row(
                phase.name, kind, str(s.count), str(s.errors), str(s.p50_ms), str(s.p95_ms), str(s.max_ms), share
            )
    console = Console()
    console.print(table)
    console.print(
        f"budget under load: recall p50 {spec.recall.p50_ms} / p95 {spec.recall.p95_ms} ms, reflect p50 "
        f"{spec.reflect.p50_ms} / p95 {spec.reflect.p95_ms} ms, recall p95 at most {spec.recall_p95_over_idle} times idle"
    )
    for breach in report.breaches:
        console.print(f"BREACH {breach}")
    console.print("within budget" if report.ok else "over budget")
    raise typer.Exit(0 if report.ok else 1)


migrate_app = typer.Typer(help="Migrate an existing bank (specification 13.4).", no_args_is_help=True)
app.add_typer(migrate_app, name="migrate")


@migrate_app.command("structure")
def migrate_structure_command(
    bank: Annotated[str, typer.Option(help="the bank to structure")],
    priority_subjects: Annotated[
        Path | None,
        typer.Option(help="JSON list of case-insensitive patterns; facts whose text or entities match go first"),
    ] = None,
    phase: Annotated[str, typer.Option(help="priority, rest or all")] = "all",
    concurrency: Annotated[
        int | None, typer.Option(min=1, help="documents in flight at once; default migrate.DEFAULT_CONCURRENCY")
    ] = None,
    retries: Annotated[int | None, typer.Option(min=0, help="retries of a failed call; default 3")] = None,
    backoff: Annotated[
        float | None, typer.Option(min=0, help="seconds before the first retry, doubling each time; default 30")
    ] = None,
    max_consecutive_failures: Annotated[
        int | None, typer.Option(min=1, help="failed calls in a row that stop the pass; default 3")
    ] = None,
    plan_only: Annotated[bool, typer.Option(help="print what is left to structure and exit")] = False,
    progress_file: Annotated[Path | None, typer.Option(help="append progress lines to this file")] = None,
) -> None:
    """Structure every valid fact without claims, document by document in statement-time order
    (specification 13.4 step 4). Resumable: rerun the same command to continue. Then run
    ``reconcile --all``. Exits 0 when the pass finished, 1 when it stopped (the reason is printed).
    """
    import sys
    from datetime import UTC, datetime

    from . import migrate

    if phase not in ("priority", "rest", "all"):
        raise typer.BadParameter("phase is priority, rest or all")
    patterns = migrate.compile_patterns(json.loads(priority_subjects.read_text())) if priority_subjects else []
    phases = ("priority", "rest") if phase == "all" else (phase,)

    def emit(line: str) -> None:
        typer.echo(line)
        if progress_file:
            with progress_file.open("a") as handle:
                handle.write(line + "\n")

    def progress(report: migrate.PassReport) -> None:
        eta = report.eta_seconds()
        emit(
            f"{datetime.now(UTC).astimezone():%Y-%m-%d %H:%M:%S} documents {report.documents_done} facts "
            f"{report.facts_answered}/{report.planned_facts} pending {report.facts_pending} claims {report.claims} "
            f"calls {report.calls} failed {report.failed_calls} | {report.facts_per_hour():.0f} facts/h, "
            f"remaining {report.remaining_facts()}, ETA {eta / 3600:.1f} h"
            if eta is not None
            else f"{datetime.now(UTC).astimezone():%Y-%m-%d %H:%M:%S} calls {report.calls} failed {report.failed_calls}"
        )

    async def work(engine: Any) -> Any:
        if plan_only:
            async with (await engine._get_pool()).acquire() as conn:
                return (await migrate.make_plan(conn, bank, patterns)).counts()
        runner = migrate.StructurePass(
            engine,
            bank,
            request_context=_context(),
            concurrency=migrate.DEFAULT_CONCURRENCY if concurrency is None else concurrency,
            retries=migrate.DEFAULT_RETRIES if retries is None else retries,
            backoff_seconds=migrate.DEFAULT_BACKOFF_SECONDS if backoff is None else backoff,
            max_consecutive_failures=migrate.DEFAULT_MAX_CONSECUTIVE_FAILURES
            if max_consecutive_failures is None
            else max_consecutive_failures,
            on_progress=progress,
        )
        return await runner.run(patterns=patterns, phases=phases)

    if not plan_only:
        emit(f"structure pass on {bank}, started {datetime.now(UTC).astimezone():%Y-%m-%d %H:%M:%S %Z}")
        emit("resume with: " + " ".join(sys.argv))
    result = asyncio.run(_with_engine(work))
    if plan_only:
        typer.echo(json.dumps(result, indent=1))
        return
    summary = result.summary()
    emit(json.dumps(summary, indent=1, default=str))
    raise typer.Exit(1 if result.stopped else 0)


@app.command("gate")
def gate_command(
    bank: Annotated[str, typer.Option(help="the bank the acceptance run and the latency check use")],
    url: Annotated[
        str | None, typer.Option(help="the server; default http://127.0.0.1:$HINDSIGHT_API_PORT (8888 when unset)")
    ] = None,
    suites: Annotated[
        str, typer.Option(help="comma-separated: deterministic, structuring, acceptance, latency")
    ] = "deterministic,structuring,acceptance,latency",
    questions: Annotated[Path | None, typer.Option(help="the questions file (default: the operating folder's)")] = None,
    keys: Annotated[Path | None, typer.Option(help="the answer keys (default: ranking/markers.json)")] = None,
    subjects: Annotated[
        Path | None, typer.Option(help='JSON {"Q01": ["subject", ...]}: where the current-state read starts')
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            help="window start for generated questions (ISO time); default the newest earlier gate run's time"
        ),
    ] = None,
    all_supersessions: Annotated[bool, typer.Option(help="ask about every supersession in the bank")] = False,
    reflect_sample: Annotated[
        int, typer.Option(min=0, help="generated questions also asked through reflect and judged (a seeded sample)")
    ] = 20,
    fixtures: Annotated[list[Path] | None, typer.Option(help="structuring fixtures; default the real fixtures")] = None,
    cold_runs: Annotated[int, typer.Option(help="structuring suite: runs from an empty catalog per fixture")] = 2,
    load_bank: Annotated[str | None, typer.Option(help="latency: the synthetic load bank; default <bank>-load")] = None,
    budget: Annotated[Path | None, typer.Option(help="latency: a budget file; default the package's")] = None,
    out_dir: Annotated[Path | None, typer.Option(help="where the table and summary go; default ranking/")] = None,
) -> None:
    """Run the evaluation gate (specification 11): the deterministic suite, the structuring suite, the
    acceptance run with the strict scorer, generated questions and the reflect judge, and the latency
    assertion. Writes gate-<time>.md and gate-<time>.json to the operating folder's ranking/ (status
    reports the newest), prints the table, and exits 0 only when every suite ran and passed.

    The acceptance run reads the ledger through the engine (source the profile of the server's
    database first) and asks the server over HTTP.
    """
    import os
    from datetime import datetime

    import httpx

    from .gate import acceptance, latency, runner, strict
    from .gate import questions as generator
    from .gate.judge import engine_judge

    chosen = [name.strip() for name in suites.split(",") if name.strip()]
    unknown = set(chosen) - set(runner.SUITES)
    if unknown:
        raise typer.BadParameter(f"unknown suites: {', '.join(sorted(unknown))}")
    base = url or f"http://127.0.0.1:{os.environ.get('HINDSIGHT_API_PORT', '8888')}"
    inputs = runner.default_inputs() | {
        name: path for name, path in (("questions", questions), ("keys", keys), ("subjects", subjects)) if path
    }
    folder = out_dir or runner.gate_dir()
    report = runner.GateReport(
        ran_at=runner.now(), release=runner.release(), bank=bank, url=base, suites={}, details={"inputs": inputs}
    )

    def say(text: str) -> None:
        typer.echo(text, err=True)

    if "deterministic" in chosen:
        say("suite 1: deterministic")
        report.suites["deterministic"] = runner.run_deterministic()
    if "structuring" in chosen:
        say("suite 2: structuring")
        report.suites["structuring"] = _structuring_suite(fixtures, cold_runs)
    if "acceptance" in chosen:
        say("suite 3: acceptance")
        window = datetime.fromisoformat(since) if since else None
        if window is None and not all_supersessions:
            previous = runner.newest_run(folder)
            if previous and previous.get("bank") == bank:
                window = datetime.fromisoformat(previous["ran_at"])
        report.details["window_since"] = window

        async def accept(engine: Any) -> runner.SuiteResult:
            keyset = strict.load_keys(json.loads(inputs["keys"].read_text()))
            asked = acceptance.load_questions(json.loads(inputs["questions"].read_text()))
            subject_terms = json.loads(inputs["subjects"].read_text())
            async with (await engine._get_pool()).acquire() as conn:
                supersessions, claims = await generator.load(conn, bank, window)
            made = generator.generate(supersessions, claims)
            reflect_ids = {
                q.id for q in generator.sample(made.questions, reflect_sample, seed=f"{bank}:{report.ran_at:%Y-%m-%d}")
            }
            judge = engine_judge(engine)
            async with httpx.AsyncClient(base_url=base, timeout=120) as client:
                server = acceptance.Server(client, bank)
                ten = await acceptance.ask_ten(
                    server, judge, asked, keyset, subject_terms, on_result=lambda r: say(f"  {r.id} done")
                )
                gen = await acceptance.ask_generated(server, judge, made.questions, reflect_ids)
            ten_rows = [asdict(r) for r in ten]
            gen_rows = [asdict(g) for g in gen]
            report.details |= {
                "ten": ten_rows,
                "generated": gen_rows,
                "generation": {
                    "supersessions": made.supersessions,
                    "questions": len(made.questions),
                    "skipped": made.skipped,
                },
                "judge": {"model": judge.name},
            }
            summary = runner.acceptance_verdict(ten_rows, gen_rows)
            note = (
                f"ten questions: read {summary['read_strict']}/{summary['ten']} strict ({summary['read_spec']} by the "
                f"specification's reading), recall {summary['recall_strict']}/{summary['ten']} strict "
                f"({summary['recall_loose']} loose), reflect {summary['reflect_correct']}/{summary['ten']}; generated "
                f"({made.supersessions} supersessions, skipped {made.skipped}): read {summary['generated_read']}/"
                f"{summary['generated']}, recall {summary['generated_recall_strict']}/{summary['generated']} strict, "
                f"reflect {summary['generated_reflect_correct']}/{summary['generated_reflected']}; hard criteria "
                f"{'pass' if summary['hard_criteria_pass'] else 'FAIL'}; recall "
                f"{'passes' if summary['recall_all_strict'] else 'fails: HSIGHT-11 before cut-over'}"
            )
            return runner.SuiteResult(
                "acceptance", ran=True, passed=summary["hard_criteria_pass"], summary=summary, note=note
            )

        try:
            report.suites["acceptance"] = asyncio.run(_with_engine(accept))
        except Exception as error:
            report.suites["acceptance"] = runner.SuiteResult(
                "acceptance", ran=True, passed=False, note=f"{type(error).__name__}: {error}"
            )
    if "latency" in chosen:
        say("latency")
        spec = latency.load_budget(budget)

        async def timed() -> latency.Report:
            async with httpx.AsyncClient(base_url=base, timeout=600) as client:
                return await latency.measure(client, bank, spec, idle=True, load_bank=load_bank or f"{bank}-load")

        try:
            measured = asyncio.run(timed())
            phases = {
                phase.name: {
                    kind: {
                        "p50_ms": getattr(phase, kind).p50_ms,
                        "p95_ms": getattr(phase, kind).p95_ms,
                        "errors": getattr(phase, kind).errors,
                    }
                    for kind in ("recall", "reflect")
                }
                | {"load_present": phase.load_present}
                for phase in measured.phases
            }
            report.details["latency"] = {
                "budget_version": spec.version,
                "phases": phases,
                "breaches": measured.breaches,
            }
            note = f"budget version {spec.version}: " + (
                "within budget" if measured.ok else "; ".join(measured.breaches)
            )
            report.suites["latency"] = runner.SuiteResult(
                "latency",
                ran=True,
                passed=measured.ok,
                summary={"phases": phases, "breaches": measured.breaches},
                note=note,
            )
        except Exception as error:
            report.suites["latency"] = runner.SuiteResult(
                "latency", ran=True, passed=False, note=f"{type(error).__name__}: {error}"
            )
    for name in runner.SUITES:
        report.suites.setdefault(name, runner.SuiteResult(name, ran=False, note="not selected"))
    table, summary_path = runner.write(report, folder)
    typer.echo(runner.render_table(report))
    typer.echo(f"table: {table}\nsummary: {summary_path}")
    raise typer.Exit(0 if report.passed else 1)


def _structuring_suite(fixtures: list[Path] | None, cold_runs: int) -> Any:
    from .gate.runner import SuiteResult
    from .structuring.fixtures import load_fixture, load_fixtures, real_fixtures_dir
    from .structuring.suite import PRECISION_TARGET, engine_retain_model, run_suite

    paths = fixtures or ([real_fixtures_dir()] if real_fixtures_dir() else [])
    if not paths:
        return SuiteResult("structuring", ran=False, passed=False, note="no fixtures (HINDSIGHT_CORTANA_REAL_FIXTURES)")
    try:
        loaded = []
        for path in paths:
            loaded.extend(load_fixtures(path) if path.is_dir() else [load_fixture(path)])
        result = asyncio.run(run_suite(loaded, engine_retain_model(), cold_runs=cold_runs, warm=True, out=None))
    except Exception as error:
        return SuiteResult("structuring", ran=True, passed=False, note=f"{type(error).__name__}: {error}")
    summary = result["summary"]
    runs = "; ".join(
        f"{label} precision {run['precision']:.3f}, recall {run['recall']:.3f}"
        for label, run in summary["runs"].items()
    )
    stability = "; ".join(f"{label} {s['same']}/{s['compared']}" for label, s in summary["stability"].items())
    note = f"target precision {PRECISION_TARGET}: {'met' if summary['meets_target'] else 'NOT met'} ({runs}; key stability {stability})"
    return SuiteResult("structuring", ran=True, passed=bool(summary["meets_target"]), summary=summary, note=note)
