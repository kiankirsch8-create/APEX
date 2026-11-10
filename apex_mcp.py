"""
Drop-in MetaTrader5 replacement that talks to MT5's native MCP server.

Legacy IPC times out on MT5 build 6272; MCP works. Enable via env ``APEX_USE_MCP=true``
so ``apex_trader.ensure_mt5`` / ``mt5_connect`` import this module instead of MetaTrader5.

Env:
  MCP_URL       default http://127.0.0.1:22346/mcp
  MT5_MCP_KEY   Bearer token for Authorization header
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

# ── MetaTrader5 constant values (real package ints) ──────────────────────────
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8
TRADE_ACTION_CLOSE_BY = 10

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_ERROR = 10011
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_CANCEL = 10007
TRADE_RETCODE_PLACED = 10008

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1
ORDER_TIME_SPECIFIED = 2
ORDER_TIME_SPECIFIED_DAY = 3

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
TIMEFRAME_MN1 = 49153

# MCP cannot set magic — APEX identifies its book via comment.
DEFAULT_ORDER_COMMENT = "APEX76P"
# When MCP returns magic=0 + APEX76P comment, synthesise private v76 magic.
SYNTH_MAGIC_APEX76P = 760761

_PROTOCOL_VERSION = "2025-06-18"
_lock = threading.RLock()
_session_id: str | None = None
_initialized = False
_last_error: tuple[int, str] = (0, "Success")
_req_id = 0


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _mcp_url() -> str:
    return _env("MCP_URL", "http://127.0.0.1:22346/mcp") or "http://127.0.0.1:22346/mcp"


def _mcp_key() -> str:
    return _env("MT5_MCP_KEY", "")


def _next_id() -> int:
    global _req_id
    with _lock:
        _req_id += 1
        return _req_id


def _set_error(code: int, msg: str) -> None:
    global _last_error
    _last_error = (int(code), str(msg))


def last_error() -> tuple[int, str]:
    return _last_error


def _parse_sse_body(raw: str) -> Any:
    """Parse MCP SSE response — first ``data:`` line as JSON."""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("data:"):
            payload = s[5:].strip()
            if payload:
                return json.loads(payload)
    # Fallback: whole body may already be JSON
    return json.loads(raw)


def _http_post(payload: dict[str, Any], *, session_id: str | None = None) -> tuple[dict[str, Any], str | None]:
    """POST JSON-RPC; return (parsed body, Mcp-Session-Id response header)."""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    key = _mcp_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(_mcp_url(), data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw_bytes = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            new_sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            text = raw_bytes.decode("utf-8", errors="replace")
            if "text/event-stream" in ctype or text.lstrip().startswith("event:") or "\ndata:" in text:
                parsed = _parse_sse_body(text)
            else:
                parsed = json.loads(text) if text.strip() else {}
            if not isinstance(parsed, dict):
                raise RuntimeError(f"MCP non-object response: {type(parsed)}")
            return parsed, new_sid
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        _set_error(int(e.code), f"HTTP {e.code}: {err_body[:300]}")
        raise
    except Exception as e:  # noqa: BLE001
        _set_error(-1, str(e))
        raise


def _rpc(method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> Any:
    global _session_id
    with _lock:
        sid = _session_id
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notification:
        payload["id"] = _next_id()
    parsed, new_sid = _http_post(payload, session_id=sid)
    if new_sid:
        with _lock:
            _session_id = new_sid
    if notification:
        return None
    if "error" in parsed and parsed["error"]:
        err = parsed["error"]
        msg = err if isinstance(err, str) else json.dumps(err)
        _set_error(-2, msg)
        raise RuntimeError(f"MCP RPC error: {msg}")
    return parsed.get("result")


def call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
    """
    POST tools/call; parse ``result.content[0].text`` as JSON when present.
    Returns the parsed JSON object, or the raw result if structure differs.
    """
    result = _rpc("tools/call", {"name": name, "arguments": args or {}})
    if result is None:
        return None
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                text = first.get("text")
                if isinstance(text, str) and text.strip():
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
        # Some servers return structuredContent / result directly
        if "structuredContent" in result:
            return result["structuredContent"]
        if "result" in result and not content:
            return result["result"]
    return result


def _handshake() -> bool:
    global _session_id, _initialized
    with _lock:
        _session_id = None
        _initialized = False
    try:
        init = _rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "apex_mcp", "version": "1.0.0"},
            },
        )
        # Session id captured in _rpc via response header
        _rpc("notifications/initialized", {}, notification=True)
        # Warm-up tool
        try:
            call_tool("get_workspace_info", {})
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            _initialized = True
        _set_error(0, "Success")
        return True
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"handshake failed: {e}")
        with _lock:
            _initialized = False
        return False


def initialize(*_args: Any, **_kwargs: Any) -> bool:
    """Handshake with MCP. Login/password/server kwargs ignored (terminal already logged in)."""
    return _handshake()


def login(*_args: Any, **_kwargs: Any) -> bool:
    """No-op success if already initialized; otherwise handshake."""
    with _lock:
        ok = _initialized
    if ok:
        _set_error(0, "Success")
        return True
    return _handshake()


def shutdown() -> None:
    global _session_id, _initialized
    with _lock:
        _session_id = None
        _initialized = False
    _set_error(0, "Success")


def _ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return {}


def _pick(d: MappingLike, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


# typing helper alias
MappingLike = Any


def _to_epoch(value: Any) -> int:
    """Coerce MCP time (epoch int/float/str or ISO-8601) to unix seconds."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        # Heuristic: ms timestamps
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return int(v)
    s = str(value).strip()
    if not s:
        return 0
    try:
        v = float(s)
        if v > 1e12:
            v /= 1000.0
        return int(v)
    except ValueError:
        pass
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _unwrap_account_payload(raw: Any) -> dict[str, Any]:
    """Real MCP nests fields under ``{\"account\": {...}}``."""
    d = _as_dict(raw)
    if not d and isinstance(raw, dict):
        d = raw
    nested = d.get("account") if isinstance(d, dict) else None
    if isinstance(nested, dict):
        return nested
    return d if isinstance(d, dict) else {}


