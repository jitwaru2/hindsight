"""The turns of a saved session chunk.

The plugin saves a session as newline-delimited JSON objects, one turn per line, each with ``role``,
``content`` and, from plugin 0.8.0 on, the transcript's ``timestamp`` (``transcript.ts``). The
engine chunks it at whole-turn boundaries. A chunk that is not in that form (an older save, a JSON
array, plain text) has no turns, and its claims take the fallback statement time.
"""

import json
from datetime import datetime


def parse_turns(text: str) -> list[tuple[str, datetime | None, str]]:
    """The chunk's turns as (role, timestamp, content), or ``[]`` when it is not a turn chunk."""
    turns = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            turn = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
            return []
        turns.append((str(turn["role"]), _timestamp(turn.get("timestamp")), str(turn["content"])))
    return turns


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None
