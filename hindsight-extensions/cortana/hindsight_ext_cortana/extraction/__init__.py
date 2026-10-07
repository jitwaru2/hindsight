"""Atomic extraction: the versioned instructions the bank's fact extraction runs under, and the
bank configuration that applies them (specification 5.1).

The engine's ``custom`` extraction mode puts ``retain_custom_instructions`` in place of its concise
guidelines and keeps the rest of its prompt and its output schema. ``instructions.md`` requires one
claim per fact, the subject named, the value in full, a state in the moment worded as provisional, a
resolution worded as a resolution, and the final state of whatever changes. It names no person: the
bank's retain missions say whose memory it is.

A change to ``instructions.md`` is a release (specification 11): raise ``VERSION``, re-pin the hash
in ``tests/test_extraction.py``, re-cut the structuring fixtures, and run the gate.
"""

from collections.abc import Mapping
from importlib.resources import files
from typing import Any

VERSION = "1"

# The modes in which the engine's extractor writes facts with a model. A strategy that sets one
# of these overrides the bank's mode (``config_resolver.apply_strategy``), so it must be switched
# to ``custom`` too; ``chunks`` and ``verbatim`` store text as given and are left alone.
_MODEL_EXTRACTION_MODES = frozenset({"concise", "verbose", "custom"})


def instructions() -> str:
    """The instructions, as the bank's ``retain_custom_instructions``."""
    return files(__name__).joinpath("instructions.md").read_text(encoding="utf-8")


def bank_config_updates(retain_strategies: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """The updates for ``PATCH /v1/default/banks/{bank_id}/config`` that apply the instructions.

    Sets ``custom`` mode and the instructions on the bank, and ``custom`` on every retain strategy
    that names a model extraction mode of its own. ``retain_strategies`` is the bank's current value;
    the engine replaces it whole, so the strategies come back complete, with only their mode changed.

    Raises ``ValueError`` for a strategy that carries its own ``retain_custom_instructions``: it would
    extract under different rules from every other retain, and all data is extracted alike
    (specification 3, principle 8).
    """
    updates: dict[str, Any] = {
        "retain_extraction_mode": "custom",
        "retain_custom_instructions": instructions(),
    }
    if retain_strategies is None:
        return updates
    strategies: dict[str, dict[str, Any]] = {}
    for name, strategy in retain_strategies.items():
        if "retain_custom_instructions" in strategy:
            raise ValueError(f"retain strategy {name!r} sets its own retain_custom_instructions")
        strategies[name] = dict(strategy)
        if strategy.get("retain_extraction_mode") in _MODEL_EXTRACTION_MODES:
            strategies[name]["retain_extraction_mode"] = "custom"
    updates["retain_strategies"] = strategies
    return updates
