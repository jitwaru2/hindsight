"""Structuring: the versioned prompt, the one model call per retain batch through the engine's
provider, and the validation of its answer into claims (specification 5.2).

- ``prompt.md`` is the structuring prompt, sent as the system prompt; ``batching.render`` builds
  each call's message. ``VERSION`` covers both, and it and the model are written on every claim. A
  change to either is a release (specification 11): raise ``VERSION``, re-pin the hash in
  ``tests/test_structuring_batching.py``, and run the structuring suite
  (``hindsight-cortana suite structuring``).
- ``batching`` packs a retain's new facts into model calls and renders each call's message.
- ``validation`` turns the model's answer into claim rows in code: subject resolution, key
  normalization, unaligned keys and ``same_as``, the earlier-state markers, the statement-time
  tuple and the content hash.
- ``runner`` drives batches against a ``Store`` (where the catalog, the subjects and the claims
  live) and a ``StructuringModel`` (who answers); ``engine`` provides both over the engine's
  database and its retain provider, which is what ``on_retain_complete`` runs.
- ``suite`` scores the step on the structuring fixtures (``fixtures``) with real model calls.
"""

from importlib.resources import files

VERSION = "8"


def prompt() -> str:
    """The structuring prompt, sent as the system prompt of every structuring call."""
    return files(__name__).joinpath("prompt.md").read_text(encoding="utf-8")
