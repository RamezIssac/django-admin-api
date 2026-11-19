"""MCP for admin_api: the tool layer plus the transports (Streamable HTTP view, ``manage.py mcp_serve``).
Set ``ADMIN_API_MCP_ENABLED = False`` to turn both transports off."""

from .sites import AdminMcpSite
from .tools import ToolResult, ToolSpec

__all__ = ["AdminMcpSite", "ToolResult", "ToolSpec"]
