#!/usr/bin/env python3
"""
Polymarket COPYBOT — paper forward-test of mirroring vetted traders.

We can't backtest these traders (their bets are still open), so this forward-tests:
every time a tracked wallet BUYs, we record a paper copy at a fixed $10 notional at the
price available, then track it. As markets resolve we book realized P&L; open positions
are marked to current price. ROI (bankroll-independent) is the metric that matters.

Self-healing/idempotent: each tick re-reads recent trades and only inserts ones we
haven't copied yet (deduped by tx+asset), so missed/throttled runs lose nothing.

Tracked wallets were vetted for copyability: small clip ($5-15, a $10 copy can ride),
taker (not a maker/rebate farmer), diversified, slow/lag-tolerant markets.

Commands:
  python3 copybot.py tick      # detect tracked wallets' new buys, record paper copies
  python3 copybot.py resolve   # settle resolved copies; mark open ones to current price
  python3 copybot.py report    # realized + unrealized copy P&L, per wallet and total
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DB = Path(__file__).parent / "copybot.db"
# REAL $20 wallet per model: each model starts with $20, bets $2/trade (so up to ~10
# positions at once), and can only open a copy if it has free cash. When a position
# closes, the proceeds return to the wallet to be reused (recycling).
BANKROLL_PER_MODEL = 20.0    # EACH TRADER gets their OWN $20 per model -> rank which trader wins
STAKE = 2.0                  # $2 per copy -> ~10 positions per trader within their own $20
# Model B (take-profit exit): sell our copy when it's up enough to bank a margin, or
# force-close after MAX_HOLD so positions turn over fast (-> 1-3 day verdict, not weeks).
TAKE_PROFIT = 0.15        # exit when the position is +15% (enough margin to profit)
MAX_HOLD_HOURS = 36       # if it hasn't hit target in 36h, close it out at current price
# Model A only: skip copying markets that resolve further out than this (short test —
# no point holding a copy whose market settles in weeks/months). Model B is exempt.
MAX_HOLD_RESOLVE_DAYS = 3
# Model C: Claude decides HOLD/SELL per position (using market + price action), to cut
# losers smarter than a fixed rule. Bounded so the $500 credit lasts.
CLAUDE_MODEL = "claude-haiku-4-5"   # cheap; swap to claude-opus-4-6 for smarter, pricier calls
MAX_CLAUDE_CALLS_PER_RUN = 40       # hard cost cap per workflow run
CLAUDE_RECHECK_HOURS = 6            # don't re-ask about the same position within 6h
CDECIDE_MAX_HOLD_HOURS = 72         # backstop: force-close a Claude position after 72h
# Model D: "cut losers, ride winners" — hard stop-loss + trailing stop. Locks gains on a
# reversal while letting winners run toward resolution (vs B's fixed +15% cap).
STOP_LOSS = 0.30            # sell if down 30% from entry (cut the trader's wrong calls fast)
TRAIL_ACTIVATE = 0.25       # once up 25%, arm the trailing stop
TRAIL_DROP = 0.20           # then sell if it falls 20% from its peak (lock the win, let it run)
TRAIL_MAX_HOLD_HOURS = 120  # otherwise hold up to 5 days

# Model E: "chart-aware" — same D exit rules, but ENTRY filtered by chart signals.
# Skip trades where the price chart says: overbought (RSI), chasing a spike, or downtrend.
# Goal: copy the same traders but only when the chart agrees → fewer losers at same upside.
CHART_LOOKBACK_MINS = 60    # 1h of 1-min CLOB candles for indicators
CHART_RSI_OVERBOUGHT = 78   # skip entry if RSI > 78 (price already spiked, likely to retrace)
CHART_CHASE_LIMIT = 0.10    # skip if price ran >10 cents absolute in last 30 min (was % of entry — broke on low-prob underdogs)
CHART_STOP_LOSS = 0.30      # same stop as D (30% loss → cut)
CHART_TRAIL_ACTIVATE = 0.25 # arm trailing at +25%
CHART_TRAIL_DROP = 0.20     # lock win if drops 20% from peak
CHART_MAX_HOLD_HOURS = 120  # 5-day max hold (same as D)

# === Category + Quality Scoring (SDD v2 highest-leverage upgrade) ===
# Auto-tag markets so we stop blindly copying specialists outside their lane.
# Track per-trader per-category stats from *our own paper resolves*.
# Only copy a trader in a category if they have proven edge here (min samples + WR/ROI).
MIN_CAT_TRADES = 6
MIN_CAT_WR = 0.53
MIN_CAT_ROI = 0.06

def categorize_market(title: str, slug: str = "", question: str = "") -> str:
    t = (title or "").lower() + " " + (slug or "").lower() + " " + (question or "").lower()
    if any(x in t for x in ["5m", "5 min", "five min", "up or down", "updown"]):
        if any(x in t for x in ["btc", "bitcoin", "eth", "ethereum", "sol", "crypto"]):
            return "crypto_5m"
    if any(x in t for x in ["hour", "up or down", "crypto"]) and "5m" not in t:
        if any(x in t for x in ["btc", "eth", "sol"]):
            return "crypto_h"
    if any(x in t for x in ["nba", "nfl", "mlb", "nhl", "tennis", "soccer", "world cup", "sports", "goal", "points"]):
        return "sports"
    if any(x in t for x in ["election", "president", "senate", "trump", "harris", "politics", "vote"]):
        return "politics"
    if "esports" in t or "league" in t:
        return "esports"
    return "other"

# vetted copyable traders (small clip, taker, diversified, slow markets)
WALLETS = {
    "GoalLineGhost": "0x0346afae2603313d2bbee96b628536c8cbe352a5",
    "alwayslate":    "0xb687f00464e33934f5d591f224e71c3559ecaee5",
    "mooseborzoi":   "0x84cfffc3f16dcc353094de30d4a45226eccd2f63",
    "afkpnl":        "0x55eca3687ea7d69632ffe0f297ea3d5158bb8c7d",
    "RN1":           "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "domahhh":       "0x9d84ce0306f8551e02efef1680475fc0f1dc1344",
    "aenews2":       "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1",
    "gopfan2":       "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817",
    "HolyMoses7":    "0xa4b366ad22fc0d06f1e934ff468e8922431a87b8",  # lottery acct ($1->$1M goal)
    # --- Dune-sweep quiet winners (vetted from 1,617 active wallets, 2026-06-12) ---
    "AJSV":          "0xad5353afe30c2da57709e2704ef3ccdcf67eef24",  # +$7.9k, 500 mkts, $18 clip
    "AshHaykArs":    "0xd0ac6e6b585c00042a0552a6cf35f2f056ef7dee",  # +$1.9k, 38 mkts, $15 clip
    "zerowander01":  "0xa30de80ba9d6cc3c2d79955b6dca38307bf941af",  # +$224, 152 mkts, $4 clip (micro)
    "ElMagoCS2":     "0xf67948563be54a8a6aa0ab7b976d80e8f7e92d3d",  # +$404, 30 mkts, $14 clip
    "fox54498":      "0x54498e4c40b17261479aeaeeddf9cc37bd46992b",  # +$456, 362 mkts, $20 clip
    "belaba":        "0xb1556d9d1a734508178d15ba822e579901a7ca84",  # +$295, 21 mkts, $38 clip
    "molodoyy":      "0x564f22744b7941ade18d5e0e4f347c30e3057026",  # +$3.0k, 256 mkts, $57 clip
    "Tee1000":       "0x5edc7bba7516cd28dc886ad576ba197bb114bbad",  # +$23k, 494 mkts (experimental)
    "Polkadot-Frog": "0x9d57c42e847173d06841703825d3fe2299e456ea",  # +$20k, 497 mkts (experimental)
    "MILKinDenial":  "0xb02188c268290bb758d6c261bdf4e552c6e1ebd9",  # +$8.7k, 498 mkts (experimental)
    "Eatpraylove":   "0xc02147dee42356b7a4edbb1c35ac4ffa95f61fa8",  # +$136k whale (experimental)
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


# ---- config helpers: .env + env-var overrides for tunable constants ----
# Matches the repo's existing pattern (module-level ALL_CAPS constants edited to fork
# behavior, e.g. TAKE_PROFIT/STOP_LOSS above) while also letting the values below be
# tuned from the environment (the same mechanism already used for ANTHROPIC_API_KEY)
# without touching any Python logic — e.g. `MAX_ENTRY_PRICE=0.60 python3 copybot.py tick`.
# This is ONE configuration system with two front-doors into the same os.environ:
# a `.env` file next to copybot.py (loaded below), and real environment variables,
# which win over `.env` so CI/one-off overrides still work.
def _load_dotenv(path=None):
    """Minimal KEY=VALUE .env reader (stdlib only, no new dependency). Never overrides a
    variable already present in the real environment. Missing/unreadable file is a no-op."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    loaded = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:      # real env wins over .env
            os.environ[k] = v
            loaded[k] = v
    return loaded


_load_dotenv()


