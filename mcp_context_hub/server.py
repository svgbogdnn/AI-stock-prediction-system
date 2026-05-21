"""MCP Context Hub server exposing stock context tools over JSON-RPC 2.0."""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Tuple

from fastapi import FastAPI, Request

# Allow running as a script: `python mcp_context_hub/server.py`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import fred_fetcher, news_fetcher, polygon_fetcher, sec_edgar_fetcher

LOGGER = logging.getLogger("mcp_context_hub")
logging.basicConfig(level=logging.INFO)

APP_VERSION = "1.0.0"
SERVER_NAME = "stock-context-hub"
DEFAULT_PROTOCOL_VERSION = os.getenv("MCP_PROTOCOL_VERSION", "2025-06-18")
CACHE_TTL_SECONDS = int(os.getenv("MCP_CONTEXT_CACHE_TTL_SECONDS", "300"))
PRICE_TAIL_LIMIT = int(os.getenv("MCP_PRICE_TAIL_LIMIT", "180"))

app = FastAPI(
    title="MCP Context Hub",
    description="Model Context Protocol hub for stock analysis context packs",
    version=APP_VERSION,
)

_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now().isoformat()


def _cache_key(name: str, args: Dict[str, Any]) -> str:
    return json.dumps({"name": name, "args": args}, sort_keys=True, default=str)


def _cache_get(key: str) -> Any:
    if CACHE_TTL_SECONDS <= 0:
        return None

    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None

        timestamp, value = entry
        if (time.time() - timestamp) > CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None

        return value


def _cache_set(key: str, value: Any) -> None:
    if CACHE_TTL_SECONDS <= 0:
        return

    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def _call_with_cache(tool_name: str, args: Dict[str, Any], fn: Callable[[], Any]) -> Any:
    key = _cache_key(tool_name, args)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    value = fn()
    _cache_set(key, value)
    return value


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_price_snapshot(price_history: Dict[str, Any]) -> Dict[str, Any]:
    points = price_history.get("data", []) if isinstance(price_history, dict) else []
    if not isinstance(points, list) or not points:
        return {
            "point_count": 0,
            "latest_close": None,
            "one_month_change_pct": None,
            "volatility_20d_pct": None,
        }

    closes = [_safe_float(item.get("close"), 0.0) for item in points if isinstance(item, dict)]
    closes = [x for x in closes if x > 0]
    if not closes:
        return {
            "point_count": 0,
            "latest_close": None,
            "one_month_change_pct": None,
            "volatility_20d_pct": None,
        }

    latest_close = closes[-1]
    month_ago_close = closes[-22] if len(closes) >= 22 else closes[0]

    one_month_change_pct = None
    if month_ago_close:
        one_month_change_pct = ((latest_close - month_ago_close) / month_ago_close) * 100

    returns: List[float] = []
    for prev, curr in zip(closes, closes[1:]):
        if prev > 0:
            returns.append((curr - prev) / prev)

    volatility_20d_pct = None
    if len(returns) >= 2:
        volatility_20d_pct = statistics.pstdev(returns[-20:]) * 100

    return {
        "point_count": len(closes),
        "latest_close": round(latest_close, 4),
        "one_month_change_pct": round(one_month_change_pct, 3) if one_month_change_pct is not None else None,
        "volatility_20d_pct": round(volatility_20d_pct, 3) if volatility_20d_pct is not None else None,
    }


def get_market_snapshot(ticker: str, price_days: int = 252) -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    price_days = max(30, min(int(price_days), 730))

    fundamentals = _call_with_cache(
        "market_fundamentals",
        {"ticker": ticker},
        lambda: polygon_fetcher.get_fundamentals(ticker),
    )
    price_history_full = _call_with_cache(
        "market_price_history",
        {"ticker": ticker, "price_days": price_days},
        lambda: polygon_fetcher.get_price_history(ticker, days=price_days),
    )

    points = price_history_full.get("data", []) if isinstance(price_history_full, dict) else []
    trimmed_points = points[-PRICE_TAIL_LIMIT:] if isinstance(points, list) else []

    price_history = {
        "ticker": price_history_full.get("ticker", ticker),
        "timespan": price_history_full.get("timespan", "day"),
        "data": trimmed_points,
        "count": len(trimmed_points),
        "raw_count": len(points) if isinstance(points, list) else 0,
        "timestamp": price_history_full.get("timestamp", _now_iso()),
        "error": price_history_full.get("error") if isinstance(price_history_full, dict) else None,
    }

    return {
        "ticker": ticker,
        "price_days": price_days,
        "fundamentals": fundamentals,
        "price_history": price_history,
        "price_snapshot": _build_price_snapshot(price_history),
        "generated_at": _now_iso(),
        "data_source": "Polygon.io API",
    }


