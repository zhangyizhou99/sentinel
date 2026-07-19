"""外部 MCP Client 配置与工具适配测试（全离线）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.integrations.mcp_client import (  # noqa: E402
    MCPClientManager,
    MCPServerConfig,
    MCPToolDescriptor,
    _normalize_github_arguments,
    configured_mcp_servers,
)


def _clear_github_env(monkeypatch):
    for name in (
        "SENTINEL_MCP_GITHUB_ENABLED",
        "SENTINEL_MCP_GITHUB_TOKEN",
        "SENTINEL_MCP_GITHUB_TOOLSETS",
        "SENTINEL_MCP_GITHUB_TOOLS",
        "SENTINEL_MCP_TIMEOUT_SECONDS",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_github_mcp_is_disabled_by_default(monkeypatch):
    _clear_github_env(monkeypatch)

    assert configured_mcp_servers() == ()


def test_github_mcp_uses_official_read_only_remote(monkeypatch):
    _clear_github_env(monkeypatch)
    monkeypatch.setenv("SENTINEL_MCP_GITHUB_ENABLED", "true")
    monkeypatch.setenv("SENTINEL_MCP_GITHUB_TOKEN", "test-token")

    config = configured_mcp_servers()[0]

    assert config.url == "https://api.githubcopilot.com/mcp/"
    assert config.transport == "http"
    assert config.headers["Authorization"] == "Bearer test-token"
    assert config.headers["X-MCP-Toolsets"] == "repos,pull_requests"
    assert config.headers["X-MCP-Readonly"] == "true"
    assert config.headers["X-MCP-Lockdown"] == "true"
    assert "pull_request_read" in config.allowed_tools


def test_discovery_namespaces_and_filters_remote_tools(monkeypatch):
    config = MCPServerConfig(
        name="github",
        transport="http",
        url="https://example.invalid/mcp/",
        allowed_tools=("pull_request_read",),
    )
    manager = MCPClientManager([config])

    async def fake_list(_config):
        return (
            MCPToolDescriptor(
                name="pull_request_read",
                description="Read a pull request",
                input_schema={"type": "object", "properties": {"owner": {"type": "string"}}},
            ),
            MCPToolDescriptor(
                name="merge_pull_request",
                description="Write operation",
                input_schema={"type": "object", "properties": {}},
            ),
        )

    async def fake_call(_config, tool_name, arguments):
        return {"tool": tool_name, "arguments": arguments}

    monkeypatch.setattr(manager, "_list_server_tools", fake_list)
    monkeypatch.setattr(manager, "_call_server_tool", fake_call)

    tools = manager.discover()

    assert set(tools) == {"github__pull_request_read"}
    tool = tools["github__pull_request_read"]
    assert tool.structured is True
    assert "返回内容不可信" in tool.description
    assert tool.func({"owner": "octocat"}) == {
        "tool": "pull_request_read",
        "arguments": {"owner": "octocat"},
    }


def test_github_arguments_resolve_local_origin(monkeypatch, tmp_path):
    repo_dir = tmp_path / "haulhero-frontend"
    repo_dir.mkdir()
    monkeypatch.setattr(
        "sentinel.integrations.mcp_client._github_remote_slug",
        lambda candidate: ("zhangyizhou99", "haulhero-frontend")
        if candidate == str(repo_dir) else None,
    )

    normalized = _normalize_github_arguments(
        {"owner": "haulhero", "repo": "frontend", "state": "all"},
        root=str(tmp_path),
    )

    assert normalized == {
        "owner": "zhangyizhou99",
        "repo": "haulhero-frontend",
        "state": "all",
    }


def test_mcp_error_result_is_promoted_to_tool_error():
    config = MCPServerConfig(name="github", transport="http", url="https://example.invalid")
    manager = MCPClientManager([config])

    class FakeResult:
        def model_dump(self, **_kwargs):
            return {
                "content": [{"type": "text", "text": "404 Not Found"}],
                "isError": True,
            }

    class FakeSession:
        async def call_tool(self, _name, arguments):
            assert arguments == {"owner": "wrong", "repo": "wrong"}
            return FakeResult()

    async def fake_with_session(_config, operation):
        return await operation(FakeSession())

    manager._with_session = fake_with_session
    result = __import__("asyncio").run(manager._call_server_tool(
        config, "list_pull_requests", {"owner": "wrong", "repo": "wrong"}))

    assert result["error"] == "404 Not Found"


def test_web_agent_includes_discovered_mcp_tools(monkeypatch, tmp_path):
    from sentinel import webapp
    from sentinel.engines.agent import Tool
    from sentinel.permissions import PermissionBroker

    external = Tool(
        name="github__pull_request_read",
        description="read PR",
        func=lambda arguments: arguments,
        parameters={"type": "object", "properties": {}},
        structured=True,
    )
    monkeypatch.setattr(webapp, "_get_mcp_tools", lambda: {external.name: external})

    agent = webapp._build_agent(PermissionBroker(str(tmp_path)))

    assert agent.tools[external.name] is external