"""The strict scorer for the acceptance run (specification 11 item 3).

An answer key gives, per question, regular expressions over lower-cased text: ``topic`` (the item is
on the question's subject), ``cur`` (it states the current position), and for some questions an old
position, either ``o_re`` (any on-topic item matching it states the old position) or ``pos`` with a
date ``T`` (an on-topic item matching it whose knowledge horizon, the latest date in its text or its
event date, is before ``T`` and which does not say it is superseded); ``c2`` is a second point the
answer must also carry. Labels follow the earlier scorer exactly: ``C`` current, ``O`` old, ``N`` on
topic but neither, ``-`` off topic.

Three parts, each scored separately:

- **Current-state read.** The read's standing claims on the question's subjects (each key's current
  claim, and both claims of a key in conflict) are labelled, each as its subject, value, fact text
  and statement date. The spec reading passes when one is ``C``. The strict reading also requires
  that no standing claim is ``O``: a stale position still standing on another key is the failure the
  read exists to remove. The gate's verdict for the read is the strict reading; both are reported.
- **Plain recall.** The loose reading (``exp.py``) passes when the first result is ``C``. The strict
  reading passes only when the first result is ``C`` and does not also match the old-position pattern
  (``o_re``, else ``pos``): a first result that states both positions leaves the reader to choose.
- **Reflect.** Judged by a model against the key (``judge``); this module builds the rubric.

``exp.py`` itself stays untouched in the operating folder for comparison.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Label = Literal["C", "O", "N", "-"]

MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
DATES = re.compile(r"(20\d\d)-(\d\d)-(\d\d)|(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:,?\s+(20\d\d))?", re.IGNORECASE)
# exp.py's default year for a month and day with no year.
DEFAULT_YEAR = 2026


@dataclass(frozen=True)
class AnswerKey:
    """One question's key, as in ``markers.json``."""

    id: str
    topic: str
    cur: str
    T: str | None = None
    pos: str | None = None
    o_re: str | None = None
    c2: str | None = None

    @classmethod
    def from_json(cls, question_id: str, data: dict[str, Any]) -> "AnswerKey":
        return cls(
            id=question_id,
            topic=data["topic"],
            cur=data["cur"],
            T=data.get("T"),
            pos=data.get("pos"),
            o_re=data.get("o_re"),
            c2=data.get("c2"),
        )

    @property
    def old(self) -> str | None:
        """The old-position pattern: ``o_re`` when set, else ``pos``."""
        return self.o_re or self.pos


def load_keys(data: dict[str, Any]) -> dict[str, AnswerKey]:
    return {qid: AnswerKey.from_json(qid, value) for qid, value in data.items() if not qid.startswith("_")}


def horizon(text: str, event_date: str | None) -> str | None:
    """The latest date the item knows of: in its text, or its event date (ISO dates as strings)."""
    found = []
    for match in DATES.finditer(text):
        try:
            if match.group(1):
                found.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
            else:
                year = int(match.group(6) or DEFAULT_YEAR)
                found.append(date(year, MONTHS[match.group(4).lower()], int(match.group(5))).isoformat())
        except ValueError:
            pass
    if event_date:
        found.append(event_date[:10])
    return max(found) if found else None


def label(key: AnswerKey, text: str, event_date: str | None) -> Label:
    """``exp.py``'s label for one item."""
    lowered = text.lower()
    if not re.search(key.topic, lowered):
        return "-"
    if re.search(key.cur, lowered):
        return "C"
    if key.o_re:
        return "O" if re.search(key.o_re, lowered) else "N"
    if key.T and key.pos and re.search(key.pos, lowered) and "supersed" not in lowered and "historical" not in lowered:
        known = horizon(text, event_date)
        if known and known < key.T:
            return "O"
    return "N"


def states_old_too(key: AnswerKey, text: str) -> bool:
    """Whether an item also matches the old-position pattern (the strict reading's exclusion)."""
    return bool(key.old and re.search(key.old, text.lower()))


@dataclass(frozen=True)
class Item:
    """A recall result or a claim, as the scorer sees it."""

    text: str
    event_date: str | None = None
    ref: str | None = None


@dataclass
class RecallScore:
    labels: str
    first: Label | None
    loose: bool
    strict: bool
    first_c: int | None
    old_above_first_c: int
    c2_rank: int | None
    first_states_old_too: bool
    first_ref: str | None = None
    first_text: str | None = None


def score_recall(key: AnswerKey, items: Sequence[Item], *, show: int = 10) -> RecallScore:
    """Score recall's results: loose (first result ``C``) and strict (``C`` and not also old)."""
    shown = list(items)[:show]
    labels = [label(key, item.text, item.event_date) for item in shown]
    first = labels[0] if labels else None
    first_c = next((rank for rank, value in enumerate(labels, start=1) if value == "C"), None)
    above = labels[: (first_c - 1) if first_c else len(labels)]
    c2_rank = None
    if key.c2:
        c2_rank = next(
            (
                rank
                for rank, item in enumerate(shown, start=1)
                if re.search(key.topic, item.text.lower()) and re.search(key.c2, item.text.lower())
            ),
            None,
        )
    old_too = bool(shown) and states_old_too(key, shown[0].text)
    return RecallScore(
        labels="".join(labels),
        first=first,
        loose=first == "C",
        strict=first == "C" and not old_too,
        first_c=first_c,
        old_above_first_c=above.count("O"),
        c2_rank=c2_rank,
        first_states_old_too=old_too,
        first_ref=shown[0].ref if shown else None,
        first_text=shown[0].text if shown else None,
    )


@dataclass
class ReadScore:
    """The current-state read's standing claims, labelled."""

    standing: int
    current: list[Item] = field(default_factory=list)
    old: list[Item] = field(default_factory=list)

    @property
    def spec(self) -> bool:
        """Specification 11 item 3 as written: a standing claim states the current position."""
        return bool(self.current)

    @property
    def strict(self) -> bool:
        """And no standing claim states an old position."""
        return bool(self.current) and not self.old


def claim_item(
    subject: str, attribute: str, value: str, text: str | None, stated_at: datetime | str | None, ref: str
) -> Item:
    """A claim as the scorer reads it: its subject, key, value, fact text and statement date."""
    when = stated_at.date().isoformat() if isinstance(stated_at, datetime) else (stated_at or "")[:10] or None
    words = f"{subject} / {attribute}: {value}. {text or ''}"
    if when:
        words += f" (stated {when})"
    return Item(text=words, event_date=when, ref=ref)


def score_read(key: AnswerKey, claims: Iterable[Item]) -> ReadScore:
    score = ReadScore(standing=0)
    for item in claims:
        score.standing += 1
        verdict = label(key, item.text, item.event_date)
        if verdict == "C":
            score.current.append(item)
        elif verdict == "O":
            score.old.append(item)
    return score


@dataclass(frozen=True)
class Rubric:
    """What the reflect judge checks an answer against."""

    current: str
    old: str | None = None
    second: str | None = None
    patterns: bool = True


def key_rubric(key: AnswerKey) -> Rubric:
    """A key's patterns as the judge's rubric (regular expressions over the answer's wording)."""
    old = key.o_re or key.pos
    if old and key.pos and not key.o_re and key.T:
        old = f"{old} (as a position held before {key.T})"
    return Rubric(current=key.cur, old=old, second=key.c2, patterns=True)


__all__ = [
    "AnswerKey",
    "Item",
    "Label",
    "ReadScore",
    "RecallScore",
    "Rubric",
    "claim_item",
    "horizon",
    "key_rubric",
    "label",
    "load_keys",
    "score_read",
    "score_recall",
    "states_old_too",
]