def get_macro_snapshot() -> Dict[str, Any]:
    indicators = _call_with_cache("macro_indicators", {}, fred_fetcher.get_macro_indicators)
    return {
        "macro_indicators": indicators,
        "generated_at": _now_iso(),
        "data_source": "FRED API",
    }


def get_filings_summary(ticker: str, filing_type: str = "10-K", days: int = 90) -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    filing_type = filing_type.strip().upper() if filing_type else "10-K"
    days = max(30, min(int(days), 365))

    latest_filings = _call_with_cache(
        "filings_latest",
        {"ticker": ticker, "filing_type": filing_type},
        lambda: sec_edgar_fetcher.get_recent_filings(ticker, filing_type=filing_type, count=3),
    )
    recent_8k = _call_with_cache(
        "filings_8k",
        {"ticker": ticker, "days": days},
        lambda: sec_edgar_fetcher.check_recent_8k_filings(ticker, days=days),
    )
    risk_factors = _call_with_cache(
        "filings_risk",
        {"ticker": ticker},
        lambda: sec_edgar_fetcher.get_risk_factors(ticker),
    )

    event_count = 0
    if isinstance(recent_8k, dict):
        event_count = int(recent_8k.get("recent_8k_count") or recent_8k.get("event_count") or 0)

    return {
        "ticker": ticker,
        "filing_type": filing_type,
        "days": days,
        "latest_filings": latest_filings,
        "recent_8k": recent_8k,
        "risk_factors": risk_factors,
        "material_event_count": event_count,
        "generated_at": _now_iso(),
        "data_source": "SEC EDGAR",
    }


def get_news_digest(ticker: str, days: int = 7, limit: int = 20) -> Dict[str, Any]:
    ticker = ticker.upper().strip()
    days = max(1, min(int(days), 30))
    limit = max(5, min(int(limit), 100))

    articles = _call_with_cache(
        "news_articles",
        {"ticker": ticker, "days": days, "limit": limit},
        lambda: news_fetcher.get_recent_news(ticker, days=days, limit=limit),
    )
    sentiment = news_fetcher.analyze_sentiment(articles)
    key_events = news_fetcher.detect_key_events(articles)

    return {
        "ticker": ticker,
        "days": days,
        "limit": limit,
        "article_count": len(articles),
        "articles": articles,
        "sentiment_analysis": sentiment,
        "key_events": key_events,
        "generated_at": _now_iso(),
        "data_source": "NewsAPI + Polygon + Google News RSS",
    }


def _build_agent_hints(ticker: str, market: Dict[str, Any], macro: Dict[str, Any], regulatory: Dict[str, Any], news: Dict[str, Any]) -> Dict[str, str]:
    price_snapshot = market.get("price_snapshot", {}) if isinstance(market, dict) else {}
    latest_close = price_snapshot.get("latest_close")
    one_month_change = price_snapshot.get("one_month_change_pct")

    macro_indicators = macro.get("macro_indicators", {}) if isinstance(macro, dict) else {}
    market_regime = macro_indicators.get("market_regime", "neutral")

    sentiment = news.get("sentiment_analysis", {}) if isinstance(news, dict) else {}
    sentiment_label = sentiment.get("overall_sentiment", "neutral")
    sentiment_score = sentiment.get("sentiment_score", 0)

    event_count = regulatory.get("material_event_count", 0) if isinstance(regulatory, dict) else 0

    return {
        "fundamental": f"{ticker} latest close {latest_close}, one month change {one_month_change}%.",
        "technical": f"Momentum context for {ticker}: one month move {one_month_change}% with recent close {latest_close}.",
        "sentiment": f"{ticker} sentiment is {sentiment_label} (score {sentiment_score}) from {news.get('article_count', 0)} articles.",
        "macro": f"Macro regime is {market_regime}; use current Fed and inflation values from macro indicators.",
        "regulatory": f"Regulatory monitor for {ticker}: {event_count} recent 8-K events in configured lookback.",
    }


