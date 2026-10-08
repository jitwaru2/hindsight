"""``hindsight-cortana``: the operator's command for this package.

``verify-base``, ``suite structuring`` (HSIGHT-4), ``reconcile``, ``merge`` and ``unmerge``
(HSIGHT-5), ``status`` and ``retrievals sweep`` (HSIGHT-7), and ``latency`` (HSIGHT-9) are here.
``gate`` (HSIGHT-8) is added by the issue that builds it.

``latency`` talks to a running server over HTTP; it does not open the engine.

``reconcile``, ``merge``, ``unmerge``, ``status`` and ``retrievals sweep`` open the engine in this process the way the engine's
``hindsight-worker`` does: from the ``HINDSIGHT_API_*`` environment (source the profile first),
with the tenant and operation-hooks extensions loaded and no migrations run. Work the engine queues
(consolidation, graph maintenance, mental-model refreshes) is written to its operations table for
the server's worker, not run here.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
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
        report = await reconcile(engine, bank, subject=subject_id, document=document, request_context=_context())
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
