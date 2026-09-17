#!/usr/bin/env python3
"""
polymarket paper trader — the "fade the favorite" reversal strategy, money-free.

Translates the AskGina tweet strategy into deterministic code and PAPER-TRADES it:
no wallet, no deposits, no real orders. It just records what the strategy WOULD do
against live Polymarket prices, then scores it against real outcomes.

Strategy (verbatim from the tweet, encoded):
  - Check hourly BTC up/down first, then ETH.
  - Valid signal: one outcome (Up or Down) is between 88% and 98%.
  - If both valid -> take BTC. If neither -> stop.
  - Slippage check: simulate the buy on the LOWER-prob (underdog) side. Skip if > 4%.
  - Size by how extreme the leader is: 88-91% -> $10, 91-94% -> $17, 94-98% -> $25.
  - Buy the OTHER (underdog) side. Paper-fill at the real ask off the orderbook.
  - Budget guard: stop if cumulative paper losses exceed $500.

Commands:
  python3 paper_trader.py tick      # run the strategy once (place 1 paper bet or log a skip)
  python3 paper_trader.py resolve   # settle any open bets whose hour has ended
  python3 paper_trader.py report    # win-rate, staked, P&L, EV per $1
  python3 paper_trader.py run       # foreground daemon: resolve+tick at :46 and :53 forever
"""

import json
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- strategy params (edit here to fork the strategy) ----------------------
SIGNAL_LOW = 0.88
SIGNAL_HIGH = 0.98
MAX_SLIPPAGE = 0.04          # 4%
RUN_MINUTES = [46, 53]       # minutes past the hour to act
COINS = ["bitcoin", "ethereum"]   # priority order: BTC first, then ETH
BUDGET_MAX_LOSS = 500.0      # stop trading once cumulative losses exceed this

def size_for(leading_price: float) -> float:
    if 0.88 <= leading_price < 0.91:
        return 10.0
    if 0.91 <= leading_price < 0.94:
        return 17.0
    if 0.94 <= leading_price <= 0.98:
        return 25.0
    return 0.0

# ---- infra -----------------------------------------------------------------
DB = Path(__file__).parent / "trades.db"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "paper-trader/1.0", "Accept": "application/json"}


def get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, coin TEXT, market_id TEXT, slug TEXT, question TEXT,
        leading_outcome TEXT, leading_price REAL,
        underdog_outcome TEXT, underdog_token TEXT,
        entry_price REAL, slippage REAL, gate_pass INTEGER, stake REAL, shares REAL,
        end_date TEXT, status TEXT, payout REAL, pnl REAL, resolve_ts TEXT,
        source TEXT, decision_min INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ticks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, detail TEXT)""")
    return c


def log_tick(c, action, detail):
    c.execute("INSERT INTO ticks(ts,action,detail) VALUES(?,?,?)",
              (now_utc().isoformat(), action, detail))
    c.commit()
    print(f"[{action}] {detail}")


# ---- market discovery ------------------------------------------------------
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
ET_OFFSET = timedelta(hours=-4)   # EDT (valid for the June test window; ET = UTC-4)


def current_hour_slug(coin: str):
    """Build the slug for the hourly up/down market covering the current ET hour,
    e.g. 'bitcoin-up-or-down-june-7-2026-6pm-et', plus its UTC resolution time
    (the next top of the hour). Polymarket's gamma `endDate` is a ~1-day-out
    placeholder, so we compute the real expiry ourselves."""
    et = now_utc() + ET_OFFSET
    h = et.hour
    ampm = "am" if h < 12 else "pm"
    hr12 = h % 12 or 12
    slug = f"{coin}-up-or-down-{MONTHS[et.month-1]}-{et.day}-{et.year}-{hr12}{ampm}-et"
    expiry = (now_utc().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return slug, expiry


def find_hourly_market(coin: str):
    """Return the live hourly up/down market dict for `coin`, or None.

    Fetched deterministically by the current-hour slug (robust against the API's
    date-ordering/pagination, which hides the now-window markets)."""
    slug, expiry = current_hour_slug(coin)
    try:
        r = get(f"{GAMMA}/markets?slug={slug}")
    except Exception:
        return None
    if not r:
        return None
    m = r[0]
    if m.get("closed") or not m.get("active"):
        return None
    return {"slug": slug, "market_id": str(m.get("id")),
            "question": m.get("question", ""),
            "outcomes": json.loads(m["outcomes"]),
            "prices": [float(p) for p in json.loads(m["outcomePrices"])],
            "tokens": json.loads(m["clobTokenIds"]),
            "end_date": expiry.isoformat()}


def simulate_buy(token_id: str, stake: float):
    """Walk the orderbook asks to fill `stake` USD. Returns (avg_price, shares, slippage)
    or None if the book can't fill it."""
    try:
        book = get(f"{CLOB}/book?token_id={token_id}")
    except Exception:
        return None
    asks = sorted(({"p": float(a["price"]), "s": float(a["size"])} for a in book.get("asks", [])),
                  key=lambda x: x["p"])
    if not asks:
        return None
    best = asks[0]["p"]
    remaining, shares = stake, 0.0
    for lvl in asks:
        lvl_cost = lvl["p"] * lvl["s"]
        take = min(remaining, lvl_cost)
        shares += take / lvl["p"]
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or shares <= 0:
        return None  # book too thin to fill -> treat as unfillable / huge slippage
    avg = stake / shares
    slippage = (avg - best) / best
    return avg, shares, slippage


