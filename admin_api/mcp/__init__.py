"""MCP for admin_api. The tool layer needs no extra dependency; the transports (Streamable HTTP
view, ``manage.py mcp_serve``) need ``pip install "django-admin-api[mcp]"``."""

from .sites import AdminMcpSite
from .tools import ToolResult, ToolSpec

__all__ = ["AdminMcpSite", "ToolResult", "ToolSpec"]
