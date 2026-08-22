#!/usr/bin/env bash
# Push the data caches to the remote every few minutes, for as long as a backfill is running.
#
# Why this exists: this container is restored from a fixed snapshot on every death, and the
# snapshot has not moved since 21:00 UTC Aug 21. Six rollbacks in a row put the bar cache back to
# exactly 18294 contracts, so seven hours of fetching produced zero net progress. Committing the
# caches at each check-in does not fix it either -- a check-in only covers work done while the
# session turn is active, and the backfill does most of its work after the turn ends, in the
# window that the next rollback erases.
#
# The remote is the only durable store, so progress has to reach it continuously rather than at
# human-visible checkpoints. This loop is what converts container uptime into progress that
# survives.
#
# Failures are deliberately non-fatal: a push that loses a race with a check-in's own commit just
# retries on the next cycle, and an index.lock collision is not worth killing the loop over.
set -u
cd "$(dirname "$0")/.." || exit 1
BRANCH=claude/options-trading-strategies-axtvbh
INTERVAL=${1:-300}

while pgrep -f "fetch_data.py" > /dev/null; do
    sleep "$INTERVAL"
    git add data/cache/bars data/cache/contracts 2>/dev/null || continue
    if git diff --cached --quiet 2>/dev/null; then
        continue  # nothing new this cycle
    fi
    n_bars=$(find data/cache/bars -type f | wc -l)
    git commit -q -m "Cache: autocommit at ${n_bars} contracts

Written by scripts/cache_autocommit.sh while the backfill runs. Progress only
survives a rollback once it is on the remote.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011JCUiwbCPzxmPpEQ78en3G" 2>/dev/null || continue
    git push -u origin "$BRANCH" 2>/dev/null || true
done