def _coerce_filling_mode(raw: Any) -> int:
    """Map MCP filling flags/strings to MetaTrader5 filling_mode bitmask."""
    if raw is None:
        return ORDER_FILLING_IOC  # bit 2 in MT5 symbol_info.filling_mode often IOC-capable
    if isinstance(raw, bool):
        return ORDER_FILLING_IOC
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().upper()
    if not s:
        return 1
    try:
        return int(s)
    except ValueError:
        pass
    bits = 0
    if "FOK" in s:
        bits |= 1  # ORDER_FILLING_FOK flag in symbol_info.filling_mode
    if "IOC" in s:
        bits |= 2
    if "RETURN" in s:
        bits |= 4
    return bits or 1


def account_info() -> SimpleNamespace | None:
    try:
        raw = call_tool("get_trading_account_info", {})
        d = _unwrap_account_payload(raw)
        login_raw = _pick(d, "login", "login_id", "account_id", default=0)
        # Avoid treating nested account dict as login when unwrapping failed partially
        if isinstance(login_raw, dict):
            login_raw = _pick(login_raw, "login", default=0)
        return _ns(
            balance=float(_pick(d, "balance", default=0) or 0),
            equity=float(_pick(d, "equity", default=0) or 0),
            margin=float(_pick(d, "margin", default=0) or 0),
            margin_free=float(_pick(d, "margin_free", "free_margin", "marginFree", default=0) or 0),
            margin_level=float(_pick(d, "margin_level", "marginLevel", default=0) or 0),
            currency=str(_pick(d, "currency", default="") or ""),
            login=int(login_raw or 0),
            server=str(_pick(d, "server", default="") or ""),
            leverage=int(_pick(d, "leverage", default=0) or 0),
            profit=float(_pick(d, "profit", default=0) or 0),
        )
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"account_info: {e}")
        return None


