# polymarket copytrader (paper)

**A live forward test that copies skilled Polymarket wallets under five exit rules, with one Claude + chart-aware variant.**

Zero real money. Zero deposits. The bot watches ~19 vetted on-chain traders (small clips, takers, diversified). It mirrors their buys with tiny paper stakes ($2 notionals) under each exit rule. The bot records everything, marks it to market, and settles it against live resolutions. The goal is to answer one question: *after realistic detection lag, which traders + which exit rule are worth copying?*

**Live dashboard:** https://howwohmm.github.io/polymarket-paper/

## why this exists

The project started as a paper trader for the popular "fade the favorite" hourly reversal prompt. The rule: in 88-98% one-sided BTC/ETH up/down markets, buy the cheap underdog side, sized $10-25. It used a slippage gate and a budget kill switch.

After the run:

- The bot detected 162 signals.
- **0** passed the 4% realistic slippage gate on the orderbook. The underdog side is almost always illiquid when the crowd is that sure.
- Raw (ignoring slippage): 4% win rate, -24% ROI, EV -$0.235 per $1 staked.

Classic favorite-longshot bias + liquidity reality won. The original experiment did its job for free.

It evolved into this: a cleaner, higher-signal research rig. The rig copies *real* skilled wallets instead of fighting one-sided hourly crypto markets.

---

## the five models (each trader gets their own $20 virtual bankroll)

- **A (hold)**: copy and hold to resolution. Slow (many markets last weeks). Baseline.
- **B (exit +15%)**: take profit at +15% or force-close after 36h. Fast turnover, 1-3 day verdicts. Closed positions currently show strong realized ROI.
- **C (Claude decides)**: when a position moves ±5-10%, the bot asks Claude Haiku whether to HOLD or SELL. Claude gets live 1h chart context (RSI, trend, momentum, S/R). Conservative by design ("let winners run"). See the live "Claude's decisions" feed on the dashboard for the reason behind each decision.
- **D (cut losers, ride winners)**: 30% hard stop-loss + trailing stop (arm at +25%, trail 20% off peak). Max 5 days.
- **E (chart-filtered entry + D exits)**: same exits as D, but with an entry filter. The bot skips entry entirely on a bad 1h 1-min chart. Bad means overbought (RSI>78), a chase (30-minute chase filter: rejects an entry if the absolute token-price increase exceeds $0.10 during the checked 30-minute window), or a strong downtrend. Goal: same upside, fewer garbage copies. (Currently very quiet. The filters are strict on low-liquidity names.)

All models recycle capital inside the per-trader $20 wallet. Median detection lag right now is ~4.3 minutes. The rig is deliberately forward-only (no look-ahead).

---

## live dashboard

The quiet, text-first dashboard at the URL above is the source of truth:

- Top row: aggregate P&L per exit model (started with $400 each = 20 traders × $20).
- Trader leaderboard: every vetted wallet, ranked by the current value of its best model.
- Claude feed: the last ~30 HOLD/SELL decisions, each with the exact one-sentence reason from Claude, plus chart context when available.

The runner pushes fresh data (every ~9 min during the long job). The page auto-refreshes in the browser.

---

## how it runs 24/7 (cheap + reliable)

- Two GitHub Actions workflows.
- `copybot.yml`: the main one. It fires on :11/:41 every hour. Inside the job, it loops 12× (tick every ~3 min for fresh activity). Each loop runs bounded resolve and, for model C, bounded Claude calls. It exports `docs/data.json` and commits only that file. Total ~1-2 min of billable time per scheduled trigger even with the inner loop.
- GitHub Actions **cache** carries DB state (open copies, bankrolls, decisions) between runs (see workflow). No more giant binary commits to the repo history.
- Self-healing: `tick` only inserts buys that it has not seen yet (keyed on tx+asset+model). Missed runs or throttled crons lose nothing.
- The original `paper-trader-tick` (the fade strat) is still wired but largely retired. It proved the point.

Cost: free (public repo + tiny runners). The bot rate-caps Claude Haiku calls per run, and the calls are cheap.

---

## usage (local / debug)

```bash
python3 copybot.py tick          # scan recent activity for the WALLETS, paper-copy new buys
python3 copybot.py resolve       # mark-to-market opens + settle anything that resolved
python3 copybot.py claude_decide # (model C only) ask Claude on moved positions (needs ANTHROPIC_API_KEY)
python3 copybot.py export        # rebuild docs/data.json for the dashboard
python3 copybot.py report        # human summary (realized, win rates, lag)

python3 copytrade.py leaderboard 30d     # discovery: who is actually printing on Polymarket
python3 copytrade.py profile <wallet>
python3 copytrade.py copytest <wallet> 12   # lag-adjusted historical mirror test (does their edge survive 12m delay?)
```

To add a trader: run the copytrade tools to vet the wallet. Then add `Name: "0x..."` to the `WALLETS` dict in `copybot.py` and let it collect its first copies.

To add model F: extend the model loop in `tick`/`resolve`/`export` and add a couple of constants. Keep the per-trader $20 bankroll pattern.

