"""
Offline deterministic tests for the Data API **v2** migration of the copy bot.

The v1 Data API (`/activity`, `/trades`, `/value`) was retired on 2026-11-02 and answers
HTTP 410 for every request, which is why every tracked wallet stopped producing copies.
These tests pin the replacement behaviour to the documented v2 contract:

  * `{"data": ..., "pagination": {...}}` envelope unwrapping
  * snake_case -> internal trade schema mapping (`token_id` -> asset, ...)
  * cursor pagination via `pagination.next_cursor` (no `offset`, ever)
  * all 20 tracked wallets are fetched, each with its own attribution
  * duplicate prevention (same tx+asset+ts is copied once, per model)
  * existing databases stay readable and historical rows stay untouched
  * models A-E all ingest an accepted trade
  * Model E price history still works, its volume feed moved to /v2/trades
  * HTTP 410 / 429 / 503 / 400 handling: failures are reported, never turned into
    "zero new trades"

No network access: `urllib.request.urlopen` is replaced by an in-process router, so the
real transport code (retries, headers, error parsing) is exercised without sockets.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import copybot  # noqa: E402


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------
class FakeResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` returns."""

    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def http_error(status, payload=None, headers=None):
    """Build the `urllib.error.HTTPError` that urlopen raises for a non-2xx response."""
    body = json.dumps(payload if payload is not None else {}).encode()
    return urllib.error.HTTPError(
        "https://data-api.polymarket.com/v2/test", status, "error",
        headers if headers is not None else {}, io.BytesIO(body))


def v2_page(items, has_more=False, next_cursor=None, limit=None):
    """A v2 page envelope, exactly as documented: {data, pagination}."""
    return {
        "data": items,
        "pagination": {"limit": limit if limit is not None else len(items),
                       "offset": 0, "has_more": has_more,
                       "next_cursor": next_cursor},
    }


GW = "0x0346afae2603313d2bbee96b628536c8cbe352a5"   # GoalLineGhost, a tracked wallet


def activity_record(**overrides):
    """A realistic v2 /activity TRADE record (BUY by default)."""
    rec = {
        "proxy_wallet": "0x0346afae2603313d2bbee96b628536c8cbe352a5",
        "timestamp": 1793879917,
        "condition_id": "0x481054b4079aed2237c4646374acf2044212223577e134644f241e06e9682d71",
        "type": "TRADE",
        "size": 10.0,
        "usdc_size": 8.2,
        "transaction_hash": "0x5ac1",
        "price": 0.82,
        "token_id": "79567222865591698100263706303217362724208613218513864414450534154310015463102",
        "side": "BUY",
        "outcome_index": 0,
        "title": "Will SpaceX land Starship on the Moon in 2026?",
        "slug": "btc-updown-15m-1793879100",
        "outcome": "Up",
        "name": "GoalLineGhost",
        "pseudonym": "Supportive-Matchmaker",
    }
    rec.update(overrides)
    return rec


class Router:
    """Deterministic URL router: each entry is a callable returning a FakeResponse or
    raising an HTTPError; every request URL is recorded for assertions."""

    def __init__(self, handler):
        self.handler = handler
        self.urls = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", req)
        self.urls.append(url)
        result = self.handler(url)
        if isinstance(result, FakeResponse):
            return result
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


