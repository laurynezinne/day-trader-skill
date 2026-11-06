---
name: trader-bot
description: >-
  Bot HQ. A team of agents (Scout, Scorer, Skeptic, Risk, Executor, Reporter)
  that trades Bitcoin, the Nasdaq 100, the S&P 500, gold and oil, long or flat,
  never leveraged. Two desks: the daily Trend Rider (live on paper: holds a
  market while its trend score is 70+, sized so each carries similar risk) and
  the hourly Scalper built from the popular "EMA 50/200 + RSI + Aroon + score
  0-100" videos (benched, because it lost money after fees in its backtest).
  Writes one file with both backtests, today's scores, the paper account and
  a daily P&L calendar, and prints a morning brief and an evening report. Use
  when the user wants the bot to run, its results, or the 7am/9pm reports.
---

# trader-bot: Bot HQ

## Run it

```
python3 ${CLAUDE_SKILL_DIR}/bot.py run --out bot.json            # fetch, backtest both desks, read today
python3 ${CLAUDE_SKILL_DIR}/bot.py run --market-type bear         # pass trader-regime's verdict to the Skeptic
python3 ${CLAUDE_SKILL_DIR}/bot.py report --file bot.json --kind morning
python3 ${CLAUDE_SKILL_DIR}/bot.py report --file bot.json --kind evening
```

Data is Yahoo's free history: about 10 years of daily closes and 2 years of
hourly bars. It is unofficial and can lag.

## The team

| agent | job |
| --- | --- |
| Scout | reads EMA 50 and 200, RSI 14, Aroon 25, ATR 14, relative volume and momentum against the S&P 500 |
| Scorer | Trend Rider: 40 for a close above the EMA 200, 30 for the EMA 50 above it, up to 29 for Aroon strength; 70+ means both trend tests pass. Scalper: five parts of 20 |
| Skeptic | only a desk whose backtest made money after fees may trade; the Scalper also vetoes chasing (RSI over 75) and falling knives (a 24-bar drop of more than 3 ATR); a crash market type vetoes new holds |
| Risk | Trend Rider: about 10% yearly volatility spread across five markets, at most 40% of the account in one, never more than 100% in total. Scalper: 1% risk per trade, stop 2 ATR, target 3 ATR |
| Executor | decides at the close, fills at the next bar or day; fees on both sides (0.25% Bitcoin, 0.05% funds) |
| Reporter | the morning brief and the evening report |

## First real results (6 Nov 2026)

- Trend Rider, Sep 2017 to Nov 2026: +159% against +584% for holding all
  five; worst drop 14% against 34%; up in 8 of 10 years, down 4-5% in 2018
  and 2022 when holding lost about 20%. Sharpe 0.86 against 0.92 for holding.
  Its point is smaller drops, not beating a bull market.
- Scalper, Dec 2023 to Nov 2026 on hourly bars: -36% against +70% for
  holding; 776 trades, 39% won. Bitcoin fees did most of the damage. A small
  fixed grid of variations (picked on 2024, judged on 2025-26) found none that
  made money in its training year. Benched.

## Rules

- The rules were fixed before the first backtest. Never tune them on the
  test years and present the result as a forecast.
- Report both desks honestly, including the benched one, and always next to
  buy-and-hold.
- Social posts that show only winning months are not evidence. The backtest
  and the paper account are.

## Safety

- Education and tooling, not financial advice. A backtest is history, not a
  forecast; real fills, gaps and slippage are worse.
- Paper trading by default. This skill places no broker orders: its paper
  account is its own ledger. Live orders go through trader-risk and
  trader-broker, and each needs the user's yes.
- Keys live in environment variables (`~/.trader-keys`), never in the repo or the chat.
