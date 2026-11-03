#!/usr/bin/env bash
# Re-run the 2018–2019 OOS go/no-go slice with PRODUCTION (caps-on) chrono config.
#
# Prerequisites:
#   - ANTHROPIC_API_KEY set
#   - This branch (or main after merge) with DIAGNOSTIC_ALLOW_MULTI_TF_PER_DAY=False
#     and _chrono_currency_cap_blocks active (no early return)
#   - Writable APEX_RESULTS_DIR (default /data, else ./results)
#
# After completion, analyze funded pass rate and compare to the 95.4% ceiling figure.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

JOB_ID="${JOB_ID:-oos2018_caps_on}"
START="${START:-2018-01-01}"
END="${END:-2019-12-31}"
API="${API:-http://127.0.0.1:8000}"

echo "== Verify production caps =="
python3 - <<'PY'
import continuous_backtester as cb
assert cb.DIAGNOSTIC_ALLOW_MULTI_TF_PER_DAY is False, "multi-TF/day still allowed"
src = open("continuous_backtester.py", encoding="utf-8").read()
assert "currency cap disabled to measure ceiling" not in src
assert "DIAGNOSTIC R01 block" not in src
print("OK: caps-on production config")
print("  DIAGNOSTIC_ALLOW_MULTI_TF_PER_DAY =", cb.DIAGNOSTIC_ALLOW_MULTI_TF_PER_DAY)
PY

echo "== Start chrono ${JOB_ID}: ${START} → ${END} =="
# Prefer API when the server is up; fall back to direct Python.
if curl -sf "${API}/api/health" >/dev/null 2>&1 || curl -sf "${API}/docs" >/dev/null 2>&1; then
  curl -sf -X POST \
    "${API}/api/chrono/start?start_date=${START}&end_date=${END}&job_id=${JOB_ID}"
  echo
  echo "Poll: curl -s ${API}/api/chrono/${JOB_ID} | jq .status,.portfolio_construction,.summary"
else
  echo "API not up — running in-process (long; blocks this shell)."
  python3 - <<PY
from continuous_backtester import run_chronological_backtest
out = run_chronological_backtest("${START}", "${END}", job_id="${JOB_ID}")
print("status=", out.get("status"), "capital=", out.get("final_capital") or out.get("capital"))
print("portfolio_construction=", out.get("portfolio_construction"))
print("summary=", out.get("summary"))
PY
fi

echo "== After status=complete, analyze funded challenge =="
echo "python3 -m chrono_analysis ${JOB_ID}"
echo "# Then compare funded_pass_rate_pct vs 95.4 ceiling; apply GO/NO-GO:"
echo "#   GO unless pass_rate < 40% OR net-negative OR genuine funded-DD breach"
echo "#   (rolling fresh-\$10k pass rate, not raw compounding-curve DD)."
