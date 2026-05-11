"""Claude vision analysis for uploaded price charts (claude-opus-4-5)."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

from anthropic import Anthropic

from utils import env, log, utcnow_iso

CLAUDE_MODEL = "claude-opus-4-5"
MAX_TOKENS = 2500

_CLIENT: Anthropic | None = None


def _client() -> Anthropic:
    global _CLIENT
    if _CLIENT is None:
        key = env("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _CLIENT = Anthropic(api_key=key)
    return _CLIENT


def _strip_data_url(b64: str) -> tuple[str, str | None]:
    """Return raw base64 and optional explicit media type from a data URL."""
    s = (b64 or "").strip()
    if s.startswith("data:"):
        # data:image/png;base64,XXXX
        head, _, rest = s.partition(",")
        mt = None
        if ";" in head:
            mt = head[5:].split(";")[0].strip() or None
        return rest.strip(), mt
    return s, None


def _detect_media_type(raw_b64: str, hint: str | None) -> str:
    if hint and hint in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return hint
    try:
        pad = (-len(raw_b64)) % 4
        blob = base64.b64decode(raw_b64 + "=" * pad, validate=False)[:12]
    except Exception:  # noqa: BLE001
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image/gif"
    if blob.startswith(b"RIFF") and b"WEBP" in blob[:12]:
        return "image/webp"
    return "image/png"


def _parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    match = re.search(r"\{[\s\S]*\}", txt)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


CHART_SYSTEM = """You are an elite technical analyst reviewing a price chart screenshot.
Infer likely price levels from visible axis labels, candlesticks, and overlays when possible.
Be decisive: give concrete numbers for levels that fit the chart, not vague ranges unless the chart is unreadable.
Return ONLY valid raw JSON with exactly these keys (no markdown, no commentary):
{
  "pattern_name": "string (e.g. ascending triangle, bull flag)",
  "pattern_confidence": number 0-100,
  "support_levels": [array of numbers, nearest first],
  "resistance_levels": [array of numbers, nearest first],
  "entry_zone": "string describing optimal entry zone with approximate prices",
  "stop_loss": number,
  "take_profit_1": number,
  "take_profit_2": number,
  "technical_verdict": "one or two sentences: bias, key risk, and what would invalidate the read"
}
All numeric fields must be JSON numbers (not strings)."""


def _normalize_result(parsed: dict[str, Any], ticker: str, timeframe: str) -> dict[str, Any]:
    def _nums(v: Any) -> list[float]:
        out: list[float] = []
        if isinstance(v, list):
            for x in v:
                try:
                    out.append(float(x))
                except (TypeError, ValueError):
                    continue
        return out

    def _fopt(key: str) -> float | None:
        v = parsed.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "pattern_name": str(parsed.get("pattern_name") or "unknown"),
        "pattern_confidence": float(parsed.get("pattern_confidence") or 0),
        "support_levels": _nums(parsed.get("support_levels")),
        "resistance_levels": _nums(parsed.get("resistance_levels")),
        "entry_zone": str(parsed.get("entry_zone") or ""),
        "stop_loss": _fopt("stop_loss"),
        "take_profit_1": _fopt("take_profit_1"),
        "take_profit_2": _fopt("take_profit_2"),
        "technical_verdict": str(parsed.get("technical_verdict") or ""),
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "analyzed_at": utcnow_iso(),
    }


async def analyze_chart_image(
    image_base64: str,
    ticker: str,
    timeframe: str,
) -> dict[str, Any]:
    """Send chart image to Claude with vision; return structured technical read."""
    raw_b64, hint = _strip_data_url(image_base64)
    if not raw_b64:
        raise ValueError("image_base64 is empty")

    try:
        decoded = base64.b64decode(raw_b64 + "=" * ((-len(raw_b64)) % 4), validate=False)
    except Exception as e:  # noqa: BLE001
        raise ValueError("invalid base64 image data") from e
    if len(decoded) > 15 * 1024 * 1024:
        raise ValueError("image exceeds 15MB decoded limit")

    media_type = _detect_media_type(raw_b64, hint)
    t = (ticker or "").upper() or "UNKNOWN"
    tf = (timeframe or "").strip() or "unspecified"

    user_text = (
        f"Chart symbol context: {t}. Stated timeframe: {tf}.\n"
        "Analyze the attached chart image and output ONLY the JSON object described in your instructions."
    )

    def _do_call() -> str:
        client = _client()
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=CHART_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": raw_b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
        if not msg.content:
            return ""
        parts: list[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "\n".join(parts).strip()

    try:
        out = await asyncio.to_thread(_do_call)
    except Exception as e:  # noqa: BLE001
        log(f"[ChartVision] Claude call failed: {e}", "error")
        raise RuntimeError("Chart vision model request failed") from e

    parsed = _parse_json(out)
    if parsed is None:
        log("[ChartVision] JSON parse failed — retrying once", "warning")
        retry_text = user_text + "\n\nReturn ONLY raw JSON. No markdown fences."

        def _retry() -> str:
            client = _client()
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=CHART_SYSTEM + " Never wrap JSON in code fences.",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": raw_b64,
                                },
                            },
                            {"type": "text", "text": retry_text},
                        ],
                    }
                ],
            )
            if not msg.content:
                return ""
            parts2: list[str] = []
            for block in msg.content:
                if getattr(block, "type", None) == "text":
                    parts2.append(block.text)
            return "\n".join(parts2).strip()

        out2 = await asyncio.to_thread(_retry)
        parsed = _parse_json(out2)

    if parsed is None:
        raise RuntimeError("Model did not return parseable JSON")

    return _normalize_result(parsed, t, tf)
