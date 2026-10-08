"""Suite 3, the acceptance run (specification 11 item 3), against a running server.

The ten questions come from the operating folder (the questions file, the answer keys, and the
question subjects the current-state read starts from; none of them is in the fork). Each is asked
three ways, each scored separately (``strict``):

- the current-state read: the subjects route for each of the question's subjects, then the current
  route for every subject it finds; the standing claims are labelled;
- plain recall: the engine's recall as a session calls it (the question, ``budget`` mid, the server's
  default window), and again with the question's tag filter when the questions file has a filtered
  variant; the first result is labelled;
- reflect: the engine's reflect with the question, judged by the model against the key.

Generated questions (``questions``) are asked the same three ways. Their read must return the
expected claim as the key's current claim; recall's first result is classed by the claims of its
fact (``C`` when the fact carries the key's current value, ``O`` when it carries a superseded value
on the key), or by its text when it is not a structured fact (an observation); reflect is judged on
a reproducible sample, because each reflect costs tens of seconds of model time.
"""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from ..rules import same_value
from .judge import Judge, Verdict
from .questions import GeneratedQuestion
from .strict import AnswerKey, Item, ReadScore, RecallScore, Rubric, claim_item, key_rubric, score_read, score_recall

SUBJECT_MATCHES = 10
REFLECT_TIMEOUT_S = 330  # the plugin's reflect tool timeout (HSIGHT-9's budget file names it)


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    tags: tuple[str, ...] = ()
    tags_match: str | None = None

    @property
    def base(self) -> str:
        return self.id[:-1] if self.id.endswith("f") else self.id

    @property
    def filtered(self) -> bool:
        return bool(self.tags)


def load_questions(data: Sequence[dict[str, Any]]) -> list[Question]:
    return [
        Question(id=q["id"], query=q["query"], tags=tuple(q.get("tags") or ()), tags_match=q.get("tags_match"))
        for q in data
    ]


class Server:
    """The routes the acceptance run uses, for one bank."""

    def __init__(self, client: httpx.AsyncClient, bank: str):
        self.client, self.bank = client, bank
        self.api = f"/v1/default/banks/{bank}"

    async def _json(self, response: httpx.Response) -> Any:
        response.raise_for_status()
        return response.json()

    async def recall(self, query: str, tags: Sequence[str] = (), tags_match: str | None = None) -> list[dict]:
        body: dict[str, Any] = {"query": query, "budget": "mid"}
        if tags:
            body |= {"tags": list(tags), "tags_match": tags_match or "any"}
        return (await self._json(await self.client.post(f"{self.api}/memories/recall", json=body)))["results"]

    async def reflect(self, query: str) -> str:
        response = await self.client.post(f"{self.api}/reflect", json={"query": query}, timeout=REFLECT_TIMEOUT_S)
        return (await self._json(response)).get("text") or ""

    async def subjects(self, text: str) -> list[dict]:
        params = {"bank_id": self.bank, "q": text, "limit": SUBJECT_MATCHES}
        return (await self._json(await self.client.get("/ext/cortana/subjects", params=params)))["items"]

    async def current(self, subject: str, attribute: str | None = None) -> dict:
        params = {"bank_id": self.bank, "subject": subject} | ({"attribute": attribute} if attribute else {})
        return await self._json(await self.client.get("/ext/cortana/current", params=params))

    async def fact(self, fact_id: str) -> dict | None:
        response = await self.client.get(f"/ext/cortana/facts/{fact_id}", params={"bank_id": self.bank})
        if response.status_code == 404:
            return None
        return await self._json(response)


def standing_claims(state: dict) -> list[dict]:
    """The claims a current-state read reports as standing: each key's current claim, and both claims
    of a key in conflict."""
    out = []
    for key in state.get("keys", []):
        if key["status"] == "current" and key.get("current"):
            out.append(key["current"])
        elif key["status"] == "conflict":
            out.extend(key.get("conflict") or [])
    return out


