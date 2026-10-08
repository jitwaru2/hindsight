"""The HTTP extension: our routes, mounted by the engine under ``/ext/``, at ``/ext/cortana/``.

Loaded from ``HINDSIGHT_API_HTTP_EXTENSION=hindsight_ext_cortana:CortanaHttpExtension``. The engine
hands ``get_router`` the memory engine rather than an extension context. Every route reads the
caller's schema, decided by the engine's tenant extension as for the engine's own routes.

- ``GET /ext/cortana/status``: the package, its migrations, and the health of structuring,
  supersession, reconciliation, the gate, the worker and the retrieval log (``status``; HSIGHT-7).
- ``GET /ext/cortana/retrievals``: the retrieval log, newest first (``retrievals``; HSIGHT-7).
- ``GET /ext/cortana/ledger``: the ledger, newest first (``ledger``; HSIGHT-7).
- ``GET /ext/cortana/current``, ``GET /ext/cortana/subjects`` and ``GET /ext/cortana/facts/<fact id>``:
  the current-state read (``current``; HSIGHT-6, specification 9).
- ``POST /ext/cortana/decisions``: decision capture (``decisions``; HSIGHT-6, specification 10.1).

The HSIGHT-6 routes act on one bank, named by the ``bank_id`` query parameter; the same reads and the
decision write are the MCP tools of ``mcp``.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from hindsight_api import MemoryEngine
from hindsight_api.extensions import AuthenticationError, HttpExtension, RequestContext
from pydantic import BaseModel

from . import current as reads
from . import ledger, retrievals
from .decisions import DecisionError, DecisionRequest, DecisionResult, record_decision
from .status import Status, build_status

# Route limits: enough rows for a diagnosis session, small enough that one response stays readable.
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


class Retrieval(BaseModel):
    """One retrieval log row. ``results`` is a recall's ranked ids with scores (empty for a reflect);
    ``tool_calls``, ``cited_ids`` and ``cited_mental_model_ids`` are a reflect's (null for a recall).
    ``reflect_id`` is a reflect's own id, or on a recall the reflect it ran inside."""

    id: int
    bank_id: str
    recorded_at: datetime
    kind: str
    caller: dict[str, Any]
    query: str
    parameters: dict[str, Any]
    results: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]] | None
    cited_ids: list[UUID] | None
    cited_mental_model_ids: list[str] | None
    reflect_id: UUID | None
    error: str | None


class Retrievals(BaseModel):
    items: list[Retrieval]


class LedgerEntry(BaseModel):
    id: int
    bank_id: str
    recorded_at: datetime
    event: str
    actor: str
    rule: str | None
    reason: str | None
    run_id: UUID | None
    claim_ids: list[UUID]
    memory_unit_ids: list[UUID]
    details: dict[str, Any]


class Ledger(BaseModel):
    subject_ids: list[UUID] | None
    items: list[LedgerEntry]


