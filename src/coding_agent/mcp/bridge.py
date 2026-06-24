from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Protocol

import httpx

from ai.types import TextContent
from agent_core import AgentTool, AgentToolResult


class MCPClient(Protocol):
    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        """
        调用 MCP 服务器工具并返回结果对象。
        """


@dataclass
class MCPToolConfig:
    name: str
    description: str
    parameters: dict[str, Any]
    server: str
    tool: str


@dataclass
class MCPRemoteServerConfig:
    name: str
    url: str
    headers: dict[str, str]
    timeout_seconds: float | None = None


class RemoteMCPClient:
    """Minimal remote MCP JSON-RPC client for tools/call."""

    def __init__(self, servers: list[MCPRemoteServerConfig]) -> None:
        self._servers = {server.name: server for server in servers}

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        endpoint = self._servers.get(server)
        if endpoint is None:
            raise RuntimeError(f"Unknown remote MCP server: {server}")

        payload = {
            "jsonrpc": "2.0",
            "id": f"mcp_{uuid.uuid4().hex}",
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": arguments,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **endpoint.headers,
        }
        async with httpx.AsyncClient(timeout=endpoint.timeout_seconds) as client:
            response = await client.post(endpoint.url, headers=headers, json=payload)
            response.raise_for_status()

        data = _decode_remote_mcp_response(response)
        if not isinstance(data, dict):
            return data
        error = data.get("error")
        if error:
            raise RuntimeError(str(error))
        return data.get("result")


def parse_remote_mcp_server_configs(raw_servers: list[dict[str, Any]] | None) -> list[MCPRemoteServerConfig]:
    if not raw_servers:
        return []
    result: list[MCPRemoteServerConfig] = []
    for server in raw_servers:
        if not isinstance(server, dict):
            continue
        name = server.get("name")
        url = server.get("url")
        if not isinstance(name, str) or not isinstance(url, str) or not url:
            continue
        raw_headers = server.get("headers")
        headers: dict[str, str] = {}
        if isinstance(raw_headers, dict):
            headers = {str(k): str(v) for k, v in raw_headers.items() if isinstance(k, str)}
        timeout_seconds = server.get("timeout_seconds")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            timeout_seconds = None
        result.append(
            MCPRemoteServerConfig(
                name=name,
                url=url,
                headers=headers,
                timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
            )
        )
    return result


def parse_mcp_tool_configs(raw_servers: list[dict[str, Any]] | None) -> list[MCPToolConfig]:
    if not raw_servers:
        return []
    result: list[MCPToolConfig] = []
    for server in raw_servers:
        if not isinstance(server, dict):
            continue
        server_name = server.get("name")
        tools = server.get("tools")
        if not isinstance(server_name, str) or not isinstance(tools, list):
            continue
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            tool = item.get("tool") or name
            description = item.get("description") or f"MCP tool proxy: {server_name}.{tool}"
            params = item.get("parameters")
            if not isinstance(name, str) or not isinstance(tool, str):
                continue
            if not isinstance(description, str):
                description = str(description)
            if not isinstance(params, dict):
                params = {"type": "object", "properties": {}, "required": [], "additionalProperties": True}
            result.append(
                MCPToolConfig(
                    name=name,
                    description=description,
                    parameters=params,
                    server=server_name,
                    tool=tool,
                )
            )
    return result


def create_mcp_proxy_tools(configs: list[MCPToolConfig], client: MCPClient | None) -> list[AgentTool]:
    tools: list[AgentTool] = []
    for cfg in configs:
        async def _execute(tool_call_id, params, signal=None, on_update=None, *, _cfg=cfg):  # type: ignore[no-untyped-def]
            _ = tool_call_id, signal, on_update
            args = params if isinstance(params, dict) else {}
            if client is None:
                raise RuntimeError(f"MCP bridge unavailable for `{_cfg.name}`")
            try:
                result = await client.call_tool(_cfg.server, _cfg.tool, args)
            except Exception as exc:  # pragma: no cover - adapter-specific
                raise RuntimeError(f"MCP call failed `{_cfg.server}.{_cfg.tool}`: {exc}") from exc
            return AgentToolResult(
                content=[TextContent(text=_normalize_mcp_result(result))],
                details={"server": _cfg.server, "tool": _cfg.tool},
            )

        tools.append(
            AgentTool(
                name=cfg.name,
                label=f"MCP/{cfg.server}",
                description=cfg.description,
                parameters=cfg.parameters,
                execute=_execute,
            )
        )
    return tools


def _decode_remote_mcp_response(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()

    for line in response.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw or raw == "[DONE]":
            continue
        return json.loads(raw)
    return {}


def _normalize_mcp_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(value)
    if isinstance(value, list):
        return "\n".join(str(x) for x in value)
    return str(value)
