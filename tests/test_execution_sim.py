"""
Tests for the paper entry/execution simulation in copybot.py (PAPER ONLY).

Run with:
    python3 -m pytest -q                      (preferred, if pytest is installed)
    python3 -m unittest discover -s tests -v   (stdlib fallback, no extra deps)

No network and no real DB: order books are plain dicts in the shape the CLOB /book
endpoint returns, and each DB-touching test gets a throwaway temp sqlite file.
Nothing here sleeps — the copy delay is timestamp arithmetic, which is the point.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import copybot  # noqa: E402


def book(asks):
    """Build a minimal CLOB-book-shaped dict from a list of (price, size) tuples."""
    return {"asks": [{"price": p, "size": s} for p, s in asks]}


DEEP = book([(0.50, 10000.0)])  # deep book: passes the book-slippage/liquidity gates


class ConfigTests(unittest.TestCase):
    """The five new settings exist with the specified defaults, in one config system."""

    def test_defaults(self):
        self.assertEqual(copybot.MAX_ENTRY_PRICE, 0.80)
        self.assertEqual(copybot.MIN_ENTRY_PRICE, 0.05)
        self.assertEqual(copybot.COPY_DELAY_SECONDS, 30)
        self.assertEqual(copybot.SLIPPAGE_BPS, 50.0)
        self.assertEqual(copybot.PAPER_FEE_BPS, 0.0)

    def test_dotenv_does_not_override_real_environment(self):
        os.environ["ZZ_TEST_KEY"] = "from_env"
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("ZZ_TEST_KEY=from_file\nZZ_TEST_NEW=from_file\n")
            path = fh.name
        try:
            copybot._load_dotenv(path)
            self.assertEqual(os.environ["ZZ_TEST_KEY"], "from_env")   # real env wins
            self.assertEqual(os.environ["ZZ_TEST_NEW"], "from_file")  # new key loaded
        finally:
            os.unlink(path)
            os.environ.pop("ZZ_TEST_KEY", None)
            os.environ.pop("ZZ_TEST_NEW", None)


class SlippageMathTests(unittest.TestCase):
    """E: slippage calculation is correct.  G: zero slippage preserves the price."""

    def test_slippage_is_adverse_and_correct(self):
        filled, amt = copybot.apply_slippage(0.6000, 50)
        self.assertAlmostEqual(filled, 0.6030, places=9)   # 0.60 * 1.005
        self.assertAlmostEqual(amt, 0.0030, places=9)
        self.assertGreater(filled, 0.6000)                  # never in the copier's favour

    def test_zero_slippage_preserves_price(self):
        filled, amt = copybot.apply_slippage(0.6000, 0)
        self.assertAlmostEqual(filled, 0.6000, places=12)
        self.assertAlmostEqual(amt, 0.0, places=12)

    def test_price_cannot_become_invalid(self):
        filled, _ = copybot.apply_slippage(0.999999, 10_000)  # +100% would exceed 1.0
        self.assertLess(filled, 1.0)
        self.assertGreater(filled, 0.0)


class FeeMathTests(unittest.TestCase):
    """H: zero paper fees preserve existing behavior."""

    def test_zero_fee_is_a_noop(self):
        fee, after = copybot.apply_fee(2.0, 0)
        self.assertEqual(fee, 0.0)
        self.assertEqual(after, 2.0)

    def test_nonzero_fee_is_supported_and_separate_from_slippage(self):
        fee, after = copybot.apply_fee(2.0, 50)      # 50 bps of $2 = $0.01
        self.assertAlmostEqual(fee, 0.01, places=9)
        self.assertAlmostEqual(after, 1.99, places=9)

    def test_zero_fee_share_count_matches_legacy_stake_over_price(self):
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, DEEP, exec_price=0.50,
                                           stake=copybot.STAKE)
        self.assertTrue(r["accepted"])
        legacy_shares = copybot.STAKE / r["entry_price"]
        self.assertAlmostEqual(r["shares"], legacy_shares, places=12)
        self.assertEqual(r["fee"], 0.0)


class DelayTests(unittest.TestCase):
    """F: delay timestamp calculation is correct — and never a real sleep."""

    def test_exec_ts_is_source_plus_configured_delay(self):
        r = copybot.evaluate_entry_filters(0.50, 1_000_000, 1_000_020, DEEP, exec_price=0.50)
        self.assertEqual(r["exec_ts"], 1_000_000 + copybot.COPY_DELAY_SECONDS)
        self.assertEqual(r["copy_delay_sec"], copybot.COPY_DELAY_SECONDS)
        self.assertEqual(r["detection_lag"], 20)

    def test_no_real_sleep_is_performed(self):
        import time as _t
        calls = []
        orig, _t.sleep = _t.sleep, lambda s: calls.append(s)
        try:
            copybot.evaluate_entry_filters(0.50, 1_000_000, 1_000_020, DEEP, exec_price=0.50)
        finally:
            _t.sleep = orig
        self.assertEqual(calls, [])

    def test_post_delay_price_is_used_not_the_wallet_price(self):
        # Wallet bought at 0.50; by exec time the market is 0.70. We must price off 0.70.
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, DEEP,
                                           wallet_price=0.50, exec_price=0.70)
        self.assertEqual(r["market_price"], 0.70)
        self.assertEqual(r["wallet_price"], 0.50)
        self.assertEqual(r["price_source"], "exec_ts_history")
        self.assertGreater(r["entry_price"], 0.70)   # plus adverse slippage

    def test_falls_back_to_current_price_when_history_unavailable(self):
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, DEEP, exec_price=None)
        self.assertEqual(r["price_source"], "current_market")
        self.assertEqual(r["market_price"], 0.50)


class EntryPriceLimitTests(unittest.TestCase):
    """A-D: limits are enforced against the FINAL simulated buy price."""

    def setUp(self):
        self._slip = copybot.SLIPPAGE_BPS

    def tearDown(self):
        copybot.SLIPPAGE_BPS = self._slip

    def _final(self, price):
        return copybot.evaluate_entry_filters(price, 1000, 1000, DEEP, exec_price=price)

    def test_A_price_exactly_at_max_accepted(self):
        copybot.SLIPPAGE_BPS = 0          # isolate the bound: final price == 0.80
        r = self._final(0.80)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["reason"], copybot.REASON_ACCEPTED)
        self.assertEqual(r["entry_price"], 0.80)

    def test_A2_price_just_below_max_accepted_with_default_slippage(self):
        r = self._final(0.79)             # 0.79 * 1.005 = 0.79395 <= 0.80
        self.assertTrue(r["accepted"])
        self.assertLessEqual(r["entry_price"], copybot.MAX_ENTRY_PRICE)

    def test_B_price_above_max_rejected(self):
        copybot.SLIPPAGE_BPS = 0
        r = self._final(0.81)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "PRICE_TOO_HIGH")

    def test_B2_price_090_rejected(self):
        r = self._final(0.90)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "PRICE_TOO_HIGH")

    def test_C_price_below_min_rejected(self):
        r = self._final(0.04)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "PRICE_TOO_LOW")

    def test_C2_price_at_min_accepted(self):
        copybot.SLIPPAGE_BPS = 0
        r = self._final(0.05)
        self.assertTrue(r["accepted"])

    def test_D_slippage_pushes_0798_over_the_max(self):
        # THE headline case: market 0.798, +50bps -> 0.80199 > 0.80 -> rejected.
        r = self._final(0.798)
        self.assertAlmostEqual(r["entry_price"], 0.798 * 1.005, places=6)
        self.assertGreater(r["entry_price"], copybot.MAX_ENTRY_PRICE)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "PRICE_TOO_HIGH")

    def test_D2_same_price_accepted_when_slippage_is_zero(self):
        copybot.SLIPPAGE_BPS = 0          # proves the rejection above is slippage-driven
        r = self._final(0.798)
        self.assertTrue(r["accepted"])

    def test_invalid_price_rejected(self):
        for bad in (0.0, 1.0, 1.5, -0.2, None, "abc"):
            r = copybot.evaluate_entry_filters(bad, 1000, 1000, DEEP, exec_price=bad)
            self.assertFalse(r["accepted"], bad)
            self.assertEqual(r["reason"], "INVALID_PRICE", bad)


class BookGateTests(unittest.TestCase):
    """Pre-existing order-book gates still work, under the renamed reason constants."""

    def setUp(self):
        self._min_liq, self._max_slip = copybot.MIN_LIQUIDITY, copybot.MAX_SLIPPAGE
        self._rs, self._rl = (copybot.REJECT_IF_SLIPPAGE_UNKNOWN,
                              copybot.REJECT_IF_LIQUIDITY_UNKNOWN)

    def tearDown(self):
        copybot.MIN_LIQUIDITY, copybot.MAX_SLIPPAGE = self._min_liq, self._max_slip
        copybot.REJECT_IF_SLIPPAGE_UNKNOWN = self._rs
        copybot.REJECT_IF_LIQUIDITY_UNKNOWN = self._rl

    def test_book_slippage_too_high_rejected(self):
        copybot.MIN_LIQUIDITY = 0.0
        b = book([(0.50, 1.0), (0.60, 100.0)])   # thin top level -> bad average fill
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, b, exec_price=0.50)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "SLIPPAGE_TOO_HIGH")

    def test_insufficient_liquidity_rejected(self):
        copybot.MAX_SLIPPAGE = 1.0
        copybot.MIN_LIQUIDITY = 20.0
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, book([(0.50, 5.0)]),
                                           exec_price=0.50)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "INSUFFICIENT_LIQUIDITY")

    def test_unknown_slippage_follows_config(self):
        copybot.REJECT_IF_SLIPPAGE_UNKNOWN = True
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, None, exec_price=0.50)
        self.assertEqual((r["accepted"], r["reason"]), (False, "SLIPPAGE_UNKNOWN"))
        copybot.REJECT_IF_SLIPPAGE_UNKNOWN = False
        copybot.REJECT_IF_LIQUIDITY_UNKNOWN = False
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, None, exec_price=0.50)
        self.assertTrue(r["accepted"])

    def test_unknown_liquidity_follows_config(self):
        copybot.REJECT_IF_SLIPPAGE_UNKNOWN = False
        copybot.REJECT_IF_LIQUIDITY_UNKNOWN = True
        r = copybot.evaluate_entry_filters(0.50, 1000, 1000, None, exec_price=0.50)
        self.assertEqual((r["accepted"], r["reason"]), (False, "LIQUIDITY_UNKNOWN"))

    def test_detection_lag_gate(self):
        old = copybot.MAX_DETECTION_LAG_SECONDS
        copybot.MAX_DETECTION_LAG_SECONDS = 60
        try:
            r = copybot.evaluate_entry_filters(0.50, 1000, 1000 + 61, DEEP, exec_price=0.50)
            self.assertEqual((r["accepted"], r["reason"]),
                             (False, "DETECTION_LAG_TOO_HIGH"))
            r = copybot.evaluate_entry_filters(0.50, 1000, 1000 + 30, DEEP, exec_price=0.50)
            self.assertTrue(r["accepted"])
        finally:
            copybot.MAX_DETECTION_LAG_SECONDS = old


class DbTestCase(unittest.TestCase):
    """Base: isolated temp DB per test."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._tmp = tmp.name
        self._old_db = copybot.DB
        copybot.DB = self._tmp
        self.c = copybot.db()

    def tearDown(self):
        try:
            self.c.close()
        except Exception:
            pass
        copybot.DB = self._old_db
        try:
            os.unlink(self._tmp)
        except OSError:
            pass