def build_context_pack(
    ticker: str,
    horizon: str = "next_quarter",
    price_days: int = 252,
    news_days: int = 7,
    news_limit: int = 20,
) -> Dict[str, Any]:
    ticker = ticker.upper().strip()

    start = time.time()

    t0 = time.time()
    market = get_market_snapshot(ticker=ticker, price_days=price_days)
    market_ms = round((time.time() - t0) * 1000, 1)

    t0 = time.time()
    macro = get_macro_snapshot()
    macro_ms = round((time.time() - t0) * 1000, 1)

    t0 = time.time()
    regulatory = get_filings_summary(ticker=ticker, filing_type="10-K", days=90)
    regulatory_ms = round((time.time() - t0) * 1000, 1)

    t0 = time.time()
    news = get_news_digest(ticker=ticker, days=news_days, limit=news_limit)
    news_ms = round((time.time() - t0) * 1000, 1)

    context_pack_id = f"ctx_{ticker}_{int(time.time() * 1000)}"

    return {
        "context_pack_id": context_pack_id,
        "ticker": ticker,
        "horizon": horizon,
        "generated_at": _now_iso(),
        "market": market,
        "macro": macro,
        "regulatory": regulatory,
        "news": news,
        "agent_hints": _build_agent_hints(ticker, market, macro, regulatory, news),
        "tools_used": [
            "get_market_snapshot",
            "get_macro_snapshot",
            "get_filings_summary",
            "get_news_digest",
        ],
        "tool_timings_ms": {
            "get_market_snapshot": market_ms,
            "get_macro_snapshot": macro_ms,
            "get_filings_summary": regulatory_ms,
            "get_news_digest": news_ms,
            "build_context_pack_total": round((time.time() - start) * 1000, 1),
        },
        "meta": {
            "server": SERVER_NAME,
            "protocol": "MCP (JSON-RPC 2.0)",
            "protocol_version": DEFAULT_PROTOCOL_VERSION,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "sources": ["Polygon.io", "FRED", "SEC EDGAR", "NewsAPI"],
        },
    }


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_market_snapshot",
        "description": "Get fundamental and price context for a ticker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "price_days": {"type": "integer", "minimum": 30, "maximum": 730},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_macro_snapshot",
        "description": "Get macro-economic indicators snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_filings_summary",
        "description": "Get SEC filings and recent 8-K activity summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "filing_type": {"type": "string"},
                "days": {"type": "integer", "minimum": 30, "maximum": 365},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_news_digest",
        "description": "Get normalized news, sentiment, and key events for a ticker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30},
                "limit": {"type": "integer", "minimum": 5, "maximum": 100},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
    {
        "name": "build_context_pack",
        "description": "Build a complete context pack for all stock analysis agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "horizon": {"type": "string"},
                "price_days": {"type": "integer", "minimum": 30, "maximum": 730},
                "news_days": {"type": "integer", "minimum": 1, "maximum": 30},
                "news_limit": {"type": "integer", "minimum": 5, "maximum": 100},
            },
            "required": ["ticker"],
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_market_snapshot": get_market_snapshot,
    "get_macro_snapshot": get_macro_snapshot,
    "get_filings_summary": get_filings_summary,
    "get_news_digest": get_news_digest,
    "build_context_pack": build_context_pack,
}


def _rpc_success(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _compact_text(value: Dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=True)
    if len(payload) <= 2000:
        return payload
    return payload[:2000] + "..."


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "MCP Context Hub",
        "status": "running",
        "mcp_endpoint": "/mcp",
        "version": APP_VERSION,
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    with _CACHE_LOCK:
        cache_size = len(_CACHE)

    return {
        "status": "healthy",
        "service": "mcp_context_hub",
        "version": APP_VERSION,
        "cache_entries": cache_size,
        "timestamp": _now_iso(),
    }


@app.post("/mcp")
async def mcp_rpc(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request")

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid Request")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": APP_VERSION},
                "capabilities": {"tools": {"listChanged": False}},
            }
            return _rpc_success(request_id, result)

        if method == "tools/list":
            return _rpc_success(request_id, {"tools": TOOL_DEFINITIONS})

        if method == "tools/call":
            if not isinstance(params, dict):
                return _rpc_error(request_id, -32602, "Invalid params")

            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                return _rpc_error(request_id, -32602, "Tool name is required")
            if not isinstance(arguments, dict):
                return _rpc_error(request_id, -32602, "Tool arguments must be an object")

            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                return _rpc_error(request_id, -32601, f"Unknown tool: {name}")

            try:
                structured = handler(**arguments)
                result = {
                    "content": [{"type": "text", "text": _compact_text(structured)}],
                    "structuredContent": structured,
                }
                return _rpc_success(request_id, result)
            except Exception as exc:
                LOGGER.exception("Tool execution failed: %s", name)
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Tool '{name}' failed: {exc}"}],
                    "structuredContent": {"tool": name, "error": str(exc)},
                }
                return _rpc_success(request_id, result)

        if method.startswith("notifications/"):
            return _rpc_success(request_id, {})

        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    except Exception as exc:
        LOGGER.exception("MCP request handling failed")
        return _rpc_error(request_id, -32000, "Internal server error", data=str(exc))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("MCP_CONTEXT_HUB_PORT", "8010"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
