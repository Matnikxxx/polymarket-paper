"""
Deterministic unit tests for Model E's chart entry filters and missing-data behavior.

Tests cover copybot._chart_entry_ok() and copybot.chart_signal():
1. RSI overbought rule (rsi > 78) and strict boundary.
2. 30-minute momentum chase rule (mom_30m > 0.10 absolute) and strict boundary.
3. Downtrend rule (trend_ph < -0.05 and mom_30m < -0.05) and compound conditions.
4. Volume drying rule (vol_ratio < 0.3 and buy_pressure < 0.35) and compound conditions.
5. Phase 6 fail-closed policy: None signal and error sentinel both reject; volume optional.
6. chart_filter_log persistence: DB rows written on accept and reject.
7. Schema idempotency: db() called twice does not raise.

All external network access is blocked via mocked interfaces.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import copybot  # noqa: E402


def _make_neutral_signal(**overrides):
    """Create a baseline chart signal where all indicators are healthy/neutral."""
    sig = {
        "current": 0.50,
        "trend_ph": 0.00,
        "rsi": 50.0,
        "mom_5m": 0.00,
        "mom_30m": 0.00,
        "price_vol": 0.01,
        "support": 0.45,
        "resistance": 0.55,
        "bars": 60,
        "vol_1h": 1000.0,
        "vol_15m": 250.0,
        "vol_ratio": 1.0,
        "buy_pressure": 0.50,
        "vwap": 0.50,
    }
    sig.update(overrides)
    return sig


class TestModelEChartFilterRules(unittest.TestCase):
    """Verify each chart entry rule and strict boundary in copybot._chart_entry_ok()."""

    def setUp(self):
        # Guard against any unexpected external HTTP call
        self.patcher = mock.patch(
            "urllib.request.urlopen",
            side_effect=RuntimeError("External network call forbidden in unit tests"),
        )
        self.mock_urlopen = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    # --- A. RSI Overbought Rule (rsi > 78) ---

    def test_rsi_boundary_at_78_accepted(self):
        """RSI = 78.0 is exactly at the boundary; strict > 78 does not reject."""
        sig = _make_neutral_signal(rsi=78.0)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_rsi_above_78_rejected(self):
        """RSI = 78.1 strictly exceeds 78.0; must be rejected for overbought_rsi."""
        sig = _make_neutral_signal(rsi=78.1)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("overbought_rsi", reason)
        self.assertEqual(reason, "overbought_rsi(78)")

    def test_rsi_well_above_78_rejected(self):
        """RSI = 85.0 is clearly overbought; rejected with appropriate reason."""
        sig = _make_neutral_signal(rsi=85.0)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "overbought_rsi(85)")

    # --- B. Thirty-Minute Chase Rule (mom_30m > 0.10) ---

    def test_chase_boundary_at_10_cents_accepted(self):
        """mom_30m = 0.100 is exactly at the $0.10 boundary; strict > 0.10 does not reject."""
        sig = _make_neutral_signal(mom_30m=0.100)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_chase_above_10_cents_rejected(self):
        """mom_30m = 0.101 strictly exceeds $0.10; must be rejected for chasing_30m."""
        sig = _make_neutral_signal(mom_30m=0.101)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("chasing_30m", reason)
        self.assertEqual(reason, "chasing_30m(+0.101 abs)")

    def test_chase_large_spike_rejected(self):
        """mom_30m = 0.250 is an obvious chase; rejected with absolute amount in reason."""
        sig = _make_neutral_signal(mom_30m=0.250)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "chasing_30m(+0.250 abs)")

    # --- C. Downtrend Rule (trend_ph < -0.05 AND mom_30m < -0.05) ---

    def test_downtrend_both_conditions_met_rejected(self):
        """Both trend_ph < -0.05 and mom_30m < -0.05: entry is rejected for downtrend."""
        sig = _make_neutral_signal(trend_ph=-0.06, mom_30m=-0.06)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("downtrend", reason)
        self.assertEqual(reason, "downtrend(-0.060/hr)")

    def test_downtrend_only_trend_below_threshold_accepted(self):
        """trend_ph < -0.05 but mom_30m >= -0.05: compound rule is not satisfied."""
        sig = _make_neutral_signal(trend_ph=-0.08, mom_30m=-0.02)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_downtrend_only_momentum_below_threshold_accepted(self):
        """mom_30m < -0.05 but trend_ph >= -0.05: compound rule is not satisfied."""
        sig = _make_neutral_signal(trend_ph=-0.02, mom_30m=-0.08)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_downtrend_boundary_values_not_rejected(self):
        """Values exactly at -0.05 do not satisfy the strict less-than comparison."""
        # Case 1: trend exactly at boundary, momentum below
        sig1 = _make_neutral_signal(trend_ph=-0.050, mom_30m=-0.06)
        ok1, reason1 = copybot._chart_entry_ok(sig1, entry=0.50)
        self.assertTrue(ok1)
        self.assertEqual(reason1, "")

        # Case 2: trend below, momentum exactly at boundary
        sig2 = _make_neutral_signal(trend_ph=-0.06, mom_30m=-0.050)
        ok2, reason2 = copybot._chart_entry_ok(sig2, entry=0.50)
        self.assertTrue(ok2)
        self.assertEqual(reason2, "")

        # Case 3: both exactly at boundary
        sig3 = _make_neutral_signal(trend_ph=-0.050, mom_30m=-0.050)
        ok3, reason3 = copybot._chart_entry_ok(sig3, entry=0.50)
        self.assertTrue(ok3)
        self.assertEqual(reason3, "")

    # --- D. Volume Drying Rule (vol_ratio < 0.3 AND buy_pressure < 0.35) ---

    def test_volume_drying_both_conditions_met_rejected(self):
        """vol_ratio < 0.3 and buy_pressure < 0.35: rejected for vol_drying."""
        sig = _make_neutral_signal(vol_ratio=0.20, buy_pressure=0.25)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("vol_drying", reason)
        self.assertEqual(reason, "vol_drying(ratio=0.2,buy=0.25)")

    def test_volume_drying_only_ratio_below_threshold_accepted(self):
        """vol_ratio < 0.3 but buy_pressure >= 0.35: not rejected."""
        sig = _make_neutral_signal(vol_ratio=0.20, buy_pressure=0.40)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_volume_drying_only_buy_pressure_below_threshold_accepted(self):
        """buy_pressure < 0.35 but vol_ratio >= 0.3: not rejected."""
        sig = _make_neutral_signal(vol_ratio=0.50, buy_pressure=0.25)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_volume_drying_boundary_values_not_rejected(self):
        """Values exactly at 0.3 or 0.35 do not satisfy strict less-than comparison."""
        # Case 1: ratio at boundary, buy_pressure below
        sig1 = _make_neutral_signal(vol_ratio=0.30, buy_pressure=0.25)
        ok1, reason1 = copybot._chart_entry_ok(sig1, entry=0.50)
        self.assertTrue(ok1)
        self.assertEqual(reason1, "")

        # Case 2: ratio below, buy_pressure at boundary
        sig2 = _make_neutral_signal(vol_ratio=0.20, buy_pressure=0.35)
        ok2, reason2 = copybot._chart_entry_ok(sig2, entry=0.50)
        self.assertTrue(ok2)
        self.assertEqual(reason2, "")

        # Case 3: both exactly at boundary
        sig3 = _make_neutral_signal(vol_ratio=0.30, buy_pressure=0.35)
        ok3, reason3 = copybot._chart_entry_ok(sig3, entry=0.50)
        self.assertTrue(ok3)
        self.assertEqual(reason3, "")

    # --- Rule Priority Check ---

    def test_rule_priority_rsi_evaluated_before_chase(self):
        """When multiple signals trigger, the first evaluated rule reports the rejection."""
        sig = _make_neutral_signal(rsi=85.0, mom_30m=0.20)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "overbought_rsi(85)")


class TestModelEMissingDataBehavior(unittest.TestCase):
    """Verify missing-data, short-history, and fallback behavior in Model E."""

    def setUp(self):
        self.patcher = mock.patch(
            "urllib.request.urlopen",
            side_effect=RuntimeError("External network call forbidden in unit tests"),
        )
        self.mock_urlopen = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_none_signal_fails_closed(self):
        """Phase 6: when sig is None, _chart_entry_ok returns (False, 'insufficient_price_history')."""
        ok, reason = copybot._chart_entry_ok(None, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_price_history")

    def test_empty_signal_fails_closed(self):
        """Phase 6: empty dict signal returns (False, 'insufficient_price_history') (fail-closed)."""
        ok, reason = copybot._chart_entry_ok({}, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "insufficient_price_history")

    def test_chart_signal_returns_none_when_price_history_api_fails(self):
        """When CLOB prices-history endpoint raises an exception, chart_signal() returns None."""
        with mock.patch("copybot.get", side_effect=Exception("CLOB 503 Service Unavailable")):
            sig = copybot.chart_signal("tok_error")
            self.assertIsNone(sig)
            # Phase 6: None signal is now fail-closed
            ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
            self.assertFalse(ok)
            self.assertEqual(reason, "insufficient_price_history")

    def test_chart_signal_returns_none_when_history_too_short(self):
        """When CLOB returns fewer than 20 bars, chart_signal() returns None (<20 bars)."""
        short_history = [{"t": 1000 + i * 60, "p": 0.50} for i in range(15)]
        with mock.patch("copybot.get", return_value={"history": short_history}):
            sig = copybot.chart_signal("tok_short")
            self.assertIsNone(sig)
            # Phase 6: None (short history) is now fail-closed
            ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
            self.assertFalse(ok)
            self.assertEqual(reason, "insufficient_price_history")

    def test_chart_signal_computes_indicators_with_20_bars(self):
        """With exactly 20 bars (minimum threshold), chart_signal() produces valid price indicators."""
        bars = [{"t": 1000 + i * 60, "p": 0.50 + i * 0.001} for i in range(20)]

        def mock_get(url):
            if "/prices-history" in url:
                return {"history": bars}
            if "/trades" in url:
                return []
            raise AssertionError(f"Unexpected URL: {url}")

        with mock.patch("copybot.get", side_effect=mock_get):
            sig = copybot.chart_signal("tok_20bars")
            self.assertIsNotNone(sig)
            self.assertEqual(sig["bars"], 20)
            self.assertIn("rsi", sig)
            self.assertIn("mom_30m", sig)
            self.assertIn("trend_ph", sig)

    def test_volume_data_missing_keeps_price_filters_active(self):
        """
        When volume trades API returns no trades or fails, volume keys are omitted,
        the volume gate is bypassed, and price filters (RSI, chase) remain fully active.
        """
        bars = [{"t": 1000 + i * 60, "p": 0.50} for i in range(30)]

        def mock_get(url):
            if "/prices-history" in url:
                return {"history": bars}
            if "/trades" in url:
                raise Exception("data-api timeout")
            raise AssertionError(f"Unexpected URL: {url}")

        with mock.patch("copybot.get", side_effect=mock_get):
            sig = copybot.chart_signal("tok_no_vol")
            self.assertIsNotNone(sig)
            # Volume keys are absent
            self.assertNotIn("vol_ratio", sig)
            self.assertNotIn("buy_pressure", sig)
            self.assertNotIn("vol_1h", sig)

            # Price signals are present
            self.assertIn("rsi", sig)
            self.assertEqual(sig["rsi"], 50.0)

            # Filter accepts neutral price signal despite missing volume
            ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
            self.assertTrue(ok)
            self.assertEqual(reason, "")

    def test_volume_missing_still_rejects_on_price_signals(self):
        """Even when volume keys are None/omitted, an overbought RSI still rejects."""
        sig_no_vol = _make_neutral_signal(rsi=80.0)
        sig_no_vol.pop("vol_ratio", None)
        sig_no_vol.pop("buy_pressure", None)

        ok, reason = copybot._chart_entry_ok(sig_no_vol, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "overbought_rsi(80)")


class TestModelEPhase6FailClosed(unittest.TestCase):
    """Phase 6 tests: malformed candle data, error sentinel, and fail-closed behavior."""

    def setUp(self):
        self.patcher = mock.patch(
            "urllib.request.urlopen",
            side_effect=RuntimeError("External network call forbidden in unit tests"),
        )
        self.mock_urlopen = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_malformed_candle_returns_error_sentinel_not_exception(self):
        """chart_signal() with a non-numeric 'p' field must NOT raise; returns error sentinel dict."""
        bad_history = [{"t": 1000 + i * 60, "p": "not_a_number"} for i in range(25)]
        with mock.patch("copybot.get", return_value={"history": bad_history}):
            # Must not raise; the defensive try/except must absorb ValueError
            sig = copybot.chart_signal("tok_malformed")
        self.assertIsNotNone(sig, "chart_signal() must return a sentinel, not None, on parse error")
        self.assertIsInstance(sig, dict)
        self.assertEqual(sig.get("_error"), "invalid_candle_data")
        self.assertIn("bars", sig)

    def test_error_sentinel_fails_closed(self):
        """_chart_entry_ok with an error sentinel returns (False, 'invalid_candle_data')."""
        sentinel = {"_error": "invalid_candle_data", "bars": 25}
        ok, reason = copybot._chart_entry_ok(sentinel, entry=0.50)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_candle_data")

    def test_malformed_candle_downstream_fails_closed(self):
        """End-to-end: malformed candles → sentinel → _chart_entry_ok rejects entry."""
        bad_history = [{"t": 1000 + i * 60, "p": None} for i in range(25)]
        with mock.patch("copybot.get", return_value={"history": bad_history}):
            sig = copybot.chart_signal("tok_none_p")
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("invalid_candle_data", reason)

    def test_missing_p_key_in_candle_fails_closed(self):
        """Candles missing the 'p' key entirely trigger the defensive guard (KeyError)."""
        bad_history = [{"t": 1000 + i * 60} for i in range(25)]  # no 'p' key
        with mock.patch("copybot.get", return_value={"history": bad_history}):
            sig = copybot.chart_signal("tok_no_key")
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertFalse(ok)
        self.assertIn("invalid_candle_data", reason)


class TestModelEChartFilterLogPersistence(unittest.TestCase):
    """Phase 6 tests: chart_filter_log DB persistence and schema idempotency."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._orig_db = copybot.DB
        copybot.DB = self._tmp.name
        self.patcher = mock.patch(
            "urllib.request.urlopen",
            side_effect=RuntimeError("External network call forbidden in unit tests"),
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        copybot.DB = self._orig_db
        import os
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _get_db(self):
        return copybot.db()

    def test_schema_creates_chart_filter_log_table(self):
        """db() must create chart_filter_log idempotently."""
        c = self._get_db()
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("chart_filter_log", tables)

    def test_schema_idempotent_second_call_does_not_raise(self):
        """Calling db() a second time on an existing DB must not raise any exception."""
        self._get_db()
        # Second call must not raise
        c2 = self._get_db()
        tables = {r[0] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("chart_filter_log", tables)

    def test_log_chart_filter_event_accepted_row(self):
        """_log_chart_filter_event with ok=True writes decision=1 to chart_filter_log."""
        c = self._get_db()
        sig = _make_neutral_signal()
        copybot._log_chart_filter_event(c, "tok_accepted", True, "", sig)
        c.commit()
        row = c.execute("SELECT decision, reason, price_data_ok, volume_data_ok FROM chart_filter_log").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)   # decision = accepted
        self.assertEqual(row[1], "")  # no rejection reason
        self.assertEqual(row[2], 1)   # price_data_ok
        self.assertEqual(row[3], 1)   # volume_data_ok (neutral signal has vol keys)

    def test_log_chart_filter_event_rejected_row(self):
        """_log_chart_filter_event with ok=False writes decision=0 and reason to chart_filter_log."""
        c = self._get_db()
        copybot._log_chart_filter_event(c, "tok_rejected", False, "insufficient_price_history", None)
        c.commit()
        row = c.execute("SELECT decision, reason, price_data_ok, volume_data_ok FROM chart_filter_log").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 0)                           # decision = rejected
        self.assertEqual(row[1], "insufficient_price_history")
        self.assertEqual(row[2], 0)                           # price_data_ok = 0 (sig is None)
        self.assertEqual(row[3], 0)                           # volume_data_ok = 0

    def test_price_data_ok_flag_for_error_sentinel(self):
        """Error sentinel (malformed candles) sets price_data_ok=0 in the log row."""
        c = self._get_db()
        sentinel = {"_error": "invalid_candle_data", "bars": 25}
        copybot._log_chart_filter_event(c, "tok_sentinel", False, "invalid_candle_data", sentinel)
        c.commit()
        row = c.execute("SELECT price_data_ok FROM chart_filter_log").fetchone()
        self.assertEqual(row[0], 0)

    def test_volume_data_ok_flag_when_volume_absent(self):
        """Signal without vol_ratio/buy_pressure sets volume_data_ok=0 in the log row."""
        c = self._get_db()
        sig_no_vol = _make_neutral_signal()
        sig_no_vol.pop("vol_ratio", None)
        sig_no_vol.pop("buy_pressure", None)
        copybot._log_chart_filter_event(c, "tok_no_vol", True, "", sig_no_vol)
        c.commit()
        row = c.execute("SELECT volume_data_ok FROM chart_filter_log").fetchone()
        self.assertEqual(row[0], 0)

    def test_multiple_log_rows_accumulate(self):
        """Multiple calls to _log_chart_filter_event accumulate separate rows."""
        c = self._get_db()
        sig = _make_neutral_signal()
        copybot._log_chart_filter_event(c, "tok_1", True, "", sig)
        copybot._log_chart_filter_event(c, "tok_2", False, "overbought_rsi(80)", sig)
        copybot._log_chart_filter_event(c, "tok_3", False, "insufficient_price_history", None)
        c.commit()
        count = c.execute("SELECT COUNT(*) FROM chart_filter_log").fetchone()[0]
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
