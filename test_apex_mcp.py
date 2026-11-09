"""Unit tests for apex_mcp — mock HTTP JSON-RPC / tools/call."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import apex_mcp as mcp
import apex_trader as at


class _FakeResp:
    def __init__(self, body: dict[str, Any] | str, *, headers: dict[str, str] | None = None, ctype: str = "application/json"):
        if isinstance(body, dict):
            self._raw = json.dumps(body).encode("utf-8")
        else:
            self._raw = str(body).encode("utf-8")
        self.headers = {"Content-Type": ctype, **(headers or {})}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


def _rpc_ok(result: Any, *, sid: str = "sess-1") -> _FakeResp:
    return _FakeResp({"jsonrpc": "2.0", "id": 1, "result": result}, headers={"Mcp-Session-Id": sid})


def _tool_text(obj: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def test_handshake_initialize_and_session_header(monkeypatch) -> None:
    mcp.shutdown()
    calls: list[dict[str, Any]] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResp:
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        method = payload.get("method")
        if method == "initialize":
            return _rpc_ok({"protocolVersion": "2025-06-18"}, sid="abc-session")
        if method == "notifications/initialized":
            return _FakeResp({"jsonrpc": "2.0", "result": None}, headers={"Mcp-Session-Id": "abc-session"})
        if method == "tools/call":
            return _rpc_ok(_tool_text({"ok": True}), sid="abc-session")
        return _rpc_ok({})

    monkeypatch.setattr(mcp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MT5_MCP_KEY", "test-key")
    assert mcp.initialize() is True
    assert any(c.get("method") == "initialize" for c in calls)
    assert any(c.get("method") == "notifications/initialized" for c in calls)
    # Session captured
    assert mcp._session_id == "abc-session"
    mcp.shutdown()


def test_sse_response_parses_first_data_line(monkeypatch) -> None:
    mcp.shutdown()
    sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"{\\\"balance\\\":10000,\\\"equity\\\":10050,\\\"margin_free\\\":8000,\\\"currency\\\":\\\"USD\\\",\\\"login\\\":123}\"}]}}\n\n"

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResp:
        payload = json.loads(req.data.decode("utf-8"))
        if payload.get("method") == "initialize":
            return _rpc_ok({}, sid="s1")
        if payload.get("method") == "notifications/initialized":
            return _FakeResp("{}", headers={"Mcp-Session-Id": "s1"})
        if payload.get("method") == "tools/call" and payload.get("params", {}).get("name") == "get_workspace_info":
            return _rpc_ok(_tool_text({"ws": 1}), sid="s1")
        return _FakeResp(sse, headers={"Mcp-Session-Id": "s1"}, ctype="text/event-stream")

    monkeypatch.setattr(mcp.urllib.request, "urlopen", fake_urlopen)
    assert mcp.initialize()
    ai = mcp.account_info()
    assert ai is not None
    assert ai.balance == 10000.0
    assert ai.equity == 10050.0
    assert ai.login == 123
    mcp.shutdown()


def test_order_send_deal_open_maps_to_trade_send_market_order(monkeypatch) -> None:
    mcp.shutdown()
    tools: list[tuple[str, dict[str, Any]]] = []

    def fake_call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
        tools.append((name, dict(args or {})))
        if name == "trade_check_market_order":
            return {"retcode": 0}
        if name == "trade_send_market_order":
            return {"retcode": 0, "deal": 99, "order": 88}
        return {"retcode": 0}

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)
    monkeypatch.setattr(mcp, "_initialized", True)

    res = mcp.order_send(
        {
            "action": mcp.TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.1,
            "type": mcp.ORDER_TYPE_BUY,
            "sl": 1.09,
            "tp": 1.12,
            "comment": "APEX76P",
        }
    )
    assert res is not None
    assert res.retcode == mcp.TRADE_RETCODE_DONE
    assert ("trade_send_market_order",) == (tools[-1][0],)
    args = tools[-1][1]
    assert args["symbol"] == "EURUSD"
    assert args["type"] == "buy"
    assert args["volume"] == 0.1
    assert args["comment"] == "APEX76P"
    assert args["sl"] == 1.09


def test_order_send_sltp_maps_to_trade_modify_sl_tp(monkeypatch) -> None:
    tools: list[tuple[str, dict[str, Any]]] = []

    def fake_call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
        tools.append((name, dict(args or {})))
        return {"retcode": "Done"}

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)
    res = mcp.order_send(
        {
            "action": mcp.TRADE_ACTION_SLTP,
            "symbol": "GBPUSD",
            "position": 555,
            "sl": 1.25,
            "tp": 1.30,
        }
    )
    assert res.retcode == mcp.TRADE_RETCODE_DONE
    assert tools[0][0] == "trade_modify_sl_tp"
    assert tools[0][1]["position_ticket"] == 555
    assert tools[0][1]["sl"] == 1.25


def test_order_send_deal_close_maps_to_trade_close_single_position(monkeypatch) -> None:
    tools: list[tuple[str, dict[str, Any]]] = []

    def fake_call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
        tools.append((name, dict(args or {})))
        return {"retcode": 0}

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)
    res = mcp.order_send(
        {
            "action": mcp.TRADE_ACTION_DEAL,
            "symbol": "USDJPY",
            "volume": 0.05,
            "type": mcp.ORDER_TYPE_SELL,
            "position": 777,
        }
    )
    assert res.retcode == mcp.TRADE_RETCODE_DONE
    assert tools[0][0] == "trade_close_single_position"
    assert tools[0][1]["position_ticket"] == 777


def test_positions_get_parses_rows(monkeypatch) -> None:
    def fake_call_tool(name: str, args: dict[str, Any] | None = None) -> Any:
        assert name == "get_trading_open_positions"
        return {
            "positions": [
                {
                    "ticket": 101,
                    "symbol": "EURUSD",
                    "type": "buy",
                    "volume": 0.2,
                    "price_open": 1.1,
                    "sl": 1.09,
                    "tp": 1.12,
                    "comment": "APEX76P",
                    "magic": 0,
                    "profit": 12.5,
                },
                {
                    "ticket": 102,
                    "symbol": "GBPUSD",
                    "type": 1,
                    "volume": 0.1,
                    "open_price": 1.3,
                    "comment": "OTHER",
                    "magic": 0,
                    "profit": -1.0,
                },
            ]
        }

    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)
    rows = mcp.positions_get()
    assert rows is not None and len(rows) == 2
    assert rows[0].ticket == 101
    assert rows[0].type == mcp.ORDER_TYPE_BUY
    assert rows[0].comment == "APEX76P"
    assert rows[0].magic == 0
    assert rows[1].type == mcp.ORDER_TYPE_SELL
    only = mcp.positions_get(symbol="EURUSD")
    assert only is not None and len(only) == 1 and only[0].ticket == 101


def test_open_apex_positions_comment_primary_magic_fallback() -> None:
    """MCP tickets: magic=0 + APEX76P comment still match; magic-only also matches."""
    positions = [
        SimpleNamespace(magic=0, comment="APEX76P", ticket=1, symbol="EURUSD"),  # MCP
        SimpleNamespace(magic=760761, comment="", ticket=2, symbol="GBPUSD"),  # magic fallback
        SimpleNamespace(magic=760761, comment="APEX76P", ticket=3, symbol="USDJPY"),  # both
        SimpleNamespace(magic=0, comment="OTHER", ticket=4, symbol="AUDUSD"),  # neither
        SimpleNamespace(magic=999, comment="NOPE", ticket=5, symbol="NZDUSD"),  # neither
    ]
    mt5 = MagicMock()
    mt5.positions_get.return_value = positions
    found = at.open_apex_positions(mt5, magic=760761, comment="APEX76P")
    assert {int(p.ticket) for p in found} == {1, 2, 3}


def test_mt5_connect_uses_apex_mcp_when_flag_set(monkeypatch) -> None:
    at._mt5_holder["m"] = None
    monkeypatch.setenv("APEX_USE_MCP", "true")
    monkeypatch.setenv("APEX_MT5_LOGIN", "1")
    monkeypatch.setenv("APEX_MT5_SERVER", "Demo")

    class _Gate:
        @staticmethod
        def resolve_mt5_login_server() -> tuple[int, str]:
            return 1, "Demo"

        @staticmethod
        def assert_login_allowed(*_a: Any, **_k: Any) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "ftmo_demo_gate", _Gate)
    monkeypatch.setattr(mcp, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(mcp, "login", lambda *a, **k: True)
    monkeypatch.setattr(mcp, "last_error", lambda: (0, "ok"))

    # Force import path: mt5_connect imports apex_mcp
    m = at.mt5_connect()
    assert m is mcp
    at._mt5_holder["m"] = None
    monkeypatch.delenv("APEX_USE_MCP", raising=False)


def test_constants_match_metatrader5_ints() -> None:
    assert mcp.TRADE_ACTION_DEAL == 1
    assert mcp.TRADE_ACTION_SLTP == 6
    assert mcp.TRADE_RETCODE_DONE == 10009
    assert mcp.ORDER_TYPE_BUY == 0
    assert mcp.ORDER_TYPE_SELL == 1
    assert mcp.ORDER_TIME_GTC == 0
    assert mcp.ORDER_FILLING_IOC == 1
    assert mcp.DEAL_ENTRY_OUT == 1
