# Weeklies backfill runbook

The 5-ticker weeklies extension (QQQ DIA IWM XLK GLD, 18 months, puts only) runs against
Polygon's free tier at 4 calls/minute. This file is the standing procedure so the scheduled
check-in prompts can stay short — they are re-sent on every wake, and there will be a couple of
hundred of them.

## The constraint that governs everything

This container is restored from a **fixed snapshot** on every death. Rollbacks #4 through #7 all
landed on the same commit with the bar cache at exactly 18,294 contracts. Anything written to
disk and not pushed is gone at the next death. Between 21:00 UTC Aug 21 and 04:35 UTC Aug 22 —
seven hours — net progress was **zero** for exactly this reason.

The remote is the only durable store. Two mechanisms follow from that, and both must be running:

1. **The caches are tracked in git.** `data/cache/bars` (the fetched contract history) and
   `data/cache/contracts` (contract listings). A rollback is undone by
   `git fetch origin <branch> && git reset --hard origin/<branch>`.
2. **`scripts/cache_autocommit.sh` pushes every 5 minutes** while `fetch_data.py` runs. This is
   the load-bearing piece. A check-in can only commit work that exists during an active session
   turn, but the backfill does most of its fetching *after* the turn ends — which is precisely
   the window a rollback erases. Without the supervisor, that work is never durable.

## The arithmetic

| Quantity | Value |
|---|---|
| Remaining contracts | ~18,650 (37,000 target − 18,350 done) |
| API calls needed | ~18,650, one per contract |
| **Pure uptime required** | **~78 hours** — the hard floor at 4 calls/min |
| Container lifetime per wake | ~20–25 minutes before reclamation |
| Check-in cadence | 35 minutes, giving ~60–65% uptime |
| Wall-clock estimate | ~5–6 days |

The 78-hour floor cannot be negotiated down by scheduling; only uptime fraction moves. That is
why the cadence is short. It is also why check-in turns should stay lean — a few hundred wakes
at a few tens of thousands of tokens each is the real budget being spent.

## Fresh-container recovery (rarer than a rollback, and worse)

Most wakes land on a container that still has the virtualenv and `.env`. Occasionally one comes
up genuinely bare — `.venv` missing, `.env` missing — because the restore point was the repo's
2023 root commit rather than the usual recent snapshot. Symptoms: `exit 127` from the fetcher,
then `OPTIONSBOT_POLYGON_API_KEY is not set`.

```
python3 -m venv .venv && .venv/bin/pip install -q -e ".[data]"
.venv/bin/pip install -q alpaca-py python-dotenv     # for the moonshot bot
```

`.env` is gitignored, so it cannot be restored from the remote and **its secrets are simply
gone**. The Alpaca paper keys are recoverable from the conversation history; the Polygon key is
not, and the backfill cannot run without it — ask the user rather than guessing. Verify Alpaca
with a `get_account()` call before trusting the moonshot Routines, because they will otherwise
fail silently at the market open.

## Procedure on each check-in

1. `git log --oneline -1`. Older than the remote tip → rollback: `git fetch origin <branch> &&
   git reset --hard origin/<branch>`. Carry the tally.
2. Check both processes with this exact command — **not** `pgrep -f fetch_data.py`:
   ```
   ps -eo pid,etime,cmd --no-headers | grep -E 'python.*fetch_data|bash scripts/cache_autocommit' | grep -v grep
   ```
   `pgrep -f` matches the check-in's own shell, whose command line contains the pattern, so it
   reports both processes alive when both are dead. That produced a false "still running" report
   on cycle #117. The `python.*` / `bash scripts/` anchors match only the real processes.

   If the fetcher is dead without `done:` in its log, relaunch **both** — fetcher first, then the supervisor ~8s later
   so its `pgrep` guard sees a live process:
   ```
   nohup .venv/bin/python scripts/fetch_data.py --tickers QQQ DIA IWM XLK GLD \
       --months 18 --puts-only --weeklies --force >> <scratch>/fetch_weekliesN.log 2>&1 &
   nohup bash scripts/cache_autocommit.sh 300 >> <scratch>/autocommit.log 2>&1 &
   ```
3. Commit anything the supervisor has not: `git add data/cache/bars data/cache/contracts`.
4. Measure `find data/cache/bars -type f | wc -l` against the previous check-in's number.
   **Measure against the remote, not the local tree** — a rolled-back local count says nothing.
5. Report to the user only on a material change: the rate moving, the rate stalling, completion,
   or a new failure mode. Routine "still running" wakes need no message.

## On completion (`done: written=...`)

Run the full 6-ticker validation, in background (each takes >120s):

```
.venv/bin/python scripts/run_backtest.py --months 6 --slippage pessimistic
.venv/bin/python scripts/run_walkforward.py --months 18 --slippage pessimistic
```

Monthly-only baselines to compare against (commit 422dec9): 6-month backtest 8 trades,
+$204.48, 71.4% win, PASS; walk-forward +$41.32/trade out-of-sample, PASS, but only 1 of 4 folds
judgeable. **The question weeklies exist to answer:** do they raise trade count enough to make
*more* folds judgeable while holding out-of-sample expectancy? More trades at collapsed
expectancy is a worse result, not a better one.

Then update PR #1 with the weeklies arc and give projected monthly dollars at $5k / $25k / $100k
account sizes.

## Standing constraints

- Do not raise the paid-plan or scope-down options. The user has heard them twice and not chosen.
- `moonshot100` (the $100 0DTE experiment) runs its own Routines and is untouched by this work.