class AuditLogTests(DbTestCase):
    """L + M: every rejection AND every acceptance creates an audit-log record."""

    def _log(self, price, key="tx:a1:1000"):
        filt = copybot.evaluate_entry_filters(price, 1000, 1000, DEEP, wallet_price=price,
                                              exec_price=price)
        copybot._log_filter_event(self.c, key, "trader1", "a1", "c1", "Market", price, filt)
        return filt

    def test_M_accepted_creates_audit_record_with_execution_values(self):
        self._log(0.50)
        row = self.c.execute("""SELECT accepted, reason, wallet_price, market_price,
            entry_price, slippage_bps, slippage_amount, fee_bps, fee, stake, shares,
            exec_ts, copy_delay_sec, detection_lag, price_source
            FROM entry_filter_log""").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "ACCEPTED")
        for i, name in enumerate(("wallet_price", "market_price", "entry_price",
                                  "slippage_bps", "slippage_amount", "fee_bps", "fee",
                                  "stake", "shares", "exec_ts", "copy_delay_sec",
                                  "detection_lag", "price_source"), start=2):
            self.assertIsNotNone(row[i], f"{name} not recorded")

    def test_L_rejection_creates_audit_record_with_specific_reason(self):
        self._log(0.90)
        row = self.c.execute(
            "SELECT accepted, reason, entry_price, market_price FROM entry_filter_log").fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "PRICE_TOO_HIGH")
        self.assertNotIn(row[1], ("failed", "FAILED", None))
        self.assertIsNotNone(row[2])   # final simulated price recorded even on rejection

    def test_summary_counts_and_acceptance_rate(self):
        self._log(0.50, "k1")
        self._log(0.90, "k2")
        self._log(0.04, "k3")
        fs = copybot._filter_summary(self.c)
        self.assertEqual((fs["total"], fs["accepted"], fs["skipped"]), (3, 1, 2))
        self.assertAlmostEqual(fs["acceptance_rate"], 1 / 3, places=4)
        self.assertEqual(fs["reasons"]["PRICE_TOO_HIGH"], 1)
        self.assertEqual(fs["reasons"]["PRICE_TOO_LOW"], 1)

    def test_legacy_reason_names_are_mapped_not_lost(self):
        # A row written by the previous revision must still be counted correctly.
        self.c.execute("""INSERT INTO entry_filter_log(key, ts, accepted, reason)
                          VALUES('old1','2026-01-01T00:00:00Z',0,'ENTRY_PRICE_ABOVE_MAX')""")
        fs = copybot._filter_summary(self.c)
        self.assertEqual(fs["reasons"]["PRICE_TOO_HIGH"], 1)
        self.assertEqual(fs["reasons"]["OTHER_FILTER"], 0)


