# APEX scalping research loop

An automated research loop for the L1 strategy library. Two scheduled agents
work on this `research` branch:

| Agent | When (Luxembourg) | Sees | Job |
|---|---|---|---|
| **Researcher** (`RESEARCHER.md`) | twice daily | 2018–2021 only | proposes ideas, implements, tests on the build period, logs everything |
| **Validator** (`VALIDATOR.md`) | daily, early morning | 2018–2024 | audits candidates, tests them blind on 2022–2024 plus cost stress, issues verdicts |

```
idea --(register)--> proposed --(build evals, max 6)--> build_fail | candidate
candidate --(validator: audit + confirm + stress)--> validated | rejected
any --(researcher)--> closed
```

## Why it's built this way

- **The holdout is enforced by data, not by trust.** Researcher sessions only download 2018–2021
  (`bootstrap.sh researcher`), and the confirm years are physically absent.
- **Every evaluation is logged automatically** (`ledger.jsonl`, append-only). The number of tries per
  idea raises the bar (edge t +0.3 per prior eval, cap 6), which limits curve fitting by brute force.
- **A separate validator** keeps the builder from grading its own work, and checks a source hash so a
  candidate can't be edited after it passes.
- **Gates come from the Section A findings** (`scalping/findings/README.md`): costs are about 1.35 pips per
  round trip, so a strategy needs a real per-trade edge well above that, consistent across years.

## Files

| Path | What |
|---|---|
| `bootstrap.sh` | fresh-session setup by role (deps, data from private `apex-data`, import, tests) |
| `evaluate.py` | runs a strategy on build / confirm / stress, applies the gates, writes `results/`, logs to the ledger |
| `ledger.py`, `ledger.jsonl`, `LEDGER.md` | idea and evaluation log, plus its rendered view |
| `config_build.json`, `config_confirm.json`, `config_confirm_stress.json` | quarterly blocks and costs |
| `strategies/` | research strategies (plug-ins; `_template.py` shows the API) |
| `results/` | one JSON per evaluation |
| `runs/` | one report per agent run: the place to see what happened |

## For Kian: daily review

Open `research/LEDGER.md` for the status table and the newest files in `research/runs/` for the reasoning.
**Validated** means it passed both gates; it still needs your review and a demo run with real broker costs
before any capital.
