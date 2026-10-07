"""``hindsight-cortana``: the operator's command for this package.

``verify-base`` is here now. ``reconcile`` (HSIGHT-5), ``gate`` (HSIGHT-8) and ``status``
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
