"""The tenant extension: the engine's default single-tenant behaviour, plus our tables.

Loaded from ``HINDSIGHT_API_TENANT_EXTENSION=hindsight_ext_cortana:CortanaTenantExtension``.
Authentication and the schema are exactly the engine's default (no authentication, the configured
schema), which is what runs when no tenant extension is set. What this subclass adds:

- ``alembic_version_locations``: the folder of our Alembic branch, so the engine's migration run
  creates and evolves our tables.
- ``extra_bank_tables``: our tables as bank-scoped, so the engine's bank deletion and
  ``hindsight-admin`` backup and restore include them (specification section 4.2).
"""

from hindsight_api.extensions import BankScopedTable
from hindsight_api.extensions.builtin.tenant import DefaultTenantExtension

from .migrations import TABLES, VERSIONS_DIR


class CortanaTenantExtension(DefaultTenantExtension):
    @classmethod
    def alembic_version_locations(cls) -> list[str]:
        return [str(VERSIONS_DIR)]

    def extra_bank_tables(self) -> list[BankScopedTable]:
        return [BankScopedTable(name=table) for table in TABLES]