def _claim(claim: dict) -> Item:
    return claim_item(
        claim["subject"],
        claim["attribute"],
        claim["value"],
        claim.get("text"),
        claim.get("stated_at"),
        ref=f"{claim['subject']} / {claim['attribute']} (claim {claim['claim_id']})",
    )


def _result(result: dict) -> Item:
    return Item(
        text=result.get("text") or "",
        event_date=result.get("occurred_start") or result.get("mentioned_at"),
        ref=f"{result.get('type')} {result.get('id')}",
    )


@dataclass
class ReflectOutcome:
    answer: str | None = None
    verdict: dict | None = None
    error: str | None = None

    @property
    def correct(self) -> bool:
        return bool(self.verdict and self.verdict["correct"])


@dataclass
class TenResult:
    id: str
    query: str
    subjects_searched: list[str]
    subjects_read: list[str]
    read: dict
    recall: dict | None
    recall_filtered: dict | None
    reflect: ReflectOutcome
    errors: list[str] = field(default_factory=list)

    @property
    def read_strict(self) -> bool:
        return bool(self.read.get("strict"))

    @property
    def recall_strict(self) -> bool:
        return bool(self.recall and self.recall["strict"])


def _read_dict(score: ReadScore) -> dict:
    return {
        "spec": score.spec,
        "strict": score.strict,
        "standing": score.standing,
        "current": [asdict(item) for item in score.current],
        "old": [asdict(item) for item in score.old],
    }


async def _reflect(server: Server, judge: Judge, query: str, rubric: Rubric) -> ReflectOutcome:
    outcome = ReflectOutcome()
    try:
        outcome.answer = await server.reflect(query)
    except Exception as error:
        outcome.error = f"reflect failed: {type(error).__name__}: {error}"
        return outcome
    try:
        verdict: Verdict = await judge(query, outcome.answer, rubric)
        outcome.verdict = verdict.model_dump()
    except Exception as error:
        outcome.error = f"judge failed: {type(error).__name__}: {error}"
    return outcome


async def ask_ten(
    server: Server,
    judge: Judge,
    questions: Sequence[Question],
    keys: dict[str, AnswerKey],
    subjects: dict[str, list[str]],
    *,
    on_result: Callable[[TenResult], None] | None = None,
) -> list[TenResult]:
    """The ten questions: read, recall (plain and filtered) and reflect, each scored."""
    by_id = {q.id: q for q in questions}
    results = []
    for qid in sorted(keys):
        question = by_id.get(qid)
        if question is None:
            continue
        key = keys[qid]
        errors: list[str] = []
        read_ids: dict[str, str] = {}
        for term in subjects.get(qid, []):
            try:
                for match in await server.subjects(term):
                    read_ids.setdefault(str(match["id"]), match["name"])
            except Exception as error:
                errors.append(f"subjects {term!r}: {type(error).__name__}: {error}")
        claims: list[Item] = []
        for subject_id in read_ids:
            try:
                claims.extend(_claim(claim) for claim in standing_claims(await server.current(subject_id)))
            except Exception as error:
                errors.append(f"current {read_ids[subject_id]!r}: {type(error).__name__}: {error}")
        read = score_read(key, claims)

        recall: RecallScore | None = None
        filtered: RecallScore | None = None
        try:
            recall = score_recall(key, [_result(r) for r in await server.recall(question.query)])
        except Exception as error:
            errors.append(f"recall: {type(error).__name__}: {error}")
        variant = by_id.get(f"{qid}f")
        if variant is not None and variant.filtered:
            try:
                found = await server.recall(variant.query, variant.tags, variant.tags_match)
                filtered = score_recall(key, [_result(r) for r in found])
            except Exception as error:
                errors.append(f"recall {variant.id}: {type(error).__name__}: {error}")
        result = TenResult(
            id=qid,
            query=question.query,
            subjects_searched=subjects.get(qid, []),
            subjects_read=sorted(read_ids.values()),
            read=_read_dict(read),
            recall=asdict(recall) if recall else None,
            recall_filtered=asdict(filtered) if filtered else None,
            reflect=await _reflect(server, judge, question.query, key_rubric(key)),
            errors=errors,
        )
        if on_result:
            on_result(result)
        results.append(result)
    return results