def terminal_info() -> SimpleNamespace | None:
    try:
        raw = call_tool("terminal_get_info", {})
        d = _as_dict(raw)
        state = str(_pick(d, "state", "connection_state", "status", default="") or "").lower()
        connected = state in ("synchronized", "connected", "sync", "ok", "ready") or bool(
            _pick(d, "connected", default=False)
        )
        # If MCP answered at all and state empty, treat as connected
        if not state and d:
            connected = True
        return _ns(
            connected=bool(connected),
            trade_allowed=bool(_pick(d, "trade_allowed", "tradeAllowed", default=True)),
            name=str(_pick(d, "name", default="MT5-MCP") or "MT5-MCP"),
            company=str(_pick(d, "company", default="") or ""),
            path=str(_pick(d, "path", default="") or ""),
            build=int(_pick(d, "build", default=0) or 0),
        )
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"terminal_info: {e}")
        return None


def _marketwatch_symbol(symbol: str) -> dict[str, Any]:
    raw = call_tool("get_marketwatch_symbols", {"symbol": symbol})
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and str(row.get("symbol") or row.get("name") or "").upper() == symbol.upper():
                return row
        return raw[0] if raw and isinstance(raw[0], dict) else {}
    if isinstance(raw, dict):
        # May be {symbols: [...]} or a single symbol dict
        syms = raw.get("symbols")
        if isinstance(syms, list):
            for row in syms:
                if isinstance(row, dict) and str(row.get("symbol") or row.get("name") or "").upper() == symbol.upper():
                    return row
            if syms and isinstance(syms[0], dict):
                return syms[0]
        return raw
    return {}


_TICK_VALUE_CACHE: dict[str, float] = {}


def _calc_tick_value(symbol: str, price: float, tick_size: float) -> float:
    """Real MCP returns trade_tick_value=0 — compute via profit of 1 lot over 1 tick."""
    try:
        if price <= 0 or tick_size <= 0:
            return 0.0
        r = call_tool(
            "trade_order_calc_profit",
            {
                "type": "buy",
                "symbol": symbol,
                "volume": 1.0,
                "price_open": float(price),
                "price_close": float(price) + float(tick_size),
            },
        )
        d = r if isinstance(r, dict) else _as_dict(r)
        return abs(float(_pick(d, "profit", default=0) or 0))
    except Exception:  # noqa: BLE001
        return 0.0


def symbol_info_tick(symbol: str) -> SimpleNamespace | None:
    try:
        d = _marketwatch_symbol(symbol)
        if not d:
            return None
        bid = float(_pick(d, "bid", default=0) or 0)
        ask = float(_pick(d, "ask", default=0) or 0)
        if bid <= 0 and ask <= 0:
            return None
        return _ns(
            bid=bid,
            ask=ask,
            last=float(_pick(d, "last", default=0) or 0),
            volume=int(_pick(d, "volume", default=0) or 0),
            time=int(_pick(d, "time", default=0) or 0),
        )
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"symbol_info_tick: {e}")
        return None