# ---------------------------------------------------------------------------
# 1-2. Envelope parsing + snake_case mapping
# ---------------------------------------------------------------------------
class EnvelopeAndMappingTests(unittest.TestCase):
    def test_unwrap_v2_page_accepts_list_null_and_empty(self):
        items, pag = copybot.unwrap_v2_page({"data": [{"a": 1}],
                                             "pagination": {"has_more": True,
                                                            "next_cursor": "c1"}})
        self.assertEqual(items, [{"a": 1}])
        self.assertEqual(pag["next_cursor"], "c1")
        self.assertEqual(copybot.unwrap_v2_page({"data": None})[0], [])
        self.assertEqual(copybot.unwrap_v2_page({"data": []})[0], [])
        self.assertEqual(copybot.unwrap_v2_page({"data": []})[1], {})

    def test_unwrap_v2_page_rejects_wrong_shapes(self):
        for bad, needle in [({"pagination": {}}, "no 'data'"),
                            ({"data": {"value": 1}}, "not"),  # documented, not a list
                            (["not", "an", "object"], "not a JSON object")]:
            with self.assertRaises(copybot.DataApiError) as ctx:
                copybot.unwrap_v2_page(bad)
            self.assertIn("data" if "object" not in needle else "object", str(ctx.exception))

    def test_map_v2_trade_snake_case_to_internal_schema(self):
        trade, reason = copybot.map_v2_trade(
            activity_record(), "GoalLineGhost", "0x0346afae2603313d2bbee96b628536c8cbe352a5")
        self.assertIsNone(reason)
        self.assertEqual(trade["ts"], 1793879917)
        self.assertEqual(trade["tx"], "0x5ac1")
        self.assertEqual(trade["condition_id"],
                         "0x481054b4079aed2237c4646374acf2044212223577e134644f241e06e9682d71")
        self.assertEqual(trade["asset"],
                         "79567222865591698100263706303217362724208613218513864414450534154310015463102")
        self.assertEqual(trade["outcome_index"], 0)
        self.assertAlmostEqual(trade["price"], 0.82)
        self.assertEqual(trade["side"], "BUY")
        self.assertEqual(trade["type"], "TRADE")
        self.assertEqual(trade["title"], "Will SpaceX land Starship on the Moon in 2026?")
        self.assertEqual(trade["outcome"], "Up")
        # Attribution is carried explicitly, never inferred from the feed's display name.
        self.assertEqual(trade["wallet_name"], "GoalLineGhost")
        self.assertEqual(trade["wallet_addr"], "0x0346afae2603313d2bbee96b628536c8cbe352a5")
        # The base key the copy is deduped on keeps its historical shape.
        self.assertEqual(f"{trade['tx']}:{trade['asset']}:{trade['ts']}",
                         "0x5ac1:79567222865591698100263706303217362724208613218513864414450534154310015463102:1793879917")

    def test_map_v2_trade_rejects_unsafe_and_incomplete_records(self):
        addr = "0x0346afae2603313d2bbee96b628536c8cbe352a5"
        cases = [
            (activity_record(side="SELL"), "not_a_buy"),
            (activity_record(type="REDEEM"), "not_a_trade"),
            (activity_record(transaction_hash=""), "missing_transaction_hash"),
            (activity_record(transaction_hash=None), "missing_transaction_hash"),
            (activity_record(token_id=""), "missing_token_id"),
            (activity_record(condition_id=None), "missing_condition_id"),
            (activity_record(timestamp="not-a-number"), "bad_timestamp"),
            (activity_record(price="free"), "bad_price"),
            (activity_record(price=0), "price_out_of_range"),
            (activity_record(price=1.0), "price_out_of_range"),
            (activity_record(outcome_index=None), "missing_outcome_index"),
            # 999 is the documented "outcome could not be labeled" sentinel
            (activity_record(outcome_index=999), "outcome_unlabelled"),
            (activity_record(outcome_index=7), "bad_outcome_index"),
            (activity_record(proxy_wallet="0x" + "9" * 40), "wallet_mismatch"),
            ("not-a-dict", "not_an_object"),
        ]
        for rec, expected in cases:
            trade, reason = copybot.map_v2_trade(rec, "GoalLineGhost", addr)
            self.assertIsNone(trade, expected)
            self.assertEqual(reason, expected)


# A REAL recorded response, captured from
# GET https://data-api.polymarket.com/v2/activity?user=<Polkadot-Frog>&type=TRADE
#   &side=BUY&start=<now-1h>&limit=2&sort_by=TIMESTAMP&sort_direction=DESC
# on 2026-11-05. It is the ground truth for the field names this migration maps: it is
# not a hand-written guess at the schema.
REAL_CAPTURED_ACTIVITY = {
    "data": [
        {"proxy_wallet": "0x9d57c42e847173d06841703825d3fe2299e456ea",
         "timestamp": 1793879944,
         "condition_id": "0xd26f049f27b9355e032e6af81840b12bb3769fd4512336877410efe3b45d1472",
         "type": "TRADE", "size": 5.0, "usdc_size": 1.55,
         "transaction_hash": "0xe90f2ba99b906a757c4edbb8c1b35ac497fa245732545235ad5c0cd4c11eeb5e",
         "price": 0.31,
         "token_id": "109699131627819158903034620778544344578618053628318090403723913154245766455370",
         "side": "BUY", "outcome_index": 1,
         "title": "Ethereum Up or Down - November 5, 6:55AM-7:00AM ET",
         "slug": "eth-updown-5m-1793879700",
         "icon": "https://polymarket-upload.s3.us-east-2.amazonaws.com/ETH+fullsize.jpg",
         "event_slug": "eth-updown-5m-1793879700", "outcome": "Down",
         "name": "Polkadot-Frog", "pseudonym": "Supportive-Matchmaker", "bio": "",
         "profile_image": "", "profile_image_optimized": ""},
        {"proxy_wallet": "0x9d57c42e847173d06841703825d3fe2299e456ea",
         "timestamp": 1793879941,
         "condition_id": "0x99618054554133fe538aabd3d02d4a9a428e3b077556c608ac52513ad55e8123",
         "type": "TRADE", "size": 20.0, "usdc_size": 2.4,
         "transaction_hash": "0xa851d54e8ed5e41e41f7586035256b7b9f934476e59dafe7c0e71f059c49cec0",
         "price": 0.12,
         "token_id": "29966812169779106041891229178642896858601560560629629975237653975670221986842",
         "side": "BUY", "outcome_index": 1,
         "title": "Bitcoin Up or Down - November 5, 6:55AM-7:00AM ET",
         "slug": "btc-updown-5m-1793879700",
         "icon": "https://polymarket-upload.s3.us-east-2.amazonaws.com/BTC+fullsize.png",
         "event_slug": "btc-updown-5m-1793879700", "outcome": "Down",
         "name": "Polkadot-Frog", "pseudonym": "Supportive-Matchmaker", "bio": "",
         "profile_image": "", "profile_image_optimized": ""},
    ],
    "pagination": {"limit": 2, "offset": 0, "has_more": True,
                   "next_cursor": "eyJkYXRhIjp7InR5cGUiOiJhY3Rpdml0eSIsInBhcmFtcyI6eyJsIjoyLCJ0cyI6MTc5Mzg3OTk0MSwic3EiOiI5Njc4Mjk5MDAwMDI5MDMxODAiLCJkIjoyfX0sInNpZyI6IjRiODU3NTBiMjUyMzVkNTE2YTI3NmRiMjA1NTRhZWM5In0"},
}


