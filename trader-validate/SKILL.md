---
name: trader-validate
description: >-
  The gauntlet. Before any strategy gets paper money it must survive
  walk-forward testing on up to 15 years of daily bars: settings chosen on the
  three years before each test year only, costs on every trade, and five
  filters on the stitched out-of-sample years (Sharpe over 0.5, max drawdown
  under 35%, at least 30 trades, out-of-sample Sharpe at least half the
  in-sample one, and a 500-run bootstrap that stays above zero), compared with
  buy-and-hold, the 200-day rule and random entries. Writes
  trader/state/validation.json; the auto runner trades only what passed. Use
  when the user asks whether a strategy works, before automating anything, or
  every month to re-check.
---

# trader-validate: the gauntlet

Most strategies that look brilliant on a chart fail once they are tested on
years they were not tuned on. In one 9,000-backtest study, the large majority
failed; in a 25,000-version test of a popular day-trading rule, 1.7% survived.
This skill is how this pack tells the difference before risking even paper
money.

## Run it

```
python3 ${CLAUDE_SKILL_DIR}/validate.py run SPY QQQ AAPL --strategy rsi_reversion
python3 ${CLAUDE_SKILL_DIR}/validate.py run --universe default --strategy sma_trend
python3 ${CLAUDE_SKILL_DIR}/validate.py run SPY --json --no-write
```

The default universe is 20 large US stocks and ETFs. Data is Yahoo's free
daily history (adjusted for splits and dividends): unofficial, can lag.

## The strategies

| strategy | buys | sells | settings tried |
| --- | --- | --- | --- |
| `rsi_reversion` | RSI(2) closes under 5, 10 or 15 while the close is above the 200-day average | close above the 5- or 10-day average | 6 |
| `sma_trend` | close above its 50-, 100- or 200-day average | close back under it | 3 |

Both are long only. A position is decided at the close and earns the NEXT
day's move, so no day ever uses its own close twice. Each change of position
costs 0.05% (commission plus slippage).

## The walk-forward

For each test year, every setting is scored on the three years before it,
the best is chosen, and only then is it run on the test year. The test years
are stitched together; those are the only numbers that count. A test proves
that changing a test year's prices never changes the setting chosen for it.

## The five filters (all must pass)

| filter | rule | why |
| --- | --- | --- |
| sharpe | out-of-sample Sharpe > 0.5 | worth the risk at all |
| drawdown | max drawdown < 35% | survivable |
| trades | at least 30 out-of-sample trades | not a fluke of a handful |
| not_overfit | out-of-sample Sharpe >= half the in-sample one | did not just memorise the past |
| bootstrap | 5th-percentile Sharpe over 500 block resamples > 0 | holds up when the order of days is shaken |

Then the baselines on the same test years: buy-and-hold, the 200-day rule,
and random entries with the same time in the market. Tell the user honestly
when a strategy passes but does not beat buy-and-hold: that is common, and
its benefit is then a smaller drawdown, not a bigger return.

## First real run (5 Nov 2026, test years 2014 to 2026)

- `rsi_reversion`: 5 of 20 passed (SPY, MSFT, NVDA, GOOGL, V). Drawdowns were
  far smaller than holding (SPY 14.5%), but no pass beat buy-and-hold's Sharpe.
- `sma_trend`: 6 of 20 passed (SPY, QQQ, DIA, AAPL, JPM, HD).

Re-run monthly: a pass is a result about the past, not a promise.

## Rules

- Never tune settings on the test years, and never quote in-sample numbers as
  the result.
- Never call a strategy "working" unless validation.json says PASS for it.
- Say plainly when nothing passes. That is a useful answer.

## Safety

- Education and tooling, not financial advice. A backtest is history, not a
  forecast; live fills, gaps and costs can be worse.
- Paper trading by default (`"paper": true`). Nothing here places an order. In
  manual mode each order still needs the user's yes; in `"auto": true` mode
  (paper only) the runner trades only symbols that passed here.
