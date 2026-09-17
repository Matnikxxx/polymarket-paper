#!/usr/bin/env python3
"""
Polymarket copytrade research tool — find skilled wallets and test whether MIRRORING
them actually works once you account for realistic copy-lag. PAPER analysis only; it
places no trades and needs no key. All data from Polymarket's public APIs.

The core question a copytrader must answer: a top trader is up millions, but by the time
you SEE their trade (on-chain confirmation + your reaction) the price has moved. Does
their edge survive you buying N minutes later at a worse price? This tool measures that.

Commands:
  python3 copytrade.py leaderboard [window]      # top traders (window: 1d|7d|30d|all)
  python3 copytrade.py profile <wallet>          # skill snapshot for one wallet
  python3 copytrade.py copytest <wallet> [lag_min]  # THE test: lag-adjusted mirror ROI
"""
import json
import sys
import urllib.request

LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def leaderboard(window="30d", limit=20):
    rows = get(f"{LB}/profit?window={window}&limit={limit}")
    print(f"\n  POLYMARKET LEADERBOARD — top {len(rows)} by profit ({window})")
    print("  " + "-" * 56)
    print(f"  {'#':>2}  {'trader':<20}{'profit':>15}")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}  {(r.get('name') or r.get('pseudonym') or '?')[:20]:<20}{('$' + format(r.get('amount', 0), ',.0f')):>15}")
    print(f"  (wallets: {', '.join(r['proxyWallet'] for r in rows[:3])} ...)\n")
    return rows


def wallet_trades(addr, max_trades=600):
    out, off = [], 0
    while len(out) < max_trades:
        t = get(f"{DATA}/trades?user={addr}&limit=500&offset={off}")
        if not t:
            break
        out += t
        off += 500
        if len(t) < 500:
            break
    return out


def profile(addr):
    val = get(f"{DATA}/value?user={addr}")
    cur_value = val[0].get("value") if val else 0
    trades = wallet_trades(addr, 1000)
    buys = [t for t in trades if t.get("side") == "BUY"]
    markets = {t.get("conditionId") for t in trades}
    avg_size = sum(float(t["size"]) * float(t["price"]) for t in trades) / max(len(trades), 1)
    print(f"\n  PROFILE {addr}")
    print("  " + "-" * 56)
    print(f"  current portfolio value : ${cur_value:,.2f}")
    print(f"  trades (last {len(trades)})      : {len(trades)}  ({len(buys)} buys)")
    print(f"  distinct markets        : {len(markets)}  <- diversification")
    print(f"  avg trade size          : ${avg_size:,.2f}")
    if avg_size > 500:
        print(f"  NOTE: avg ${avg_size:,.0f}/trade — a $10 account cannot mirror this meaningfully.")
    print()


def price_at(hist, ts):
    return min(hist, key=lambda p: abs(p["t"] - ts))["p"]


def copytest(addr, lag_min=10, max_trades=80):
    """For each BUY the trader made, simulate YOU mirroring it `lag_min` minutes later
    at the then-current price, held to resolution. Compares the trader's raw edge to
    your lag-adjusted edge. Resolution is read from the token's own final price (~1=won,
    ~0=lost)."""
    lag_min = int(lag_min)
    trades = wallet_trades(addr, max_trades)
    buys = [t for t in trades if t.get("side") == "BUY"]
    their_roi, your_roi, your_wins = [], [], 0
    resolved, skipped = 0, 0
    for t in buys:
        asset = t.get("asset")
        ts = int(t["timestamp"])
        their_px = float(t["price"])
        if not asset or their_px <= 0 or their_px >= 1:
            skipped += 1
            continue
        import time as _t
        now = int(_t.time())
        # settlement: coarse window from trade time to now (fast even for old trades);
        # a resolved token's price is pinned at ~0 or ~1.
        try:
            tail = get(f"{CLOB}/prices-history?market={asset}"
                       f"&startTs={ts}&endTs={now}&fidelity=60").get("history", [])
        except Exception:
            tail = []
        if not tail:
            skipped += 1
            continue
        settle = tail[-1]["p"]
        if not (settle < 0.05 or settle > 0.95):   # market not yet resolved
            skipped += 1
            continue
        won = 1.0 if settle > 0.5 else 0.0
        # entry at lag: fine-grained small window around ts+lag
        try:
            win = get(f"{CLOB}/prices-history?market={asset}"
                      f"&startTs={ts-120}&endTs={ts+lag_min*60+600}&fidelity=1").get("history", [])
        except Exception:
            win = []
        if not win:
            skipped += 1
            continue
        your_px = price_at(win, ts + lag_min * 60)
        if your_px <= 0 or your_px >= 1:
            skipped += 1
            continue
        resolved += 1
        their_roi.append(won / their_px - 1)
        your_roi.append(won / your_px - 1)
        your_wins += 1 if won else 0

    print(f"\n  COPYTEST {addr}  (lag {lag_min} min, buy-and-hold-to-resolution)")
    print("  " + "-" * 60)
    if resolved == 0:
        print(f"  no resolved buys to test (skipped {skipped} — open markets / no history)\n")
        return
    tmean = sum(their_roi) / resolved
    ymean = sum(your_roi) / resolved
    print(f"  resolved buys tested    : {resolved}   (skipped {skipped})")
    print(f"  your win rate           : {your_wins/resolved*100:.0f}%")
    print(f"  THEIR avg ROI / trade   : {tmean*100:+.1f}%   (their fill price)")
    print(f"  YOUR avg ROI / trade    : {ymean*100:+.1f}%   (mirrored {lag_min}m later)")
    print(f"  edge lost to lag        : {(tmean-ymean)*100:.1f} pts")
    verdict = ("COPYABLE — edge survives lag" if ymean > 0.02 else
               "MARGINAL — edge mostly eaten by lag" if ymean > 0 else
               "NOT COPYABLE — lag kills the edge (you lose)")
    print(f"  verdict                 : {verdict}")
    print("  note: assumes hold-to-resolution (ignores their early exits); no spread/fee added\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "leaderboard"
    if cmd == "leaderboard":
        leaderboard(sys.argv[2] if len(sys.argv) > 2 else "30d")
    elif cmd == "profile":
        profile(sys.argv[2])
    elif cmd == "copytest":
        copytest(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 10)
    else:
        print(__doc__)
