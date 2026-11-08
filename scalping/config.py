"""Load the scalping run configuration (pairs, blocks, costs)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scalping.costs import CostModel

_PACKAGE_CONFIG = Path(__file__).with_name("config.json")


@dataclass(frozen=True)
class Block:
    """Half-open UTC window ``[start, end)``."""

    id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ScalpingConfig:
    pairs: tuple[str, ...]
    blocks: tuple[Block, ...]
    costs: CostModel
    initial_equity: float
    data_dir: Path
    source_path: Path
    request_delay: float = 0.3
    max_retries: int = 6
    max_backoff: float = 60.0

    def block(self, block_id: str) -> Block:
        for block in self.blocks:
            if block.id == block_id:
                return block
        known = ", ".join(b.id for b in self.blocks)
        raise KeyError(f"Unknown block {block_id!r}. Known: {known}")


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if len(text) == 10:
        text = text + "T00:00:00"
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_config(path: str | Path | None = None) -> ScalpingConfig:
    source = Path(path) if path else _PACKAGE_CONFIG
    raw = json.loads(source.read_text(encoding="utf-8"))
    blocks = tuple(
        Block(id=str(item["id"]), start=_parse_utc(item["start"]), end=_parse_utc(item["end"]))
        for item in raw["blocks"]
    )
    for block in blocks:
        if block.end <= block.start:
            raise ValueError(f"Block {block.id} end must be after start")
    costs = CostModel(
        commission_per_lot_side=float(raw.get("commission_per_lot_side", 3.5)),
        commission_currency=str(raw.get("commission_currency", "USD")).upper(),
        slippage_points=float(raw.get("slippage_points", 2.0)),
        account_currency=str(raw.get("account_currency", "EUR")).upper(),
    )
    data_dir = Path(raw.get("data_dir", "scalping_data"))
    return ScalpingConfig(
        pairs=tuple(str(p).upper() for p in raw["pairs"]),
        blocks=blocks,
        costs=costs,
        initial_equity=float(raw.get("initial_equity", 100_000.0)),
        data_dir=data_dir,
        source_path=source,
        request_delay=float(raw.get("request_delay", 0.3)),
        max_retries=int(raw.get("max_retries", 6)),
        max_backoff=float(raw.get("max_backoff", 60.0)),
    )
