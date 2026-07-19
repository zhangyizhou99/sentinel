"""外部系统集成。"""

from sentinel.integrations.mcp_client import (
    MCPClientManager,
    MCPServerConfig,
    MCPToolDescriptor,
    configured_mcp_servers,
)

__all__ = [
    "MCPClientManager",
    "MCPServerConfig",
    "MCPToolDescriptor",
    "configured_mcp_servers",
]