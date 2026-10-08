"""The retrieval log (specification 12, acceptance criterion 11).

One ``retrievals`` row for every recall, including reflect's internal ones, and one for every
reflect, written from the engine's post-operation hooks so a wrong answer can be diagnosed from the
record without re-running the call:

- a recall row holds the query, the caller as the request context shows it, the retrieval
  parameters, and the memory ids returned in rank order with their scores;
- a reflect row holds the query, every tool call in order with the ids it returned, and the ids the
  answer cited (``cited_ids`` for memories and observations, ``cited_mental_model_ids`` for mental
  models);
- ``reflect_id`` links the two: a reflect row carries its own id, and each recall the reflect made
  through its tools carries the same id, so the scores behind a reflect's tool calls are one lookup
  away.

The link is a context variable set in ``validate_reflect``, which the engine awaits in the task
that then runs the reflect's tool loop, so every tool recall sees it (tool calls run in that task
or in tasks created from it, which copy its context). ``on_reflect_complete`` clears it. The engine
calls no hook when a reflect fails, so after a failed reflect the variable stays set until its task
ends; a recall is linked only when it is internal (reflect's tool recalls are) and on the same bank,
which keeps an unrelated recall from being attached.

Rows are written synchronously in the hook. HSIGHT-7 measured the cost on the scratch server and
recorded it in its report; HSIGHT-9's latency measurements decide whether that stays.

Retention: rows older than ``HINDSIGHT_CORTANA_RETRIEVALS_RETENTION_DAYS`` (default 30) are deleted
by ``sweep``, which a bank-wide ``reconcile`` runs and ``hindsight-cortana retrievals sweep`` runs on
demand.
"""

import json
import logging
import os
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from hindsight_api.engine.schema import fq_table, fq_table_explicit

logger = logging.getLogger(__name__)

RETENTION_ENV = "HINDSIGHT_CORTANA_RETRIEVALS_RETENTION_DAYS"
# Thirty days, because a wrong answer noticed within a month must be diagnosable from the record
# without re-running the call (specification 12 and 16 item 1; the 2026-10-07 investigation had to
# re-run a reflect to see what it had used).
DEFAULT_RETENTION_DAYS = 30

KINDS = ("recall", "reflect")
# based_on's memory types, in the order the engine builds them; mental models are cited separately.
CITED_MEMORY_TYPES = ("world", "experience", "opinion", "observation")

_reflect: ContextVar[tuple[str, UUID] | None] = ContextVar("cortana_reflect", default=None)


def retention_days() -> int:
    """The configured retention in days; a positive whole number."""
    raw = os.environ.get(RETENTION_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(f"{RETENTION_ENV} must be a whole number of days, not {raw!r}") from None
    if days < 1:
        raise ValueError(f"{RETENTION_ENV} must be at least 1 day, not {days}")
    return days


def begin_reflect(bank_id: str) -> UUID:
    """Mark the current task as running a reflect on ``bank_id``; returns the reflect's id."""
    reflect_id = uuid.uuid4()
    _reflect.set((bank_id, reflect_id))
    return reflect_id


def current_reflect(bank_id: str) -> UUID | None:
    marked = _reflect.get()
    return marked[1] if marked and marked[0] == bank_id else None


def end_reflect() -> None:
    _reflect.set(None)


@dataclass(frozen=True)
class Row:
    bank_id: str
    kind: str
    query: str
    caller: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] | None = None
    cited_ids: list[UUID] | None = None
    cited_mental_model_ids: list[str] | None = None
    reflect_id: UUID | None = None
    error: str | None = None


def caller_of(request_context: Any) -> dict[str, Any]:
    """Who asked, as far as the request context shows it. Never the API key itself."""
    caller: dict[str, Any] = {
        "internal": bool(getattr(request_context, "internal", False)),
        "user_initiated": bool(getattr(request_context, "user_initiated", False)),
    }
    for name in ("api_key_id", "tenant_id"):
        value = getattr(request_context, name, None)
        if value:
            caller[name] = value
    if getattr(request_context, "mcp_authenticated", False):
        caller["mcp_authenticated"] = True
    if getattr(request_context, "retry_count", 0):
        caller["retry_count"] = request_context.retry_count
    headers = getattr(request_context, "extra_headers", None)
    if headers:
        caller["extra_headers"] = dict(headers)
    return caller


