"""Cortana's extension to the Hindsight engine, loaded through four of the engine's slots.

    HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_ext_cortana:CortanaOperationHooks
    HINDSIGHT_API_HTTP_EXTENSION=hindsight_ext_cortana:CortanaHttpExtension
    HINDSIGHT_API_MCP_EXTENSION=hindsight_ext_cortana:CortanaMcpExtension
    HINDSIGHT_API_TENANT_EXTENSION=hindsight_ext_cortana:CortanaTenantExtension

The binding specification is ``docs/work/hindsight/specs/HINDSIGHT.md`` in Cortana's vault.
"""

from .hooks import CortanaOperationHooks
from .http import CortanaHttpExtension
from .mcp import CortanaMcpExtension
from .tenant import CortanaTenantExtension

__all__ = [
    "CortanaHttpExtension",
    "CortanaMcpExtension",
    "CortanaOperationHooks",
    "CortanaTenantExtension",
]
