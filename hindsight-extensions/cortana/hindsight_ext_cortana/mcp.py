"""The MCP extension: our tools on the engine's own ``/mcp`` endpoint.

Loaded from ``HINDSIGHT_API_MCP_EXTENSION=hindsight_ext_cortana:CortanaMcpExtension``. The engine
hands it the FastMCP server and the memory engine when it builds ``/mcp``. The tools
(``cortana_current``, ``cortana_subjects``, ``cortana_record_decision``; specification sections 9
and 10) are registered here by HSIGHT-6; until then the extension registers none.
"""

from fastmcp import FastMCP
from hindsight_api import MemoryEngine
from hindsight_api.extensions import MCPExtension


class CortanaMcpExtension(MCPExtension):
    def register_tools(self, mcp: FastMCP, memory: MemoryEngine) -> None:
        return None
