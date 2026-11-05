#!/usr/bin/env python3
"""Market-type brain (trader-regime). Standard library only.

What kind of market is it: crash, bear, neutral, bull or euphoria? A hidden
Markov model reads two things per trading day of a benchmark (SPY by default):
the day's log return and the last 10 days' volatility. It tries 3 to 6 hidden
market types, keeps the count with the best BIC score, and names them by their
average return, lowest first.

No look-ahead:
  - fitting only ever sees the days up to the one being judged (walk-forward
    in `backtest`; up to today in `now`);
  - today's market type comes from the FORWARD pass only, which uses no later
    day. (The smoothed, two-way pass would peek at the future, so it is never
    used for a decision.)

Stability: a new market type only counts once it has been the most likely one
for `confirm_days` (3) days in a row; until then the old one stands.

Sizing: each named type has a size factor that trader-risk multiplies into
your max loss per trade. "crash" = 0: no new buys.

    python3 regime.py now                      # fetch SPY, fit, write trader/state/regime.json
    python3 regime.py now --symbol QQQ --json
    python3 regime.py now --closes my.json     # offline: {"closes": {"SPY": [[date, close], ...]}}
"""

import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "trader-risk"))

REGIME_PATH = "trader/state/regime.json"
VOL_DAYS = 10
CONFIRM_DAYS = 3
MIN_VAR = 1e-8

# Names by rank of average return, for each number of states.
NAMES = {
    2: ["bear", "bull"],
    3: ["bear", "neutral", "bull"],
    4: ["crash", "bear", "neutral", "bull"],
    5: ["crash", "bear", "neutral", "bull", "euphoria"],
    6: ["crash", "bear", "neutral", "neutral", "bull", "euphoria"],
    7: ["crash", "bear", "bear", "neutral", "bull", "bull", "euphoria"],
}
FACTORS = {"crash": 0.0, "bear": 0.5, "neutral": 0.75, "bull": 1.0, "euphoria": 0.75}


class RegimeError(Exception):
    pass


# ---------- features ----------

def features(closes):
    """[(date, close)] -> (dates, [[log_return, vol10], ...]) starting once vol10 exists."""
    rows = sorted((str(d)[:10], float(c)) for d, c in closes if c and float(c) > 0)
    rets = []
    for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
        rets.append((d1, math.log(c1 / c0)))
    dates, X = [], []
    for i in range(VOL_DAYS - 1, len(rets)):
        win = [r for _, r in rets[i - VOL_DAYS + 1:i + 1]]
        m = sum(win) / len(win)
        vol = math.sqrt(sum((x - m) ** 2 for x in win) / (len(win) - 1))
        dates.append(rets[i][0])
        X.append([rets[i][1], vol])
    return dates, X


# ---------- Gaussian HMM, diagonal covariance ----------

def _logpdf(x, mean, var):
    s = 0.0
    for xi, m, v in zip(x, mean, var):
        s += -0.5 * (math.log(2 * math.pi * v) + (xi - m) ** 2 / v)
    return s


def _emissions(X, model):
    return [[_logpdf(x, model["means"][k], model["vars"][k]) for k in range(model["n"])] for x in X]


def forward(X, model):
    """Scaled forward pass. Returns (alphas: filtered P(state_t | x_1..t), loglik). Uses no later day."""
    n, A, pi = model["n"], model["trans"], model["start"]
    E = _emissions(X, model)
    alphas, ll = [], 0.0
    prev = None
    for t, e in enumerate(E):
        mx = max(e)
        b = [math.exp(v - mx) for v in e]
        if prev is None:
            a = [pi[k] * b[k] for k in range(n)]
        else:
            a = [b[j] * sum(prev[i] * A[i][j] for i in range(n)) for j in range(n)]
        s = sum(a)
        if s <= 0:
            a, s = [1.0 / n] * n, 1.0
        a = [v / s for v in a]
        ll += math.log(s) + mx
        alphas.append(a)
        prev = a
    return alphas, ll