# ---------------------------------------------------------------------------
# 3-4. Transport: query shape, cursor pagination, errors and retries
# ---------------------------------------------------------------------------
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = mock.patch.object(copybot.time, "sleep", return_value=None)
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def _run(self, handler, addr=GW):
        router = Router(handler)
        with mock.patch("urllib.request.urlopen", side_effect=router):
            result = copybot.fetch_wallet_activity(addr, 1793000000, wallet_name="w")
        return result, router.urls

    def test_query_shape_uses_v2_activity_without_offset(self):
        result, urls = self._run(lambda url: v2_page([activity_record()]))
        self.assertEqual(len(urls), 1)
        url = urls[0]
        self.assertTrue(url.startswith("https://data-api.polymarket.com/v2/activity?"))
        self.assertIn("user=" + GW, url)
        self.assertIn("type=TRADE", url)
        self.assertIn("side=BUY", url)
        self.assertIn("limit=%d" % copybot.ACTIVITY_PAGE_LIMIT, url)
        self.assertIn("sort_by=TIMESTAMP", url)
        self.assertIn("sort_direction=DESC", url)
        self.assertIn("start=1793000000", url)
        self.assertNotIn("offset", url)
        # Full-history/1e9 sentinels are not used: the window is explicit.
        self.assertNotIn("start=1&", url)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["complete"])
        self.assertFalse(result["truncated"])

    def test_cursor_pagination_follows_next_cursor_and_keeps_filters(self):
        pages = [v2_page([activity_record(transaction_hash="0xa")], True, "CUR1"),
                 v2_page([activity_record(transaction_hash="0xb")], True, "CUR2"),
                 v2_page([activity_record(transaction_hash="0xc")], False, None)]

        def handler(url):
            if "cursor=CUR1" in url:
                return pages[1]
            if "cursor=CUR2" in url:
                return pages[2]
            return pages[0]

        result, urls = self._run(handler)
        self.assertEqual([t["tx"] for t in result["trades"]], ["0xa", "0xb", "0xc"])
        self.assertEqual(result["pages"], 3)
        self.assertTrue(result["complete"])
        self.assertNotIn("cursor", urls[0])
        self.assertIn("cursor=CUR1", urls[1])
        self.assertIn("cursor=CUR2", urls[2])
        # v2 re-anchors silently if a filter changes mid-walk: every page must repeat them.
        for url in urls:
            for param in ("user=" + GW, "type=TRADE", "side=BUY", "sort_by=TIMESTAMP",
                          "sort_direction=DESC", "start=1793000000"):
                self.assertIn(param, url)

    def test_cursor_loop_is_an_error_not_an_infinite_walk(self):
        loop = lambda url: v2_page([activity_record()], True, "SAME")  # noqa: E731
        with self.assertRaises(copybot.DataApiError) as ctx:
            self._run(loop)
        self.assertIn("cursor loop", str(ctx.exception))

    def test_page_cap_truncation_is_reported(self):
        pages = [v2_page([activity_record()], True, "C%d" % i) for i in range(10)]
        state = {"i": 0}

        def handler(url):
            page = pages[state["i"]]
            state["i"] += 1
            return page

        router = Router(handler)
        with mock.patch("urllib.request.urlopen", side_effect=router):
            result = copybot.fetch_wallet_activity(GW, 1793000000, max_pages=2)
        self.assertEqual(result["pages"], 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(len(router.urls), 2)

    def test_http_410_raises_and_is_never_an_empty_result(self):
        def handler(url):
            return http_error(410, {"error": "Data API v1 has been retired",
                                    "code": "gone", "retryable": False,
                                    "trace_id": "t-1"})

        with self.assertRaises(copybot.DataApiError) as ctx:
            self._run(handler)
        self.assertEqual(ctx.exception.status, 410)
        self.assertEqual(ctx.exception.code, "gone")
        self.assertEqual(ctx.exception.trace_id, "t-1")
        self.assertFalse(ctx.exception.is_retryable())
        self.assertEqual(self.sleep.call_count, 0)   # a 410 is never retried

    def test_400_is_not_retried(self):
        calls = {"n": 0}

        def handler(url):
            calls["n"] += 1
            return http_error(400, {"error": "required query param 'user' not provided",
                                    "code": "invalid_request", "retryable": False})

        with self.assertRaises(copybot.DataApiError):
            self._run(handler)
        self.assertEqual(calls["n"], 1)

    def test_429_honours_retry_after_then_succeeds(self):
        state = {"n": 0}

        def handler(url):
            state["n"] += 1
            if state["n"] == 1:
                return http_error(429, {"error": "busy", "code": "rate_limited",
                                        "retryable": True}, headers={"Retry-After": "7"})
            return v2_page([activity_record()])

        result, urls = self._run(handler)
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(result["trades"]), 1)
        self.sleep.assert_called_once_with(7.0)

    def test_503_retries_then_raises_after_max_attempts(self):
        calls = {"n": 0}

        def handler(url):
            calls["n"] += 1
            return http_error(503, {"error": "request_timeout", "code": "request_timeout",
                                    "retryable": True}, headers={"Retry-After": "1"})

        with self.assertRaises(copybot.DataApiError):
            self._run(handler)
        self.assertEqual(calls["n"], copybot.API_MAX_ATTEMPTS)

    def test_network_error_and_bad_json_are_errors(self):
        with self.assertRaises(copybot.DataApiError):
            self._run(lambda url: urllib.error.URLError("connection reset"))

        class BadJson(FakeResponse):
            def __init__(self):
                super().__init__(b"{not json", 200)

        calls = {"n": 0}

        def handler(url):
            calls["n"] += 1
            return BadJson()

        with self.assertRaises(copybot.DataApiError):
            self._run(handler)
        self.assertEqual(calls["n"], copybot.API_MAX_ATTEMPTS)   # retried, then reported

    def test_envelope_without_data_is_an_error(self):
        with self.assertRaises(copybot.DataApiError):
            self._run(lambda url: {"items": []})


