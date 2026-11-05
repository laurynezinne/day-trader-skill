---
name: trader-auto
description: >-
  Automatic paper runner. Once a day, about 15:20 New York time, it logs the
  account's equity, checks the circuit breakers (a day stop or a halt
  flattens everything), reads today's market type and the gauntlet's passes,
  runs the validated strategy on each passed symbol, sells what the rule no
  longer holds, and sends bracket entries that trader-risk passed in full.
  Paper only: it refuses unless the config has "auto": true and "paper": true,
  and its broker URL is Alpaca's paper endpoint, hard-coded. Every run writes a
  plain journal. Use when the user wants the bot to trade paper money on its
  own, to set up the daily schedule, or to read what it did.
---

# trader-auto: the automatic paper runner

This is the one skill that places orders without a yes for each one, and it
can only ever do it on a **paper** account.

## Before the first run

1. Alpaca paper keys (app.alpaca.markets, Paper Trading, API keys). Put them in
   `~/.trader-keys`, outside the repo, then `chmod 600 ~/.trader-keys`:
   ```
   export APCA_API_KEY_ID=...
   export APCA_API_SECRET_KEY=...
   ```
   Never paste keys into the chat or commit them.
2. `cp trader-budget/config.example.json trader/config.json`, then set
   `"auto": true` (leave `"paper": true`).
3. `python3 trader-regime/regime.py now` and
   `python3 trader-validate/validate.py run --universe default`.
4. A dry run: `python3 trader-auto/auto.py run --dry-run`. Read every line.

## One run, in order

| step | what happens |
| --- | --- |
| guard | refuses unless auto and paper are both true; the URL is paper-only |
| clock | market closed: logs it and stops |
| equity | logs the account value; evaluates the four circuit breakers |
| breakers | day stop (-3%) or halt (-10% from peak): closes everything, no entries |
| universe | only symbols with PASS in trader/state/validation.json |
| signal | the validated strategy at its current settings, on daily closes + today's price |
| exits | held and no longer wanted: cancels its stop/target legs, sells at market |
| entries | bracket: stop 2x the 20-day daily move below (at least 2%), target 1.5x that above, sized to max loss |
| risk | the whole batch goes through trader-risk (breakers, market type, correlation, every limit); only PASS is sent |
| memory | each order in trader/state/broker.json; each run in trader/journal/auto-YYYY-MM.md |

## The schedule

`trader-auto/daily.sh` runs one day: market type, the monthly gauntlet,
the runner, the breaker status. Its output goes to `trader/journal/daily-*.log`.
Run it at 20:20 UK time on weekdays. On a Mac, with `crontab -e`:

```
20 20 * * 1-5 /path/to/day-trader-skill/trader-auto/daily.sh
```

US and UK clocks change on different weekends in March and Oct/Nov; in those
weeks 15:20 New York is 19:20 UK, so the run may land after the entry
window and simply place nothing. `DRY_RUN=1` in front runs it without orders.

## Status and review

- `python3 trader-auto/auto.py status`: halted or not, what passed, market type.
- Read the journal with the user every Friday: what it entered, exited and
  blocked, and why. Compare the account with SPY over the same weeks.
- Four weeks of paper results first. Real money is a separate decision the
  user makes themselves; this runner will never do it.

## Rules

- Never set `"paper": false` with `"auto": true`; check.py and auto.py both refuse.
- Never delete `trader/state/HALTED` for the user; they look first, then delete it.
- Never trade a symbol the gauntlet did not pass, and never loosen a limit to
  get an order through.

## Safety

- Education and tooling, not financial advice. Paper results flatter: real
  fills, gaps and slippage are worse.
- Paper only. In manual mode every order in this pack still needs the user's
  yes; this runner is the one exception, and only on Alpaca's paper endpoint.
- Keys live in environment variables (`~/.trader-keys`), never in the repo or the chat.
