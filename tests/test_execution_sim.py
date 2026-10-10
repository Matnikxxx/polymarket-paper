"""
Tests for the paper entry/execution simulation in copybot.py (PAPER ONLY).

Run with:
    python3 -m pytest -q                      (preferred, if pytest is installed)
    python3 -m unittest discover -s tests -v   (stdlib fallback, no extra deps)

No network and no real DB: order books are plain dicts in the shape the CLOB /book
endpoint returns, and each DB-touching test gets a throwaway temp sqlite file.
Nothing here sleeps — the copy delay is timestamp arithmetic, which is the point.
"""
import os
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