@dataclass
class GeneratedResult:
    id: str
    question: str
    subject: str
    attribute: str
    expected: str
    superseded: list[str]
    read: bool
    read_detail: str
    recall_first: str | None
    recall_strict: bool
    recall_detail: str
    reflect: ReflectOutcome | None


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".")


async def classify_first(server: Server, q: GeneratedQuestion, first: dict | None) -> tuple[str | None, str]:
    """``C``, ``O`` or ``N`` for recall's first result on a generated question, with how it was read."""
    if first is None:
        return None, "no results"
    fact = await server.fact(str(first["id"])) if first.get("type") in ("world", "experience") else None
    on_key = [
        c
        for c in (fact or {}).get("claims", [])
        if str(c["subject_id"]) == str(q.subject_id) and c["attribute"] == q.attribute
    ]
    if on_key:
        if any(c["state"] in ("current", "provisional") and same_value(c["value"], q.expected.value) for c in on_key):
            return "C", f"fact {first['id']} carries the current value"
        if any(c["state"] == "superseded" for c in on_key):
            return "O", f"fact {first['id']} carries a superseded claim on the key"
        return "N", f"fact {first['id']} has another claim on the key"
    text = _norm(first.get("text") or "")
    has_current = _norm(q.expected.value) in text
    has_old = any(_norm(c.value) in text for c in q.superseded)
    how = f"{first.get('type')} {first['id']} read by text"
    if has_current and not has_old:
        return "C", how
    if has_old:
        return "O" if not has_current else "C+O", how
    return "N", how


async def ask_generated(
    server: Server,
    judge: Judge,
    questions: Sequence[GeneratedQuestion],
    reflect_ids: set[str],
    *,
    on_result: Callable[[GeneratedResult], None] | None = None,
) -> list[GeneratedResult]:
    results = []
    for q in questions:
        try:
            state = await server.current(str(q.subject_id), q.attribute)
            key = next((k for k in state.get("keys", []) if k["attribute"] == q.attribute), None)
            current = (key or {}).get("current") or {}
            read = bool(key and key["status"] == "current" and str(current.get("claim_id")) == str(q.expected.claim_id))
            read_detail = f"status {key['status'] if key else 'no key'}, current {current.get('value')!r}"
        except Exception as error:
            read, read_detail = False, f"current failed: {type(error).__name__}: {error}"
        try:
            found = await server.recall(q.question)
            label, how = await classify_first(server, q, found[0] if found else None)
        except Exception as error:
            label, how = None, f"recall failed: {type(error).__name__}: {error}"
        reflect = None
        if q.id in reflect_ids:
            rubric = Rubric(
                current=q.expected.value, old="; ".join(c.value for c in q.superseded) or None, patterns=False
            )
            reflect = await _reflect(server, judge, q.question, rubric)
        result = GeneratedResult(
            id=q.id,
            question=q.question,
            subject=q.subject,
            attribute=q.attribute,
            expected=q.expected.value,
            superseded=[c.value for c in q.superseded],
            read=read,
            read_detail=read_detail,
            recall_first=label,
            recall_strict=label == "C",
            recall_detail=how,
            reflect=reflect,
        )
        if on_result:
            on_result(result)
        results.append(result)
    return results


__all__ = [
    "GeneratedResult",
    "Question",
    "ReflectOutcome",
    "Server",
    "TenResult",
    "ask_generated",
    "ask_ten",
    "classify_first",
    "load_questions",
    "standing_claims",
]
