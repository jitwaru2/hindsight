"""The HTTP extension: our routes, mounted by the engine under ``/ext/``, at ``/ext/cortana/``.

Loaded from ``HINDSIGHT_API_HTTP_EXTENSION=hindsight_ext_cortana:CortanaHttpExtension``. The engine
hands ``get_router`` the memory engine rather than an extension context.

``GET /ext/cortana/status`` reports the package version, the state of our migration branch in the
caller's schema and whether our tables exist. Its full content (pending structuring, the last
reconciliation and gate run, recent retirements, the worker's health; specification section 12)
is HSIGHT-7's. The current-state, subject, fact, ledger, retrieval and decision routes arrive with
HSIGHT-6 and HSIGHT-7.
"""

from importlib.metadata import version
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from hindsight_api import MemoryEngine
from hindsight_api.engine.schema import fq_table_explicit
from hindsight_api.extensions import AuthenticationError, HttpExtension, RequestContext
from pydantic import BaseModel

from .migrations import BRANCH, TABLES, branch_revisions, head_revision

DISTRIBUTION = "hindsight-ext-cortana"


class MigrationState(BaseModel):
    branch: str
    head: str
    applied: str | None
    current: bool


class Status(BaseModel):
    extension: str
    version: str
    database_schema: str
    migrations: MigrationState
    tables: dict[str, bool]


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

        @router.get("/status")
        async def status(schema: Annotated[str, Depends(tenant_schema)]) -> Status:
            pool = await memory._get_pool()
            async with pool.acquire() as conn:
                versions = await conn.fetch(f"SELECT version_num FROM {fq_table_explicit('alembic_version', schema)}")
                present = await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = $1 AND table_name = ANY($2::text[])",
                    schema,
                    list(TABLES),
                )
            ours = branch_revisions()
            applied = next((row["version_num"] for row in versions if row["version_num"] in ours), None)
            head = head_revision()
            found = {row["table_name"] for row in present}
            return Status(
                extension=DISTRIBUTION,
                version=version(DISTRIBUTION),
                database_schema=schema,
                migrations=MigrationState(branch=BRANCH, head=head, applied=applied, current=applied == head),
                tables={table: table in found for table in TABLES},
            )

        return router