# ---- core commands ---------------------------------------------------------
def cumulative_pnl(c):
    row = c.execute("SELECT COALESCE(SUM(pnl),0) FROM bets WHERE status IN('won','lost')").fetchone()
    return row[0] or 0.0


def tick():
    c = db()
    # budget guard
    pnl = cumulative_pnl(c)
    if pnl < -BUDGET_MAX_LOSS:
        log_tick(c, "SKIP", f"Budget exceeded (cumulative P&L ${pnl:.2f} < -${BUDGET_MAX_LOSS})")
        return

    chosen = None
    for coin in COINS:
        mk = find_hourly_market(coin)
        if not mk:
            continue
        # find the leader (highest price) and check the 88-98% window
        lead_idx = max(range(len(mk["prices"])), key=lambda i: mk["prices"][i])
        lead_price = mk["prices"][lead_idx]
        if SIGNAL_LOW <= lead_price <= SIGNAL_HIGH:
            # dedupe: never bet the same market twice (hedge cron slots run >1x/window)
            already = c.execute("SELECT 1 FROM bets WHERE market_id=?",
                                (mk["market_id"],)).fetchone()
            if already:
                log_tick(c, "INFO", f"{coin}: already have a bet on {mk['slug']}, skipping")
                continue
            chosen = (coin, mk, lead_idx, lead_price)
            break  # BTC first; first valid wins
        else:
            log_tick(c, "INFO", f"{coin}: leader {mk['outcomes'][lead_idx]} @ "
                                f"{lead_price:.3f} outside [{SIGNAL_LOW},{SIGNAL_HIGH}]")

    if not chosen:
        log_tick(c, "SKIP", "No valid signal on BTC or ETH this tick")
        return

    coin, mk, lead_idx, lead_price = chosen
    under_idx = 1 - lead_idx
    under_token = mk["tokens"][under_idx]
    under_outcome = mk["outcomes"][under_idx]
    stake = size_for(lead_price)

    sim = simulate_buy(under_token, stake)
    if sim is None:
        log_tick(c, "SKIP", f"{coin}: orderbook can't fill ${stake} at any price on {under_outcome}")
        return
    entry, shares, slippage = sim
    gate_pass = 1 if slippage <= MAX_SLIPPAGE else 0
    # Shadow mode: record EVERY signal at its realistic walked-book fill, but flag
    # whether the strategy's own 4% slippage rule would actually allow it. This lets
    # the week's report show both "strategy-as-written" (gate-passing trades) AND the
    # raw fade edge (all signals) — otherwise the slippage gate blocks ~everything and
    # we learn nothing.
    c.execute("""INSERT INTO bets(ts,coin,market_id,slug,question,leading_outcome,leading_price,
        underdog_outcome,underdog_token,entry_price,slippage,gate_pass,stake,shares,end_date,status,payout,pnl)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_utc().isoformat(), coin, mk["market_id"], mk["slug"], mk["question"],
         mk["outcomes"][lead_idx], lead_price, under_outcome, under_token,
         entry, slippage, gate_pass, stake, shares, mk["end_date"], "open", None, None))
    c.commit()
    flag = "" if gate_pass else "  [GATE-BLOCKED: slippage > 4%]"
    log_tick(c, "BET", f"{coin} {mk['question']} | leader {mk['outcomes'][lead_idx]} @ {lead_price:.3f} "
                       f"-> ${stake:.0f} on {under_outcome} @ {entry:.3f} "
                       f"({shares:.1f} shares, slip {slippage*100:.1f}%){flag}")


def resolve():
    c = db()
    open_bets = c.execute("SELECT id,market_id,slug,underdog_outcome,shares,stake,end_date "
                          "FROM bets WHERE status='open'").fetchall()
    for bid, mid, slug, under_outcome, shares, stake, end_date in open_bets:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end > now_utc():
            continue  # hour not over yet
        try:
            m = get(f"{GAMMA}/markets/{mid}")
        except Exception as e:
            print(f"resolve fetch failed for bet {bid}: {e}")
            continue
        if not m.get("closed"):
            continue  # ended but not yet settled on-chain; try again next cycle
        outcomes = json.loads(m["outcomes"])
        prices = [float(p) for p in json.loads(m["outcomePrices"])]
        # resolved market: winning outcome price -> 1.0
        win_idx = max(range(len(prices)), key=lambda i: prices[i])
        win_outcome = outcomes[win_idx]
        if abs(prices[win_idx] - 0.5) < 0.1:   # ~50/50 -> void/refund
            status, payout = "void", stake
        elif win_outcome == under_outcome:
            status, payout = "won", shares * 1.0
        else:
            status, payout = "lost", 0.0
        pnl = payout - stake
        c.execute("UPDATE bets SET status=?,payout=?,pnl=?,resolve_ts=? WHERE id=?",
                  (status, payout, pnl, now_utc().isoformat(), bid))
        c.commit()
        print(f"[RESOLVE] bet {bid} {slug}: winner={win_outcome} -> {status.upper()} "
              f"(P&L ${pnl:+.2f})")


def _stat_block(label, settled):
    wins = [r for r in settled if r[0] == "won"]
    staked = sum(r[1] for r in settled)
    pnl = sum(r[3] for r in settled)
    print(f"  -- {label} --")
    if not settled:
        print("     no settled bets in this bucket yet")
        return
    wr = len(wins) / len(settled) * 100
    print(f"     settled bets:   {len(settled)}   wins: {len(wins)}  ({wr:.0f}% win rate)")
    print(f"     staked:         ${staked:.2f}")
    print(f"     P&L:            ${pnl:+.2f}")
    if staked:
        print(f"     EV per $1:      ${pnl/staked:+.3f}   ROI: {pnl/staked*100:+.0f}%")
    print(f"     verdict:        {'+EV (edge?)' if pnl > 0 else '-EV (losing, as expected)'}")


def backfill(hours=72):
    """Reconstruct the strategy over the last `hours` completed hourly markets using
    Polymarket's minute-by-minute price history. For each market we read the price at
    :46 and :53 past the hour (the strategy's check times), take the first moment the
    leader is in [88%,98%], and settle it against the market's KNOWN outcome.

    History is permanent, so this is idempotent and self-healing: re-running re-scans
    the window and only inserts markets we haven't recorded yet. A throttled/missed
    scheduled run therefore loses nothing — the next run backfills the gap.

    Entry uses the historical mid price (no order book in history), i.e. this measures
    the RAW fade edge. Real execution suffers the penny-underdog slippage we observed
    live, so these are recorded gate_pass=0 (they'd be blocked by the 4% rule)."""
    c = db()
    floor_hr = now_utc().replace(minute=0, second=0, microsecond=0)
    added = 0
    for h in range(1, hours + 1):
        wstart = floor_hr - timedelta(hours=h)
        et = wstart + ET_OFFSET
        ampm = "am" if et.hour < 12 else "pm"
        hr12 = et.hour % 12 or 12
        for coin in COINS:
            slug = f"{coin}-up-or-down-{MONTHS[et.month-1]}-{et.day}-{et.year}-{hr12}{ampm}-et"
            try:
                r = get(f"{GAMMA}/markets?slug={slug}&closed=true")
            except Exception:
                continue
            if not r:
                continue
            m = r[0]
            mid = str(m.get("id"))
            if c.execute("SELECT 1 FROM bets WHERE market_id=?", (mid,)).fetchone():
                continue  # already recorded
            try:
                outcomes = json.loads(m["outcomes"])
                finals = [float(p) for p in json.loads(m["outcomePrices"])]
                tokens = json.loads(m["clobTokenIds"])
            except Exception:
                continue
            win_idx = max(range(len(finals)), key=lambda i: finals[i])
            if finals[win_idx] < 0.9:        # not a clean 1/0 settlement -> skip (void/50-50)
                continue
            ws = int(wstart.timestamp())
            try:
                hist = get(f"{CLOB}/prices-history?market={tokens[0]}"
                           f"&startTs={ws-180}&endTs={ws+3600+180}&fidelity=1").get("history", [])
            except Exception:
                continue
            if not hist:
                continue

            def price_at(ts):
                return min(hist, key=lambda pt: abs(pt["t"] - ts))["p"]

            for dmin in (46, 53):
                p_up = price_at(ws + dmin * 60)
                lead_idx = 0 if p_up >= 0.5 else 1
                lead_price = max(p_up, 1 - p_up)
                if not (SIGNAL_LOW <= lead_price <= SIGNAL_HIGH):
                    continue
                under_idx = 1 - lead_idx
                entry = round(1 - lead_price, 4) or 0.01
                stake = size_for(lead_price)
                shares = stake / entry
                won = (win_idx == under_idx)
                payout = shares if won else 0.0
                pnl = payout - stake
                c.execute("""INSERT INTO bets(ts,coin,market_id,slug,question,leading_outcome,
                    leading_price,underdog_outcome,underdog_token,entry_price,slippage,gate_pass,
                    stake,shares,end_date,status,payout,pnl,resolve_ts,source,decision_min)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (wstart.isoformat(), coin, mid, slug, m.get("question", ""),
                     outcomes[lead_idx], lead_price, outcomes[under_idx], tokens[under_idx],
                     entry, None, 0, stake, shares, (wstart + timedelta(hours=1)).isoformat(),
                     "won" if won else "lost", payout, pnl, now_utc().isoformat(),
                     "backfill", dmin))
                c.commit()
                added += 1
                break  # one bet per market (first in-band check time)
    print(f"backfill: scanned {hours}h, added {added} new settled signals")


STRATEGIES = [
    # (name, side, low, high)  side: 'fade' buys the underdog, 'ride' buys the leader
    ("fade favorite 88-98%",   "fade", 0.88, 0.98),
    ("fade 80-88%",            "fade", 0.80, 0.88),
    ("fade extreme 98-99.9%",  "fade", 0.98, 0.999),
    ("ride favorite 88-98%",   "ride", 0.88, 0.98),
    ("ride 70-88%",            "ride", 0.70, 0.88),
    ("ride 55-70%",            "ride", 0.55, 0.70),
    ("ride extreme 98-99.9%",  "ride", 0.98, 0.999),
]


def slip_pct(price):
    """Realistic order-book slippage for a ~$10 market buy, calibrated to live book
    samples observed on these markets (underdog @0.03-0.05 filled ~48-57% worse than
    best ask on $25; cheaper = thinner = worse). The leader side (0.55-0.99) is liquid."""
    if price >= 0.30: return 0.02
    if price >= 0.15: return 0.06
    if price >= 0.08: return 0.15
    if price >= 0.04: return 0.30
    if price >= 0.02: return 0.45
    return 0.70


def compare_strategies(hours=288):
    """Backtest every strategy over the last `hours` of real hourly markets, NET of a
    realistic slippage model, and SPLIT into in-sample (older half) vs out-of-sample
    (recent half). A real edge survives out-of-sample; an overfit/lucky one collapses.
    Flat $10/signal."""
    floor_hr = now_utc().replace(minute=0, second=0, microsecond=0)
    STAKE = 10.0
    split = hours // 2  # signals older than this = in-sample, newer = out-of-sample
    blank = lambda: {"n": 0, "wins": 0, "staked": 0.0, "pnl": 0.0}
    tally = {s[0]: {"IS": blank(), "OOS": blank()} for s in STRATEGIES}
    markets = 0
    for h in range(1, hours + 1):
        wstart = floor_hr - timedelta(hours=h)
        et = wstart + ET_OFFSET
        ampm = "am" if et.hour < 12 else "pm"
        hr12 = et.hour % 12 or 12
        bucket = "IS" if h > split else "OOS"
        for coin in COINS:
            slug = f"{coin}-up-or-down-{MONTHS[et.month-1]}-{et.day}-{et.year}-{hr12}{ampm}-et"
            try:
                r = get(f"{GAMMA}/markets?slug={slug}&closed=true")
            except Exception:
                continue
            if not r:
                continue
            m = r[0]
            try:
                finals = [float(p) for p in json.loads(m["outcomePrices"])]
                tokens = json.loads(m["clobTokenIds"])
            except Exception:
                continue
            win_idx = max(range(len(finals)), key=lambda i: finals[i])
            if finals[win_idx] < 0.9:
                continue
            ws = int(wstart.timestamp())
            try:
                hist = get(f"{CLOB}/prices-history?market={tokens[0]}"
                           f"&startTs={ws-180}&endTs={ws+3780}&fidelity=1").get("history", [])
            except Exception:
                continue
            if not hist:
                continue
            markets += 1

            def price_at(ts):
                return min(hist, key=lambda pt: abs(pt["t"] - ts))["p"]

            for name, side, lo, hi in STRATEGIES:
                for dmin in (46, 53):
                    p_up = price_at(ws + dmin * 60)
                    lead_idx = 0 if p_up >= 0.5 else 1
                    lead_price = max(p_up, 1 - p_up)
                    if not (lo <= lead_price <= hi):
                        continue
                    bet_idx = lead_idx if side == "ride" else 1 - lead_idx
                    mid = lead_price if side == "ride" else max(round(1 - lead_price, 4), 0.01)
                    entry = min(0.99, mid * (1 + slip_pct(mid)))   # apply realistic fill
                    shares = STAKE / entry
                    won = (win_idx == bet_idx)
                    t = tally[name][bucket]
                    t["n"] += 1
                    t["wins"] += 1 if won else 0
                    t["staked"] += STAKE
                    t["pnl"] += (shares - STAKE) if won else -STAKE
                    break

    def roi(b):
        return (b["pnl"] / b["staked"] * 100) if b["staked"] else None
    print("\n" + "=" * 78)
    print(f"  STRATEGY BAKE-OFF (net of slippage) — {markets} real markets, {hours//24}d, $10/signal")
    print(f"  IS = in-sample (older {split//24}d)   OOS = out-of-sample (recent {split//24}d)")
    print("=" * 78)
    print(f"  {'strategy':<24}{'IS n':>6}{'IS ROI':>9}{'OOS n':>7}{'OOS ROI':>9}   verdict")
    print("  " + "-" * 74)
    for name, *_ in sorted(STRATEGIES, key=lambda s: -(roi(tally[s[0]]['OOS']) or -999)):
        IS, OOS = tally[name]["IS"], tally[name]["OOS"]
        ri, ro = roi(IS), roi(OOS)
        ris = f"{ri:+.0f}%" if ri is not None else "—"
        ros = f"{ro:+.0f}%" if ro is not None else "—"
        if ri is not None and ro is not None:
            v = "HOLDS" if (ri > 0 and ro > 0) else "FELL APART" if ri > 0 else "loses both"
        else:
            v = "too few signals"
        print(f"  {name:<24}{IS['n']:>6}{ris:>9}{OOS['n']:>7}{ros:>9}   {v}")
    print("=" * 78)
    print("  HOLDS = profitable in BOTH halves (edge may be real). FELL APART = profitable")
    print("  in-sample but lost out-of-sample (overfit/luck). Slippage model in slip_pct().\n")


def report():
    c = db()
    rows = c.execute("SELECT status,stake,payout,pnl,gate_pass FROM bets").fetchall()
    settled = [r for r in rows if r[0] in ("won", "lost")]
    opens = [r for r in rows if r[0] == "open"]
    voids = [r for r in rows if r[0] == "void"]
    gated = [r for r in settled if r[4] == 1]   # passed the 4% slippage rule
    blocked = [r for r in settled if r[4] == 0]
    print("\n" + "=" * 60)
    print("  POLYMARKET PAPER TRADER — fade-the-favorite report")
    print("=" * 60)
    print(f"  signals taken: {len(rows)}   settled: {len(settled)}   "
          f"open: {len(opens)}   void: {len(voids)}")
    print(f"  of settled: {len(gated)} executable under 4% slippage rule, "
          f"{len(blocked)} blocked by it")
    print()
    # the strategy AS WRITTEN only trades the gate-passers
    _stat_block("STRATEGY AS WRITTEN (only trades passing the 4% slippage gate)", gated)
    print()
    # the raw edge: would fading favorites win if you ignored slippage entirely?
    _stat_block("RAW FADE EDGE (all signals, slippage ignored)", settled)
    print("=" * 60 + "\n")


def run():
    print("paper trader daemon up. acting at minutes", RUN_MINUTES, "past each hour. ctrl-c to stop.")
    last_min = -1
    while True:
        n = now_utc()
        if n.minute in RUN_MINUTES and n.minute != last_min:
            last_min = n.minute
            print(f"\n--- cycle @ {n.isoformat()} ---")
            try:
                resolve()
                tick()
            except Exception as e:
                print("cycle error:", e)
        if n.minute not in RUN_MINUTES:
            last_min = -1
        time.sleep(20)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "backfill":
        hrs = int(sys.argv[2]) if len(sys.argv) > 2 else 72
        backfill(hrs)
    elif cmd == "strategies":
        hrs = int(sys.argv[2]) if len(sys.argv) > 2 else 72
        compare_strategies(hrs)
    else:
        {"tick": tick, "resolve": resolve, "report": report, "run": run}.get(cmd, report)()