def ranked(facts: list[Any]) -> list[dict[str, Any]]:
    """Recall results in rank order: id, type and scores, plus the source facts of an observation."""
    rows = []
    for fact in facts:
        row: dict[str, Any] = {"id": str(fact.id), "type": fact.fact_type}
        scores = getattr(fact, "scores", None)
        row["scores"] = scores.model_dump(exclude_none=True) if scores is not None else None
        if getattr(fact, "source_fact_ids", None):
            row["source_fact_ids"] = [str(i) for i in fact.source_fact_ids]
        rows.append(row)
    return rows


def recall_row(result: Any, *, reflect_id: UUID | None = None) -> Row:
    """The row for one recall, from the engine's ``RecallResult`` hook context."""
    parameters = {
        "fact_types": list(result.fact_types),
        "budget": getattr(result.budget, "value", result.budget),
        "max_tokens": result.max_tokens,
        "include_entities": result.include_entities,
        "include_chunks": result.include_chunks,
        "question_date": result.question_date.isoformat() if result.question_date else None,
    }
    facts = result.result.results if result.success and result.result is not None else []
    return Row(
        bank_id=result.bank_id,
        kind="recall",
        query=result.query,
        caller=caller_of(result.request_context),
        parameters=parameters,
        results=ranked(facts),
        reflect_id=reflect_id,
        error=None if result.success else (result.error or "recall failed"),
    )


def ids_of(tool: str, output: dict[str, Any]) -> dict[str, list[str]]:
    """The ids a reflect tool call returned, in the order it returned them.

    ``ids`` is the tool's own result list (memories, observations, mental models, expanded memories);
    ``source_fact_ids`` the facts an observation search embedded beside its observations.
    """
    if not isinstance(output, dict):
        return {"ids": []}
    found: dict[str, list[str]] = {"ids": []}
    for key in ("memories", "observations", "mental_models"):
        for item in output.get(key) or []:
            if isinstance(item, dict) and item.get("id"):
                found["ids"].append(str(item["id"]))
    for item in output.get("results") or []:
        if isinstance(item, dict):
            memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
            ident = memory.get("id") or item.get("memory_id") or item.get("id")
            if ident:
                found["ids"].append(str(ident))
    sources = output.get("source_facts")
    if isinstance(sources, dict) and sources:
        found["source_fact_ids"] = [str(i) for i in sources]
    return found


def tool_call(trace: Any) -> dict[str, Any]:
    """One reflect tool call as the log keeps it: its tool, input, reason, timing and returned ids."""
    output = trace.output if isinstance(trace.output, dict) else {}
    call: dict[str, Any] = {
        "iteration": trace.iteration,
        "tool": trace.tool,
        "input": trace.input,
        "reason": trace.reason,
        "duration_ms": trace.duration_ms,
        **ids_of(trace.tool, output),
    }
    if output.get("error"):
        call["error"] = str(output["error"])
    return call


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        return None