class PaperPositionTests(DbTestCase):
    """I, J, K: position creation is driven by the filter outcome."""

    def test_J_rejected_trade_creates_no_paper_position(self):
        filt = copybot.evaluate_entry_filters(0.90, 1000, 1000, DEEP, exec_price=0.90)
        self.assertFalse(filt["accepted"])
        copybot._log_filter_event(self.c, "k", "t1", "a1", "c1", "M", 0.90, filt)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)

    def test_I_accepted_trade_creates_exactly_one_paper_position(self):
        filt = copybot.evaluate_entry_filters(0.50, 1000, 1000, DEEP, wallet_price=0.50,
                                              exec_price=0.50)
        ok = copybot.record_paper_copy(
            self.c, "k", "t1", 1000, "c1", "a1", "M", "Yes", 0, 0.50,
            filt["entry_price"], copybot.STAKE, filt["shares"], "hold",
            exec_ts=filt["exec_ts"], market_price=filt["market_price"], fee=filt["fee"])
        self.assertTrue(ok)
        rows = self.c.execute(
            "SELECT their_price, entry, market_price, fee, exec_ts FROM copies").fetchall()
        self.assertEqual(len(rows), 1)
        their_price, entry, market_price, fee, exec_ts = rows[0]
        self.assertEqual(their_price, 0.50)              # wallet's own price preserved
        self.assertGreater(entry, 0.50)                  # our simulated fill is worse
        self.assertEqual(market_price, 0.50)
        self.assertEqual(fee, 0.0)
        self.assertEqual(exec_ts, 1000 + copybot.COPY_DELAY_SECONDS)

    def test_K_duplicate_candidate_is_not_copied_twice(self):
        args = (self.c, "k", "t1", 1000, "c1", "a1", "M", "Yes", 0, 0.50, 0.5025,
                copybot.STAKE, 4.0, "hold")
        self.assertTrue(copybot.record_paper_copy(*args))
        self.assertFalse(copybot.record_paper_copy(*args))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 1)


