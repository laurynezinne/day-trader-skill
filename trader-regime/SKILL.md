---
name: trader-regime
description: >-
  Market-type brain. Works out what kind of market it is right now (crash,
  bear, neutral, bull or euphoria) from a benchmark's daily returns and
  volatility with a hidden Markov model, using only past data: the number of
  market types is picked by BIC, today's verdict comes from the forward pass
  alone, and a change only counts after three days in a row. Writes
  trader/state/regime.json, which trader-risk reads to scale your max loss per
  trade (crash = no new buys). Use when the user asks what the market is
  doing, whether to size down, or before the morning scan.
---

# trader-regime: what kind of market is it?

A strategy that works in a calm rising market can bleed in a falling, jumpy
one. This skill answers one question every morning, using only data up to
today: **which kind of market are we in?** trader-risk then sizes every new
entry to match.

## Run it

```
python3 ${CLAUDE_SKILL_DIR}/regime.py now                 # SPY, 5 years, writes trader/state/regime.json
python3 ${CLAUDE_SKILL_DIR}/regime.py now --symbol QQQ
python3 ${CLAUDE_SKILL_DIR}/regime.py now --json
python3 ${CLAUDE_SKILL_DIR}/regime.py now --closes trader/state/closes.json   # offline
```

Run it once a day before the scan (the scheduled morning run does). The data
is Yahoo's free daily bars: unofficial, and can lag or rate-limit.

## How it decides

1. **Two numbers per day** of the benchmark: the log return, and the last 10
   days' volatility.
2. **A hidden Markov model** with 3, 4, 5 and 6 hidden market types, each fitted
   from 3 starting points. The count with the lowest BIC wins: BIC rewards a
   better fit but charges for every extra parameter, so it does not add types
   just to memorise the past.
3. **Named by average return**, lowest first: crash, bear, neutral, bull,
   euphoria (a 6-type model has two neutrals).
4. **Forward pass only.** Today's probabilities use days up to today and none
   after. The two-way (smoothed) pass would quietly use tomorrow, so nothing
   here ever decides on it. A test proves adding later days never changes an
   earlier day's verdict.
5. **Three days to confirm.** A new market type counts only after it has been
   the most likely one three days in a row. One wild day is reported as
   "watching", not acted on.

## What trader-risk does with it

| market type | size factor | effect on new buys |
| --- | --- | --- |
| crash | 0 | blocked until the market type changes |
| bear | 0.5 | max loss per trade x 0.5 |
| neutral | 0.75 | max loss per trade x 0.75 |
| bull | 1 | unchanged |
| euphoria | 0.75 | max loss per trade x 0.75 (late rallies reverse hard) |

The factor stacks with the circuit breakers: a bear market on a day the
account is down 2% gives 0.5 x 0.5 = a quarter of the normal max loss.
A regime file more than 4 days old is ignored with a warning. Sells and
exits are never blocked.

## Reading the output

```
SPY market type on 2026-11-05: BEAR, size factor 0.5
  watching: today looks most like crash (96%); it only counts after 3 days in a row
  6 market types (best BIC): crash -0.10%/day, bear -0.02%/day, ...
```

Say it plainly to the user: the confirmed market type, what it does to their
size, and anything it is watching.

## Rules

- Never tell the user the market type predicts tomorrow. It describes the
  recent past; the sizing is a seatbelt, not a forecast.
- Never use the smoothed (forward-backward) probabilities for a decision.
- Never edit regime.json by hand to get a bigger size.
- If the fetch fails, say so; do not reuse an old verdict silently.

## Safety

- Education and tooling, not financial advice. A market type is a description of
  the past, not a recommendation.
- Paper trading by default (`"paper": true`). Nothing here places an order:
  it only shrinks or blocks what trader-risk lets through. In manual mode each
  order still needs the user's yes; in `"auto": true` mode (paper only) the
  runner places what trader-risk passes.
- Broker keys live in environment variables, never in the chat.