# ---------------------------------------------------------------------------
# 5. All 20 tracked wallets
# ---------------------------------------------------------------------------
class AllWalletsTests(unittest.TestCase):
    """Every one of the 20 tracked wallets is fetched from v2, with its own attribution."""

    def test_all_twenty_wallets_are_fetched_and_attributed(self):
        self.assertEqual(len(copybot.WALLETS), 20, "the tracked wallet list changed")

        requested = []

        def handler(url):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)
            user = q["user"][0]
            requested.append(user)
            return v2_page([activity_record(proxy_wallet=user,
                                            transaction_hash="0xfeed%d" % len(requested))])

        router = Router(handler)
        feeds = {}
        with mock.patch("urllib.request.urlopen", side_effect=router):
            for name, addr in copybot.WALLETS.items():
                feeds[name] = copybot.fetch_wallet_activity(addr, 1793000000, wallet_name=name)

        self.assertEqual(len(requested), 20)
        self.assertEqual(sorted(requested), sorted(copybot.WALLETS.values()))
        self.assertEqual(len(set(requested)), 20)          # no wallet fetched twice
        for name, feed in feeds.items():
            self.assertEqual(len(feed["trades"]), 1)
            trade = feed["trades"][0]
            self.assertEqual(trade["wallet_name"], name)
            self.assertEqual(trade["wallet_addr"], copybot.WALLETS[name])
            self.assertEqual(trade["side"], "BUY")

    def test_a_record_for_the_wrong_wallet_is_rejected(self):
        def handler(url):
            return v2_page([activity_record(proxy_wallet="0x" + "1" * 40)])

        with mock.patch("urllib.request.urlopen", side_effect=Router(handler)):
            feed = copybot.fetch_wallet_activity("0x" + "2" * 40, 1793000000, wallet_name="w")
        self.assertEqual(feed["trades"], [])
        self.assertEqual(feed["rejected"], {"wallet_mismatch": 1})


class RealCapturedPayloadTests(unittest.TestCase):
    """The client is verified against a real recorded v2 body, not a guess at the schema."""

    ADDR = "0x9d57c42e847173d06841703825d3fe2299e456ea"      # Polkadot-Frog

    def test_real_page_parses_and_maps(self):
        calls = []

        def handler(url):
            calls.append(url)
            if len(calls) == 1:
                return REAL_CAPTURED_ACTIVITY
            return {"data": [], "pagination": {"limit": 0, "offset": 0, "has_more": False,
                                               "next_cursor": None}}

        with mock.patch("urllib.request.urlopen", side_effect=Router(handler)), \
                mock.patch.object(copybot.time, "sleep", return_value=None):
            feed = copybot.fetch_wallet_activity(self.ADDR, 1793876300,
                                                 wallet_name="Polkadot-Frog")

        self.assertEqual(len(feed["trades"]), 2)
        self.assertEqual(feed["pages"], 2)
        self.assertTrue(feed["complete"])
        self.assertEqual(feed["rejected"], {})
        first = feed["trades"][0]
        self.assertEqual(first["tx"],
                         "0xe90f2ba99b906a757c4edbb8c1b35ac497fa245732545235ad5c0cd4c11eeb5e")
        self.assertEqual(first["ts"], 1793879944)
        self.assertEqual(first["outcome_index"], 1)
        self.assertAlmostEqual(first["price"], 0.31)
        self.assertEqual(first["condition_id"],
                         "0xd26f049f27b9355e032e6af81840b12bb3769fd4512336877410efe3b45d1472")
        self.assertEqual(first["wallet_addr"], self.ADDR)
        # the real pagination block carries `offset`; the walk must ignore it and follow
        # `next_cursor` instead (page 2 is requested with the cursor from the real body)
        self.assertIn("cursor=" + REAL_CAPTURED_ACTIVITY["pagination"]["next_cursor"][:20],
                      calls[1])
        self.assertNotIn("offset=", "".join(calls))