def symbol_info(symbol: str) -> SimpleNamespace | None:
    try:
        d = _marketwatch_symbol(symbol)
        if not d:
            return None
        # Real MCP may nest trading conditions under "symbol" / "info" / "trade"
        for nest_key in ("symbol", "info", "trade", "properties"):
            nested = d.get(nest_key) if isinstance(d, dict) else None
            if isinstance(nested, dict) and (
                "digits" in nested or "point" in nested or "volume_min" in nested or "tick_value" in nested
            ):
                # Keep top-level name/bid/ask; merge nested trade fields
                merged = dict(d)
                merged.update(nested)
                d = merged
                break
        point = float(_pick(d, "point", default=0) or 0)
        digits = int(_pick(d, "digits", default=5) or 5)
        if point <= 0:
            point = 10 ** (-digits)
        name_raw = _pick(d, "symbol", "name", default=symbol)
        if isinstance(name_raw, dict):
            name_raw = _pick(name_raw, "name", "symbol", default=symbol)
        tick_size = float(_pick(d, "trade_tick_size", "tick_size", "tickSize", default=0) or 0) or point
        tick_value = float(_pick(d, "trade_tick_value", "tick_value", "tickValue", default=0) or 0)
        # Live MCP feed returns trade_tick_value=0 → bad_tick_value kills all sizing.
        if tick_value <= 0:
            cached = _TICK_VALUE_CACHE.get(symbol, 0.0)
            if cached > 0:
                tick_value = cached
            else:
                px = float(_pick(d, "bid", default=0) or 0) or float(_pick(d, "ask", default=0) or 0)
                tv = _calc_tick_value(symbol, px, tick_size)
                if tv > 0:
                    tick_value = tv
                    _TICK_VALUE_CACHE[symbol] = tv
        return _ns(
            name=str(name_raw or symbol),
            digits=digits,
            point=point,
            trade_stops_level=int(_pick(d, "trade_stops_level", "stops_level", "stopsLevel", default=0) or 0),
            trade_freeze_level=int(_pick(d, "trade_freeze_level", "freeze_level", default=0) or 0),
            volume_min=float(_pick(d, "volume_min", "min_volume", "volumeMin", default=0.01) or 0.01),
            volume_max=float(_pick(d, "volume_max", "max_volume", "volumeMax", default=100.0) or 100.0),
            volume_step=float(_pick(d, "volume_step", "volumeStep", default=0.01) or 0.01),
            trade_tick_value=tick_value,
            trade_tick_size=tick_size,
            trade_contract_size=float(
                _pick(d, "trade_contract_size", "contract_size", "contractSize", default=100000) or 100000
            ),
            filling_mode=_coerce_filling_mode(
                _pick(d, "filling_mode", "trade_filling_mode", "tradeFillingMode", default=1)
            ),
            visible=True,
        )
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"symbol_info: {e}")
        return None


def symbol_select(symbol: str, _enable: bool = True) -> bool:
    try:
        call_tool("add_marketwatch_symbol", {"symbol": symbol})
        return True
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"symbol_select: {e}")
        # Some terminals already have the symbol — treat as success if tick works
        return symbol_info_tick(symbol) is not None


def _parse_position(row: dict[str, Any]) -> SimpleNamespace:
    # Real MCP uses ``action`` (buy/sell) and ``position_id`` instead of type/ticket.
    typ_raw = _pick(row, "action", "type", "position_type", "order_type", "side", default=0)
    if isinstance(typ_raw, str):
        t = typ_raw.strip().lower()
        typ = ORDER_TYPE_BUY if t in ("buy", "long", "0", "position_type_buy") else ORDER_TYPE_SELL
    else:
        typ = int(typ_raw or 0)
    ticket = int(
        _pick(row, "position_id", "ticket", "position", "position_ticket", "id", default=0) or 0
    )
    comment = str(_pick(row, "comment", default="") or "").strip()
    magic = int(_pick(row, "magic", "magic_number", "magicNumber", default=0) or 0)
    # MCP order tools cannot set magic — synthesise for APEX76P book.
    if magic == 0 and comment == DEFAULT_ORDER_COMMENT:
        magic = SYNTH_MAGIC_APEX76P
    return _ns(
        ticket=ticket,
        symbol=str(_pick(row, "symbol", default="") or ""),
        type=typ,
        volume=float(_pick(row, "volume", "lots", default=0) or 0),
        price_open=float(
            _pick(
                row,
                "price_open",
                "open_price",
                "priceOpen",
                "openPrice",
                "price",
                default=0,
            )
            or 0
        ),
        sl=float(_pick(row, "sl", "stop_loss", "stopLoss", default=0) or 0),
        tp=float(_pick(row, "tp", "take_profit", "takeProfit", default=0) or 0),
        comment=comment,
        magic=magic,
        profit=float(_pick(row, "profit", default=0) or 0),
        swap=float(_pick(row, "swap", default=0) or 0),
        time=_to_epoch(_pick(row, "create_time", "time", "open_time", "time_create", default=0)),
        identifier=int(_pick(row, "identifier", "position_id", "ticket", default=ticket) or ticket),
    )


