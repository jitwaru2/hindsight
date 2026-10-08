"""The distribution is what specification 13.1 installs: an installable package whose dependencies
are the engine's own plus a short, named list, with the ``hindsight-cortana`` command."""

from importlib.metadata import entry_points, requires

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _names(distribution: str) -> set[str]:
    return {
        canonicalize_name(Requirement(spec).name) for spec in requires(distribution) or [] if "extra ==" not in spec
    }


# Dependencies the engine does not carry, each adopted for a job a library does (the engineering
# standard: adopt before building). Production's tool environment installs them beside the engine.
ADDED_DEPENDENCIES = {
    "python-slugify",  # attribute keys: lowercase, ASCII, hyphenated (structuring.validation)
}


def test_every_dependency_is_an_engine_dependency_or_a_named_addition():
    ours = _names("hindsight-ext-cortana")
    assert ours, "the package declares what it imports"
    assert ours - ADDED_DEPENDENCIES <= _names("hindsight-api-slim"), ours - _names("hindsight-api-slim")
    assert ADDED_DEPENDENCIES <= ours, "a named addition the package no longer declares"


def test_the_engine_is_not_a_runtime_dependency():
    assert "hindsight-api-slim" not in _names("hindsight-ext-cortana")


def test_the_console_command_is_declared():
    (command,) = entry_points(group="console_scripts", name="hindsight-cortana")
    assert command.value == "hindsight_ext_cortana.cli:app"
