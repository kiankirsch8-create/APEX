"""Compact JSON option for large chrono results."""
from __future__ import annotations

import json
from pathlib import Path

from utils import save_json


def test_save_json_default_is_indented(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    save_json(path, {"a": 1, "b": [2, 3]})
    text = path.read_text(encoding="utf-8")
    assert "\n" in text
    assert '  "a"' in text or '"a": 1' in text
    assert json.loads(text) == {"a": 1, "b": [2, 3]}


def test_save_json_compact_omits_indent(tmp_path: Path) -> None:
    path = tmp_path / "chrono.json"
    payload = {"status": "running", "trades": [{"x": i} for i in range(5)]}
    save_json(path, payload, compact=True)
    text = path.read_text(encoding="utf-8")
    assert "\n  " not in text  # no indented pretty-print
    assert json.loads(text) == payload
    # Compact should be smaller than indented for the same payload.
    pretty = tmp_path / "pretty.json"
    save_json(pretty, payload, compact=False)
    assert path.stat().st_size < pretty.stat().st_size
