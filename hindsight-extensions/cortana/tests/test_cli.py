"""``hindsight-cortana verify-base`` runs the HSIGHT-1 check against a synthetic wheel."""

import base64
import hashlib
from pathlib import Path

from typer.testing import CliRunner

from hindsight_ext_cortana.cli import app

FILES = {"__init__.py": "VALUE = 1\n", "sub/module.py": "def f():\n    return 2\n"}


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _install(root: Path) -> tuple[Path, Path]:
    """A wheel installed into site-packages, its RECORD, and a checkout holding the same files.

    The installed copy matters: importlib.metadata (Python 3.12 and later) leaves out RECORD rows
    whose installed file is missing.
    """
    rows = []
    for name, text in FILES.items():
        for tree in ("site-packages", "checkout"):
            path = root / tree / "demo" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        rows.append(f"demo/{name},{_record_hash(text.encode())},{len(text.encode())}")
    package = root / "checkout" / "demo"
    dist_info = root / "site-packages" / "demo-1.0.dist-info"
    dist_info.mkdir(parents=True)
    rows.append("demo-1.0.dist-info/RECORD,,")
    record = dist_info / "RECORD"
    record.write_text("\n".join(rows) + "\n")
    return record, package


def test_matching_checkout_exits_zero(tmp_path):
    record, package = _install(tmp_path)
    result = CliRunner().invoke(app, ["verify-base", str(record), str(package)])
    assert result.exit_code == 0, result.output
    assert "matching:   2 of 2" in result.output
    assert "result: OK" in result.output


def test_changed_file_exits_one_and_names_it(tmp_path):
    record, package = _install(tmp_path)
    (package / "sub" / "module.py").write_text("def f():\n    return 3\n")
    result = CliRunner().invoke(app, ["verify-base", str(record), str(package)])
    assert result.exit_code == 1, result.output
    assert "mismatched: demo/sub/module.py" in result.output
    assert "result: DIFFERS" in result.output


def test_a_path_that_is_not_a_record_is_a_usage_error(tmp_path):
    _, package = _install(tmp_path)
    result = CliRunner().invoke(app, ["verify-base", str(package / "__init__.py"), str(package)])
    assert result.exit_code == 2


def test_reconcile_needs_exactly_one_scope():
    result = CliRunner().invoke(app, ["reconcile", "--bank", "b", "--all", "--document", "d"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_status_and_retrievals_sweep_are_commands():
    for command in (["status", "--help"], ["retrievals", "sweep", "--help"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 0, result.output


def test_retrievals_sweep_refuses_a_retention_under_one_day():
    result = CliRunner().invoke(app, ["retrievals", "sweep", "--days", "0"])
    assert result.exit_code == 2