---

## current caveats (be honest)

- Paper only. No spread, no taker fees, no gas, no failed tx simulation on the copy side.
- The bot enters at the *current* mid after it sees the on-chain trade (conservative). A real copy would execute a few seconds later.
- Many Polymarket tokens are thin. 1-min CLOB candles for E/C can be noisy or missing. That is part of why E is quiet.
- Claude is Haiku (fast/cheap) with a deliberately "let winners run, only sell clear losers or spikes" prompt. A swap to a stronger model would be easy but costs more.
- The repo includes the original fade-the-favorite hourly strat for the historical record. We do not recommend it.

More data comes first. A model plus a subset of traders must show persistent positive expectancy after lag. Only then decide to put real (tiny) size on a refined version.

---

## repo map

- `copybot.py`: the live engine (tick/resolve/Claude/export/report)
- `copytrade.py`: offline research + wallet vetting CLI (no keys needed)
- `paper_trader.py`: the original fade experiment (mostly historical now)
- `docs/index.html` + `docs/data.json`: the entire public dashboard (pure static, zero backend, Manrope + quiet dark per the design system)
- `.github/workflows/copybot.yml`: the reliable near-continuous runner + cache for state
- `copybot.db` (local only, gitignored): working state for the runner

---

## paper execution assumptions

**This bot is paper-only.** It never signs, submits, or settles anything: no private keys,
no wallet signing, no order submission, no deposits or withdrawals. It reads public
Polymarket endpoints and writes rows to a local SQLite file. Nothing here can move money.

Before a detected wallet trade is allowed to become a paper copy, it goes through one
shared entry/execution simulation (the same gate for **every** model A–E, never per-model):

```
wallet trade
  -> simulated detection/copy delay   (COPY_DELAY_SECONDS)
  -> market price at simulated execution time
  -> adverse slippage                 (SLIPPAGE_BPS)
  -> entry price limits               (MIN/MAX_ENTRY_PRICE, on the FINAL price)
  -> order-book slippage + liquidity gates
  -> paper fee + share sizing         (PAPER_FEE_BPS)
  -> paper copy (or a logged rejection)
```

- **Copy delay is simulated, not a real sleep.** `exec_ts = trade_ts + COPY_DELAY_SECONDS`
  is timestamp arithmetic used to look up the price at that moment. Ticks and tests run at
  full speed; nothing ever calls `time.sleep()`.
- **Slippage is simulated.** `filled = price * (1 + SLIPPAGE_BPS / 10_000)`, always against
  the copier (a buy fills higher), clamped so a price can never leave `(0, 1)`.
- **Fees default to zero.** `PAPER_FEE_BPS=0` because this repo does *not* assume or invent
  a Polymarket fee schedule. Fees are modelled separately from slippage, so you can set a
  non-zero value later; at `0` the accounting is identical to the previous fee-free runs.
- **Max/min entry prices are configurable**, and are checked against the *final* simulated
  buy price. A wallet can buy at 0.798 and still be rejected if delay + slippage push our
  simulated fill above `MAX_ENTRY_PRICE`.
- **Rejected trades are logged**, never silently dropped. Every candidate gets a row in
  `entry_filter_log` with an explicit reason (`ACCEPTED`, `PRICE_TOO_HIGH`, `PRICE_TOO_LOW`,
  `INVALID_PRICE`, `SLIPPAGE_TOO_HIGH`, `INSUFFICIENT_LIQUIDITY`, `DUPLICATE`, …) plus the
  full execution trail: wallet price, market price, slippage amount, final entry, fee,
  stake, shares, and detection delay. `copybot report` and the dashboard summarise it.

Configure via a `.env` file next to `copybot.py` (see `.env.example`) or real environment
variables, which win over `.env`:

```
MAX_ENTRY_PRICE=0.80
MIN_ENTRY_PRICE=0.05
COPY_DELAY_SECONDS=30
SLIPPAGE_BPS=50
PAPER_FEE_BPS=0
```

**These assumptions are for research.** They are a model of copying conditions, not a
promise about live execution — real fills depend on the book at the moment of the trade,
queue position, and fees that this repo deliberately does not guess at. Paper results
under these settings do not guarantee live results.

---

## next improvements (pull requests welcome)

- Richer dashboard (equity history sparklines in pure SVG, per-trader position drilldown, filter by market type, win-rate columns more prominent).
- Persist + surface "E skip reasons" counts and per-model expectancy / drawdown style metrics.
- Tune or relax chart filters for E once we have enough skips logged.
- Optional: Dune-powered (or LB sweep) wallet discovery refresh, for more names that survive lag + copytest.
- Model F ideas: resolution-aware sizing, mean-reversion on spikes, or multi-leg.

Everything here is deliberately simple (stdlib + one direct Anthropic call, no heavy deps, no framework). It stays cheap, auditable, and fast to hack on.

Run it yourself, add a wallet, fork a model, watch the data after a few hundred copies. That is the whole point.