class CortanaHttpExtension(HttpExtension):
    def get_router(self, memory: MemoryEngine) -> APIRouter:
        router = APIRouter(prefix="/cortana", tags=["Cortana"])
        bearer = HTTPBearer(auto_error=False)

        async def tenant_schema(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> str:
            """The caller's schema, decided by the engine's tenant extension as for its own routes."""
            context = RequestContext(api_key=credentials.credentials if credentials else None)
            try:
                return (await memory.tenant_extension.authenticate(context)).schema_name
            except AuthenticationError as error:
                raise HTTPException(status_code=401, detail=error.reason) from error

        async def caller(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> tuple[RequestContext, str]:
            """The caller's request context, authenticated as the engine authenticates its own routes, and
            its schema (also set for the engine's ``fq_table``, which the decision write and the resolver use)."""
            context = RequestContext(api_key=credentials.credentials if credentials else None)
            try:
                return context, await reads.schema_for(memory, context)
            except AuthenticationError as error:
                raise HTTPException(status_code=401, detail=error.reason) from error

        Schema = Annotated[str, Depends(tenant_schema)]
        Caller = Annotated[tuple[RequestContext, str], Depends(caller)]
        OneBank = Annotated[str, Query(min_length=1, description="the bank")]
        Bank = Annotated[str | None, Query(description="one bank; every bank of the schema when omitted")]
        Since = Annotated[datetime | None, Query(description="only rows recorded at or after this time (ISO 8601)")]
        Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]

        @router.get("/status")
        async def status(schema: Schema, bank_id: Bank = None) -> Status:
            return await build_status(await memory._get_pool(), schema, bank_id=bank_id)

        @router.get("/retrievals")
        async def retrieval_log(
            schema: Schema,
            bank_id: Bank = None,
            kind: Literal["recall", "reflect"] | None = None,
            since: Since = None,
            reflect_id: Annotated[UUID | None, Query(description="a reflect and the recalls it made")] = None,
            limit: Limit = DEFAULT_LIMIT,
        ) -> Retrievals:
            async with (await memory._get_pool()).acquire() as conn:
                rows = await retrievals.fetch(
                    conn, schema=schema, bank_id=bank_id, kind=kind, since=since, reflect_id=reflect_id, limit=limit
                )
            return Retrievals(items=[Retrieval(**row) for row in rows])

        @router.get("/ledger")
        async def ledger_entries(
            schema: Schema,
            bank_id: Bank = None,
            subject: Annotated[str | None, Query(description="entity id, or exact name (case-insensitive)")] = None,
            attribute: Annotated[str | None, Query(description="attribute key")] = None,
            fact_id: Annotated[UUID | None, Query(description="an engine fact (memory unit) id")] = None,
            document: Annotated[str | None, Query(description="a document id")] = None,
            since: Since = None,
            limit: Limit = DEFAULT_LIMIT,
        ) -> Ledger:
            async with (await memory._get_pool()).acquire() as conn:
                subject_ids = (
                    await ledger.subject_ids(conn, schema=schema, subject=subject, bank_id=bank_id)
                    if subject is not None
                    else None
                )
                if subject_ids == []:
                    return Ledger(subject_ids=[], items=[])
                rows = await ledger.fetch(
                    conn,
                    schema=schema,
                    bank_id=bank_id,
                    subject_ids=subject_ids,
                    attribute=attribute,
                    fact_id=fact_id,
                    document=document,
                    since=since,
                    limit=limit,
                )
            return Ledger(subject_ids=subject_ids, items=[LedgerEntry(**row) for row in rows])

        @router.get("/current")
        async def current_state(
            caller: Caller,
            bank_id: OneBank,
            subject: Annotated[str, Query(min_length=1, description="entity name or id")],
            attribute: Annotated[str | None, Query(description="attribute key; every key when omitted")] = None,
        ) -> reads.CurrentState:
            _, schema = caller
            async with (await memory._get_pool()).acquire() as conn:
                return await reads.current(memory, conn, schema, bank_id, subject, attribute)

        @router.get("/subjects")
        async def subject_search(
            caller: Caller,
            bank_id: OneBank,
            q: Annotated[str, Query(min_length=1, description="part of a subject's name")],
            limit: Limit = DEFAULT_LIMIT,
        ) -> reads.Subjects:
            _, schema = caller
            async with (await memory._get_pool()).acquire() as conn:
                return await reads.subjects(memory, conn, schema, bank_id, q, limit)

        @router.get("/facts/{fact_id}")
        async def fact_claims(caller: Caller, bank_id: OneBank, fact_id: UUID) -> reads.FactClaims:
            _, schema = caller
            async with (await memory._get_pool()).acquire() as conn:
                found = await reads.fact(conn, schema, bank_id, fact_id)
            if found is None:
                raise HTTPException(status_code=404, detail=f"no fact {fact_id} in bank {bank_id}")
            return found

        @router.post("/decisions")
        async def decision(caller: Caller, bank_id: OneBank, body: DecisionRequest) -> DecisionResult:
            context, _ = caller
            try:
                return await record_decision(memory, bank_id, body, context)
            except DecisionError as error:
                raise HTTPException(status_code=error.status, detail=str(error)) from error

        return router
