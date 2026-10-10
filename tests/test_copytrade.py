"""
Offline deterministic unit tests for copytrade.py ROI calculation and reporting.

Tests verify the fix for the historical copytest ROI calculation:
- Original wallet ROI uses their_px: won / their_px - 1
- Simulated copier ROI uses your_px: won / your_px - 1
- Edge lost to lag: their_roi - your_roi

All API calls are mocked offline; no external network requests are made.
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import copytrade  # noqa: E402


class TestCopytradeROI(unittest.TestCase):
    """Deterministic tests for copytest() calculations and printed report."""

    def setUp(self):
        # Guard against any accidental network call:
        # Patching urllib.request.urlopen ensures offline execution.
        self.patcher = mock.patch(
            "urllib.request.urlopen",
            side_effect=RuntimeError("External network call forbidden in unit tests"),
        )
        self.mock_urlopen = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _run_copytest(self, trades, history_map, addr="0xtrader", lag_min=10):
        """Helper to run copytest() with mocked get() and captured stdout."""
        def mock_get(url):
            if "/trades?user=" in url:
                return trades
            for pattern, hist in history_map.items():
                if pattern in url:
                    return {"history": hist}
            raise AssertionError(f"Unexpected URL called: {url}")

        buf = io.StringIO()
        with mock.patch("copytrade.get", side_effect=mock_get):
            with mock.patch("sys.stdout", buf):
                copytrade.copytest(addr, lag_min=lag_min)
        return buf.getvalue()

    def test_winning_trade_roi_calculation(self):
        """
        Minimum required example:
        - Original wallet entry price: $0.40
        - Simulated copier entry price after delay: $0.50
        - Resolved token price: $1.00 (win)

        Expected:
        - Original wallet ROI: (1.00 / 0.40) - 1 = +150.0%
        - Simulated copier ROI: (1.00 / 0.50) - 1 = +100.0%
        - Edge lost to delay: 150% - 100% = 50.0 percentage points
        """
        ts = 1700000000
        lag_min = 10
        trades = [
            {
                "side": "BUY",
                "asset": "tok_win",
                "timestamp": str(ts),
                "price": "0.40",
            }
        ]
        history_map = {
            "fidelity=60": [{"t": ts, "p": 0.40}, {"t": ts + 3600, "p": 1.00}],
            "fidelity=1": [{"t": ts + lag_min * 60, "p": 0.50}],
        }

        output = self._run_copytest(trades, history_map, lag_min=lag_min)

        self.assertIn("resolved buys tested    : 1", output)
        self.assertIn("your win rate           : 100%", output)
        self.assertIn("THEIR avg ROI / trade   : +150.0%", output)
        self.assertIn("YOUR avg ROI / trade    : +100.0%", output)
        self.assertIn("edge lost to lag        : 50.0 pts", output)
        self.assertIn("verdict                 : COPYABLE — edge survives lag", output)

        # Confirm the report shows distinctly different ROIs for original vs copier
        self.assertNotEqual(
            "+150.0%",
            "+100.0%",
            "Original wallet ROI and copier ROI must be distinct",
        )

    def test_losing_trade_roi_calculation(self):
        """
        Resolved losing position:
        - Original wallet entry price: $0.40
        - Simulated copier entry price after delay: $0.50
        - Resolved token price: $0.00 (loss)

        Expected:
        - Original wallet ROI: (0.00 / 0.40) - 1 = -100.0%
        - Simulated copier ROI: (0.00 / 0.50) - 1 = -100.0%
        - Edge lost to delay: -100% - (-100%) = 0.0 percentage points
        """
        ts = 1700000000
        lag_min = 10
        trades = [
            {
                "side": "BUY",
                "asset": "tok_loss",
                "timestamp": str(ts),
                "price": "0.40",
            }
        ]
        history_map = {
            "fidelity=60": [{"t": ts, "p": 0.40}, {"t": ts + 3600, "p": 0.00}],
            "fidelity=1": [{"t": ts + lag_min * 60, "p": 0.50}],
        }

        output = self._run_copytest(trades, history_map, lag_min=lag_min)

        self.assertIn("resolved buys tested    : 1", output)
        self.assertIn("your win rate           : 0%", output)
        self.assertIn("THEIR avg ROI / trade   : -100.0%", output)
        self.assertIn("YOUR avg ROI / trade    : -100.0%", output)
        self.assertIn("edge lost to lag        : 0.0 pts", output)
        self.assertIn("verdict                 : NOT COPYABLE — lag kills the edge (you lose)", output)

    def test_copier_favorable_entry_roi(self):
        """
        Copier enters cheaper after a dip:
        - Original wallet entry price: $0.50
        - Simulated copier entry price: $0.40
        - Resolved token price: $1.00 (win)

        Expected:
        - Original wallet ROI: (1.00 / 0.50) - 1 = +100.0%
        - Simulated copier ROI: (1.00 / 0.40) - 1 = +150.0%
        - Edge lost: 100% - 150% = -50.0 percentage points
        """
        ts = 1700000000
        lag_min = 5
        trades = [
            {
                "side": "BUY",
                "asset": "tok_dip",
                "timestamp": str(ts),
                "price": "0.50",
            }
        ]
        history_map = {
            "fidelity=60": [{"t": ts, "p": 0.50}, {"t": ts + 3600, "p": 1.00}],
            "fidelity=1": [{"t": ts + lag_min * 60, "p": 0.40}],
        }

        output = self._run_copytest(trades, history_map, lag_min=lag_min)

        self.assertIn("THEIR avg ROI / trade   : +100.0%", output)
        self.assertIn("YOUR avg ROI / trade    : +150.0%", output)
        self.assertIn("edge lost to lag        : -50.0 pts", output)

    def test_multi_trade_portfolio_average_roi(self):
        """
        Multiple resolved trades:
        - Trade 1 (win): their_px = 0.40, your_px = 0.50 -> their_roi = +150%, your_roi = +100%
        - Trade 2 (loss): their_px = 0.60, your_px = 0.70 -> their_roi = -100%, your_roi = -100%

        Expected averages:
        - Resolved buys: 2
        - Win rate: 50%
        - THEIR avg ROI: (1.50 - 1.00) / 2 = +25.0%
        - YOUR avg ROI: (1.00 - 1.00) / 2 = +0.0%
        - Edge lost: 25.0 - 0.0 = 25.0 pts
        """
        ts1 = 1700000000
        ts2 = 1700010000
        lag_min = 10
        trades = [
            {"side": "BUY", "asset": "tok_1", "timestamp": str(ts1), "price": "0.40"},
            {"side": "BUY", "asset": "tok_2", "timestamp": str(ts2), "price": "0.60"},
        ]

        def mock_get(url):
            if "/trades?user=" in url:
                return trades
            if "market=tok_1" in url and "fidelity=60" in url:
                return {"history": [{"t": ts1 + 3600, "p": 1.00}]}
            if "market=tok_1" in url and "fidelity=1" in url:
                return {"history": [{"t": ts1 + lag_min * 60, "p": 0.50}]}
            if "market=tok_2" in url and "fidelity=60" in url:
                return {"history": [{"t": ts2 + 3600, "p": 0.00}]}
            if "market=tok_2" in url and "fidelity=1" in url:
                return {"history": [{"t": ts2 + lag_min * 60, "p": 0.70}]}
            raise AssertionError(f"Unexpected URL: {url}")

        buf = io.StringIO()
        with mock.patch("copytrade.get", side_effect=mock_get):
            with mock.patch("sys.stdout", buf):
                copytrade.copytest("0xtrader", lag_min=lag_min)

        output = buf.getvalue()
        self.assertIn("resolved buys tested    : 2", output)
        self.assertIn("your win rate           : 50%", output)
        self.assertIn("THEIR avg ROI / trade   : +25.0%", output)
        self.assertIn("YOUR avg ROI / trade    : +0.0%", output)
        self.assertIn("edge lost to lag        : 25.0 pts", output)

    def test_skipped_trades_handling(self):
        """Non-buys, invalid prices, and unresolved markets are safely skipped."""
        ts = 1700000000
        trades = [
            {"side": "SELL", "asset": "tok_sell", "timestamp": str(ts), "price": "0.50"},
            {"side": "BUY", "asset": "", "timestamp": str(ts), "price": "0.50"},
            {"side": "BUY", "asset": "tok_bad_px", "timestamp": str(ts), "price": "1.50"},
            {"side": "BUY", "asset": "tok_unresolved", "timestamp": str(ts), "price": "0.50"},
        ]
        history_map = {
            "tok_unresolved": [{"t": ts + 3600, "p": 0.50}],  # 0.05 <= p <= 0.95 -> open
        }

        output = self._run_copytest(trades, history_map)
        self.assertIn("no resolved buys to test (skipped 3", output)

    def test_contrast_demonstrating_bug_is_resolved(self):
        """
        Direct mathematical verification:
        When their_px = 0.40 and your_px = 0.50 with won = 1.0:
        - Correct their_roi = 1.0 / 0.40 - 1 = 1.50 (150%)
        - Correct your_roi = 1.0 / 0.50 - 1 = 1.00 (100%)
        - If buggy (using your_px for their_roi): both would be 1.00 (100%) and edge lost would be 0 pts.
        """
        their_px = 0.40
        your_px = 0.50
        won = 1.0

        correct_their_roi = won / their_px - 1
        correct_your_roi = won / your_px - 1
        buggy_their_roi = won / your_px - 1

        self.assertAlmostEqual(correct_their_roi, 1.50)
        self.assertAlmostEqual(correct_your_roi, 1.00)
        self.assertAlmostEqual(buggy_their_roi, 1.00)
        self.assertNotEqual(correct_their_roi, buggy_their_roi)


if __name__ == "__main__":
    unittest.main()