def reflect_row(ctx: Any, *, reflect_id: UUID | None) -> Row:
    """The row for one reflect, from the engine's ``ReflectResultContext`` hook context."""
    result = ctx.result
    parameters: dict[str, Any] = {"budget": getattr(ctx.budget, "value", ctx.budget)}
    if ctx.context:
        parameters["context"] = ctx.context
    if not ctx.success or result is None:
        return Row(
            bank_id=ctx.bank_id,
            kind="reflect",
            query=ctx.query,
            caller=caller_of(ctx.request_context),
            parameters=parameters,
            tool_calls=[],
            cited_ids=[],
            cited_mental_model_ids=[],
            reflect_id=reflect_id,
            error=ctx.error or "reflect failed",
        )
    based_on = result.based_on or {}
    cited: list[UUID] = []
    for memory_type in CITED_MEMORY_TYPES:
        for fact in based_on.get(memory_type) or []:
            ident = _uuid_or_none(getattr(fact, "id", None) or (fact.get("id") if isinstance(fact, dict) else None))
            if ident is not None and ident not in cited:
                cited.append(ident)
    models = [
        str(getattr(model, "id", None) or model.get("id"))
        for model in based_on.get("mental-models") or []
        if getattr(model, "id", None) or (isinstance(model, dict) and model.get("id"))
    ]
    return Row(
        bank_id=ctx.bank_id,
        kind="reflect",
        query=ctx.query,
        caller=caller_of(ctx.request_context),
        parameters=parameters,
        tool_calls=[tool_call(trace) for trace in result.tool_trace or []],
        cited_ids=cited,
        cited_mental_model_ids=models,
        reflect_id=reflect_id,
    )


def _table(schema: str | None) -> str:
    """Our table in an explicit schema, or in the schema the engine set for the operation."""
    return fq_table_explicit("retrievals", schema) if schema else fq_table("retrievals")


async def insert(conn: Any, row: Row, *, schema: str | None = None) -> int:
    return await conn.fetchval(
        f"""
        INSERT INTO {_table(schema)}
            (bank_id, kind, caller, query, parameters, results, tool_calls, cited_ids, cited_mental_model_ids,
             reflect_id, error)
        VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::uuid[], $9::text[], $10, $11)
        RETURNING id
        """,
        row.bank_id,
        row.kind,
        json.dumps(row.caller, default=str),
        row.query,
        json.dumps(row.parameters, default=str),
        json.dumps(row.results, default=str),
        None if row.tool_calls is None else json.dumps(row.tool_calls, default=str),
        row.cited_ids,
        row.cited_mental_model_ids,
        row.reflect_id,
        row.error,
    )


async def record(engine: Any, row: Row) -> None:
    """Write one row on the engine's pool, in the operation's tenant schema. Never raises: the log
    must not fail the recall or reflect it describes, so a failure is logged and dropped."""
    try:
        pool = await engine._get_pool()
        async with pool.acquire() as conn:
            await insert(conn, row)
    except Exception:
        logger.exception("cortana retrieval log: %s row for bank %s not written", row.kind, row.bank_id)


async def sweep(conn: Any, *, bank_id: str | None = None, days: int | None = None, schema: str | None = None) -> int:
    """Delete rows older than the retention (one bank, or every bank); returns how many."""
    days = days if days is not None else retention_days()
    args: list[Any] = [days]
    where = "recorded_at < now() - make_interval(days => $1)"
    if bank_id is not None:
        args.append(bank_id)
        where += " AND bank_id = $2"
    status = await conn.execute(f"DELETE FROM {_table(schema)} WHERE {where}", *args)
    return int(status.split()[-1])


async def fetch(
    conn: Any,
    *,
    schema: str | None = None,
    bank_id: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    reflect_id: UUID | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Rows newest first, filtered by bank, kind, time and reflect link."""
    filters, args = [], []
    for column, value in (("bank_id", bank_id), ("kind", kind), ("reflect_id", reflect_id)):
        if value is not None:
            args.append(value)
            filters.append(f"{column} = ${len(args)}")
    if since is not None:
        args.append(since)
        filters.append(f"recorded_at >= ${len(args)}")
    args.append(limit)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = await conn.fetch(
        f"""
        SELECT id, bank_id, recorded_at, kind, caller, query, parameters, results, tool_calls, cited_ids,
               cited_mental_model_ids, reflect_id, error
        FROM {_table(schema)} {where}
        ORDER BY recorded_at DESC, id DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    out = []
    for row in rows:
        item = dict(row)
        for column in ("caller", "parameters", "results", "tool_calls"):
            if isinstance(item[column], str):
                item[column] = json.loads(item[column])
        out.append(item)
    return out