def _backward(X, model, E):
    n, A = model["n"], model["trans"]
    T = len(X)
    betas = [[1.0] * n for _ in range(T)]
    for t in range(T - 2, -1, -1):
        mx = max(E[t + 1])
        b = [math.exp(v - mx) for v in E[t + 1]]
        nb = [sum(A[i][j] * b[j] * betas[t + 1][j] for j in range(n)) for i in range(n)]
        s = sum(nb) or 1.0
        betas[t] = [v / s for v in nb]
    return betas


def _init(X, n, rng):
    d = len(X[0])
    order = sorted(range(len(X)), key=lambda i: X[i][0])
    chunks = [order[int(k * len(X) / n):int((k + 1) * len(X) / n)] for k in range(n)]
    allvar = []
    for j in range(d):
        col = [x[j] for x in X]
        m = sum(col) / len(col)
        allvar.append(max(sum((c - m) ** 2 for c in col) / len(col), MIN_VAR))
    means, vars_ = [], []
    for ch in chunks:
        pts = [X[i] for i in ch] or X
        mean = [sum(p[j] for p in pts) / len(pts) * (1 + 0.1 * (rng.random() - 0.5)) for j in range(d)]
        means.append(mean)
        vars_.append([max(sum((p[j] - mean[j]) ** 2 for p in pts) / len(pts), allvar[j] * 0.05)
                      for j in range(d)])
    stay = 0.9
    trans = [[stay if i == j else (1 - stay) / (n - 1) for j in range(n)] for i in range(n)]
    return {"n": n, "means": means, "vars": vars_, "trans": trans, "start": [1.0 / n] * n}


def fit(X, n, seed=0, iters=40, tol=1e-4):
    """Baum-Welch. Returns the model with its training log-likelihood."""
    if len(X) < n * 20:
        raise RegimeError("need at least %d days to fit %d market types, have %d" % (n * 20, n, len(X)))
    rng = random.Random(seed)
    model = _init(X, n, rng)
    d = len(X[0])
    last = None
    for _ in range(iters):
        E = _emissions(X, model)
        alphas, ll = forward(X, model)
        betas = _backward(X, model, E)
        T = len(X)
        gam = []
        for t in range(T):
            g = [alphas[t][k] * betas[t][k] for k in range(n)]
            s = sum(g) or 1.0
            gam.append([v / s for v in g])
        xi_sum = [[0.0] * n for _ in range(n)]
        for t in range(T - 1):
            mx = max(E[t + 1])
            b = [math.exp(v - mx) for v in E[t + 1]]
            m = [[alphas[t][i] * model["trans"][i][j] * b[j] * betas[t + 1][j] for j in range(n)]
                 for i in range(n)]
            s = sum(sum(r) for r in m) or 1.0
            for i in range(n):
                for j in range(n):
                    xi_sum[i][j] += m[i][j] / s
        for i in range(n):
            row = sum(xi_sum[i]) or 1.0
            model["trans"][i] = [max(v / row, 1e-6) for v in xi_sum[i]]
            z = sum(model["trans"][i])
            model["trans"][i] = [v / z for v in model["trans"][i]]
        model["start"] = gam[0][:]
        for k in range(n):
            w = sum(g[k] for g in gam) or 1e-12
            mean = [sum(g[k] * x[j] for g, x in zip(gam, X)) / w for j in range(d)]
            var = [max(sum(g[k] * (x[j] - mean[j]) ** 2 for g, x in zip(gam, X)) / w, MIN_VAR) for j in range(d)]
            model["means"][k], model["vars"][k] = mean, var
        if last is not None and abs(ll - last) < tol * abs(last):
            break
        last = ll
    _, ll = forward(X, model)
    model["loglik"] = ll
    return _sorted(model)


def _sorted(model):
    """Reorder states by average return, lowest first, and name them."""
    n = model["n"]
    order = sorted(range(n), key=lambda k: model["means"][k][0])
    m = {"n": n,
         "means": [model["means"][k] for k in order],
         "vars": [model["vars"][k] for k in order],
         "start": [model["start"][k] for k in order],
         "trans": [[model["trans"][i][j] for j in order] for i in order],
         "loglik": model.get("loglik")}
    m["names"] = NAMES.get(n) or (["state%d" % i for i in range(n)])
    return m


def bic(model, T):
    n, d = model["n"], len(model["means"][0])
    k = n * (n - 1) + (n - 1) + n * d * 2
    return -2 * model["loglik"] + k * math.log(T)


