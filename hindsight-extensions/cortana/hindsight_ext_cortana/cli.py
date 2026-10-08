"""``hindsight-cortana``: the operator's command for this package.

``verify-base`` and ``suite structuring`` (HSIGHT-4) are here. ``reconcile`` (HSIGHT-5), ``gate`` (HSIGHT-8) and ``status``
(HSIGHT-7) are added by the issues that build them.
"""

from pathlib import Path
from typing import Annotated

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
    import asyncio

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