# ---------------------------------------------------------------------------
# 6-7. tick(): all five models, duplicates, failures, DB compatibility
# ---------------------------------------------------------------------------
def soon(days=1):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def far(days=30):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


NEUTRAL_SIGNAL = {
    "current": 0.50, "trend_ph": 0.00, "rsi": 50.0, "mom_5m": 0.00, "mom_30m": 0.00,
    "price_vol": 0.01, "support": 0.45, "resistance": 0.55, "bars": 60,
    "vol_1h": 1000.0, "vol_15m": 250.0, "vol_ratio": 1.0, "buy_pressure": 0.50,
    "vwap": 0.50,
}
DEEP_BOOK = {"asks": [{"price": 0.50, "size": 10000.0}]}


class TickTests(unittest.TestCase):
    """tick() driven entirely by the fake transport (real client + real DB code)."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self._old_db = copybot.DB
        copybot.DB = self.db_path

        # neutral market data so the only variable under test is ingestion
        self._old_market = copybot._market_by_condition
        self._old_book = copybot._fetch_book
        self._old_price_at = copybot._price_at
        self._old_chart = copybot.chart_signal
        copybot._market_by_condition = lambda cid: {
            "outcomePrices": json.dumps([0.50, 0.50]), "endDate": soon(), "closed": False}
        copybot._fetch_book = lambda token_id: DEEP_BOOK
        copybot._price_at = lambda token_id, ts: None
        copybot.chart_signal = lambda token_id, condition_id=None: NEUTRAL_SIGNAL

    def tearDown(self):
        copybot.DB = self._old_db
        copybot._market_by_condition = self._old_market
        copybot._fetch_book = self._old_book
        copybot._price_at = self._old_price_at
        copybot.chart_signal = self._old_chart
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _tick(self, handler):
        router = Router(handler)
        with mock.patch("urllib.request.urlopen", side_effect=router), \
                mock.patch.object(copybot.time, "sleep", return_value=None):
            summary = copybot.tick()
        return summary, router.urls

    def _one_trade_per_wallet(self, ts, tx_prefix="0xa", outcome_index=0, price=0.50):
        def handler(url):
            from urllib.parse import parse_qs, urlparse
            user = parse_qs(urlparse(url).query)["user"][0]
            return v2_page([activity_record(proxy_wallet=user, timestamp=ts,
                                            transaction_hash="%s%s" % (tx_prefix, user[-6:]),
                                            outcome_index=outcome_index, price=price,
                                            token_id="tok" + user[-6:],
                                            condition_id="cond" + user[-6:])])
        return handler

    def test_all_twenty_wallets_reach_all_five_models(self):
        ts = int(copybot.time.time()) - 10
        summary, urls = self._tick(self._one_trade_per_wallet(ts))
        self.assertEqual(summary["wallets_ok"], 20)
        self.assertEqual(summary["wallets_failed"], 0)
        self.assertEqual(len(urls), 20)
        c = copybot.db()
        total = c.execute("SELECT COUNT(*) FROM copies").fetchone()[0]
        # 20 wallets x 5 models, one accepted trade each
        self.assertEqual(total, 100)
        self.assertEqual(summary["added"], 100)
        models = {r[0] for r in c.execute("SELECT DISTINCT model FROM copies")}
        self.assertEqual(models, {"hold", "exit", "cdecide", "trail", "chart"})
        # every model got exactly one copy per wallet, at one shared simulated fill
        for name, addr in copybot.WALLETS.items():
            rows = c.execute("SELECT model FROM copies WHERE wallet=?", (name,)).fetchall()
            self.assertEqual(len(rows), 5, name)
            price = c.execute("SELECT COUNT(DISTINCT entry) FROM copies WHERE wallet=?",
                              (name,)).fetchone()[0]
            self.assertEqual(price, 1, name)
        c.close()

    def test_rerun_creates_no_duplicates(self):
        ts = int(copybot.time.time()) - 10
        handler = self._one_trade_per_wallet(ts)
        self._tick(handler)
        second, _ = self._tick(handler)
        self.assertEqual(second["added"], 0)
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 100)
        # the per-(key) uniqueness the DB enforces is intact
        dupes = c.execute("SELECT key, COUNT(*) c FROM copies GROUP BY key HAVING c > 1").fetchall()
        self.assertEqual(dupes, [])
        c.close()

    def test_duplicate_records_inside_one_page_are_copied_once(self):
        ts = int(copybot.time.time()) - 10

        def handler(url):
            from urllib.parse import parse_qs, urlparse
            user = parse_qs(urlparse(url).query)["user"][0]
            rec = activity_record(proxy_wallet=user, timestamp=ts,
                                  transaction_hash="0xdup" + user[-4:],
                                  token_id="tok" + user[-4:],
                                  condition_id="cond" + user[-4:])
            return v2_page([rec, dict(rec), dict(rec)])

        summary, _ = self._tick(handler)
        self.assertEqual(summary["added"], 100)      # 20 wallets x 5 models, not 300
        self.assertEqual(summary["skipped_dupes"], 40)

    def test_410_for_every_wallet_is_reported_not_silently_zero(self):
        def handler(url):
            return http_error(410, {"error": "Data API v1 has been retired",
                                    "code": "gone", "retryable": False})

        summary, urls = self._tick(handler)
        self.assertEqual(len(urls), 20)               # it really tried all 20
        self.assertEqual(summary["wallets_ok"], 0)
        self.assertEqual(summary["wallets_failed"], 20)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(len(summary["fetch_failures"]), 20)
        self.assertEqual({reason for _, reason in summary["fetch_failures"]},
                         {"HTTP 410 gone"})
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)
        c.close()

    def test_partial_outage_only_skips_the_failing_wallet(self):
        ts = int(copybot.time.time()) - 10
        failing = list(copybot.WALLETS.values())[0]

        def handler(url):
            from urllib.parse import parse_qs, urlparse
            user = parse_qs(urlparse(url).query)["user"][0]
            if user == failing:
                return http_error(503, {"error": "request_timeout",
                                        "code": "request_timeout", "retryable": True},
                                  headers={"Retry-After": "1"})
            return v2_page([activity_record(proxy_wallet=user, timestamp=ts,
                                            transaction_hash="0xok" + user[-4:],
                                            token_id="tok" + user[-4:],
                                            condition_id="cond" + user[-4:])])

        summary, _ = self._tick(handler)
        self.assertEqual(summary["wallets_ok"], 19)
        self.assertEqual(summary["wallets_failed"], 1)
        self.assertEqual(summary["added"], 95)        # 19 wallets x 5 models
        c = copybot.db()
        self.assertEqual(c.execute("SELECT COUNT(DISTINCT wallet) FROM copies").fetchone()[0], 19)
        c.close()

    def test_stale_trade_is_not_copied(self):
        old_ts = int(copybot.time.time()) - copybot.MAX_TRADE_AGE_SEC - 60
        summary, _ = self._tick(self._one_trade_per_wallet(old_ts))
        self.assertEqual(summary["wallets_ok"], 20)
        self.assertEqual(summary["added"], 0)

    def test_unlabelled_outcome_is_skipped_and_counted(self):
        ts = int(copybot.time.time()) - 10
        summary, _ = self._tick(self._one_trade_per_wallet(ts, outcome_index=999))
        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["rejected"].get("outcome_unlabelled"), 20)

    def test_model_a_eligibility_rule_is_unchanged(self):
        ts = int(copybot.time.time()) - 10
        copybot._market_by_condition = lambda cid: {
            "outcomePrices": json.dumps([0.50, 0.50]), "endDate": far(), "closed": False}
        summary, _ = self._tick(self._one_trade_per_wallet(ts))
        self.assertEqual(summary["added"], 80)        # A skipped: resolves too far out
        c = copybot.db()
        self.assertNotIn("hold", {r[0] for r in c.execute("SELECT model FROM copies")})
        c.close()


# ---------------------------------------------------------------------------
# 8. Existing database compatibility + historical preservation
# ---------------------------------------------------------------------------
LEGACY_DDL = """CREATE TABLE copies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE, copied_ts TEXT, wallet TEXT, their_ts INTEGER,
    condition_id TEXT, asset TEXT, title TEXT, outcome TEXT, outcome_index INTEGER,
    their_price REAL, entry REAL, stake REAL, shares REAL,
    status TEXT, mark_price REAL, pnl REAL, resolved_ts TEXT,
    model TEXT DEFAULT 'hold', last_check TEXT, last_reason TEXT, peak REAL,
    exec_ts INTEGER, market_price REAL, fee REAL)"""


class DatabaseCompatibilityTests(unittest.TestCase):
    """A pre-migration database keeps its rows, its schema and its P&L history."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self._old_db = copybot.DB
        copybot.DB = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.execute(LEGACY_DDL)
        # Two historical copies written by the v1-era code (v1 field names in the key).
        conn.execute("""INSERT INTO copies(key,copied_ts,wallet,their_ts,condition_id,asset,
                        title,outcome,outcome_index,their_price,entry,stake,shares,status,
                        mark_price,pnl,model,peak)
                        VALUES('0xold1:assetA:1700000000:hold','2026-10-01T00:00:00+00:00',
                        'GoalLineGhost',1700000000,'condA','assetA','Old market','Yes',0,
                        0.40,0.41,2.0,4.878,'won',1.0,2.438,'hold',0.9)""")
        conn.execute("""INSERT INTO copies(key,copied_ts,wallet,their_ts,condition_id,asset,
                        title,outcome,outcome_index,their_price,entry,stake,shares,status,
                        mark_price,pnl,model)
                        VALUES('0xold2:assetB:1700000100:exit','2026-10-01T00:20:00+00:00',
                        'RN1',1700000100,'condB','assetB','Older open','No',1,
                        0.55,0.56,2.0,3.571,'open',0.50,-0.214,'exit')""")
        conn.commit()
        conn.close()
        self._old_market = copybot._market_by_condition
        self._old_book = copybot._fetch_book
        self._old_price_at = copybot._price_at
        self._old_chart = copybot.chart_signal
        copybot._market_by_condition = lambda cid: {
            "outcomePrices": json.dumps([0.50, 0.50]), "endDate": soon(), "closed": False}
        copybot._fetch_book = lambda token_id: DEEP_BOOK
        copybot._price_at = lambda token_id, ts: None
        copybot.chart_signal = lambda token_id, condition_id=None: NEUTRAL_SIGNAL

    def tearDown(self):
        copybot.DB = self._old_db
        copybot._market_by_condition = self._old_market
        copybot._fetch_book = self._old_book
        copybot._price_at = self._old_price_at
        copybot.chart_signal = self._old_chart
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _snapshot_legacy(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("""SELECT key,copied_ts,wallet,their_ts,condition_id,asset,title,
                               outcome,outcome_index,their_price,entry,stake,shares,status,
                               mark_price,pnl,model,peak FROM copies
                               WHERE key LIKE '0xold%' ORDER BY key""").fetchall()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(copies)")]
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        conn.close()
        return rows, cols, tables

    def test_schema_is_additive_only_and_rows_are_untouched(self):
        before_rows, before_cols, _ = self._snapshot_legacy()
        conn = copybot.db()          # runs the additive migrations, as production does
        conn.commit()
        conn.close()
        after_rows, after_cols, tables = self._snapshot_legacy()
        self.assertEqual(before_rows, after_rows)                 # not one byte changed
        self.assertEqual(before_cols, after_cols)                 # no column dropped/renamed
        self.assertIn("copies", tables)
        self.assertEqual(len(after_cols), 25)                     # 18 CREATE + 7 ALTER

    def test_new_v2_copies_coexist_with_history(self):
        before_rows, _, _ = self._snapshot_legacy()
        ts = int(copybot.time.time()) - 10
        w_name, w_addr = list(copybot.WALLETS.items())[0]

        def handler(url):
            from urllib.parse import parse_qs, urlparse
            user = parse_qs(urlparse(url).query)["user"][0]
            if user != w_addr:
                return v2_page([])
            return v2_page([activity_record(proxy_wallet=user, timestamp=ts,
                                            transaction_hash="0xnew1", token_id="assetNew",
                                            condition_id="condNew")])

        with mock.patch("urllib.request.urlopen", side_effect=Router(handler)), \
                mock.patch.object(copybot.time, "sleep", return_value=None):
            summary = copybot.tick()
        self.assertEqual(summary["added"], 5)
        after_rows, _, _ = self._snapshot_legacy()
        self.assertEqual(before_rows, after_rows)                 # history preserved exactly
        conn = sqlite3.connect(self.db_path)
        new_keys = [r[0] for r in conn.execute(
            "SELECT key FROM copies WHERE key LIKE '0xnew1:%' ORDER BY key")]
        model_names = [r[0] for r in conn.execute(
            "SELECT model FROM copies WHERE key LIKE '0xnew1:%' ORDER BY key")]
        conn.close()
        self.assertEqual(new_keys, ["0xnew1:assetNew:%d:%s" % (ts, m) for m in
                                    ["cdecide", "chart", "exit", "hold", "trail"]])
        self.assertEqual(sorted(model_names), ["cdecide", "chart", "exit", "hold", "trail"])

    def test_export_still_reads_legacy_rows(self):
        cwd = os.getcwd()
        tmpdir = tempfile.mkdtemp()
        try:
            os.chdir(tmpdir)
            copybot.export()
            data = json.load(open(os.path.join("docs", "data.json")))
            quality = json.load(open(os.path.join("docs", "quality.json")))
        finally:
            os.chdir(cwd)
        self.assertIn("models", data)
        self.assertEqual(sorted(data["models"]), ["A", "B", "C", "D", "E"])
        # the legacy realized win for GoalLineGhost under model A is still counted
        rows = [t for t in data["traders"] if t["name"] == "GoalLineGhost"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["A"]["realized"], 2.44, places=2)
        self.assertIsInstance(quality, dict)


# ---------------------------------------------------------------------------
# 8b. Model E price history + volume feed
# ---------------------------------------------------------------------------
class ModelEDataTests(unittest.TestCase):
    def test_price_history_still_comes_from_clob_and_volume_from_v2(self):
        bars = [{"t": 1000 + i * 60, "p": 0.50 + i * 0.001} for i in range(30)]
        now = int(copybot.time.time())
        trades = [
            {"proxy_wallet": "0xw", "token_id": "tokA", "condition_id": "condA",
             "side": "BUY", "size": 100.0, "price": 0.50, "timestamp": now - 300,
             "outcome_index": 0},
            {"proxy_wallet": "0xw", "token_id": "tokA", "condition_id": "condA",
             "side": "SELL", "size": 100.0, "price": 0.50, "timestamp": now - 200,
             "outcome_index": 0},
            {"proxy_wallet": "0xw", "token_id": "tokB", "condition_id": "condA",
             "side": "BUY", "size": 999.0, "price": 0.50, "timestamp": now - 100,
             "outcome_index": 0},
        ]

        def handler(url):
            if "/v2/trades" in url:
                return v2_page(trades)
            return http_error(500, {"error": "unexpected v2 route"})

        with mock.patch("copybot.get", return_value={"history": bars}), \
                mock.patch("urllib.request.urlopen", side_effect=Router(handler)):
            sig = copybot.chart_signal("tokA", "condA")

        self.assertIsNotNone(sig)
        self.assertEqual(sig["bars"], 30)
        # price filters still active and unchanged
        self.assertIn("rsi", sig)
        self.assertIn("mom_30m", sig)
        self.assertIn("trend_ph", sig)
        # volume now comes from the v2 condition feed, filtered to this token
        self.assertEqual(sig["vol_1h"], 100.0)          # only tokA's BUY (100 * 0.50)
        self.assertAlmostEqual(sig["buy_pressure"], 0.5, places=2)
        self.assertAlmostEqual(sig["vwap"], 0.5, places=4)

    def test_volume_is_optional_without_a_condition_id(self):
        bars = [{"t": 1000 + i * 60, "p": 0.50} for i in range(30)]
        with mock.patch("copybot.get", return_value={"history": bars}), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP")):
            sig = copybot.chart_signal("tokA")
        self.assertIsNotNone(sig)
        self.assertNotIn("vol_ratio", sig)
        ok, reason = copybot._chart_entry_ok(sig, entry=0.50)
        self.assertTrue(ok, reason)

    def test_v2_trades_failure_leaves_price_filters_working(self):
        bars = [{"t": 1000 + i * 60, "p": 0.50} for i in range(30)]

        def handler(url):
            return http_error(503, {"error": "request_timeout", "retryable": True},
                              headers={"Retry-After": "1"})

        with mock.patch("copybot.get", return_value={"history": bars}), \
                mock.patch("urllib.request.urlopen", side_effect=Router(handler)), \
                mock.patch.object(copybot.time, "sleep", return_value=None):
            sig = copybot.chart_signal("tokA", "condA")
        self.assertIsNotNone(sig)
        self.assertNotIn("vol_ratio", sig)      # volume unknown -> gate skipped, as before
        self.assertIn("rsi", sig)


# ---------------------------------------------------------------------------
# 8c. fetch_check: the read-only migration check
# ---------------------------------------------------------------------------
class FetchCheckTests(unittest.TestCase):
    """`python3 copybot.py fetch_check` must prove the feed works and write nothing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._old_db = copybot.DB
        copybot.DB = os.path.join(self.tmpdir, "copybot.db")

    def tearDown(self):
        copybot.DB = self._old_db

    def test_reports_every_wallet_and_creates_no_database(self):
        def handler(url):
            from urllib.parse import parse_qs, urlparse
            user = parse_qs(urlparse(url).query)["user"][0]
            return v2_page([activity_record(proxy_wallet=user,
                                            timestamp=int(copybot.time.time()) - 60,
                                            transaction_hash="0xfc" + user[-4:],
                                            token_id="tok" + user[-4:],
                                            condition_id="cond" + user[-4:])])

        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=Router(handler)), \
                mock.patch("sys.stdout", buf):
            result = copybot.fetch_check()
        self.assertEqual(result["ok"], 20)
        self.assertEqual(result["failed"], 0)
        out = buf.getvalue()
        for name in copybot.WALLETS:
            self.assertIn(name, out)
        # read-only: no DB file, no copies, nothing written anywhere
        self.assertFalse(os.path.exists(copybot.DB))
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_reports_410_instead_of_a_quiet_zero(self):
        def handler(url):
            return http_error(410, {"error": "retired", "code": "gone", "retryable": False})

        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=Router(handler)), \
                mock.patch("sys.stdout", buf):
            result = copybot.fetch_check()
        self.assertEqual(result["ok"], 0)
        self.assertEqual(result["failed"], 20)
        self.assertEqual({d for _, d in result["failures"]}, {"HTTP 410 gone"})
        self.assertIn("FAILED: HTTP 410 gone", buf.getvalue())


# ---------------------------------------------------------------------------
# 9. No retired endpoint may be reintroduced
# ---------------------------------------------------------------------------
class NoV1LeftoversTests(unittest.TestCase):
    def test_sources_contain_no_retired_v1_routes(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        forbidden = ["{DATA}" + "/activity", "{DATA}" + "/trades", "{DATA}" + "/value",
                     "data-api.polymarket.com/trades", "data-api.polymarket.com/activity",
                     "data-api.polymarket.com/value",
                     "&offset=", "?offset=", "offset={", '"offset"']
        for name in ("copybot.py", "copytrade.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                src = fh.read()
            for needle in forbidden:
                self.assertNotIn(needle, src, "%s still references a retired v1 route: %s"
                                 % (name, needle))

    def test_v2_routes_are_the_ones_actually_used(self):
        self.assertEqual(copybot.DATA_V2, "https://data-api.polymarket.com/v2")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "copybot.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('api_get("/activity"', src)
        self.assertIn('api_get("/trades"', src)


if __name__ == "__main__":
    unittest.main()
