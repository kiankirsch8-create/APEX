#!/usr/bin/env bash
# Prepare a fresh session for research.  Usage:  research/bootstrap.sh researcher|validator
#
#   researcher  downloads and imports ONLY 2018-2021 (the build period). The
#               confirm years 2022-2024 are physically absent, so they cannot
#               leak into strategy design.
#   validator   downloads and imports everything.
#
# Data source: private repo kiankirsch8-create/apex-data, layout
#   histdata/EURUSD/<year>/DAT_ASCII_EURUSD_T_<YYYYMM>.zip
# Idempotent: re-running skips work already done.
set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in
  researcher) YEARS="2018 2019 2020 2021" ;;
  validator)  YEARS="2018 2019 2020 2021 2022 2023 2024" ;;
  *) echo "usage: $0 researcher|validator" >&2; exit 64 ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA_URL="${APEX_DATA_URL:-https://github.com/kiankirsch8-create/apex-data}"
DATA_DIR="${APEX_DATA_CLONE:-$REPO/../apex-data}"
cd "$REPO"

echo "== deps"
python3 -c "import numba, pandas, pyarrow" 2>/dev/null \
  || pip install -q --break-system-packages -r scalping/requirements.txt

echo "== data clone ($ROLE: $YEARS)"
if [ ! -d "$DATA_DIR/.git" ]; then
  for attempt in 1 2; do
    git clone --depth 1 --filter=blob:none --sparse "$DATA_URL" "$DATA_DIR" && break
    echo "clone failed (attempt $attempt); retrying in 10s" >&2; rm -rf "$DATA_DIR"; sleep 10
  done
fi
SPARSE=""
for y in $YEARS; do SPARSE="$SPARSE histdata/EURUSD/$y"; done
git -C "$DATA_DIR" sparse-checkout set $SPARSE

echo "== import"
MARK="$REPO/scalping_data/.imported_$ROLE"
if [ ! -f "$MARK" ]; then
  ZIPS=()
  for y in $YEARS; do ZIPS+=("$DATA_DIR"/histdata/EURUSD/"$y"/DAT_ASCII_EURUSD_T_*.zip); done
  python3 -W ignore -m scalping.data.histdata "${ZIPS[@]}" --data-dir "$REPO/scalping_data" | tail -3
  touch "$MARK"
fi

if [ "$ROLE" = researcher ]; then
  # The Dec 2021 file legitimately writes the first hours of 2022-01-01 (a holiday); anything else is a leak.
  LEAK=$(find "$REPO"/scalping_data/ticks/pair=EURUSD -name '202[2-9]-*.parquet' ! -name '2022-01-01.parquet' | head -1)
  if [ -n "$LEAK" ]; then
    echo "ERROR: confirm-period data (2022+) present in a researcher session. Stop." >&2
    exit 65
  fi
fi

echo "== tests"
python3 -m pytest -q scalping/tests research/tests 2>&1 | tail -1
echo "== ready ($ROLE)"
