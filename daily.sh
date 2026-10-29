#!/bin/bash
# Everything the board needs, in dependency order, once a day.
#
# The expensive stage is the first run of `fetch.py boxes`, which pulls
# every box score of three seasons (~2,700 games, a few minutes). After
# that it only fetches last night's, and the whole chain is about two
# minutes, most of it the backtest.
#
# set -e, so a non-zero exit means a named stage failed and nothing
# downstream ran on stale inputs.
set -euo pipefail
cd "$(dirname "$0")"
# Overridable, so the same script runs in a checkout with no venv --
# a CI runner installs into the system interpreter.
PY="${PY:-./.venv/bin/python}"
JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc
LOG=data/daily.log
# A fresh checkout has neither: both hold only generated files, so git
# does not carry them, and the first stage to write one would fail.
mkdir -p data out

# One run at a time. The chain rewrites the same SQLite files and the same
# index.html, so two overlapping runs -- the scheduled one and a click of
# the board's Refresh button -- would interleave writes. mkdir is the
# atomic test-and-set that is on every machine; flock is not on macOS.
# Exit 3 says "already running", which is not the same as a failure.
LOCK=data/.running
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "daily.sh: a run is already in progress ($LOCK); doing nothing" >&2
  exit 3
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

stage() { printf '\n=== %s  %s ===\n' "$1" "$(date '+%H:%M:%S')"; }

{
  printf '\n\n######## %s ########\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"

  stage "schedule and last night's box scores"
  $PY fetch.py games
  $PY fetch.py boxes

  stage "positions for anyone new"
  $PY fetch.py positions | tail -1

  stage "features and current state"
  $PY features.py

  stage "refit and score, out of sample"
  # Writes data/calib.json, which everything downstream prices from.
  $PY backtest.py > out/scorecard.txt 2>&1
  sed -n '/^Brier/p' out/scorecard.txt

  stage "team defence ratings"
  # sed, not head: head closes the pipe after its line, the producer takes
  # a broken pipe mid-run, and pipefail turns a stage that worked into a
  # failed one. sed reads to the end and prints what was asked for.
  $PY defense.py | sed -n '1p'

  stage "who is out tonight"
  # Books post NBA injury news late; an early run may find little.
  $PY fetch.py injuries || echo "  (injury report unavailable this run)"

  stage "tonight's slate"
  $PY project.py | tail -3

  stage "real prop lines, if a key is set"
  # The only stage that costs money, and the only one with a budget. Five
  # markets cost five credits a game and a season is 166 nights, so a free
  # tier buys about three games a night; ODDS_BUDGET is the cap in credits
  # and fetch_odds spends it on the least-sampled clubs. Skipped silently
  # without a key, and the rest of the chain is unaffected.
  BUDGET="${ODDS_BUDGET:-15}"
  case "$BUDGET" in
    ''|*[!0-9]*) echo "  (ODDS_BUDGET='$BUDGET' is not a number; using 15)"
                 BUDGET=15 ;;
  esac
  if [ "$BUDGET" -eq 0 ]; then
    # A free refresh: reprice tonight against the lines already stored
    # rather than buying new ones. The board stays honest about their age.
    echo "  (ODDS_BUDGET=0; spending nothing, repricing the stored lines)"
    $PY edges.py | sed -n '1,6p'
  elif [ -n "${ODDS_API_KEY:-}" ] || [ -f .env ]; then
    $PY fetch_odds.py --budget="$BUDGET" | tail -4
    $PY edges.py | sed -n '1,6p'
  else
    echo "  (ODDS_API_KEY not set; no market prices this run)"
  fi

  stage "grade whatever has finished"
  $PY grade.py | tail -12

  stage "the page"
  $PY export_tool.py
  $PY tool/build.py

  stage "publish to the live site"
  # Copying is free and always safe; pushing is an outward-facing action
  # and only happens when it has been asked for. A push is a deploy.
  if [ "${PUBLISH_PUSH:-0}" = "1" ]; then
    $PY publish.py --push | tail -4
  else
    $PY publish.py | tail -3
    echo "  (set PUBLISH_PUSH=1 to commit and push this to the live site)"
  fi

  stage "tests"
  $PY tests.py | tail -2
  $PY tool/maketruth.py > /dev/null
  if [ -x "$JSC" ]; then $JSC tool/mathtest.js; else
    echo "  (jsc not present; skipped the browser maths test)"; fi

  printf '\nOK  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
} 2>&1 | tee -a "$LOG"
