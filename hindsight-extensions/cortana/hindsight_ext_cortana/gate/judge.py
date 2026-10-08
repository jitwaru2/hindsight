"""The model judge for reflect answers (specification 11 item 3).

One bounded call per answer through the engine's LLM provider (the server's own configuration, so on
this machine the Claude subscription). The judge is given the question, the answer and the rubric:
for the ten questions, the answer key's patterns (regular expressions describing the current position,
an old position and a second required point); for a generated question, the key's current value and
its superseded values. It returns a structured verdict; ``correct`` requires the current position
stated as current, no old position presented as current, and the second point when the rubric has
one. Mentioning an old position as history is allowed: the questions often ask for the sequence.

The prompt is versioned (``VERSION``) and the version is recorded with every verdict.
"""

from typing import Any

from pydantic import BaseModel, Field

from .strict import Rubric

VERSION = "1"
MAX_RETRIES = 2

SYSTEM = """You grade one answer from a memory system against a rubric. You do not know the facts
yourself; the rubric is the only source of truth.

The rubric names the CURRENT position, possibly an OLD position, and possibly a SECOND point.
When the rubric is given as patterns, each is a case-insensitive regular expression, and "|"
separates alternatives: the answer states that position when its wording expresses one of the
alternatives (exact words, or an unmistakable paraphrase of them, such as a date written another way).

Decide:
- states_current: the answer gives the CURRENT position as the position now in effect.
- old_as_current: the answer presents an OLD position as the one now in effect, or leaves it
  undecided between an old position and the current one. Describing an old position as earlier,
  replaced, cancelled or superseded is history, not this.
- second_point: the answer carries the SECOND point (true when the rubric has none).
- correct: states_current and second_point and not old_as_current.

Reply with JSON only: {"states_current": bool, "old_as_current": bool, "second_point": bool,
"correct": bool, "reason": "<one sentence>"}."""


class Verdict(BaseModel):
    states_current: bool
    old_as_current: bool
    second_point: bool
    correct: bool
    reason: str = Field(default="")


def render(question: str, answer: str, rubric: Rubric) -> str:
    form = "patterns (regular expressions)" if rubric.patterns else "values"
    lines = [f"QUESTION:\n{question}", "", f"ANSWER:\n{answer.strip() or '(empty)'}", "", f"RUBRIC, as {form}:"]
    lines.append(f"CURRENT: {rubric.current}")
    lines.append(f"OLD: {rubric.old or '(none)'}")
    lines.append(f"SECOND: {rubric.second or '(none)'}")
    return "\n".join(lines)


def settle(verdict: Verdict, rubric: Rubric) -> Verdict:
    """``correct`` recomputed from the parts, so the verdict is consistent whatever the model wrote."""
    second = verdict.second_point or rubric.second is None
    return verdict.model_copy(
        update={
            "second_point": second,
            "correct": verdict.states_current and second and not verdict.old_as_current,
        }
    )


class Judge:
    """The judge bound to an LLM (the engine's ``LLMConfig``-like object with ``call``)."""

    def __init__(self, llm: Any):
        self._llm = llm
        self.name = f"{getattr(llm, 'provider', 'unknown')}/{getattr(llm, 'model', 'unknown')}"

    async def __call__(self, question: str, answer: str, rubric: Rubric) -> Verdict:
        result = await self._llm.call(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": render(question, answer, rubric)},
            ],
            response_format=Verdict,
            skip_validation=True,
            scope="memory",
            max_retries=MAX_RETRIES,
        )
        content = result.content
        verdict = Verdict.model_validate_json(content) if isinstance(content, str) else Verdict.model_validate(content)
        return settle(verdict, rubric)


def engine_judge(engine: Any) -> Judge:
    """The judge on the engine's default LLM, configured from the environment as on the server (the
    structuring suite reaches the retain provider the same way)."""
    return Judge(engine._llm_config)


__all__ = ["Judge", "SYSTEM", "VERSION", "Verdict", "engine_judge", "render", "settle"]
