"""Each documented slot value loads our class through the engine's own loader (specification 13.1).

The values in pyproject.toml's ``env`` are the ones the profile environment file carries; the
server resolves them with ``load_extension`` at startup, so a broken re-export would otherwise
surface only as a boot failure.
"""

import os
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from hindsight_api.api import create_app
from hindsight_api.extensions import (
    HttpExtension,
    MCPExtension,
    OperationValidatorExtension,
    TenantExtension,
    load_extension,
)

import hindsight_ext_cortana
from hindsight_ext_cortana import (
    CortanaHttpExtension,
    CortanaMcpExtension,
    CortanaOperationHooks,
    CortanaTenantExtension,
)

SLOTS = [
    ("OPERATION_VALIDATOR", OperationValidatorExtension, CortanaOperationHooks),
    ("HTTP", HttpExtension, CortanaHttpExtension),
    ("MCP", MCPExtension, CortanaMcpExtension),
    ("TENANT", TenantExtension, CortanaTenantExtension),
]


@pytest.mark.parametrize(("slot", "base", "cls"), SLOTS, ids=[slot for slot, _, _ in SLOTS])
def test_the_documented_value_loads_our_class(slot, base, cls):
    assert os.environ[f"HINDSIGHT_API_{slot}_EXTENSION"] == f"hindsight_ext_cortana:{cls.__name__}"
    assert isinstance(load_extension(slot, base), cls)


def test_the_package_root_exports_exactly_the_four_classes():
    assert sorted(hindsight_ext_cortana.__all__) == sorted(cls.__name__ for _, _, cls in SLOTS)


async def test_the_engine_mounts_our_router_under_ext(cortana_memory):
    app = create_app(cortana_memory, initialize_memory=False)
    assert "/ext/cortana/status" in app.openapi()["paths"]


async def test_the_engines_mcp_servers_hand_our_extension_their_tools_server(cortana_memory):
    """The engine builds a multi-bank and a single-bank MCP server; each loads our extension."""
    with patch.object(CortanaMcpExtension, "register_tools", autospec=True) as register_tools:
        create_app(cortana_memory, initialize_memory=False, mcp_api_enabled=True)
    assert register_tools.call_count == 2
    for call in register_tools.call_args_list:
        _, mcp, memory = call.args
        assert isinstance(mcp, FastMCP)
        assert memory is cortana_memory


async def test_the_engine_gives_our_hooks_its_context(cortana_memory):
    hooks = cortana_memory._operation_validator
    assert isinstance(hooks, CortanaOperationHooks)
    assert hooks.context.get_memory_engine() is cortana_memory
