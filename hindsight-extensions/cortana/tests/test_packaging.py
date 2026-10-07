"""The distribution is what specification 13.1 installs: an installable package whose dependencies
are all the engine's own, with the ``hindsight-cortana`` command."""

from importlib.metadata import entry_points, requires

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _names(distribution: str) -> set[str]:
    return {
        canonicalize_name(Requirement(spec).name) for spec in requires(distribution) or [] if "extra ==" not in spec
    }


def test_every_dependency_is_already_an_engine_dependency():
    ours = _names("hindsight-ext-cortana")
    assert ours, "the package declares what it imports"
    assert ours <= _names("hindsight-api-slim"), ours - _names("hindsight-api-slim")


def test_the_engine_is_not_a_runtime_dependency():
    assert "hindsight-api-slim" not in _names("hindsight-ext-cortana")


def test_the_console_command_is_declared():
    (command,) = entry_points(group="console_scripts", name="hindsight-cortana")
    assert command.value == "hindsight_ext_cortana.cli:app"