class MigrationTests(unittest.TestCase):
    """13: an existing pre-migration DB is upgraded in place, losing nothing."""

    def test_existing_rows_survive_migration(self):
        import sqlite3
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        old_db = copybot.DB
        try:
            # Build a DB in the OLD schema (no execution columns) with real data in it.
            raw = sqlite3.connect(tmp.name)
            raw.execute("""CREATE TABLE copies(
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, copied_ts TEXT,
                wallet TEXT, their_ts INTEGER, condition_id TEXT, asset TEXT, title TEXT,
                outcome TEXT, outcome_index INTEGER, their_price REAL, entry REAL,
                stake REAL, shares REAL, status TEXT, mark_price REAL, pnl REAL,
                resolved_ts TEXT)""")
            raw.execute("""CREATE TABLE entry_filter_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, ts TEXT,
                wallet TEXT, asset TEXT, condition_id TEXT, title TEXT, entry_price REAL,
                detection_lag REAL, slippage REAL, liquidity_usd REAL, accepted INTEGER,
                reason TEXT)""")
            raw.execute("INSERT INTO copies(key,wallet,entry,stake,shares,status) "
                        "VALUES('legacy1','t1',0.42,2.0,4.76,'open')")
            raw.execute("INSERT INTO entry_filter_log(key,accepted,reason) "
                        "VALUES('legacyfilt',0,'ENTRY_PRICE_ABOVE_MAX')")
            raw.commit()
            raw.close()

            copybot.DB = tmp.name
            c = copybot.db()          # runs the additive migration
            # old data intact
            self.assertEqual(
                c.execute("SELECT entry, status FROM copies WHERE key='legacy1'").fetchone(),
                (0.42, "open"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM entry_filter_log").fetchone()[0], 1)
            # new columns present and NULL for old rows
            self.assertIsNone(
                c.execute("SELECT fee FROM copies WHERE key='legacy1'").fetchone()[0])
            self.assertIsNone(c.execute(
                "SELECT slippage_bps FROM entry_filter_log WHERE key='legacyfilt'").fetchone()[0])
            c.close()
        finally:
            copybot.DB = old_db
            os.unlink(tmp.name)


class TickIntegrationTests(unittest.TestCase):
    """N + end-to-end: the gate runs once, before models A-E, with no network."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._tmp = tmp.name
        self._old_db, copybot.DB = copybot.DB, self._tmp
        self._old_wallets = dict(copybot.WALLETS)
        copybot.WALLETS.clear()
        copybot.WALLETS["tester"] = "0xdead"
        self._saved = (copybot.get, copybot._market_by_condition, copybot.chart_signal,
                       copybot._fetch_book, copybot._price_at)
        # Phase 6: chart_signal None is now fail-closed; return a neutral passing signal so
        # these tick integration tests (which verify all 5 models receive a copy) still work.
        _neutral_sig = {
            "current": 0.50, "trend_ph": 0.00, "rsi": 50.0,
            "mom_5m": 0.00, "mom_30m": 0.00, "price_vol": 0.01,
            "support": 0.45, "resistance": 0.55, "bars": 60,
            "vol_1h": 1000.0, "vol_15m": 250.0,
            "vol_ratio": 1.0, "buy_pressure": 0.50, "vwap": 0.50,
        }
        copybot.chart_signal = lambda asset: _neutral_sig

    def tearDown(self):
        copybot.DB = self._old_db
        copybot.WALLETS.clear()
        copybot.WALLETS.update(self._old_wallets)
        (copybot.get, copybot._market_by_condition, copybot.chart_signal,
         copybot._fetch_book, copybot._price_at) = self._saved
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    def _wire(self, price, timestamp, end_iso, book_result=DEEP, exec_price=None,
              duplicate=False):
        act = {"type": "TRADE", "side": "BUY", "timestamp": timestamp,
               "transactionHash": "0xtx1", "conditionId": "cond1", "asset": "asset1",
               "outcomeIndex": 0, "price": price, "title": "Will X happen?",
               "outcome": "Yes", "slug": "will-x-happen"}
        activity = [act, dict(act)] if duplicate else [act]
        copybot.get = lambda url: activity if "/activity" in url else (_ for _ in ()).throw(
            AssertionError(f"unexpected get(): {url}"))
        copybot._market_by_condition = lambda cid: {
            "outcomePrices": copybot.json.dumps([price, 1 - price]),
            "endDate": end_iso, "closed": False}
        copybot._fetch_book = lambda asset: book_result
        copybot._price_at = lambda asset, ts: exec_price

    @staticmethod
    def _soon(days=1):
        from datetime import timedelta
        return (copybot.datetime.now(copybot.timezone.utc) + timedelta(days=days)).isoformat()

    def test_N_accepted_trade_reaches_all_five_models(self):
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon())
        copybot.tick()
        c = copybot.db()
        self.assertEqual(sorted(r[0] for r in c.execute("SELECT model FROM copies")),
                         sorted(["hold", "exit", "cdecide", "trail", "chart"]))
        # all five entered at the SAME simulated fill — one shared gate, not per-model
        entries = {r[0] for r in c.execute("SELECT entry FROM copies")}
        self.assertEqual(len(entries), 1)
        self.assertGreater(entries.pop(), 0.50)   # slippage applied
        self.assertEqual(c.execute(
            "SELECT COUNT(*) FROM entry_filter_log WHERE accepted=1").fetchone()[0], 1)

    def test_model_A_hold_eligibility_rule_unchanged(self):
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon(days=30))   # too far out for A
        copybot.tick()
        c = copybot.db()
        self.assertEqual(sorted(r[0] for r in c.execute("SELECT model FROM copies")),
                         sorted(["exit", "cdecide", "trail", "chart"]))

    def test_090_rejected_end_to_end(self):
        now = int(copybot.time.time())
        self._wire(0.90, now - 10, self._soon())
        copybot.tick()
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)
        self.assertEqual(
            c.execute("SELECT accepted, reason FROM entry_filter_log").fetchone(),
            (0, "PRICE_TOO_HIGH"))

    def test_0798_rejected_end_to_end_by_slippage(self):
        now = int(copybot.time.time())
        self._wire(0.798, now - 10, self._soon())
        copybot.tick()
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)
        accepted, reason, market, entry = c.execute(
            "SELECT accepted, reason, market_price, entry_price FROM entry_filter_log"
        ).fetchone()
        self.assertEqual((accepted, reason), (0, "PRICE_TOO_HIGH"))
        self.assertEqual(market, 0.798)
        self.assertGreater(entry, copybot.MAX_ENTRY_PRICE)

    def test_post_delay_price_move_can_reject_a_cheap_wallet_entry(self):
        # Wallet bought at 0.50, but 30s later the market is 0.90 -> we must not copy.
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon(), exec_price=0.90)
        copybot.tick()
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)
        accepted, reason, wallet_price, market = c.execute(
            "SELECT accepted, reason, wallet_price, market_price FROM entry_filter_log"
        ).fetchone()
        self.assertEqual((accepted, reason), (0, "PRICE_TOO_HIGH"))
        self.assertEqual((wallet_price, market), (0.50, 0.90))

    def test_K_duplicate_candidate_in_one_run_copied_once(self):
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon(), duplicate=True)
        copybot.tick()
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM entry_filter_log").fetchone()[0], 1)

    def test_repeated_ticks_do_not_duplicate_positions(self):
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon())
        copybot.tick()
        copybot.tick()
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 5)

    def test_export_exposes_execution_settings_and_stats(self):
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon())
        copybot.tick()
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            copybot.export()
            data = copybot.json.load(open("docs/data.json"))
        finally:
            os.chdir(cwd)
        f = data["filters"]
        for k in ("max_entry_price", "min_entry_price", "copy_delay_seconds",
                  "slippage_bps", "paper_fee_bps", "max_slippage", "min_liquidity",
                  "max_detection_lag_seconds", "reject_if_slippage_unknown",
                  "reject_if_liquidity_unknown", "candidates", "accepted", "skipped",
                  "acceptance_rate", "reasons", "median_detection_lag_sec",
                  "avg_detection_lag_sec", "avg_slippage_amount", "total_paper_fees",
                  "recent_accepted"):
            self.assertIn(k, f, f"{k} missing from docs/data.json filters")
        self.assertEqual(f["max_entry_price"], 0.80)
        self.assertEqual(f["min_entry_price"], 0.05)
        self.assertEqual(f["accepted"], 1)
        # Verify Phase 8.2 export of unrealized P&L
        self.assertIn("unrealized", data["models"]["A"])
        self.assertIn("total_unrealized", data["meta"])
        self.assertEqual(data["meta"]["total_unrealized"],
                         round(sum(m["unrealized"] for m in data["models"].values()), 2))
        audit = f["recent_accepted"][0]
        for k in ("wallet_price", "market_price", "entry_price", "slippage_amount",
                  "fee", "stake", "shares", "detection_lag"):
            self.assertIn(k, audit)

    def test_report_runs_and_prints_execution_assumptions(self):
        import io
        from contextlib import redirect_stdout
        now = int(copybot.time.time())
        self._wire(0.50, now - 10, self._soon())
        copybot.tick()
        buf = io.StringIO()
        with redirect_stdout(buf):
            copybot.report()
        out = buf.getvalue()
        for frag in ("max entry price", "min entry price", "copy delay", "slippage",
                     "paper fee", "candidates=", "accepted=", "acceptance_rate="):
            self.assertIn(frag, out)


class PaperOnlySafetyTests(unittest.TestCase):
    """11: no live trading surface was introduced."""

    def test_no_keys_signing_or_order_submission(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "copybot.py")).read().lower()
        for bad in ("private_key", "privatekey", "eth_account", "web3", "sign_order",
                    "signer", "place_order", "post_order", "withdraw", "deposit"):
            self.assertNotIn(bad, src, f"paper-only violation: {bad}")


class Phase82FinancialMetricsExportTests(unittest.TestCase):
    """Phase 8.2: Verify actual unrealized P&L export and accounting reconciliation."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._orig_db = copybot.DB
        copybot.DB = self._tmp.name
        copybot.db()

    def tearDown(self):
        copybot.DB = self._orig_db
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_export_includes_unrealized_and_reconciles_accounting(self):
        """Verify:
        1. Each model exports actual 'unrealized'.
        2. meta exports 'total_unrealized' matching the sum.
        3. Accounting identity: value == started + realized + unrealized (within rounding).
        """
        c = copybot.db()
        # Ensure bankroll row exists before crediting/debiting
        copybot._cash(c, "hold", "fox54498")
        # Seed an open position with unrealized gain
        # stake=2.0, mark_price=0.60, entry=0.50 -> pnl = +0.40
        c.execute("""INSERT INTO copies(key,copied_ts,wallet,condition_id,asset,
            outcome,outcome_index,their_price,entry,stake,shares,status,model,mark_price,pnl)
            VALUES('t1:0x1:1','2026-10-11T00:00:00Z','fox54498','c1','0x1',
            'Yes',0,0.50,0.50,2.0,4.0,'open','hold',0.60,0.40)""")
        # Lock stake out of bankroll
        copybot._credit(c, "hold", "fox54498", -2.0)
        c.commit()

        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            copybot.export()
            data = copybot.json.load(open("docs/data.json"))
        finally:
            os.chdir(cwd)

        models = data["models"]
        # Model A (hold) should have unrealized = 0.40
        self.assertEqual(models["A"]["unrealized"], 0.40)
        self.assertEqual(models["A"]["open"], 1)
        self.assertEqual(models["A"]["closed"], 0)
        self.assertEqual(models["A"]["realized"], 0.0)

        # Value should reflect cash (400 - 2 = 398) + at_risk (2) + unreal (0.40) = 400.40
        self.assertEqual(models["A"]["value"], 400.40)

        # Accounting identity: value == started + realized + unrealized
        for mn, m in models.items():
            expected = round(m["started"] + m["realized"] + m["unrealized"], 2)
            self.assertAlmostEqual(m["value"], expected, places=2,
                                   msg=f"Model {mn} accounting does not reconcile")

        # meta.total_unrealized matches sum of models
        self.assertEqual(data["meta"]["total_unrealized"],
                         round(sum(m["unrealized"] for m in models.values()), 2))

    def test_export_meta_lag_p50_calculation(self):
        """Regression test for Phase 5.7:
        Verify meta.lag_p50_min accurately calculates the median of lag values
        even when copies are stored/inserted in a deliberately non-sorted order.
        Tests:
        1. Deliberately unsorted lag dataset produces the correct sorted median.
        2. Even-sized lag dataset follows existing convention (index len//2).
        3. Empty dataset produces None.
        """
        c = copybot.db()
        # Clean out any pre-existing copies in temporary DB
        c.execute("DELETE FROM copies")
        c.commit()

        # 1. Empty test
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            copybot.export()
            with open("docs/data.json") as f:
                data = json.load(f)
            self.assertIsNone(data["meta"]["lag_p50_min"])

            # 2. Odd-sized dataset with deliberately unsorted lags:
            # Lags in seconds: [600, 60, 300, 1200, 180] -> in minutes: [10.0, 1.0, 5.0, 20.0, 3.0]
            # Unsorted insertion order would take middle index [2] -> 300s (5.0 min).
            # Sorted order: [60, 180, 300, 600, 1200] -> middle index [2] happens to be 300s (5.0 min).
            # Let's use: [1200, 60, 1800, 600, 300] -> minutes: [20.0, 1.0, 30.0, 10.0, 5.0]
            # Unsorted index [2] -> 1800s (30.0 min).
            # Sorted: [60, 300, 600, 1200, 1800] -> index [2] is 600s (10.0 min).
            # Base timestamp: 1780000000
            base_ts = 1780000000
            deliberate_lags = [1200, 60, 1800, 600, 300]
            for idx, lag in enumerate(deliberate_lags):
                copied_dt = datetime.fromtimestamp(base_ts + lag, timezone.utc).isoformat()
                c.execute("""INSERT INTO copies(key,copied_ts,their_ts,status,model,wallet,stake)
                    VALUES(?,?,?,'open','hold','fox54498',2.0)""",
                    (f"lag_odd_{idx}", copied_dt, str(base_ts)))
            c.commit()

            copybot.export()
            with open("docs/data.json") as f:
                data = json.load(f)
            # Correct sorted median is 600s / 60 = 10.0 minutes
            self.assertEqual(data["meta"]["lag_p50_min"], 10.0)

            # 3. Even-sized dataset: add one more lag of 60s
            # Now lags are: [1200, 60, 1800, 600, 300, 60] -> sorted: [60, 60, 300, 600, 1200, 1800]
            # len=6, len//2 = 3 -> index 3 is 600s (10.0 minutes).
            c.execute("""INSERT INTO copies(key,copied_ts,their_ts,status,model,wallet,stake)
                VALUES('lag_even_5',?,'1780000000','open','hold','fox54498',2.0)""",
                (datetime.fromtimestamp(base_ts + 60, timezone.utc).isoformat(),))
            c.commit()

            copybot.export()
            with open("docs/data.json") as f:
                data = json.load(f)
            self.assertEqual(data["meta"]["lag_p50_min"], 10.0)
        finally:
            os.chdir(cwd)



class Phase810CategoryQualityTests(unittest.TestCase):
    """Phase 8.10: Regression tests for Trader Edge Breakdown (Section F).
    Verifies:
    1. Valid classified result updates correct category statistics.
    2. Unknown / unclassified titles fall back to 'other' without fabricating categories.
    3. Strategy exits (exit_won, d_stop, etc.) do NOT update trader category statistics.
    4. Market resolution deduplicates across Models A-E (5 model copies = 1 sample).
    5. Repeated resolve runs are idempotent and cannot double-count.
    6. docs/quality.json structure conforms to dashboard expectations.
    """

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._orig_db = copybot.DB
        copybot.DB = self._tmp.name
        copybot.db()

    def tearDown(self):
        copybot.DB = self._orig_db
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_category_classification_rules(self):
        """Verify market categorization mapping."""
        self.assertEqual(copybot.categorize_market("NBA: Lakers vs Celtics"), "sports")
        self.assertEqual(copybot.categorize_market("Will BTC reach 100k in 5m?"), "crypto_5m")
        self.assertEqual(copybot.categorize_market("Will Trump win the election?"), "politics")
        self.assertEqual(copybot.categorize_market("ETH price at end of 1 hour"), "crypto_h")
        self.assertEqual(copybot.categorize_market("Will Alex Warren release a single in June?"), "other")

    def test_market_resolution_updates_trader_stats_and_deduplicates(self):
        """Verify market resolution updates stats once even if 5 models hold the copy."""
        c = copybot.db()
        trader = "GoalLineGhost"
        base_key = "0xtx:0xasset:1780000000"
        cid = "c_mkt_1"

        # Insert 5 copies (Models A-E) for the same underlying trader trade
        for model in ("hold", "exit", "cdecide", "trail", "chart"):
            c.execute("""INSERT INTO copies(key,copied_ts,wallet,condition_id,asset,title,
                outcome,outcome_index,their_price,entry,stake,shares,status,model)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{base_key}:{model}", "2026-10-11T00:00:00Z", trader, cid, "0xasset",
                 "NBA: Lakers vs Celtics", "Yes", 0, 0.50, 0.50, 2.0, 4.0, "open", model))
        c.commit()

        # Mock market returned from _market_by_condition
        fake_market = {
            "closed": True,
            "outcomePrices": json.dumps(["1.0", "0.0"]),
        }

        with mock.patch("copybot._market_by_condition", return_value=fake_market):
            copybot.resolve()

        # Check copies were settled
        rows = c.execute("SELECT status FROM copies WHERE condition_id=?", (cid,)).fetchall()
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(r[0], "won")

        # Verify trader_cat_stats in DB has EXACTLY 1 trade, not 5
        stat_row = c.execute("SELECT trades, wins, realized FROM trader_cat_stats WHERE trader=? AND category=?",
                             (trader, "sports")).fetchone()
        self.assertIsNotNone(stat_row)
        self.assertEqual(stat_row[0], 1)  # 1 trade
        self.assertEqual(stat_row[1], 1)  # 1 win
        self.assertTrue(stat_row[2] > 0)  # positive realized PnL

        # _get_trader_cat_quality returns trusted=False and wr=0.0 until MIN_CAT_TRADES (6) reached
        q = copybot._get_trader_cat_quality(c, trader, "sports")
        self.assertEqual(q["trades"], 1)
        self.assertFalse(q["trusted"])  # below MIN_CAT_TRADES (6)

        # Verify idempotency: running resolve again does not increment
        with mock.patch("copybot._market_by_condition", return_value=fake_market):
            copybot.resolve()

        stat_row_after = c.execute("SELECT trades, wins FROM trader_cat_stats WHERE trader=? AND category=?",
                                   (trader, "sports")).fetchone()
        self.assertEqual(stat_row_after[0], 1)
        self.assertEqual(stat_row_after[1], 1)

    def test_strategy_exits_do_not_update_category_stats(self):
        """Verify Model B take-profit / Model D stop exits do not record category prediction stats."""
        c = copybot.db()
        trader = "fox54498"
        base_key = "0xtx2:0xasset2:1780000000"
        cid = "c_mkt_2"

        # Model B copy with cur > entry * 1.15 -> triggers exit_won
        c.execute("""INSERT INTO copies(key,copied_ts,wallet,condition_id,asset,title,
            outcome,outcome_index,their_price,entry,stake,shares,status,model)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{base_key}:exit", "2026-10-11T00:00:00Z", trader, cid, "0xasset2",
             "Will Donald Trump tweet tomorrow?", "Yes", 0, 0.50, 0.50, 2.0, 4.0, "open", "exit"))
        c.commit()

        # Unclosed market with current price 0.65 (+30% gain)
        fake_market = {
            "closed": False,
            "outcomePrices": json.dumps(["0.65", "0.35"]),
        }

        with mock.patch("copybot._market_by_condition", return_value=fake_market):
            copybot.resolve()

        # Status should be exit_won
        row = c.execute("SELECT status FROM copies WHERE key=?", (f"{base_key}:exit",)).fetchone()
        self.assertEqual(row[0], "exit_won")

        # trader_cat_stats for politics must remain 0 trades
        q = copybot._get_trader_cat_quality(c, trader, "politics")
        self.assertEqual(q["trades"], 0)

    def test_quality_json_structure(self):
        """Verify export writes docs/quality.json with expected schema."""
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            copybot.export()
            self.assertTrue(os.path.exists("docs/quality.json"))
            with open("docs/quality.json") as f:
                data = json.load(f)
            self.assertIn("updated", data)
            self.assertIn("quality", data)
            self.assertEqual(len(data["quality"]), len(copybot.WALLETS))
            for row in data["quality"]:
                self.assertIn("name", row)
                for cat in ("sports", "crypto_5m", "politics", "crypto_h", "other"):
                    self.assertIn(cat, row)
                    for key in ("trades", "wr", "roi", "trusted"):
                        self.assertIn(key, row[cat])
        finally:
            os.chdir(cwd)

    def test_atomicity_on_stats_update_failure(self):
        """Regression: a failure between the dedup INSERT and the stats UPDATE must leave
        neither a partial dedup record nor a partial stats row.  The trade must be
        retryable and must ultimately count exactly once.

        Failure is injected via a thin ConnectionProxy wrapper that raises
        sqlite3.OperationalError on the first INSERT/UPDATE to trader_cat_stats.
        sqlite3.Connection.execute is immutable in CPython 3.14+, so monkey-patching
        the class is not possible; the proxy is passed directly to the function under
        test instead.
        """
        import sqlite3 as _sqlite3

        real_c = copybot.db()
        trader = "mooseborzoi"
        source_key = "0xfailtest:0xasset99:1780000001"
        category = "sports"

        # Thin proxy: delegates everything to the real connection but intercepts execute()
        # to inject a one-shot failure on the first stats write.
        class ConnectionProxy:
            def __init__(self, conn):
                self._conn = conn
                self._fail_next_stats = True  # armed for one failure

            def execute(self, sql, params=()):
                sql_upper = sql.strip().upper()
                if (self._fail_next_stats
                        and "TRADER_CAT_STATS" in sql_upper
                        and (sql_upper.startswith("INSERT") or sql_upper.startswith("UPDATE"))):
                    self._fail_next_stats = False  # disarm after first injection
                    raise _sqlite3.OperationalError("injected test failure")
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                # Delegate commit, rollback, close, and any other attribute to real conn
                return getattr(self._conn, name)

        proxy = ConnectionProxy(real_c)

        # --- First attempt: stats write fails ---
        with self.assertRaises(_sqlite3.OperationalError):
            copybot._update_trader_cat_stat(proxy, trader, category, True, 2.0,
                                            source_key=source_key)

        # After failure: dedup row must NOT exist (savepoint rolled it back)
        dedup_row = real_c.execute(
            "SELECT source_key FROM trader_cat_resolved WHERE source_key=?",
            (source_key,)).fetchone()
        self.assertIsNone(dedup_row, "Dedup row must not persist after a failed stats update")

        # After failure: stats row must NOT exist either
        stat_row = real_c.execute(
            "SELECT trades FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, category)).fetchone()
        self.assertIsNone(stat_row, "Stats row must not exist after a failed stats update")

        # --- Second attempt (retry): use real connection directly, both writes must succeed ---
        result = copybot._update_trader_cat_stat(real_c, trader, category, True, 2.0,
                                                 source_key=source_key)
        self.assertTrue(result, "Retry must return True (success)")

        dedup_row = real_c.execute(
            "SELECT source_key FROM trader_cat_resolved WHERE source_key=?",
            (source_key,)).fetchone()
        self.assertIsNotNone(dedup_row, "Dedup row must exist after successful retry")

        stat_row = real_c.execute(
            "SELECT trades, wins FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, category)).fetchone()
        self.assertIsNotNone(stat_row, "Stats row must exist after successful retry")
        self.assertEqual(stat_row[0], 1, "Exactly 1 trade after first successful write")
        self.assertEqual(stat_row[1], 1, "Exactly 1 win after first successful write")

        # --- Third attempt (duplicate): must be idempotent ---
        result2 = copybot._update_trader_cat_stat(real_c, trader, category, True, 2.0,
                                                  source_key=source_key)
        self.assertFalse(result2, "Duplicate attempt must return False")

        stat_row_final = real_c.execute(
            "SELECT trades, wins FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, category)).fetchone()
        self.assertEqual(stat_row_final[0], 1, "Trades must still be 1 after duplicate attempt")
        self.assertEqual(stat_row_final[1], 1, "Wins must still be 1 after duplicate attempt")


    def test_resolver_retry_on_category_stats_failure(self):
        """Integration regression: when _update_trader_cat_stat raises during resolve(),
        the affected copy must stay 'open' (sp_settle rolled back its status change),
        while other model copies of the same trade settle normally and record the stat
        exactly once.  A subsequent resolve() run must settle the deferred copy without
        producing a second category-stats sample.
        """
        c = copybot.db()
        trader = "AshHaykArs"
        base_key = "0xretry:0xasset_r:1780000002"
        cid = "c_retry"

        # Insert 5 copies (Models A-E), same underlying trade.
        # ORDER BY id DESC means the last-inserted model is processed first by resolve().
        for model in ("hold", "exit", "cdecide", "trail", "chart"):
            c.execute(
                """INSERT INTO copies(key,copied_ts,wallet,condition_id,asset,title,
                    outcome,outcome_index,their_price,entry,stake,shares,status,model)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{base_key}:{model}", "2026-10-11T00:00:00Z", trader, cid,
                 "0xasset_r", "NBA: Lakers vs Celtics", "Yes", 0,
                 0.50, 0.50, 2.0, 4.0, "open", model))
        c.commit()

        # Closed market: outcome 0 wins.
        fake_market = {"closed": True, "outcomePrices": json.dumps(["1.0", "0.0"])}

        # Inject a failure on the very first call to _update_trader_cat_stat.
        # The real implementation is bypassed for this call only; resolve()'s
        # sp_settle savepoint rolls back that copy's status change.
        call_count = [0]
        real_update = copybot._update_trader_cat_stat

        def failing_once(conn, trd, cat, w, pnl, source_key=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise sqlite3.OperationalError("injected stat failure")
            return real_update(conn, trd, cat, w, pnl, source_key=source_key)

        with mock.patch("copybot._update_trader_cat_stat", side_effect=failing_once):
            with mock.patch("copybot._market_by_condition", return_value=fake_market):
                copybot.resolve()

        # Exactly one copy must remain 'open' (the one whose settlement was rolled back).
        open_rows = c.execute(
            "SELECT key FROM copies WHERE condition_id=? AND status='open'",
            (cid,)).fetchall()
        self.assertEqual(len(open_rows), 1, "Exactly one copy must be deferred (still open)")

        # The other four must be settled.
        settled_rows = c.execute(
            "SELECT key FROM copies WHERE condition_id=? AND status='won'",
            (cid,)).fetchall()
        self.assertEqual(len(settled_rows), 4, "Four copies must be settled after first resolve")

        # Category stat must have been recorded exactly once by one of the settled copies.
        stat = c.execute(
            "SELECT trades, wins FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, "sports")).fetchone()
        self.assertIsNotNone(stat, "Category stat must exist after partial resolve")
        self.assertEqual(stat[0], 1, "Exactly 1 trade after partial resolve")
        self.assertEqual(stat[1], 1, "Exactly 1 win after partial resolve")

        # Second resolve() — no injected failure; deferred copy must settle cleanly.
        with mock.patch("copybot._market_by_condition", return_value=fake_market):
            copybot.resolve()

        open_after = c.execute(
            "SELECT key FROM copies WHERE condition_id=? AND status='open'",
            (cid,)).fetchall()
        self.assertEqual(len(open_after), 0, "No copies must remain open after retry")

        won_after = c.execute(
            "SELECT key FROM copies WHERE condition_id=? AND status='won'",
            (cid,)).fetchall()
        self.assertEqual(len(won_after), 5, "All five copies must be settled after retry")

        # Category stat must still be exactly one sample — not double-counted.
        stat_after = c.execute(
            "SELECT trades, wins FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, "sports")).fetchone()
        self.assertEqual(stat_after[0], 1, "Trades must remain 1 after retry (no double-count)")
        self.assertEqual(stat_after[1], 1, "Wins must remain 1 after retry (no double-count)")

    def test_unrelated_integrity_error_propagates(self):
        """Verify that an IntegrityError that is NOT a duplicate source_key is re-raised
        rather than silently treated as a duplicate.

        The proxy intercepts INSERT into trader_cat_resolved at the Python level without
        the row ever reaching SQLite (simulating a non-PK constraint failure that leaves
        no row behind).  The handler then confirms source_key is absent, and re-raises.
        All other SQL (SAVEPOINT, ROLLBACK TO, RELEASE, SELECT) passes through to the
        real connection so SQLite's savepoint stack stays consistent.
        """
        import sqlite3 as _sqlite3

        # Use an explicit-transaction connection so SAVEPOINT nests predictably.
        # isolation_level=None means autocommit; we issue BEGIN manually via the
        # no-op DML that opens the Python implicit transaction.
        real_c = copybot.db()
        trader = "belaba"
        source_key = "0xunrelated:0xbad:1780000003"
        category = "sports"

        class FakeIntegrityProxy:
            """Raises IntegrityError on INSERT to trader_cat_resolved without sending
            the statement to SQLite.  Everything else delegates to the real connection.
            Critically, SAVEPOINT / ROLLBACK TO / RELEASE pass through so the SQLite
            savepoint stack stays consistent with what _update_trader_cat_stat expects.
            """
            def __init__(self, conn):
                self._conn = conn
                self._armed = True

            def execute(self, sql, params=()):
                sql_upper = sql.strip().upper()
                if (self._armed
                        and sql_upper.startswith("INSERT")
                        and "TRADER_CAT_RESOLVED" in sql_upper):
                    self._armed = False
                    # Raise before the row reaches SQLite — savepoint sp_cat is still
                    # active on the real connection, so ROLLBACK TO sp_cat will work.
                    raise _sqlite3.IntegrityError("simulated non-duplicate constraint error")
                # All other statements (SAVEPOINT, ROLLBACK TO, RELEASE, SELECT, UPDATE)
                # go directly to the real connection.
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        # Open a Python-level implicit transaction so SAVEPOINT sp_cat nests inside it.
        # This mirrors the production path where resolve() issues UPDATE copies first.
        real_c.execute("UPDATE trader_cat_stats SET trades=0 WHERE 1=0")  # no-op DML; opens BEGIN

        proxy = FakeIntegrityProxy(real_c)

        # The call must raise IntegrityError (not return False, not be swallowed).
        with self.assertRaises(_sqlite3.IntegrityError):
            copybot._update_trader_cat_stat(proxy, trader, category, True, 2.0,
                                            source_key=source_key)

        # Neither the dedup row nor the stats row must exist.
        dedup = real_c.execute(
            "SELECT 1 FROM trader_cat_resolved WHERE source_key=?",
            (source_key,)).fetchone()
        self.assertIsNone(dedup, "No dedup row must persist after unrelated IntegrityError")

        stat = real_c.execute(
            "SELECT trades FROM trader_cat_stats WHERE trader=? AND category=?",
            (trader, category)).fetchone()
        self.assertIsNone(stat, "No stats row must persist after unrelated IntegrityError")

        real_c.rollback()  # discard the no-op DML that opened the transaction


if __name__ == "__main__":
    unittest.main()
