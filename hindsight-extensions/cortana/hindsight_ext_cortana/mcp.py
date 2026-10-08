"""The MCP extension: our tools on the engine's own ``/mcp`` endpoint (specification 9 and 10.1).

Loaded from ``HINDSIGHT_API_MCP_EXTENSION=hindsight_ext_cortana:CortanaMcpExtension``. The engine
hands it the FastMCP server and the memory engine when it builds ``/mcp``, once for the single-bank
server (``/mcp/<bank>/``, the registered form) and once for the multi-bank one. Each tool works on
the connection's bank: the path segment, else the ``X-Bank-Id`` header, else the engine's default,
exactly as the engine's own tools resolve it, with the caller's credentials passed on as the engine's
tools pass them.

- ``cortana_current``: the current position on a subject's key, or on all its keys, with history,
  conflicts and stale documents (``current.current``).
- ``cortana_subjects``: subjects matching a text with their keys, to find the key before asking.
- ``cortana_record_decision``: record a decision Josh states, in his words, as the current claim on
  its key within the call (``decisions.record_decision``); answers in one line.

``hooks.CortanaOperationHooks.filter_mcp_tools`` narrows ``/mcp`` to exactly these three (``TOOLS``).
"""

from datetime import datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from hindsight_api import MemoryEngine
from hindsight_api.extensions import MCPExtension
from pydantic import Field

from . import current as reads
from .decisions import DecisionError, DecisionRequest, record_decision

TOOLS: frozenset[str] = frozenset({"cortana_current", "cortana_subjects", "cortana_record_decision"})

# How many subjects cortana_subjects lists: enough to find a key among name variants, few enough to read.
SUBJECTS_LIMIT = 20

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _session() -> tuple[str, Any]:
    """The connection's bank and a request context carrying its credentials, as the engine's tools build them."""
    from hindsight_api.api import mcp as engine_mcp
    from hindsight_api.models import RequestContext

    bank_id = engine_mcp.get_current_bank_id()
    if not bank_id:
        raise ToolError("no bank: connect to /mcp/<bank>/ or send X-Bank-Id")
    context = RequestContext(
        api_key=engine_mcp.get_current_api_key(),
        tenant_id=engine_mcp.get_current_tenant_id(),
        api_key_id=engine_mcp.get_current_api_key_id(),
        mcp_authenticated=engine_mcp.get_current_mcp_authenticated(),
        extra_headers=engine_mcp.get_current_extra_headers(),
    )
    return bank_id, context


class CortanaMcpExtension(MCPExtension):
    def register_tools(self, mcp: FastMCP, memory: MemoryEngine) -> None:
        @mcp.tool(annotations=READ_ONLY)
        async def cortana_current(
            subject: Annotated[str, Field(description="entity name or id, as cortana_subjects lists it")],
            attribute: Annotated[
                str | None, Field(description="the attribute key; every key of the subject when omitted")
            ] = None,
        ) -> reads.CurrentState:
            """The current position on a subject's key, exactly, from the claims table: no ranking, no model.

            Ask this before answering any question about what Josh decided or what is currently true of
            something. Each key reports its current claim (for a decision record, Josh's words), later
            provisional statements, and superseded claims newest first. A key in conflict carries both
            claims: tell Josh which says what and ask which is right; never choose. A stale document is
            named: the document still says otherwise. "No position recorded" means exactly that.
            Read `summary` first; the keys carry the detail."""
            bank_id, context = _session()
            schema = await reads.schema_for(memory, context)
            async with (await memory._get_pool()).acquire() as conn:
                return await reads.current(memory, conn, schema, bank_id, subject, attribute)

        @mcp.tool(annotations=READ_ONLY)
        async def cortana_subjects(
            q: Annotated[str, Field(description="part of a subject's name")],
        ) -> reads.Subjects:
            """Subjects whose name contains the text, or that the engine's entity resolution maps it to,
            with each one's attribute keys, current value and last statement time. Use it to find the
            exact subject and key before cortana_current or cortana_record_decision."""
            if not q.strip():
                raise ToolError("q is empty")
            bank_id, context = _session()
            schema = await reads.schema_for(memory, context)
            async with (await memory._get_pool()).acquire() as conn:
                return await reads.subjects(memory, conn, schema, bank_id, q, SUBJECTS_LIMIT)

        @mcp.tool(annotations=WRITES)
        async def cortana_record_decision(
            words: Annotated[str, Field(description="Josh's words, verbatim, exactly as he said them")],
            subject: Annotated[str, Field(description="what it is about: a subject from cortana_subjects, or new")],
            attribute: Annotated[
                str,
                Field(description="the key: reuse one cortana_subjects lists for the subject; new only if none fits"),
            ],
            value: Annotated[str, Field(description="the decided value, short, as the current-state read shows it")],
            stated_at: Annotated[
                datetime | None,
                Field(description="when Josh said it (ISO 8601); now when omitted; no zone means US Eastern"),
            ] = None,
            session_id: Annotated[str | None, Field(description="this session's id")] = None,
            domain: Annotated[str | None, Field(description="this session's domain: work, recovery or general")] = None,
        ) -> str:
            """Record a decision Josh states, in his words, as the current position on its key.

            Call it when Josh decides something, with his words verbatim and the subject, key and value.
            Find the subject and key with cortana_subjects first and reuse an existing key. The decision
            is the current claim on its key when this returns; the answer is one line naming the fact,
            the key and what it replaced, to repeat to Josh. Repeating the same call records nothing new."""
            bank_id, context = _session()
            request = DecisionRequest(
                words=words,
                subject=subject,
                attribute=attribute,
                value=value,
                stated_at=stated_at,
                session_id=session_id,
                domain=domain,
            )
            try:
                result = await record_decision(memory, bank_id, request, context)
            except DecisionError as error:
                raise ToolError(str(error)) from error
            return result.message
