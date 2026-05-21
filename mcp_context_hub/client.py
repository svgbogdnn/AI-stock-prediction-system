"""Lightweight MCP JSON-RPC client for the local stock context hub."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import requests


class MCPClientError(RuntimeError):
    """Raised when MCP server interaction fails."""


class MCPContextClient:
    """Client for the local MCP Context Hub JSON-RPC endpoint."""

    def __init__(self, endpoint: Optional[str] = None, timeout_seconds: int = 30):
        self.endpoint = endpoint or os.getenv("MCP_CONTEXT_HUB_URL", "http://localhost:8010/mcp")
        self.timeout_seconds = int(os.getenv("MCP_CONTEXT_HUB_TIMEOUT_SECONDS", str(timeout_seconds)))
        self._request_id = 0

    @classmethod
    def from_env(cls) -> "MCPContextClient":
        return cls()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        try:
            response = requests.post(self.endpoint, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MCPClientError(f"MCP request failed for method '{method}': {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise MCPClientError("MCP response is not valid JSON") from exc

        if "error" in body:
            error = body["error"]
            code = error.get("code", "unknown")
            message = error.get("message", "Unknown MCP error")
            raise MCPClientError(f"MCP error {code}: {message}")

        return body.get("result", {})

    def initialize(self) -> Dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18"),
                "clientInfo": {"name": "stock-prediction-orchestrator", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        )

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._rpc("tools/list", {})
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

        if result.get("isError"):
            details = result.get("structuredContent") or {}
            message = details.get("error") if isinstance(details, dict) else None
            raise MCPClientError(message or f"Tool '{name}' returned error")

        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]

        # Fallback to parsing the text content block if structured content is missing.
        content = result.get("content", [])
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                text = first["text"].strip()
                if text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        return {"text": text}

        return {"result": result}

    def build_context_pack(
        self,
        ticker: str,
        horizon: str = "next_quarter",
        price_days: int = 252,
        news_days: int = 7,
        news_limit: int = 20,
    ) -> Dict[str, Any]:
        return self.call_tool(
            "build_context_pack",
            {
                "ticker": ticker,
                "horizon": horizon,
                "price_days": price_days,
                "news_days": news_days,
                "news_limit": news_limit,
            },
        )

    def health_check(self) -> Dict[str, Any]:
        parsed = urlparse(self.endpoint)
        path = parsed.path
        if path.endswith("/mcp"):
            path = f"{path[:-4]}/health"
        else:
            path = "/health"

        health_url = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

        try:
            response = requests.get(health_url, timeout=min(self.timeout_seconds, 10))
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"status": "unknown"}
        except Exception as exc:
            raise MCPClientError(f"Failed to reach MCP health endpoint: {exc}") from exc