def positions_get(symbol: str | None = None, ticket: int | None = None) -> tuple[SimpleNamespace, ...] | None:
    try:
        args: dict[str, Any] = {}
        if symbol:
            args["symbol"] = symbol
        raw = call_tool("get_trading_open_positions", args)
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("positions", "data", "result", "items"):
                v = raw.get(key)
                if isinstance(v, list):
                    rows = [r for r in v if isinstance(r, dict)]
                    break
            if not rows and (
                "ticket" in raw or "symbol" in raw or "position_id" in raw or "action" in raw
            ):
                rows = [raw]
        out = [_parse_position(r) for r in rows]
        if symbol:
            su = symbol.upper()
            out = [p for p in out if str(p.symbol).upper() == su or str(p.symbol).upper().startswith(su)]
        if ticket is not None:
            tid = int(ticket)
            out = [p for p in out if int(p.ticket) == tid]
        return tuple(out)
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"positions_get: {e}")
        return None


def history_deals_get(*_args: Any, **kwargs: Any) -> tuple[SimpleNamespace, ...] | None:
    """Map to get_trading_history_positions (best-effort deal-like rows)."""
    try:
        args: dict[str, Any] = {}
        if "position" in kwargs:
            args["position_ticket"] = int(kwargs["position"])
            args["position"] = int(kwargs["position"])
        if "symbol" in kwargs:
            args["symbol"] = kwargs["symbol"]
        raw = call_tool("get_trading_history_positions", args)
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("deals", "positions", "history", "data", "items"):
                v = raw.get(key)
                if isinstance(v, list):
                    rows = [r for r in v if isinstance(r, dict)]
                    break
        out: list[SimpleNamespace] = []
        for r in rows:
            entry_raw = _pick(r, "entry", "deal_entry", default=DEAL_ENTRY_OUT)
            if isinstance(entry_raw, str):
                entry = DEAL_ENTRY_OUT if "out" in entry_raw.lower() else DEAL_ENTRY_IN
            else:
                entry = int(entry_raw or DEAL_ENTRY_OUT)
            out.append(
                _ns(
                    ticket=int(_pick(r, "ticket", "deal", default=0) or 0),
                    order=int(_pick(r, "order", default=0) or 0),
                    position_id=int(_pick(r, "position_id", "position", "position_ticket", default=0) or 0),
                    symbol=str(_pick(r, "symbol", default="") or ""),
                    type=int(_pick(r, "type", default=0) or 0),
                    entry=entry,
                    profit=float(_pick(r, "profit", default=0) or 0),
                    swap=float(_pick(r, "swap", default=0) or 0),
                    commission=float(_pick(r, "commission", default=0) or 0),
                    magic=int(_pick(r, "magic", default=0) or 0),
                    comment=str(_pick(r, "comment", default="") or ""),
                    volume=float(_pick(r, "volume", default=0) or 0),
                    price=float(_pick(r, "price", "price_open", default=0) or 0),
                    time=int(_pick(r, "time", default=0) or 0),
                )
            )
        pos_filter = kwargs.get("position")
        if pos_filter is not None:
            pid = int(pos_filter)
            out = [d for d in out if int(getattr(d, "position_id", 0) or 0) == pid or int(getattr(d, "ticket", 0) or 0) == pid]
        return tuple(out)
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"history_deals_get: {e}")
        return None