def select(X, states=(3, 4, 5, 6), seeds=(0, 1, 2)):
    """Best model by BIC across state counts and seeds. Returns (model, table)."""
    best, table = None, []
    for n in states:
        top = None
        for s in seeds:
            try:
                m = fit(X, n, seed=s)
            except RegimeError:
                continue
            if top is None or m["loglik"] > top["loglik"]:
                top = m
        if top is None:
            continue
        b = bic(top, len(X))
        table.append({"states": n, "bic": round(b, 1), "loglik": round(top["loglik"], 1)})
        if best is None or b < best[0]:
            best = (b, top)
    if best is None:
        raise RegimeError("not enough history to fit any market type model")
    return best[1], table


def stable_path(alphas, confirm=CONFIRM_DAYS):
    """Confirmed state per day: a new most-likely state counts after `confirm` days in a row."""
    out, cur, run_s, run_n = [], None, None, 0
    for a in alphas:
        k = max(range(len(a)), key=lambda i: a[i])
        if k == run_s:
            run_n += 1
        else:
            run_s, run_n = k, 1
        if cur is None or (k != cur and run_n >= confirm):
            cur = k
        out.append(cur)
    return out


def classify(dates, X, model, confirm=CONFIRM_DAYS):
    """Today's verdict from the forward pass only."""
    alphas, _ = forward(X, model)
    path = stable_path(alphas, confirm)
    k = path[-1]
    name = model["names"][k]
    raw = max(range(model["n"]), key=lambda i: alphas[-1][i])
    by_name = {}
    for i, nm in enumerate(model["names"]):
        by_name[nm] = by_name.get(nm, 0.0) + alphas[-1][i]
    raw_name = model["names"][raw]
    return {
        "date": dates[-1],
        "regime": name,
        "state": k,
        "today_probabilities": {nm: round(p, 3) for nm, p in by_name.items()},
        "most_likely_today": raw_name,
        "most_likely_today_prob": round(by_name[raw_name], 3),
        "pending_change": raw_name != name,
        "factor": FACTORS.get(name, 1.0),
        "states": model["n"],
        "state_names": model["names"],
        "state_avg_return_pct": [round(m[0] * 100, 3) for m in model["means"]],
        "state_avg_vol_pct": [round(m[1] * 100, 3) for m in model["means"]],
    }


def read_regime(path=REGIME_PATH):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Market-type brain (trader-regime).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("now", help="judge today's market type and write it for trader-risk")
    n.add_argument("--symbol", default="SPY")
    n.add_argument("--range", default="5y")
    n.add_argument("--closes", help="offline closes file instead of fetching")
    n.add_argument("--out", default=REGIME_PATH)
    n.add_argument("--json", action="store_true")
    n.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    try:
        if a.closes:
            with open(a.closes, encoding="utf-8") as fh:
                doc = json.load(fh)
            closes = (doc.get("closes", doc) or {}).get(sym)
            if not closes:
                raise RegimeError("%s has no closes for %s" % (a.closes, sym))
        else:
            import correlation  # trader-risk/correlation.py: Yahoo daily fetch
            closes = correlation.fetch_daily(sym, a.range)
        dates, X = features(closes)
        model, table = select(X)
        res = classify(dates, X, model)
        res.update(symbol=sym, fitted_on_days=len(X), bic_table=table,
                   written=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except (RegimeError, RuntimeError, OSError, ValueError) as e:
        print("regime.py: %s" % e, file=sys.stderr)
        return 1
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print("%s market type on %s: %s, size factor %s"
              % (sym, res["date"], res["regime"].upper(), res["factor"]))
        if res["pending_change"]:
            print("  watching: today looks most like %s (%.0f%%); it only counts after %d days in a row"
                  % (res["most_likely_today"], res["most_likely_today_prob"] * 100, CONFIRM_DAYS))
        else:
            print("  today agrees (%.0f%%)" % (res["most_likely_today_prob"] * 100))
        print("  %d market types (best BIC): " % res["states"] + ", ".join(
            "%s %+.2f%%/day" % (nm, r) for nm, r in zip(res["state_names"], res["state_avg_return_pct"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
