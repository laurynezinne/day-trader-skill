---
name: trader-race
description: >-
  The £200 race. A one-week paper simulation from real daily closes: a safe
  bot (the gauntlet-validated RSI reversion strategy, 20% per signal, scaled
  by the market type) and an all-in bot (100% in whichever 3x leveraged ETF
  rose most over 3 days, no brakes) each start with £200 and compound every
  trading day, scored against £200 compounding at 3.44% a day (£1M in a year),
  at 5% a day, and SPY held. No broker and no keys needed. Use when the user
  asks how the race is going, to run the day's step, or for the final result.
---

# trader-race: the £200 race

The user wants £200 to become £1M by compounding daily returns. That needs
3.44% every trading day for a year. This race shows, with real prices and no
hype, how close two very different bots get.

## The runners

| runner | rule | brakes |
| --- | --- | --- |
| safe | rsi_reversion on SPY, MSFT, NVDA, GOOGL, V at their walk-forward settings; 20% of the account per signal | market type: crash = cash, bear x0.5, neutral x0.75 |
| all_in | 100% in the 3x ETF (TQQQ, SOXL, UPRO, TNA, TECL) with the best 3-day rise | none; cannot go below £0 |
| spy_hold | SPY, bought at the start and held | none |
| target_1y | £200 x 1.0344 per day: £1M after 252 days | the dream line |
| target_8m | £200 x 1.05 per day | the faster dream line |

## Run it

```
python3 ${CLAUDE_SKILL_DIR}/race.py step     # catch up to the latest close
python3 ${CLAUDE_SKILL_DIR}/race.py show     # the scoreboard
```

`step` processes every close after the last one it saw, so a missed day is
caught up next time. It fetches about two years of daily closes from Yahoo
(unofficial, can lag). State lives in `trader-race/race.json`.

## How a day works

1. At each close, each bot decides what to hold, using data up to that close
   only. A test proves a jump on the next day never changes the decision.
2. The next close, the bot earns the day's move on what it held, and pays
   0.05% on any change of holding.
3. The market type is refitted on SPY up to that close (forward pass only).
4. Returns are dollar % moves applied to a £ balance; currency moves are
   ignored and the report says so.

## Reporting to the user

Lead with the day's numbers, plainly: each runner's £ balance and % today,
the gap to the £1M line, and what each holds next. Celebrate a green day,
call a red day red. Never round a loss away or call the race "on track" for
£1M unless the balance is at or above the line.

## Rules

- The race is a simulation: no order is placed anywhere, and no keys are used.
- Never change a bot's rules mid-race to improve its number. A rule change
  means a new race, started fresh, with the old result kept.
- Never edit race.json by hand.

## Safety

- Education and tooling, not financial advice. A one-week race is far too
  short to prove a strategy; leveraged ETFs can lose most of their value fast.
- Paper only: nothing here trades. Any real-money order elsewhere in this pack
  still needs the user's yes.