_TF_TO_MCP: dict[int, str] = {
    TIMEFRAME_M1: "M1",
    TIMEFRAME_M5: "M5",
    TIMEFRAME_M15: "M15",
    TIMEFRAME_M30: "M30",
    TIMEFRAME_H1: "H1",
    TIMEFRAME_H4: "H4",
    TIMEFRAME_D1: "D1",
    TIMEFRAME_W1: "W1",
    TIMEFRAME_MN1: "MN1",
}

# Minutes per bar — used to build datetime_from/to for get_chart_history
# (schema is additionalProperties:false; rejects timeframe/start_pos/count/bars).
_TF_MINUTES: dict[int, int] = {
    TIMEFRAME_M1: 1,
    TIMEFRAME_M5: 5,
    TIMEFRAME_M15: 15,
    TIMEFRAME_M30: 30,
    TIMEFRAME_H1: 60,
    TIMEFRAME_H4: 240,
    TIMEFRAME_D1: 1440,
    TIMEFRAME_W1: 10080,
    TIMEFRAME_MN1: 43200,
}


def copy_rates_from_pos(symbol: str, timeframe: int, start_pos: int, count: int) -> list[dict[str, Any]] | None:
    """Return list of bar dicts with keys time/open/high/low/close/tick_volume (dict-indexable)."""
    try:
        period = _TF_TO_MCP.get(int(timeframe), str(timeframe))
        tf_min = _TF_MINUTES.get(int(timeframe), 60)
        need = int(start_pos) + int(count)
        span_min = need * tf_min * 3 + tf_min * 24 * 60
        now = datetime.now(timezone.utc)
        raw = call_tool(
            "get_chart_history",
            {
                "symbol": symbol,
                "period": period,
                "datetime_from": (now - timedelta(minutes=span_min)).strftime("%Y-%m-%dT%H:%M:%S"),
                "datetime_to": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                "limit": max(need + 50, 200),
            },
        )
        rows: list[Any] = []
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            # Prefer real MCP ``history`` key; also accept bars/rates/candles/data.
            for key in ("history", "bars", "rates", "candles", "data"):
                v = raw.get(key)
                if isinstance(v, list):
                    rows = v
                    break
        out: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append(
                {
                    "time": _to_epoch(_pick(r, "time", "t", default=0)),
                    "open": float(_pick(r, "open", "o", default=0) or 0),
                    "high": float(_pick(r, "high", "h", default=0) or 0),
                    "low": float(_pick(r, "low", "l", default=0) or 0),
                    "close": float(_pick(r, "close", "c", default=0) or 0),
                    "tick_volume": int(_pick(r, "tick_volume", "volume", "tickVolume", default=0) or 0),
                    "spread": int(_pick(r, "spread", default=0) or 0),
                    "real_volume": int(_pick(r, "real_volume", "realVolume", default=0) or 0),
                }
            )
        return out if out else None
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"copy_rates_from_pos: {e}")
        return None


def _mcp_retcode_ok(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, dict):
        rc = _pick(raw, "retcode", "code", "status", default=None)
        if rc is None:
            # success if no error key
            return not raw.get("error")
        if isinstance(rc, str):
            return rc.strip().lower() in ("0", "done", "ok", "success", "trade_retcode_done")
        try:
            return int(rc) == 0 or int(rc) == TRADE_RETCODE_DONE
        except (TypeError, ValueError):
            return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("0", "done", "ok", "success")
    try:
        return int(raw) == 0
    except (TypeError, ValueError):
        return False


def _order_result(ok: bool, raw: Any = None, *, comment: str = "") -> SimpleNamespace:
    deal = 0
    order = 0
    volume = 0.0
    price = 0.0
    if isinstance(raw, dict):
        deal = int(_pick(raw, "deal", "deal_ticket", default=0) or 0)
        order = int(_pick(raw, "order", "order_ticket", "ticket", default=0) or 0)
        volume = float(_pick(raw, "volume", default=0) or 0)
        price = float(_pick(raw, "price", default=0) or 0)
        if not comment:
            comment = str(_pick(raw, "comment", "message", default="") or "")
    return _ns(
        retcode=TRADE_RETCODE_DONE if ok else TRADE_RETCODE_ERROR,
        deal=deal,
        order=order,
        volume=volume,
        price=price,
        bid=0.0,
        ask=0.0,
        comment=comment or ("Done" if ok else "Error"),
        request_id=0,
        retcode_external=0,
    )


