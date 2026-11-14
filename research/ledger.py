"""Research ledger: one append-only JSONL file, rendered to LEDGER.md.

Model
-----
* An **idea is a family**: one market mechanism (e.g. "London-open sweep of the Asian range").
  Ids carry the section: A### mean reversion, B### liquidity/flow, C### momentum/breakout, X### other.
  (R001-R003 are pre-section ids from the first run.)
* A family gets **one first build test plus at most two revisions**. Each revision must cite a
  post-mortem of the previous failure (kind "postmortem"). The third failed build test (or a
  failed robustness run counted the same way) sends the family to the **graveyard** automatically.
* A build eval only counts if the strategy passed `research.check` for that exact source hash.
* Status flow: proposed -> (scanned) -> build_fail (strikes left) | candidate -> robust | robust_fail
  -> validated | rejected;  graveyard / closed at any time.
* Confirm/stress results are written here as pass/fail only. Their numbers live on the separate
  `holdout` branch, so researchers never see 2022-2024 details.

    python -m research.ledger idea --section B --title "..." --mechanism "..." --hypothesis "..." --rules "..." [--source kian]
    python -m research.ledger postmortem --id B001 --strategy NAME --failure-mode MODE --diagnosis "..." --constraint "..."
    python -m research.ledger close --id B001 --reason "..."
    python -m research.ledger validate --id B001 --strategy NAME --verdict validated|rejected --reason "..."
    python -m research.ledger render
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "ledger.jsonl"
RENDERED = ROOT / "LEDGER.md"
SECTIONS = {"A": "Mean reversion", "B": "Liquidity / flow", "C": "Momentum / breakout", "X": "Other", "R": "Pre-section"}
MAX_BUILD_FAILS = 3          # first test + 2 revisions
FAILURE_MODES = (
    "no_edge",            # edge before costs ~0 or negative: the signal does not predict
    "edge_below_costs",   # positive edge before costs but smaller than the ~1.05-1.1 pip round trip
    "wrong_sign",         # edge significantly negative: the opposite effect exists (do NOT just flip it)
    "regime_dependent",   # edge positive in some years, negative in others
    "session_dependent",  # edge concentrated in a session, diluted elsewhere
    "news_dependent",     # result driven by trades near high-impact events
    "adverse_selection",  # entries (often limits) fill mostly on moves that keep going
    "too_few_trades",     # not enough events to judge
    "overfit",            # failed robustness: collapses under small parameter changes
    "implementation",     # bug / logic did not match the hypothesis (does not count as a strike)
)


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def append(entry: dict) -> dict:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": _commit(), **entry}
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def entries() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def _section(idea_id: str) -> str:
    return idea_id[:1] if idea_id[:1] in SECTIONS else "X"


def ideas() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in entries():
        if e["kind"] == "idea":
            out[e["id"]] = {**e, "section": e.get("section") or _section(e["id"]), "status": "proposed",
                            "evals": defaultdict(int), "best": None, "strategies": set(), "build_fails": 0,
                            "postmortems": [], "scans": 0, "last_build_ts": None, "checks": {}}
    for e in entries():
        idea = out.get(e.get("id") or e.get("idea"))
        if idea is None:
            continue
        kind = e["kind"]
        if kind == "scan":
            idea["scans"] += 1
            if idea["status"] == "proposed":
                idea["status"] = "scanned"
        elif kind == "check":
            idea["checks"][e["source_sha"]] = e["verdict"]
        elif kind == "postmortem":
            idea["postmortems"].append(e)
        elif kind == "eval":
            period = e["period"]
            idea["evals"][period] += 1
            idea["strategies"].add(e["strategy"])
            if period == "build":
                idea["last_build_ts"] = e["ts"]
                if e["verdict"] == "pass":
                    idea["status"] = "candidate"
                    idea["best"] = e
                else:
                    idea["build_fails"] += 1
                    if idea["status"] in ("proposed", "scanned", "build_fail"):
                        idea["status"] = "build_fail"
                    if idea["best"] is None or (idea["best"]["verdict"] != "pass" and e["metrics"].get("net_pips", -9)
                                                > idea["best"]["metrics"].get("net_pips", -9)):
                        idea["best"] = e
            elif period == "robust":
                if e["verdict"] == "pass":
                    idea["status"] = "robust"
                else:
                    idea["status"] = "robust_fail"
                    idea["build_fails"] += 1      # an overfit pass is a failed attempt
        elif kind == "validate":
            idea["status"] = e["verdict"]
            idea["validation"] = e
        elif kind == "close":
            if idea["status"] not in ("validated",):
                idea["status"] = "closed"
            idea["closed"] = e
    for idea in out.values():
        # one "implementation" post-mortem (a bug, not a market result) refunds one failed build, at most once
        if any(p.get("failure_mode") == "implementation" for p in idea["postmortems"]) and idea["build_fails"] > 0:
            idea["build_fails"] -= 1
        if idea["build_fails"] >= MAX_BUILD_FAILS and idea["status"] in ("build_fail", "robust_fail"):
            idea["status"] = "graveyard"
    return out


def build_evals(idea_id: str) -> int:
    return sum(1 for e in entries() if e["kind"] == "eval" and e.get("idea") == idea_id and e["period"] == "build")


def can_build(idea_id: str, source_sha: str) -> tuple[bool, str]:
    """Rules a build eval must satisfy before it may run (enforced by evaluate.py)."""
    idea = ideas().get(idea_id)
    if idea is None:
        return False, f"unknown idea {idea_id}"
    if idea["status"] in ("graveyard", "closed", "validated", "rejected"):
        return False, f"idea {idea_id} is {idea['status']}; register a new family with a different mechanism"
    if idea["status"] in ("candidate", "robust"):
        return False, f"idea {idea_id} already has a passing variant; leave it to robustness/validation"
    if idea["checks"].get(source_sha) != "pass":
        return False, "strategy has no passing `python -m research.check` for this exact code; run it first"
    if idea["build_fails"] >= MAX_BUILD_FAILS:
        return False, f"idea {idea_id} used its {MAX_BUILD_FAILS} build attempts"
    if idea["last_build_ts"] is not None:
        cited = [p for p in idea["postmortems"] if p["ts"] >= idea["last_build_ts"] and p.get("failure_mode") != "implementation"]
        bug = [p for p in idea["postmortems"] if p["ts"] >= idea["last_build_ts"] and p.get("failure_mode") == "implementation"]
        if not cited and not bug:
            return False, ("a revision needs a post-mortem of the previous failure first: "
                           "`python -m research.ledger postmortem --id ... --constraint ...`")
    return True, "ok"


def next_id(section: str) -> str:
    section = section.upper()
    nums = [int(e["id"][1:]) for e in entries() if e["kind"] == "idea" and e["id"][:1] == section and e["id"][1:].isdigit()]
    return f"{section}{(max(nums) + 1) if nums else 1:03d}"


def render() -> str:
    all_ideas = ideas()
    lines = ["# Research ledger", "",
             "Generated by `python -m research.ledger render` from `ledger.jsonl`. Do not edit by hand.",
             "Shared knowledge (graveyard, leads, facts): `MEMORY.md`. Current plan: `AGENDA.md`.", ""]
    counts = defaultdict(int)
    for i in all_ideas.values():
        counts[i["status"]] += 1
    lines += ["Totals: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none yet"), ""]
    for sec, label in SECTIONS.items():
        rows = sorted((i for i in all_ideas.values() if i["section"] == sec), key=lambda i: i["id"])
        if not rows:
            continue
        lines += [f"## {sec}: {label}", "",
                  "| id | title | status | fails | scans | best build net / edge (t) | strategies |",
                  "|---|---|---|---:|---:|---|---|"]
        for i in rows:
            b = i["best"]["metrics"] if i["best"] else None
            best = f"{b['net_pips']:+.2f} / {b['edge_pips']:+.2f} ({(b.get('edge_t') or 0):+.1f})" if b else "-"
            lines.append(f"| {i['id']} | {i['title']} | **{i['status']}** | {i['build_fails']}/{MAX_BUILD_FAILS} | "
                         f"{i['scans']} | {best} | {', '.join(sorted(i['strategies'])) or '-'} |")
        lines.append("")
    lines += ["## Idea details", ""]
    for i in sorted(all_ideas.values(), key=lambda i: i["id"]):
        lines += [f"### {i['id']}: {i['title']}" + (f"  _(source: {i['source']})_" if i.get("source") else ""), "",
                  f"- **Mechanism:** {i.get('mechanism', '')}",
                  f"- **Hypothesis:** {i.get('hypothesis', '')}",
                  f"- **Rules:** {i.get('rules', '')}",
                  f"- **Status:** {i['status']} ({i['build_fails']}/{MAX_BUILD_FAILS} failed builds, {i['scans']} scans)"]
        for p in i["postmortems"]:
            lines.append(f"- **Post-mortem {p['ts'][:10]} ({p['strategy']}, {p['failure_mode']}):** {p['diagnosis']} "
                         f"→ _constraint:_ {p['constraint']}")
        if i.get("validation"):
            v = i["validation"]
            lines.append(f"- **Validator ({v['ts'][:10]}):** {v['verdict']}: {v.get('reason', '')}")
        if i.get("closed"):
            lines.append(f"- **Closed ({i['closed']['ts'][:10]}):** {i['closed'].get('reason', '')}")
        lines.append("")
    text = "\n".join(lines)
    RENDERED.write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("idea")
    p.add_argument("--section", required=True, choices=list("ABCX"))
    p.add_argument("--id", default=None, help="defaults to the next free id in the section")
    p.add_argument("--source", default="agent", help="who proposed it, e.g. kian / agent / lead")
    for f in ("title", "mechanism", "hypothesis", "rules"):
        p.add_argument(f"--{f}", required=True)
    p = sub.add_parser("postmortem")
    p.add_argument("--id", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--failure-mode", required=True, choices=FAILURE_MODES)
    p.add_argument("--diagnosis", required=True, help="what the numbers show, entry-time features only")
    p.add_argument("--constraint", required=True, help="binding rule for the next revision of this family")
    p = sub.add_parser("close")
    p.add_argument("--id", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("validate")
    p.add_argument("--id", required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--verdict", choices=["validated", "rejected"], required=True)
    p.add_argument("--reason", required=True, help="audit findings + which gate failed; NO confirm-period numbers")
    sub.add_parser("render")
    p = sub.add_parser("next-id")
    p.add_argument("--section", required=True, choices=list("ABCX"))
    a = ap.parse_args(argv)

    known = ideas()
    if a.cmd == "idea":
        idea_id = a.id or next_id(a.section)
        if idea_id in known:
            ap.error(f"idea {idea_id} already exists")
        append({"kind": "idea", "id": idea_id, "section": a.section, "source": a.source, "title": a.title,
                "mechanism": a.mechanism, "hypothesis": a.hypothesis, "rules": a.rules})
        print(idea_id)
    elif a.cmd == "postmortem":
        if a.id not in known:
            ap.error(f"unknown idea {a.id}")
        append({"kind": "postmortem", "id": a.id, "strategy": a.strategy, "failure_mode": a.failure_mode,
                "diagnosis": a.diagnosis, "constraint": a.constraint})
    elif a.cmd in ("close", "validate"):
        if a.id not in known:
            ap.error(f"unknown idea {a.id}")
        if a.cmd == "close":
            append({"kind": "close", "id": a.id, "reason": a.reason})
        else:
            append({"kind": "validate", "id": a.id, "strategy": a.strategy, "verdict": a.verdict, "reason": a.reason})
    elif a.cmd == "next-id":
        print(next_id(a.section))
        return 0
    render()
    if a.cmd == "render":
        print(RENDERED.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
