#!/usr/bin/env bash
# autopush.sh — watch the repo, debounce, then git commit + push.
#
# Usage:
#   chmod +x autopush.sh
#   ./autopush.sh
#
# Env:
#   AUTOPUSH_DEBOUNCE  seconds to wait after a change before commit (default 2)
#   AUTOPUSH_POLL      polling interval if inotifywait missing (default 5)
#
# Requires: git remote configured; credentials for push (SSH key or gh/ghp).
# Install inotifywait: sudo apt install inotify-tools   (Debian/Ubuntu)

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DEBOUNCE="${AUTOPUSH_DEBOUNCE:-2}"
POLL_INTERVAL="${AUTOPUSH_POLL:-5}"

# Paths we never want to trigger commits from (regex for inotifywait --exclude)
EXCLUDE_REGEX='(\.git/|\.cursor/|__pycache__/|\.venv/|venv/|node_modules/|\.mypy_cache/|\.pytest_cache/|\.ruff_cache/|dist/|build/|\.egg-info/)'

commit_push() {
  # Skip if nothing to record
  if git diff --quiet && git diff --cached --quiet; then
    [[ -z "$(git ls-files --others --exclude-standard)" ]] && return 0
  fi

  git add -A

  if git diff --cached --quiet; then
    return 0
  fi

  local branch msg
  branch="$(git branch --show-current 2>/dev/null || echo main)"
  msg="wip: auto-save $(date -Iseconds)"

  if git commit -m "$msg"; then
    echo "[autopush] committed on $branch"
    if git push -u origin "$branch"; then
      echo "[autopush] pushed origin/$branch"
    else
      echo "[autopush] WARNING: push failed — fix auth/remote and retry" >&2
    fi
  fi
}

loop_inotify() {
  echo "[autopush] watching $ROOT (inotifywait, debounce ${DEBOUNCE}s). Ctrl+C to stop."
  while true; do
    # Blocks until a matching filesystem event, then coalesce rapid saves.
    if inotifywait -r -q -e close_write -e moved_to \
      --exclude "$EXCLUDE_REGEX" \
      "$ROOT" 2>/dev/null; then
      :
    else
      echo "[autopush] inotifywait exited; sleeping 5s before retry..." >&2
      sleep 5
      continue
    fi
    sleep "$DEBOUNCE"
    commit_push || true
  done
}

loop_poll() {
  echo "[autopush] watching $ROOT (poll every ${POLL_INTERVAL}s). Install inotify-tools for instant triggers. Ctrl+C to stop."
  local prev=""
  while true; do
    local cur
    cur="$(git status --porcelain 2>/dev/null | sha256sum | awk '{print $1}')"
    if [[ -n "$(git status --porcelain 2>/dev/null)" && "$cur" != "$prev" ]]; then
      prev="$cur"
      sleep "$DEBOUNCE"
      commit_push || true
      prev="$(git status --porcelain 2>/dev/null | sha256sum | awk '{print $1}')"
    fi
    sleep "$POLL_INTERVAL"
  done
}

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[autopush] ERROR: not a git repository: $ROOT" >&2
  exit 1
fi

if command -v inotifywait >/dev/null 2>&1; then
  loop_inotify
else
  echo "[autopush] inotifywait not found; using polling fallback." >&2
  loop_poll
fi