def _env_float(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")



def chart_signal(token_id):
    """Fetch last 1h of price + volume data for a Polymarket token.

    Price (CLOB prices-history): RSI, trend, momentum, volatility, support/resistance.
    Volume (data-api trades): $ volume, buy pressure, VWAP, vol_ratio.
    Returns None if price fetch fails or <20 bars. Volume keys absent if trades fail.
    """
    end_ts = int(time.time())
    start_ts = end_ts - CHART_LOOKBACK_MINS * 60
    try:
        data = get(f"{CLOB}/prices-history?market={token_id}&startTs={start_ts}&endTs={end_ts}&fidelity=1")
        hist = data.get("history", [])
    except Exception:
        return None
    if len(hist) < 20:
        return None
    try:
        prices = [float(h["p"]) for h in hist]
    except (ValueError, TypeError, KeyError):
        # One or more candles have a non-numeric or missing price field.
        # Return a sentinel so _chart_entry_ok can produce a specific reason
        # rather than letting an uncaught exception crash the tick loop.
        return {"_error": "invalid_candle_data", "bars": len(hist)}
    n = len(prices)

    # Linear regression slope (per minute → per hour)
    x_mean = (n - 1) / 2
    y_mean = sum(prices) / n
    denom = sum((i - x_mean) ** 2 for i in range(n))
    slope = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n)) / denom if denom else 0
    trend_ph = slope * 60

    # RSI(14) on 1-min changes
    changes = [prices[i] - prices[i - 1] for i in range(1, n)]
    period = min(14, len(changes))
    gains = [max(c, 0) for c in changes[-period:]]
    losses = [abs(min(c, 0)) for c in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0 and avg_gain == 0:
        rsi = 50.0
    elif avg_loss == 0:
        rsi = 100.0
    else:
        rsi = round(100 - 100 / (1 + avg_gain / avg_loss), 1)

    # Momentum
    mom_5 = round(prices[-1] - prices[max(0, n - 6)], 4)
    mom_30 = round(prices[-1] - prices[max(0, n - 31)], 4)

    # Volatility (30-bar std)
    window = prices[max(0, n - 30):]
    mean_w = sum(window) / len(window)
    price_vol = round((sum((p - mean_w) ** 2 for p in window) / len(window)) ** 0.5, 4)

    # Support / resistance (20th / 80th pct of last 60 bars)
    hist60 = sorted(prices[max(0, n - 60):])
    support = round(hist60[len(hist60) // 5], 4)
    resistance = round(hist60[4 * len(hist60) // 5], 4)

    sig = {
        "current": round(prices[-1], 4),
        "trend_ph": round(trend_ph, 4),
        "rsi": rsi,
        "mom_5m": mom_5,
        "mom_30m": mom_30,
        "price_vol": price_vol,
        "support": support,
        "resistance": resistance,
        "bars": n,
    }

    # --- Volume (data-api trades) ---
    # Each trade: size = shares, price = $/share → dollar_value = size * price
    try:
        now_ts = int(time.time())
        cutoff_1h  = now_ts - 3600
        cutoff_15m = now_ts - 900
        trades = None
        for url in [f"{DATA}/trades?market={token_id}&limit=500",
                    f"{DATA}/trades?asset={token_id}&limit=500"]:
            try:
                result = get(url)
                if result and isinstance(result, list) and len(result) > 0:
                    trades = result
                    break
            except Exception:
                continue
        if trades:
            def dv(t): return float(t.get("size") or 0) * float(t.get("price") or 0)
            h1 = [t for t in trades if int(t.get("timestamp") or 0) >= cutoff_1h]
            if h1:
                vol_1h   = sum(dv(t) for t in h1)
                vol_15m  = sum(dv(t) for t in h1 if int(t.get("timestamp") or 0) >= cutoff_15m)
                avg_15m  = vol_1h / 4  # expected 15-min slice if uniform
                vol_ratio = round(vol_15m / avg_15m, 2) if avg_15m > 0 else 1.0
                buy_dv   = sum(dv(t) for t in h1 if (t.get("side") or "").upper() == "BUY")
                buy_pressure = round(buy_dv / vol_1h, 2) if vol_1h > 0 else 0.5
                sum_pv   = sum(float(t.get("price") or 0) * float(t.get("size") or 0) for t in h1)
                sum_v    = sum(float(t.get("size") or 0) for t in h1)
                vwap     = round(sum_pv / sum_v, 4) if sum_v > 0 else None
                sig.update({
                    "vol_1h":        round(vol_1h, 0),   # total $ vol last hour
                    "vol_15m":       round(vol_15m, 0),  # $ vol last 15 min
                    "vol_ratio":     vol_ratio,           # >1.5=surging, <0.5=drying up
                    "buy_pressure":  buy_pressure,        # 0.6+=more buyers than sellers
                    "vwap":          vwap,                # vol-weighted avg price
                })
    except Exception:
        pass  # volume is optional — price signals still valid without it

    return sig


def _chart_entry_ok(sig, entry):
    """Return (ok: bool, reason: str). False = skip this Model E entry.

    Fail-closed policy (Phase 6):
      - sig is None        → required price history unavailable → reject
      - sig has '_error'   → price data malformed → reject with specific reason
      - volume keys absent → volume gate is skipped; price rules still apply

    Price filters: RSI overbought, chasing 30m spike, strong downtrend.
    Volume filters: volume drying up (vol_ratio < 0.3) signals conviction is fading.
    """
    if sig is None:
        return False, "insufficient_price_history"
    error = sig.get("_error") if isinstance(sig, dict) else None
    if error:
        return False, error
    if not sig:
        return False, "insufficient_price_history"
    rsi    = sig["rsi"]
    mom_30 = sig["mom_30m"]
    trend  = sig["trend_ph"]
    if rsi > CHART_RSI_OVERBOUGHT:
        return False, f"overbought_rsi({rsi:.0f})"
    if mom_30 > CHART_CHASE_LIMIT:
        return False, f"chasing_30m(+{mom_30:.3f} abs)"
    if trend < -0.05 and mom_30 < -0.05:
        return False, f"downtrend({trend:+.3f}/hr)"
    # Volume: if we have data and volume is drying up fast with sell pressure, skip
    vol_ratio    = sig.get("vol_ratio")
    buy_pressure = sig.get("buy_pressure")
    if vol_ratio is not None and buy_pressure is not None:
        if vol_ratio < 0.3 and buy_pressure < 0.35:
            return False, f"vol_drying(ratio={vol_ratio},buy={buy_pressure})"
    return True, ""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS copies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE, copied_ts TEXT, wallet TEXT, their_ts INTEGER,
        condition_id TEXT, asset TEXT, title TEXT, outcome TEXT, outcome_index INTEGER,
        their_price REAL, entry REAL, stake REAL, shares REAL,
        status TEXT, mark_price REAL, pnl REAL, resolved_ts TEXT)""")
    try:
        c.execute("ALTER TABLE copies ADD COLUMN model TEXT DEFAULT 'hold'")  # A=hold, B=exit
    except Exception:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS market_end(condition_id TEXT PRIMARY KEY, end_date TEXT)")
    for col in ("last_check TEXT", "last_reason TEXT", "peak REAL"):
        try:
            c.execute(f"ALTER TABLE copies ADD COLUMN {col}")
        except Exception:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, copy_id INTEGER, wallet TEXT,
        title TEXT, entry REAL, current REAL, gain REAL, decision TEXT, why TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bankroll(
        model TEXT, trader TEXT, cash REAL, PRIMARY KEY(model,trader))""")
    # Category + Quality Scoring tables (SDD v2)
    c.execute("""CREATE TABLE IF NOT EXISTS trader_cat_stats(
        trader TEXT, category TEXT, trades INTEGER, wins INTEGER, realized REAL,
        last_ts TEXT, PRIMARY KEY(trader, category))""")
    c.execute("""CREATE TABLE IF NOT EXISTS trader_cat_resolved(
        source_key TEXT PRIMARY KEY, trader TEXT, category TEXT, won INTEGER, pnl REAL, resolved_ts TEXT)""")
    # Entry-filter audit log: one row per unique source trade considered (keyed like
    # `copies` but WITHOUT the :model suffix, since the new filters are per-trade, not
    # per-model), whether accepted or rejected. Powers FILTER_SUMMARY / dashboard.
    c.execute("""CREATE TABLE IF NOT EXISTS entry_filter_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, ts TEXT, wallet TEXT,
        asset TEXT, condition_id TEXT, title TEXT, entry_price REAL,
        detection_lag REAL, slippage REAL, liquidity_usd REAL,
        accepted INTEGER, reason TEXT)""")
    # --- migration (additive only; no drops, no rewrites, no data loss) ---
    # Execution-simulation audit columns. Pre-existing rows keep their values and simply
    # read NULL for these, which the report/dashboard already tolerate.
    for col in ("wallet_price REAL", "detected_ts INTEGER", "exec_ts INTEGER",
                "copy_delay_sec INTEGER", "price_source TEXT", "market_price REAL",
                "slippage_bps REAL", "slippage_amount REAL", "fee_bps REAL", "fee REAL",
                "stake REAL", "shares REAL"):
        try:
            c.execute(f"ALTER TABLE entry_filter_log ADD COLUMN {col}")
        except Exception:
            pass
    # Chart-filter audit log: one row per Model E chart-gate decision (accepted and
    # rejected).  Persists what was previously ephemeral (the skipped_chart counter).
    c.execute("""CREATE TABLE IF NOT EXISTS chart_filter_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, asset TEXT, decision INTEGER, reason TEXT,
        price_data_ok INTEGER, volume_data_ok INTEGER,
        rsi REAL, mom_30m REAL, trend_ph REAL,
        vol_ratio REAL, buy_pressure REAL)""")

    # Same idea on `copies`, so an accepted paper position carries its own execution
    # provenance without needing a join. `their_price` (wallet price) and `entry` (final
    # simulated buy price) already exist and keep their meaning.
    for col in ("exec_ts INTEGER", "market_price REAL", "fee REAL"):
        try:
            c.execute(f"ALTER TABLE copies ADD COLUMN {col}")
        except Exception:
            pass
    return c


def _cash(c, model, trader):
    """Each (model, trader) is its own $20 wallet. Lazily initialised on first use."""
    r = c.execute("SELECT cash FROM bankroll WHERE model=? AND trader=?", (model, trader)).fetchone()
    if r is None:
        c.execute("INSERT OR IGNORE INTO bankroll(model,trader,cash) VALUES(?,?,?)",
                  (model, trader, BANKROLL_PER_MODEL))
        return BANKROLL_PER_MODEL
    return r[0]


def _credit(c, model, trader, amount):
    c.execute("UPDATE bankroll SET cash=cash+? WHERE model=? AND trader=?", (amount, model, trader))


def _log_filter_event(c, key, wallet, asset, cid, title, entry_price, filt):
    """Persist one entry_filter_log row capturing the full simulated execution — accepted
    or rejected, every candidate gets a row with an explicit reason. Idempotent: INSERT OR
    IGNORE on the same key, so re-seeing an unaged trade across ticks can't double-count."""
    try:
        c.execute("""INSERT OR IGNORE INTO entry_filter_log(
            key, ts, wallet, asset, condition_id, title, entry_price,
            detection_lag, slippage, liquidity_usd, accepted, reason,
            wallet_price, detected_ts, exec_ts, copy_delay_sec, price_source,
            market_price, slippage_bps, slippage_amount, fee_bps, fee, stake, shares)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, now_iso(), wallet, asset, cid, (title or "")[:80],
             filt.get("entry_price", entry_price),
             filt["detection_lag"], filt["slippage"], filt["liquidity_usd"],
             1 if filt["accepted"] else 0, filt["reason"],
             filt.get("wallet_price"), filt.get("detected_ts"), filt.get("exec_ts"),
             filt.get("copy_delay_sec"), filt.get("price_source"),
             filt.get("market_price"), filt.get("slippage_bps"),
             filt.get("slippage_amount"), filt.get("fee_bps"), filt.get("fee"),
             filt.get("stake"), filt.get("shares")))
    except Exception:
        pass


def _canonical_reason(reason):
    """Map legacy reason names onto the current constants (see LEGACY_REASON_ALIASES)."""
    return LEGACY_REASON_ALIASES.get(reason, reason)


def _log_chart_filter_event(c, asset, ok, reason, sig):
    """Persist one chart_filter_log row for every Model E chart-gate evaluation.

    This replaces the ephemeral skipped_chart counter with a durable, auditable record.
    Silently suppressed on any DB error so a logging failure can't crash the tick.
    """
    try:
        price_data_ok = 0 if (sig is None or (isinstance(sig, dict) and sig.get("_error"))) else 1
        volume_data_ok = 1 if (isinstance(sig, dict) and "vol_ratio" in sig and "buy_pressure" in sig) else 0
        rsi         = sig.get("rsi")         if isinstance(sig, dict) else None
        mom_30m     = sig.get("mom_30m")     if isinstance(sig, dict) else None
        trend_ph    = sig.get("trend_ph")    if isinstance(sig, dict) else None
        vol_ratio   = sig.get("vol_ratio")   if isinstance(sig, dict) else None
        buy_pressure= sig.get("buy_pressure")if isinstance(sig, dict) else None
        c.execute("""INSERT INTO chart_filter_log(
            ts, asset, decision, reason,
            price_data_ok, volume_data_ok,
            rsi, mom_30m, trend_ph, vol_ratio, buy_pressure)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso(), asset, 1 if ok else 0, reason,
             price_data_ok, volume_data_ok,
             rsi, mom_30m, trend_ph, vol_ratio, buy_pressure))
    except Exception:
        pass


def _filter_summary(c):
    """Aggregate entry_filter_log into the numbers report()/export() display."""
    row = c.execute("SELECT COUNT(*), COALESCE(SUM(accepted),0) FROM entry_filter_log").fetchone()
    total, accepted = (row[0] or 0), (row[1] or 0)
    skipped = total - accepted
    reasons = {r: 0 for r in SKIP_REASONS}
    for reason, cnt in c.execute(
            "SELECT reason, COUNT(*) FROM entry_filter_log WHERE accepted=0 GROUP BY reason"):
        canon = _canonical_reason(reason)
        if canon in reasons:
            reasons[canon] += cnt
        else:
            reasons["OTHER_FILTER"] += cnt
    lags = sorted(r[0] for r in c.execute(
        "SELECT detection_lag FROM entry_filter_log WHERE detection_lag IS NOT NULL").fetchall())
    avg_lag = round(sum(lags) / len(lags), 1) if lags else None
    med_lag = round(lags[len(lags) // 2], 1) if lags else None
    # Execution stats over accepted copies only (what we actually "paid").
    ex = c.execute("""SELECT AVG(slippage_amount), COALESCE(SUM(fee),0), AVG(fee)
                      FROM entry_filter_log WHERE accepted=1""").fetchone()
    avg_slip_amt = round(ex[0], 6) if ex and ex[0] is not None else None
    total_fees = round(ex[1], 6) if ex and ex[1] is not None else 0.0
    return {"total": total, "accepted": accepted, "skipped": skipped, "reasons": reasons,
            "acceptance_rate": round(accepted / total, 4) if total else None,
            "avg_detection_lag_sec": avg_lag, "median_detection_lag_sec": med_lag,
            "avg_slippage_amount": avg_slip_amt, "total_paper_fees": total_fees}


def record_paper_copy(c, base_key, wallet, their_ts, cid, asset, title, outcome, oidx,
                       their_price, entry, stake, shares, model, exec_ts=None,
                       market_price=None, fee=0.0):
    """Insert exactly one `copies` row for (base_key, model) — the same insert tick()
    already did inline, extracted so it's independently testable ('accepted trade creates
    exactly one paper position', 'rejection creates none'). Returns True iff a row was
    inserted; False on dedupe-hit or insufficient per-model bankroll (unchanged existing
    behavior, just no longer duplicated per model).

    `entry` is the FINAL simulated buy price (post delay + slippage); `their_price` stays
    the copied wallet's own price; `market_price` is the pre-slippage price; `fee` is the
    paper fee (0.0 by default, so bankroll maths is unchanged from before)."""
    key = f"{base_key}:{model}"
    if c.execute("SELECT 1 FROM copies WHERE key=?", (key,)).fetchone():
        return False
    if _cash(c, model, wallet) < stake:   # this trader's OWN $20 wallet for this model
        return False
    c.execute("""INSERT OR IGNORE INTO copies(key,copied_ts,wallet,their_ts,condition_id,
        asset,title,outcome,outcome_index,their_price,entry,stake,shares,status,model,
        exec_ts,market_price,fee)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, now_iso(), wallet, their_ts, cid, asset, (title or "")[:80], outcome, oidx,
         their_price, entry, stake, shares, "open", model, exec_ts, market_price, fee))
    _credit(c, model, wallet, -stake)     # lock stake out of THIS trader's wallet
    return True


_endcache = {}


def _resolves_soon(cid, c):
    """True if this market resolves within MAX_HOLD_RESOLVE_DAYS. Caches end dates
    (in-memory + db) so we only look up each market once. Unknown/unfetchable -> False
    (skip, since a short test can't wait on an unknown resolution)."""
    if not cid:
        return False
    if cid in _endcache:
        ed = _endcache[cid]
    else:
        row = c.execute("SELECT end_date FROM market_end WHERE condition_id=?", (cid,)).fetchone()
        if row:
            ed = row[0]
        else:
            m = _market_by_condition(cid)
            ed = m.get("endDate") if m else None
            c.execute("INSERT OR REPLACE INTO market_end(condition_id,end_date) VALUES(?,?)", (cid, ed))
        _endcache[cid] = ed
    if not ed:
        return False
    try:
        end = datetime.fromisoformat(ed.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return now < end <= now + timedelta(days=MAX_HOLD_RESOLVE_DAYS)
    except Exception:
        return False


# only copy VERY fresh trades so our entry ~= the trader's fill (a true forward test).
# The data feed itself lags ~6 min, so 15 min catches new trades while killing the
# look-ahead bias of copying hours-old, already-moved positions at their stale price.
MAX_TRADE_AGE_SEC = 15 * 60

# Skip markets that resolve faster than our ~6-min feed lag — by the time we see the
# trade the outcome is already decided, so copying it is look-ahead, not a real signal.
# (5/15-min crypto "up or down", in-game sports, etc.) Leaves genuinely copyable markets.
FAST_MARKET_PATTERNS = ("up or down", "halftime", "at the half", "next goal", "this drive")


def _is_fast_market(title):
    t = (title or "").lower()
    return any(p in t for p in FAST_MARKET_PATTERNS)


# === Entry / execution simulation (PAPER ONLY — simulated, never submitted anywhere) ===
# All tunable from .env or the environment; see .env.example and the README section
# "Paper execution assumptions".
# Rejects anything strictly ABOVE this, so 0.80 is still eligible and 0.81 / 0.90 are
# rejected. Checked against the FINAL simulated buy price (after delay + slippage),
# not the wallet's own fill price. Single source of truth for the setting.
MAX_ENTRY_PRICE = _env_float("MAX_ENTRY_PRICE", 0.80)   # reject copies priced above this
MIN_ENTRY_PRICE = _env_float("MIN_ENTRY_PRICE", 0.05)   # reject copies priced below this
# Simulated detection/copy delay in seconds. This is arithmetic on timestamps
# (exec_ts = source_ts + COPY_DELAY_SECONDS) used to look up the market price at the
# simulated execution moment — it is NEVER a real time.sleep(), so ticks and tests run
# at full speed.
COPY_DELAY_SECONDS = _env_int("COPY_DELAY_SECONDS", 30)
# Simulated adverse slippage on entry, in basis points (50 = 0.50%). Applied against the
# copier: a BUY fills HIGHER than the market price. Kept separate from PAPER_FEE_BPS.
SLIPPAGE_BPS = _env_float("SLIPPAGE_BPS", 50.0)
# Paper fee in basis points, charged on stake. Defaults to 0 deliberately: this repo does
# NOT assume or invent a Polymarket fee schedule. At 0 the maths is identical to the
# previous fee-free behavior. Set it later if you want to model a fee.
PAPER_FEE_BPS = _env_float("PAPER_FEE_BPS", 0.0)

# Slippage: reuse the project's OWN orderbook-walk definition of slippage (see
# paper_trader.simulate_buy: walk CLOB asks to fill a stake, slippage = (avg_fill-best_ask)/
# best_ask). 4% is not a new invented number — it is literally that file's existing
# MAX_SLIPPAGE, i.e. the slippage this repo already treats as acceptable for a real market
# buy. We default to the same figure here rather than invent a copybot-specific one.
# NOTE: this is the *observed book* slippage gate and is distinct from SLIPPAGE_BPS, which
# is the modelled cost actually applied to the simulated fill price.
MAX_SLIPPAGE = _env_float("MAX_SLIPPAGE", 0.04)
# Recommended default: if we can't walk the book (fetch failed / book too thin to fill our
# stake at any price), treat it the same way paper_trader.py already does — as unfillable —
# and reject rather than silently assume a good fill.
REJECT_IF_SLIPPAGE_UNKNOWN = _env_bool("REJECT_IF_SLIPPAGE_UNKNOWN", True)
# Liquidity: derived from the SAME CLOB order book fetched for the slippage walk (no
# separate/fabricated metric) — total resting $ notional (price*size) across all ask
# levels. STAKE is $2/copy; requiring 10x STAKE of resting ask depth keeps our copy from
# being a large fraction of the visible book.
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", STAKE * 10)
# Symmetric with REJECT_IF_SLIPPAGE_UNKNOWN: if the book can't tell us liquidity, don't
# assume it's fine.
REJECT_IF_LIQUIDITY_UNKNOWN = _env_bool("REJECT_IF_LIQUIDITY_UNKNOWN", True)
# Detection lag: paper_copy_timestamp - source_trade_timestamp. The repo already treats
# "trade older than 15 min when we see it" as too stale to copy fairly (MAX_TRADE_AGE_SEC
# above) — MAX_DETECTION_LAG_SECONDS reuses that exact figure as its default rather than
# inventing a new one; it's evaluated as its own filter (with its own reason/counter) so
# it stays tunable independently of MAX_TRADE_AGE_SEC.
MAX_DETECTION_LAG_SECONDS = _env_float("MAX_DETECTION_LAG_SECONDS", MAX_TRADE_AGE_SEC)

# Audit-log reason constants. ACCEPTED is recorded on accepted candidates so every
# candidate has an explicit, non-vague outcome.
REASON_ACCEPTED = "ACCEPTED"
SKIP_REASONS = ("PRICE_TOO_HIGH", "PRICE_TOO_LOW", "INVALID_PRICE", "SLIPPAGE_TOO_HIGH",
                "SLIPPAGE_UNKNOWN", "INSUFFICIENT_LIQUIDITY", "LIQUIDITY_UNKNOWN",
                "DETECTION_LAG_TOO_HIGH", "DUPLICATE", "OTHER_FILTER")
# Rows written by the previous revision used older names. Mapping them keeps historical
# paper results readable in the report/dashboard instead of silently falling into
# OTHER_FILTER — no data is rewritten or deleted.
LEGACY_REASON_ALIASES = {
    "ENTRY_PRICE_ABOVE_MAX": "PRICE_TOO_HIGH",
    "ENTRY_PRICE_BELOW_MIN": "PRICE_TOO_LOW",
    "LIQUIDITY_TOO_LOW": "INSUFFICIENT_LIQUIDITY",
}


def _price_at(token_id, ts):
    """Market price for `token_id` at (or immediately before) unix time `ts`, from the
    CLOB prices-history endpoint the project already uses in chart_signal().

    This is how the simulated copy delay becomes a real price difference: we ask for the
    price at source_ts + COPY_DELAY_SECONDS instead of the price the wallet got.
    Returns None if history is unavailable or has no bar at/before ts — the caller then
    falls back to the current market price rather than fabricating one.
    """
    if not token_id or ts is None:
        return None
    try:
        data = get(f"{CLOB}/prices-history?market={token_id}"
                   f"&startTs={int(ts) - 900}&endTs={int(ts) + 60}&fidelity=1")
        hist = data.get("history", [])
    except Exception:
        return None
    at_or_before = [h for h in hist if int(h.get("t", 0)) <= int(ts)]
    if not at_or_before:
        return None
    try:
        return float(max(at_or_before, key=lambda h: int(h["t"]))["p"])
    except (KeyError, TypeError, ValueError):
        return None


def apply_slippage(price, slippage_bps=None):
    """Adverse entry slippage for a BUY, in basis points (1 bp = 0.01%).

        filled_price = price * (1 + slippage_bps / 10_000)

    The copier always fills WORSE (higher) than the market price, never better. The
    result is clamped into the valid probability range (0.0, 1.0) exclusive, so a
    slippage-inflated price can never become an invalid (>=1.0) share price.
    Returns (filled_price, slippage_amount). 0 bps returns the price unchanged.
    """
    bps = SLIPPAGE_BPS if slippage_bps is None else slippage_bps
    filled = price * (1.0 + bps / 10_000.0)
    filled = min(max(filled, 1e-6), 0.999999)
    return filled, filled - price


def apply_fee(stake, fee_bps=None):
    """Paper fee charged on stake, in basis points, kept entirely separate from slippage.

        fee = stake * fee_bps / 10_000

    Defaults to PAPER_FEE_BPS, which is 0: this project does NOT assume a Polymarket fee
    schedule. At 0 bps fee == 0.0 and the remaining stake is unchanged, so accounting is
    byte-identical to the previous fee-free behavior.
    Returns (fee, stake_after_fee).
    """
    bps = PAPER_FEE_BPS if fee_bps is None else fee_bps
    fee = stake * bps / 10_000.0
    return fee, stake - fee


def _fetch_book(token_id):
    """Fetch the live CLOB order book for a token. Returns the raw dict, or None if it
    can't be fetched (network error, bad token, etc.) — callers must treat None as
    'unknown', never as 'empty'."""
    if not token_id:
        return None
    try:
        return get(f"{CLOB}/book?token_id={token_id}")
    except Exception:
        return None


def _walk_book_for_stake(asks, stake):
    """Walk asks (ascending price) to fill `stake` USD, exactly like
    paper_trader.simulate_buy. Returns (avg_price, slippage) or (None, None) if the book
    is empty or too thin to fill `stake` at any price (treated as unknown/unfillable,
    matching that function's existing convention)."""
    if not asks:
        return None, None
    asks = sorted(asks, key=lambda a: a["price"])
    best = asks[0]["price"]
    if best <= 0:
        return None, None
    remaining, shares = stake, 0.0
    for lvl in asks:
        lvl_cost = lvl["price"] * lvl["size"]
        take = min(remaining, lvl_cost)
        if lvl["price"] > 0:
            shares += take / lvl["price"]
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or shares <= 0:
        return None, None
    avg = stake / shares
    return avg, (avg - best) / best


def _book_liquidity_usd(asks):
    """Total resting $ notional (price*size) across all ask levels of an already-fetched
    book — an honest sum of data already in the payload, not a fabricated metric.
    Returns None if we don't even have a parsed ask list (unknown), 0.0 for a genuinely
    empty book (known, just zero)."""
    if asks is None:
        return None
    return round(sum(a["price"] * a["size"] for a in asks), 2)


def evaluate_entry_filters(market_price, source_ts, now_ts, book, wallet_price=None,
                           exec_price=None, stake=None):
    """Simulate copying one wallet trade and decide whether it may become a paper copy.

    PAPER ONLY: nothing here signs, submits or settles anything — it computes what a copy
    *would* have cost under the configured assumptions.

    Pipeline (the order matters, and is the order required by the spec):
        wallet trade
          -> simulated detection/copy delay   (exec_ts = source_ts + COPY_DELAY_SECONDS)
          -> market price at simulated execution time  (`exec_price`, else `market_price`)
          -> adverse slippage                 (SLIPPAGE_BPS)
          -> entry price limits               (MIN/MAX_ENTRY_PRICE, on the FINAL price)
          -> book slippage / liquidity gates  (pre-existing, unchanged)
          -> paper fee + share sizing         (PAPER_FEE_BPS)

    Args:
        market_price: current market price for the outcome (fallback if no exec price).
        source_ts:    unix ts of the wallet's original trade, or None if unknown.
        now_ts:       unix ts at detection ("paper_copy_timestamp").
        book:         raw CLOB order book dict with an "asks" list, or None if unavailable.
        wallet_price: the price the copied wallet itself got (recorded for audit only —
                      never used for the MAX_ENTRY_PRICE decision).
        exec_price:   market price observed at exec_ts (from _price_at). None -> fall back
                      to market_price, which is recorded via price_source.
        stake:        stake to size the copy with; defaults to STAKE.

    Returns a dict with the full execution audit trail. `accepted` is a bool and `reason`
    is always set: REASON_ACCEPTED on success, otherwise exactly one SKIP_REASONS value
    (checks stop at the first failure, which keeps accepted+skipped and the per-reason
    counters consistent).
    """
    stake = STAKE if stake is None else stake
    detection_lag = (now_ts - source_ts) if source_ts is not None else None
    exec_ts = (source_ts + COPY_DELAY_SECONDS) if source_ts is not None else None
    # Price the delay lands us on: the post-delay quote when we have one, else the current
    # market price. price_source records which, so the audit trail never overstates.
    price_source = "exec_ts_history" if exec_price is not None else "current_market"
    pre_slip = exec_price if exec_price is not None else market_price

    base = {"wallet_price": wallet_price, "detected_ts": now_ts, "exec_ts": exec_ts,
            "copy_delay_sec": COPY_DELAY_SECONDS, "detection_lag": detection_lag,
            "price_source": price_source, "market_price": pre_slip,
            "slippage_bps": SLIPPAGE_BPS, "slippage_amount": None, "entry_price": None,
            "fee_bps": PAPER_FEE_BPS, "fee": None, "stake": stake, "shares": None,
            "slippage": None, "liquidity_usd": None}

    def result(accepted, reason, **over):
        out = dict(base, accepted=accepted, reason=reason)
        out.update(over)
        return out

    # --- price must be a usable probability before anything else is computed ---
    try:
        pre_slip = float(pre_slip)
    except (TypeError, ValueError):
        return result(False, "INVALID_PRICE")
    if not (0.0 < pre_slip < 1.0):
        return result(False, "INVALID_PRICE", market_price=pre_slip)
    base["market_price"] = pre_slip

    # --- simulated adverse slippage, applied BEFORE the entry-price limits ---
    final_price, slip_amt = apply_slippage(pre_slip)
    base["slippage_amount"] = round(slip_amt, 6)
    base["entry_price"] = round(final_price, 6)
    if not (0.0 < final_price < 1.0):        # defensive; apply_slippage already clamps
        return result(False, "INVALID_PRICE")

    # --- entry price limits, on the FINAL simulated buy price ---
    # A wallet that bought at 0.79 can still be rejected here if delay+slippage push our
    # simulated fill above MAX_ENTRY_PRICE. That is the point of the ordering.
    if final_price > MAX_ENTRY_PRICE:
        return result(False, "PRICE_TOO_HIGH")
    if final_price < MIN_ENTRY_PRICE:
        return result(False, "PRICE_TOO_LOW")

    if detection_lag is not None and detection_lag > MAX_DETECTION_LAG_SECONDS:
        return result(False, "DETECTION_LAG_TOO_HIGH")

    # --- pre-existing observed-book gates (unchanged behavior) ---
    asks = None
    if book is not None:
        try:
            asks = [{"price": float(a["price"]), "size": float(a["size"])}
                    for a in book.get("asks", [])]
        except Exception:
            asks = None

    _, slippage = _walk_book_for_stake(asks, stake)
    liquidity_usd = _book_liquidity_usd(asks)
    base["slippage"], base["liquidity_usd"] = slippage, liquidity_usd

    if slippage is None:
        if REJECT_IF_SLIPPAGE_UNKNOWN:
            return result(False, "SLIPPAGE_UNKNOWN")
    elif slippage > MAX_SLIPPAGE:
        return result(False, "SLIPPAGE_TOO_HIGH")

    if liquidity_usd is None:
        if REJECT_IF_LIQUIDITY_UNKNOWN:
            return result(False, "LIQUIDITY_UNKNOWN")
    elif liquidity_usd < MIN_LIQUIDITY:
        return result(False, "INSUFFICIENT_LIQUIDITY")

    # --- accepted: size the paper position (fee separate from slippage) ---
    fee, stake_after_fee = apply_fee(stake)
    return result(True, REASON_ACCEPTED, fee=round(fee, 6),
                  shares=stake_after_fee / final_price)


# === SDD v2 Category + Quality helpers ===
def _get_trader_cat_quality(c, trader, category):
    row = c.execute("SELECT trades, wins, realized FROM trader_cat_stats WHERE trader=? AND category=?",
                    (trader, category)).fetchone()
    if not row or row[0] < MIN_CAT_TRADES:
        return {"trades": row[0] if row else 0, "wr": 0.0, "roi": 0.0, "trusted": False}
    trades, wins, realized = row
    wr = wins / trades
    roi = realized / max(1, trades * 2.0)  # proxy
    trusted = (wr >= MIN_CAT_WR or roi >= MIN_CAT_ROI)
    return {"trades": trades, "wr": round(wr, 3), "roi": round(roi, 3), "trusted": trusted}

def _update_trader_cat_stat(c, trader, category, won: bool, pnl: float, source_key: str = None):
    """Update trader_cat_stats on actual market resolution.

    If source_key is provided, deduplicates across Models A-E and repeated runs so that
    each unique underlying trader trade contributes at most one sample to trader_cat_stats.

    Atomicity guarantee: the dedup INSERT into trader_cat_resolved and the corresponding
    UPDATE/INSERT into trader_cat_stats are wrapped in a SQLite SAVEPOINT.  If the stats
    update fails for any reason both changes are rolled back together, leaving no orphaned
    dedup row that would permanently block a future retry.

    Error handling:
      - sqlite3.IntegrityError on the dedup INSERT  → duplicate, skip gracefully → False
      - Any other exception (stats update or unexpected)  → ROLLBACK TO savepoint, then
        re-raise so the caller's try/except can log/suppress it.  The re-raise ensures the
        outer resolve() loop does not silently commit a half-written pair.
    """
    if source_key:
        # SAVEPOINT nests inside the outer transaction that resolve() commits at the end.
        # Both the dedup INSERT and the stats UPDATE must commit or roll back together.
        c.execute("SAVEPOINT sp_cat")
        try:
            try:
                c.execute(
                    "INSERT INTO trader_cat_resolved"
                    "(source_key, trader, category, won, pnl, resolved_ts) VALUES(?,?,?,?,?,?)",
                    (source_key, trader, category, 1 if won else 0, pnl or 0.0, now_iso())
                )
            except sqlite3.IntegrityError:
                # Confirm the error is a duplicate primary key on source_key, not an
                # unrelated constraint violation (NOT NULL, CHECK, etc.).  If source_key
                # is not in the table, the IntegrityError came from something else and
                # must propagate so the caller can roll back and retry.
                existing = c.execute(
                    "SELECT 1 FROM trader_cat_resolved WHERE source_key=?",
                    (source_key,)
                ).fetchone()
                if existing:
                    # Confirmed duplicate — skip stats update, release savepoint.
                    c.execute("RELEASE sp_cat")
                    return False
                # Unrelated integrity violation — re-raise so outer except block handles ROLLBACK/RELEASE
                raise
            # Stats update — inside the same savepoint so a failure rolls back the dedup
            # INSERT as well.
            row = c.execute(
                "SELECT trades, wins, realized FROM trader_cat_stats WHERE trader=? AND category=?",
                (trader, category)).fetchone()
            if row:
                trades, wins, realized = row
                c.execute(
                    """UPDATE trader_cat_stats SET trades=?, wins=?, realized=?, last_ts=?
                       WHERE trader=? AND category=?""",
                    (trades + 1, wins + (1 if won else 0), realized + (pnl or 0),
                     now_iso(), trader, category))
            else:
                c.execute(
                    "INSERT INTO trader_cat_stats VALUES (?,?,?,?,?,?)",
                    (trader, category, 1, 1 if won else 0, pnl or 0, now_iso()))
            c.execute("RELEASE sp_cat")
            return True
        except Exception:
            # Roll back BOTH the dedup INSERT and the failed stats update atomically,
            # then release the savepoint so the outer transaction continues cleanly.
            c.execute("ROLLBACK TO sp_cat")
            c.execute("RELEASE sp_cat")
            raise  # Re-raise: the outer resolve() try/except will handle/log it
    else:
        # Legacy path: no source_key, no dedup, no savepoint needed.
        row = c.execute(
            "SELECT trades, wins, realized FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, category)).fetchone()
        if row:
            trades, wins, realized = row
            c.execute(
                """UPDATE trader_cat_stats SET trades=?, wins=?, realized=?, last_ts=?
                   WHERE trader=? AND category=?""",
                (trades + 1, wins + (1 if won else 0), realized + (pnl or 0),
                 now_iso(), trader, category))
        else:
            c.execute(
                "INSERT INTO trader_cat_stats VALUES (?,?,?,?,?,?)",
                (trader, category, 1, 1 if won else 0, pnl or 0, now_iso()))
        return True


def tick():
    c = db()
    now = int(time.time())
    added = 0
    skipped_chart = 0
    skipped_filters = 0
    skipped_dupes = 0
    mkt_cache = {}    # condition_id -> gamma market data
    chart_cache = {}  # asset (token_id) -> chart_signal result
    book_cache = {}   # asset (token_id) -> _fetch_book() result (for entry filters)
    exec_cache = {}   # (asset, exec_ts) -> market price at simulated execution time
    seen_keys = set()  # candidate keys already evaluated in THIS run (duplicate guard)
    for name, addr in WALLETS.items():
        try:
            act = get(f"{DATA}/activity?user={addr}&limit=100")
        except Exception as e:
            print(f"  {name}: fetch failed ({e})")
            continue
        for a in act:
            if a.get("type") != "TRADE" or a.get("side") != "BUY":
                continue
            if now - int(a.get("timestamp") or 0) > MAX_TRADE_AGE_SEC:
                continue  # too old — would be look-ahead, skip
            if _is_fast_market(a.get("title")):
                continue  # resolves faster than our feed lag -> can't copy fairly
            asset = a.get("asset")
            tx = a.get("transactionHash") or ""
            cid = a.get("conditionId")
            oidx = a.get("outcomeIndex") or 0
            their_price = float(a.get("price") or 0)
            if not asset or not cid:
                continue
            # fetch the market once (cached per tick); ENTRY = current price (same source we
            # mark with) => gain starts at 0, no look-ahead regardless of market speed.
            if cid not in mkt_cache:
                mkt_cache[cid] = _market_by_condition(cid)
            m = mkt_cache[cid]
            if not m:
                continue
            try:
                prices = [float(p) for p in json.loads(m["outcomePrices"])]
                entry = prices[oidx]
            except Exception:
                continue
            if entry <= 0.02 or entry >= 0.98:   # already (near) resolved -> no fair copy
                continue
            base = f"{tx}:{asset}:{a.get('timestamp')}"

            # === Entry / execution simulation (price / slippage / liquidity / lag) ===
            # Per-TRADE, not per-model: a bad entry is bad for every model equally, so we
            # gate once here, before any model-specific position is created. Models A-E
            # then all receive the SAME accepted trade at the SAME simulated fill.
            if base in seen_keys:
                skipped_dupes += 1
                continue  # DUPLICATE candidate within this run — never copied twice
            seen_keys.add(base)
            try:
                source_ts = int(a.get("timestamp"))
            except (TypeError, ValueError):
                source_ts = None
            if asset not in book_cache:
                book_cache[asset] = _fetch_book(asset)
            # Simulated copy delay: look up the price at source_ts + COPY_DELAY_SECONDS.
            # Pure timestamp arithmetic + a history lookup — no real sleep anywhere.
            exec_price = None
            if source_ts is not None and COPY_DELAY_SECONDS:
                ek = (asset, source_ts + COPY_DELAY_SECONDS)
                if ek not in exec_cache:
                    exec_cache[ek] = _price_at(asset, source_ts + COPY_DELAY_SECONDS)
                exec_price = exec_cache[ek]
            filt = evaluate_entry_filters(entry, source_ts, now, book_cache[asset],
                                          wallet_price=their_price, exec_price=exec_price)
            _log_filter_event(c, base, name, asset, cid, a.get("title"), entry, filt)
            if not filt["accepted"]:
                skipped_filters += 1
                continue  # rejected — no paper position for this trade, in any model

            # Final simulated buy price (post delay + slippage) and fee-adjusted sizing.
            entry = filt["entry_price"]
            shares = filt["shares"]

            # Model A (hold) only for markets resolving within 3 days
            hold_ok = False
            try:
                end = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
                hnow = datetime.now(timezone.utc)
                hold_ok = hnow < end <= hnow + timedelta(days=MAX_HOLD_RESOLVE_DAYS)
            except Exception:
                hold_ok = False
            # Chart signal (fetched once per asset, cached across this tick run)
            if asset not in chart_cache:
                chart_cache[asset] = chart_signal(asset)
            sig = chart_cache[asset]

            # === SDD v2 Category + Quality gate (biggest lever) ===
            # Only proceed with this trader in this category if they have proven edge in *our* paper history.
            cat = categorize_market(a.get("title", ""), a.get("slug", ""), "")
            q = _get_trader_cat_quality(c, name, cat)
            if q["trades"] >= MIN_CAT_TRADES and not q["trusted"]:
                # skip low-edge category for this specialist
                continue

            for model in ("hold", "exit", "cdecide", "trail", "chart"):  # A B C D E
                if model == "hold" and not hold_ok:
                    continue
                if model == "chart":
                    ok, reason = _chart_entry_ok(sig, entry)
                    _log_chart_filter_event(c, asset, ok, reason, sig)
                    if not ok:
                        skipped_chart += 1
                        continue  # chart says skip this entry
                if record_paper_copy(c, base, name, a.get("timestamp"), a.get("conditionId"),
                                      asset, a.get("title"), a.get("outcome"),
                                      a.get("outcomeIndex"), their_price, entry, STAKE,
                                      shares, model, exec_ts=filt["exec_ts"],
                                      market_price=filt["market_price"], fee=filt["fee"]):
                    added += 1
        c.commit()
    print(f"tick: recorded {added} new paper copies across {len(WALLETS)} wallets "
          f"(chart filtered {skipped_chart} entries, entry-filter rejected "
          f"{skipped_filters} trades, {skipped_dupes} duplicates)")


def _market_by_condition(cid):
    if not cid:
        return None
    try:
        r = get(f"{GAMMA}/markets?condition_ids={cid}")
        return r[0] if r else None
    except Exception:
        return None


def resolve():
    c = db()
    # newest-open prioritized for Model B (exit needs frequent price checks); bounded by
    # row count + wall-clock so a slow gamma API can't blow the job timeout.
    rows = c.execute("SELECT id,condition_id,outcome_index,shares,stake,entry,"
                     "COALESCE(model,'hold'),copied_ts,wallet,peak,asset,title,key FROM copies "
                     "WHERE status='open' ORDER BY id DESC LIMIT 150").fetchall()
    deadline = time.time() + 300
    now = time.time()
    settled = marked = exited = 0
    chart_cache = {}   # asset -> chart_signal, fetched lazily for Model E
    for cid_id, cid, oidx, shares, stake, entry, model, copied_ts, wallet, peak, asset, title, copy_key in rows:
        if time.time() > deadline:
            print("resolve: hit 5-min deadline, stopping early (rest next run)")
            break
        m = _market_by_condition(cid)
        if not m:
            continue
        try:
            prices = [float(p) for p in json.loads(m["outcomePrices"])]
        except Exception:
            continue
        oidx = oidx if oidx is not None else 0
        cur = prices[oidx] if oidx < len(prices) else entry
        if m.get("closed"):                       # market resolved -> settle any model
            win_idx = max(range(len(prices)), key=lambda i: prices[i])
            won = (win_idx == oidx) and prices[win_idx] > 0.5
            payout = shares if won else 0.0
            # SAVEPOINT wraps the entire settlement: status change + bankroll credit +
            # category-stat update.  If the category update raises, everything rolls
            # back together so the copy stays 'open' and is retried next run.
            # _update_trader_cat_stat returns False (not raises) for duplicates, so a
            # copy whose category was already counted by another model copy still settles.
            c.execute("SAVEPOINT sp_settle")
            try:
                c.execute("UPDATE copies SET status=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                           ("won" if won else "lost", 1.0 if won else 0.0, payout - stake, now_iso(), cid_id))
                _credit(c, model, wallet, payout)  # proceeds back to this trader's wallet
                # SDD v2 quality stats update (per trader per category from our resolves)
                cat = categorize_market(title or "", "", "")
                source_key = copy_key.rsplit(":", 1)[0] if (copy_key and ":" in copy_key) else f"cid:{cid}:{wallet}"
                # Canonical prediction PnL on standard $2.00 stake ($2*(1.0/entry-1) if won else -$2.00)
                norm_pnl = (2.0 / entry - 2.0) if (won and entry > 0) else -2.0
                _update_trader_cat_stat(c, wallet, cat, won, norm_pnl, source_key=source_key)
                c.execute("RELEASE sp_settle")
                settled += 1
            except Exception as _exc:
                # Roll back status change and bankroll credit atomically.
                # Copy remains 'open'; will be retried on the next resolve() run.
                c.execute("ROLLBACK TO sp_settle")
                c.execute("RELEASE sp_settle")
                print(f"resolve: settlement deferred for copy id={cid_id} ({_exc!r}); will retry next run")

        elif model == "exit":                     # Model B: take-profit / timeout exit
            gain = (cur - entry) / entry if entry > 0 else 0
            try:
                age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
            except Exception:
                age_h = 0
            if gain >= TAKE_PROFIT:
                c.execute("UPDATE copies SET status='exit_won',mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "exit", wallet, shares * cur); exited += 1
            elif age_h >= MAX_HOLD_HOURS:
                c.execute("UPDATE copies SET status='exit_closed',mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "exit", wallet, shares * cur); exited += 1
            else:
                c.execute("UPDATE copies SET mark_price=?,pnl=? WHERE id=?",
                          (cur, shares * cur - stake, cid_id)); marked += 1
        elif model == "trail":                    # Model D: stop-loss + trailing stop
            pk = max(peak or entry, cur)
            gain = (cur - entry) / entry if entry else 0
            try:
                age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
            except Exception:
                age_h = 0
            if gain <= -STOP_LOSS:                # cut a wrong call fast
                c.execute("UPDATE copies SET status='d_stop',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "trail", wallet, shares * cur); exited += 1
            elif pk >= entry * (1 + TRAIL_ACTIVATE) and cur <= pk * (1 - TRAIL_DROP):  # winner reversing -> lock it
                c.execute("UPDATE copies SET status='d_trail',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "trail", wallet, shares * cur); exited += 1
            elif age_h >= TRAIL_MAX_HOLD_HOURS:
                c.execute("UPDATE copies SET status='d_closed',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "trail", wallet, shares * cur); exited += 1
            else:                                 # still running -> update peak, let it ride
                c.execute("UPDATE copies SET peak=?,mark_price=?,pnl=? WHERE id=?",
                          (pk, cur, shares * cur - stake, cid_id)); marked += 1
        elif model == "chart":                    # Model E: chart-aware (filtered entry + same D exits)
            pk = max(peak or entry, cur)
            gain = (cur - entry) / entry if entry else 0
            try:
                age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
            except Exception:
                age_h = 0
            # Fetch chart signal lazily (only for positions that have moved ≥5%)
            sig = None
            if abs(gain) >= 0.05 and asset:
                if asset not in chart_cache:
                    chart_cache[asset] = chart_signal(asset)
                sig = chart_cache[asset]
            if gain <= -CHART_STOP_LOSS:
                c.execute("UPDATE copies SET status='e_stop',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "chart", wallet, shares * cur); exited += 1
            elif pk >= entry * (1 + CHART_TRAIL_ACTIVATE) and cur <= pk * (1 - CHART_TRAIL_DROP):
                c.execute("UPDATE copies SET status='e_trail',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "chart", wallet, shares * cur); exited += 1
            elif age_h >= CHART_MAX_HOLD_HOURS:
                c.execute("UPDATE copies SET status='e_closed',peak=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (pk, cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "chart", wallet, shares * cur); exited += 1
            else:
                c.execute("UPDATE copies SET peak=?,mark_price=?,pnl=? WHERE id=?",
                          (pk, cur, shares * cur - stake, cid_id)); marked += 1
        else:                                     # Model A: hold, just mark-to-market
            c.execute("UPDATE copies SET mark_price=?,pnl=? WHERE id=?",
                      (cur, shares * cur - stake, cid_id)); marked += 1
    c.commit()
    print(f"resolve: settled {settled}, exits {exited}, marked {marked}")


def _ask_claude(key, title, outcome, entry, cur, gain, age_h, wallet, sig=None):
    """Ask Claude HOLD or SELL for one copied position. Returns (decision, why) or (None, err).
    sig: optional chart_signal() dict — if provided, Claude gets RSI/trend/momentum context.
    """
    chart_ctx = ""
    if sig:
        trend_dir = "uptrend" if sig["trend_ph"] > 0.02 else ("downtrend" if sig["trend_ph"] < -0.02 else "flat")
        vol_ctx = ""
        if sig.get("vol_ratio") is not None:
            vr = sig["vol_ratio"]
            bp = sig.get("buy_pressure", 0.5)
            vwap = sig.get("vwap")
            vol_trend = "surging" if vr > 1.5 else ("drying up" if vr < 0.5 else "steady")
            pressure = "buy-heavy" if bp > 0.6 else ("sell-heavy" if bp < 0.4 else "balanced")
            vwap_str = f" | VWAP {vwap:.3f} ({'above' if sig['current'] > vwap else 'below'} VWAP)" if vwap else ""
            vol_ctx = f" | vol {vol_trend} (ratio {vr:.1f}x) | {pressure} ({bp:.0%} buys){vwap_str}"
        chart_ctx = (
            f"\nChart (last 1h): RSI {sig['rsi']:.0f} | trend {trend_dir} ({sig['trend_ph']:+.3f}/hr) | "
            f"30m momentum {sig['mom_30m']:+.3f} | support {sig['support']:.3f} | resistance {sig['resistance']:.3f}"
            f"{vol_ctx}."
        )
    prompt = (
        f"You manage a Polymarket COPY position (mirroring trader {wallet}).\n"
        f'Market: "{title}"\n'
        f"Bought outcome: {outcome} at {entry:.3f}, now {cur:.3f} ({gain*100:+.1f}%), held {age_h:.0f}h."
        f"{chart_ctx}\n"
        f"You're copying a SKILLED trader who bought this expecting it to win. Default to HOLD "
        f"to capture the full move. SELL only if (a) clearly going wrong — down sharply with no path back, "
        f"or (b) spiked up and now reversing (high RSI + falling from peak = lock the gain). "
        f"Do NOT bank small early gains — let winners run toward resolution.\n"
        f'Reply with ONLY compact JSON: {{"decision":"HOLD"|"SELL","why":"<=12 words"}}.')
    body = {"model": CLAUDE_MODEL, "max_tokens": 120,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        txt = r["content"][0]["text"]
        d = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        return d.get("decision", "HOLD").upper(), (d.get("why", "") or "")[:120]
    except Exception as e:
        return None, str(e)[:80]


def claude_decide():
    """Model C: for moved Model-C positions, let Claude decide HOLD/SELL.
    Claude now receives live chart data (RSI, trend, momentum) for each position,
    enabling richer reasoning than price + age alone.
    Bounded by MAX_CLAUDE_CALLS_PER_RUN + a 6h re-check throttle."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("claude_decide: no ANTHROPIC_API_KEY, skipping"); return
    c = db()
    now = time.time()
    calls = sold = held = closed = 0
    chart_cache = {}  # asset -> chart_signal, fetched once per unique token this run
    rows = c.execute("SELECT id,shares,stake,entry,title,outcome,copied_ts,last_check,mark_price,wallet,asset "
                     "FROM copies WHERE model='cdecide' AND status='open' AND mark_price IS NOT NULL "
                     "ORDER BY id DESC LIMIT 500").fetchall()
    for cid_id, shares, stake, entry, title, outcome, copied_ts, last_check, cur, wallet, asset in rows:
        if calls >= MAX_CLAUDE_CALLS_PER_RUN:
            break
        try:
            age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
        except Exception:
            age_h = 0
        gain = (cur - entry) / entry if entry else 0
        if age_h >= CDECIDE_MAX_HOLD_HOURS:    # backstop force-close
            c.execute("UPDATE copies SET status='c_closed',pnl=?,resolved_ts=?,last_reason=? WHERE id=?",
                      (shares * cur - stake, now_iso(), "auto: 72h cap", cid_id))
            _credit(c, "cdecide", wallet, shares * cur)
            c.execute("INSERT INTO decisions(ts,copy_id,wallet,title,entry,current,gain,decision,why) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (now_iso(), cid_id, wallet, title, entry, cur, round(gain, 3), "SELL", "auto: 72h cap"))
            closed += 1
            c.commit(); continue
        moved = gain >= 0.05 or gain <= -0.10
        recently = False
        if last_check:
            try:
                recently = (now - datetime.fromisoformat(last_check).timestamp()) < CLAUDE_RECHECK_HOURS * 3600
            except Exception:
                recently = False
        if not moved or recently:
            continue
        # Fetch chart signal for this position (cached per asset per run)
        sig = None
        if asset:
            if asset not in chart_cache:
                chart_cache[asset] = chart_signal(asset)
            sig = chart_cache[asset]
        decision, why = _ask_claude(key, title, outcome, entry, cur, gain, age_h, wallet, sig=sig)
        calls += 1
        if decision is None:
            continue
        c.execute("INSERT INTO decisions(ts,copy_id,wallet,title,entry,current,gain,decision,why) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (now_iso(), cid_id, wallet, title, entry, cur, round(gain, 3), decision, why))
        c.execute("UPDATE copies SET last_check=?,last_reason=? WHERE id=?", (now_iso(), why, cid_id))
        if decision == "SELL":
            c.execute("UPDATE copies SET status='c_sold',pnl=?,resolved_ts=? WHERE id=?",
                      (shares * cur - stake, now_iso(), cid_id)); _credit(c, "cdecide", wallet, shares * cur); sold += 1
        else:
            held += 1
        c.commit()
    print(f"claude_decide: {calls} calls -> {sold} sold, {held} held, {closed} auto-closed")


CLOSED = ("won", "lost", "exit_won", "exit_closed", "c_sold", "c_closed",
          "d_stop", "d_trail", "d_closed", "e_stop", "e_trail", "e_closed")


def export():
    """Write docs/data.json: each trader's OWN $20 wallet under each model (A/B/C),
    a trader leaderboard (who's worth copying), model aggregates, and Claude's decisions."""
    c = db()
    MODELS = [("hold", "A"), ("exit", "B"), ("cdecide", "C"), ("trail", "D"), ("chart", "E")]
    agg = {}   # (model, trader) -> stats
    for model, wallet, status, pnl, stake in c.execute(
            "SELECT model,wallet,status,COALESCE(pnl,0),stake FROM copies"):
        d = agg.setdefault((model, wallet),
                           {"realized": 0.0, "unreal": 0.0, "at_risk": 0.0, "open": 0, "closed": 0, "wins": 0})
        if status in CLOSED:
            d["closed"] += 1; d["realized"] += pnl
            if pnl > 0:
                d["wins"] += 1
        elif status == "open":
            d["open"] += 1; d["unreal"] += pnl; d["at_risk"] += stake
    cash = {(m, t): v for m, t, v in c.execute("SELECT model,trader,cash FROM bankroll")}

    traders = []
    for t in sorted(WALLETS.keys()):
        row = {"name": t}
        for mk, mn in MODELS:
            d = agg.get((mk, t), {"realized": 0, "unreal": 0, "at_risk": 0, "open": 0, "closed": 0, "wins": 0})
            ch = cash.get((mk, t), BANKROLL_PER_MODEL)
            row[mn] = dict(value=round(ch + d["at_risk"] + d["unreal"], 2),
                           realized=round(d["realized"], 2),
                           unrealized=round(d["unreal"], 2),
                           open=d["open"], closed=d["closed"],
                           win_rate=round(d["wins"] / d["closed"] * 100) if d["closed"] else 0)
        row["best"] = round(max(row["A"]["value"], row["B"]["value"], row["C"]["value"],
                                row["D"]["value"], row["E"]["value"]), 2)
        traders.append(row)
    traders.sort(key=lambda r: -r["best"])

    models = {}
    for mk, mn in MODELS:
        models[mn] = dict(started=round(BANKROLL_PER_MODEL * len(WALLETS), 2),
                          value=round(sum(r[mn]["value"] for r in traders), 2),
                          realized=round(sum(r[mn]["realized"] for r in traders), 2),
                          unrealized=round(sum(r[mn]["unrealized"] for r in traders), 2),
                          open=sum(r[mn]["open"] for r in traders),
                          closed=sum(r[mn]["closed"] for r in traders))
    decs = c.execute("SELECT ts,wallet,title,entry,current,gain,decision,why FROM decisions "
                     "ORDER BY id DESC LIMIT 150").fetchall()
    decisions = [dict(ts=d[0], wallet=d[1], title=(d[2] or "")[:80], entry=d[3],
                      current=d[4], gain=round((d[5] or 0) * 100, 1), decision=d[6], why=d[7]) for d in decs]

    # Health / meta for dashboard (lag, open risk, rough activity)
    lags = []
    for ct, tt in c.execute("SELECT copied_ts, their_ts FROM copies WHERE their_ts IS NOT NULL"):
        try:
            lags.append(datetime.fromisoformat(ct).timestamp() - int(tt))
        except Exception:
            pass
    lag_p50 = round(lags[len(lags)//2]/60, 1) if lags else None

    total_at_risk = sum(r["at_risk"] for r in [agg.get((mk, t), {"at_risk":0}) for t in WALLETS for mk,_ in MODELS])
    # crude recent claude activity count (last 24h decisions as proxy)
    try:
        recent_claude = c.execute(
            "SELECT COUNT(*) FROM decisions WHERE ts >= datetime('now','-24 hours')"
        ).fetchone()[0]
    except Exception:
        recent_claude = 0

    meta = {
        "lag_p50_min": lag_p50,
        "wallets": len(WALLETS),
        "open_risk": round(total_at_risk, 2),
        "total_unrealized": round(sum(m["unrealized"] for m in models.values()), 2),
        "claude_calls_24h": recent_claude,
        "note": "E entry filter: RSI>78 or 30m absolute price increase >$0.10"
    }

    fs = _filter_summary(c)
    # Recent accepted copies, with enough detail to audit the simulated execution.
    recent_accepted = [
        dict(zip(("ts", "wallet", "title", "wallet_price", "market_price", "entry_price",
                  "slippage_amount", "fee", "stake", "shares", "detection_lag",
                  "exec_ts", "price_source"), r))
        for r in c.execute("""SELECT ts, wallet, title, wallet_price, market_price,
            entry_price, slippage_amount, fee, stake, shares, detection_lag, exec_ts,
            price_source FROM entry_filter_log WHERE accepted=1
            ORDER BY id DESC LIMIT 25""").fetchall()]
    recent_rejected = [
        dict(zip(("ts", "wallet", "title", "reason", "market_price", "entry_price"), r))
        for r in c.execute("""SELECT ts, wallet, title, reason, market_price, entry_price
            FROM entry_filter_log WHERE accepted=0 ORDER BY id DESC LIMIT 25""").fetchall()]
    filters = {
        "max_entry_price": MAX_ENTRY_PRICE, "min_entry_price": MIN_ENTRY_PRICE,
        "copy_delay_seconds": COPY_DELAY_SECONDS, "slippage_bps": SLIPPAGE_BPS,
        "paper_fee_bps": PAPER_FEE_BPS,
        "max_slippage": MAX_SLIPPAGE, "min_liquidity": MIN_LIQUIDITY,
        "max_detection_lag_seconds": MAX_DETECTION_LAG_SECONDS,
        "reject_if_slippage_unknown": REJECT_IF_SLIPPAGE_UNKNOWN,
        "reject_if_liquidity_unknown": REJECT_IF_LIQUIDITY_UNKNOWN,
        "candidates": fs["total"], "accepted": fs["accepted"], "skipped": fs["skipped"],
        "acceptance_rate": fs["acceptance_rate"], "reasons": fs["reasons"],
        "avg_detection_lag_sec": fs["avg_detection_lag_sec"],
        "median_detection_lag_sec": fs["median_detection_lag_sec"],
        "avg_slippage_amount": fs["avg_slippage_amount"],
        "total_paper_fees": fs["total_paper_fees"],
        "recent_accepted": recent_accepted, "recent_rejected": recent_rejected,
    }

    # Chart-filter audit summary for docs/data.json
    cfl = c.execute("""SELECT COUNT(*), COALESCE(SUM(decision),0),
        COALESCE(SUM(CASE WHEN price_data_ok=0 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN volume_data_ok=1 THEN 1 ELSE 0 END),0)
        FROM chart_filter_log""").fetchone()
    chart_filter_summary = {
        "total_evaluations": cfl[0] or 0,
        "accepted": cfl[1] or 0,
        "rejected": (cfl[0] or 0) - (cfl[1] or 0),
        "rejected_no_price_data": cfl[2] or 0,
        "evaluations_with_volume": cfl[3] or 0,
        "fail_closed_policy": "price_required_volume_optional",
    }
    recent_chart = [
        dict(zip(("ts", "asset", "decision", "reason",
                  "price_data_ok", "volume_data_ok",
                  "rsi", "mom_30m", "trend_ph"), r))
        for r in c.execute("""SELECT ts, asset, decision, reason,
            price_data_ok, volume_data_ok, rsi, mom_30m, trend_ph
            FROM chart_filter_log ORDER BY id DESC LIMIT 25""").fetchall()
    ]
    chart_filter_summary["recent"] = recent_chart

    os.makedirs("docs", exist_ok=True)
    json.dump(dict(updated=now_iso(), models=models, traders=traders, decisions=decisions,
                   meta=meta, filters=filters, chart_filter=chart_filter_summary),
              open("docs/data.json", "w"))
    print("export: traders=%d, model totals:" % len(traders),
          {mn: models[mn]["value"] for _, mn in MODELS})

    # SDD v2: dump quality matrix (category specialization scores) for dashboard intelligence
    quality = []
    for trader in list(WALLETS.keys()):
        row = {"name": trader}
        for cat in ["sports", "crypto_5m", "politics", "crypto_h", "other"]:
            q = _get_trader_cat_quality(c, trader, cat)
            row[cat] = q
        quality.append(row)
    with open("docs/quality.json", "w") as f:
        json.dump({"updated": now_iso(), "quality": quality}, f)
    print("export: also wrote docs/quality.json (trader x category edge matrix)")


def _model_block(label, rows, note):
    # rows: (status, stake, pnl)
    closed = [r for r in rows if r[0] in CLOSED]
    open_ = [r for r in rows if r[0] == "open"]
    wins = [r for r in closed if r[2] and r[2] > 0]
    real = sum(r[2] or 0 for r in closed)
    unreal = sum(r[2] or 0 for r in open_)
    staked = sum(r[1] for r in closed)
    print(f"  -- {label} --   {note}")
    print(f"     copies: {len(rows)}   closed: {len(closed)}   open: {len(open_)}")
    if closed:
        wr = len(wins) / len(closed) * 100
        print(f"     win rate:   {wr:.0f}%  ({len(wins)}/{len(closed)})")
        print(f"     realized:   ${real:+.2f}" + (f"   ROI {real/staked*100:+.1f}%" if staked else ""))
    print(f"     open (mark-to-market): ${unreal:+.2f}")


def _print_filter_summary(fs):
    print("\n  -- ENTRY / EXECUTION SIMULATION (paper only) --")
    print(f"     max entry price: {MAX_ENTRY_PRICE}    min entry price: {MIN_ENTRY_PRICE}")
    print(f"     copy delay: {COPY_DELAY_SECONDS}s (simulated, not a real sleep)    "
          f"slippage: {SLIPPAGE_BPS} bps    paper fee: {PAPER_FEE_BPS} bps")
    print(f"     book gates: MAX_SLIPPAGE={MAX_SLIPPAGE}  MIN_LIQUIDITY={MIN_LIQUIDITY}  "
          f"MAX_DETECTION_LAG_SECONDS={MAX_DETECTION_LAG_SECONDS}")
    rate = "n/a" if fs["acceptance_rate"] is None else f"{fs['acceptance_rate'] * 100:.1f}%"
    print("  FILTER_SUMMARY")
    print(f"     candidates={fs['total']}  accepted={fs['accepted']}  "
          f"rejected={fs['skipped']}  acceptance_rate={rate}")
    print(f"     detection lag: median {fs['median_detection_lag_sec']}s / "
          f"avg {fs['avg_detection_lag_sec']}s")
    print(f"     avg simulated slippage: {fs['avg_slippage_amount']}    "
          f"total simulated fees: ${fs['total_paper_fees']}")
    for reason in SKIP_REASONS:
        print(f"     {reason}={fs['reasons'][reason]}")


def report():
    c = db()
    rows = c.execute("SELECT COALESCE(model,'hold'),status,stake,pnl FROM copies").fetchall()
    print("\n" + "=" * 64)
    print("  POLYMARKET COPYBOT — two parallel models ($10/copy)")
    print("=" * 64)
    if not rows:
        print("  no copies yet — run tick.\n")
        fs = _filter_summary(c)
        if fs["total"]:   # trades were evaluated even though none were copied (e.g. all rejected)
            _print_filter_summary(fs)
        return
    hold = [(r[1], r[2], r[3]) for r in rows if r[0] == "hold"]
    exit_ = [(r[1], r[2], r[3]) for r in rows if r[0] == "exit"]
    cdec = [(r[1], r[2], r[3]) for r in rows if r[0] == "cdecide"]
    _model_block("MODEL A: copy & HOLD to resolution", hold, "(slow — markets resolve weeks out)")
    print()
    _model_block("MODEL B: copy & SELL at +%d%% / %dh cap" % (TAKE_PROFIT * 100, MAX_HOLD_HOURS),
                 exit_, "(fast — closes in 1-3 days)")
    print()
    _model_block("MODEL C: Claude decides HOLD/SELL", cdec, "(AI exits — see dashboard for whys)")
    # detection lag
    lags = []
    for ct, tt in c.execute("SELECT copied_ts, their_ts FROM copies WHERE their_ts IS NOT NULL"):
        try:
            lags.append(datetime.fromisoformat(ct).timestamp() - int(tt))
        except Exception:
            pass
    if lags:
        lags.sort()
        print(f"\n  detection lag: median {lags[len(lags)//2]/60:.1f} min over {len(lags)} trades")

    _print_filter_summary(_filter_summary(c))
    print("=" * 64 + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"tick": tick, "resolve": resolve, "report": report,
     "claude_decide": claude_decide, "export": export}.get(cmd, report)()