def order_send(request: dict[str, Any]) -> SimpleNamespace | None:
    """
    Route by ``request['action']``:
      DEAL open  → trade_send_market_order
      DEAL close → trade_close_single_position (when ``position`` set)
      SLTP       → trade_modify_sl_tp
    """
    try:
        action = int(request.get("action") or 0)
        symbol = str(request.get("symbol") or "")
        comment = str(request.get("comment") or DEFAULT_ORDER_COMMENT).strip() or DEFAULT_ORDER_COMMENT
        position = request.get("position")
        sl = float(request.get("sl") or 0)
        tp = float(request.get("tp") or 0)
        volume = float(request.get("volume") or 0)

        if action == TRADE_ACTION_SLTP:
            if position is None:
                _set_error(-1, "SLTP missing position")
                return _order_result(False, comment="missing position")
            raw = call_tool(
                "trade_modify_sl_tp",
                {
                    "symbol": symbol,
                    "position_ticket": int(position),
                    "sl": sl,
                    "tp": tp,
                },
            )
            ok = _mcp_retcode_ok(raw)
            return _order_result(ok, raw)

        if action == TRADE_ACTION_DEAL:
            # Close of an existing position
            if position is not None:
                close_args: dict[str, Any] = {
                    "symbol": symbol,
                    "position_ticket": int(position),
                }
                if volume > 0:
                    close_args["volume"] = volume
                raw = call_tool("trade_close_single_position", close_args)
                ok = _mcp_retcode_ok(raw)
                return _order_result(ok, raw)

            # Market open
            typ = int(request.get("type") or ORDER_TYPE_BUY)
            side = "buy" if typ == ORDER_TYPE_BUY else "sell"
            args = {
                "symbol": symbol,
                "type": side,
                "volume": volume,
                "sl": sl,
                "tp": tp,
                "comment": comment,
            }
            # Optional pre-check
            try:
                call_tool("trade_check_market_order", dict(args))
            except Exception:  # noqa: BLE001
                pass
            raw = call_tool("trade_send_market_order", args)
            ok = _mcp_retcode_ok(raw)
            return _order_result(ok, raw)

        _set_error(-1, f"unsupported action={action}")
        return _order_result(False, comment=f"unsupported action {action}")
    except Exception as e:  # noqa: BLE001
        _set_error(-1, f"order_send: {e}")
        return _order_result(False, comment=str(e))


# Module-as-package mimic: allow ``import apex_mcp as mt5`` then ``mt5.initialize()``.
__all__ = [
    "initialize",
    "login",
    "shutdown",
    "last_error",
    "account_info",
    "terminal_info",
    "symbol_info_tick",
    "symbol_info",
    "symbol_select",
    "positions_get",
    "history_deals_get",
    "copy_rates_from_pos",
    "order_send",
    "call_tool",
    "ORDER_TYPE_BUY",
    "ORDER_TYPE_SELL",
    "TRADE_ACTION_DEAL",
    "TRADE_ACTION_SLTP",
    "TRADE_RETCODE_DONE",
    "ORDER_FILLING_FOK",
    "ORDER_FILLING_IOC",
    "ORDER_FILLING_RETURN",
    "ORDER_TIME_GTC",
    "DEAL_ENTRY_IN",
    "DEAL_ENTRY_OUT",
    "TIMEFRAME_H4",
    "TIMEFRAME_D1",
    "TIMEFRAME_W1",
    "DEFAULT_ORDER_COMMENT",
    "SYNTH_MAGIC_APEX76P",
]
