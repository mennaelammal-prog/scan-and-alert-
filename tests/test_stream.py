from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scanalert.config import Settings
from scanalert.models import Bar, HistoricalBarsRequest, ProviderEvent, Quote, SessionEvent, Trade
from scanalert.providers.alpaca import AlpacaProvider, build_subscribe, parse_messages
from scanalert.providers.stream import (
    AuthError,
    Backoff,
    EventBuffer,
    RateLimited,
    ResilientStream,
    Subscriptions,
)

T0 = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
SECRET = "SUPER-SECRET-VALUE-123"


class FakeConn:
    """Scripted connection: items are messages, exceptions (raised) or the string 'BLOCK' (never returns)."""

    def __init__(self, script):
        self.script, self.sent, self.closed = list(script), [], False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if item == "BLOCK":
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


def trade(i=1, price=10.0, sym="AAA", ts=T0):
    return Trade(sym, ts + timedelta(seconds=i), price, 100, trade_id=str(i))


def parse_identity(raw):
    return list(raw)


def make_stream(conns, parse=parse_identity, **kw):
    it = iter(conns)
    sleeps: list[float] = []

    async def connect():
        try:
            return next(it)
        except StopIteration:
            await asyncio.sleep(3600)

    async def auth(conn):
        await conn.send({"action": "auth", "key": "K", "secret": SECRET})

    async def fake_sleep(d):
        sleeps.append(d)
        await asyncio.sleep(0)

    s = ResilientStream(
        "fake",
        connect,
        auth,
        lambda subs: (
            [{"action": "subscribe", "trades": sorted(subs.trades), "bars": sorted(subs.bars)}]
            if (subs.trades or subs.bars)
            else []
        ),
        parse,
        backoff=Backoff(base=1, cap=8, jitter=0),
        sleep=fake_sleep,
        **kw,
    )
    return s, sleeps


async def collect(stream, n, timeout=3.0):
    out = []

    async def go():
        async for ev in stream.events():
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(go(), timeout)
    return out


async def test_reconnect_with_backoff_and_resubscribe():
    c1 = FakeConn([[trade(1)], ConnectionError("boom")])
    c2 = FakeConn([[trade(2)], ConnectionResetError("again")])
    c3 = FakeConn([[trade(3)], "BLOCK"])
    s, sleeps = make_stream([c1, c2, c3])
    await s.subscribe(trades=["AAA", "BBB"], bars=["AAA"])
    await s.start()
    evs = await collect(s, 10)
    await s.stop()
    kinds = [e.kind_ for e in evs if isinstance(e, ProviderEvent)]
    assert (
        kinds.count("connected") == 3
        and kinds.count("disconnected") == 2
        and kinds.count("reconnecting") == 2
    )
    assert [e.trade_id for e in evs if isinstance(e, Trade)] == ["1", "2", "3"]
    for c in (c1, c2, c3):  # every (re)connection re-sent auth then the full subscription set
        assert c.sent[0]["action"] == "auth"
        assert c.sent[1] == {"action": "subscribe", "trades": ["AAA", "BBB"], "bars": ["AAA"]}
    assert sleeps[:2] == [1.0, 1.0]  # backoff reset after data was received between failures
    assert s.health.reconnects == 2 and c1.closed and c2.closed


async def test_backoff_grows_exponentially_and_caps_when_no_data():
    conns = [FakeConn([ConnectionError("x")]) for _ in range(6)]
    s, sleeps = make_stream(conns)
    await s.subscribe(trades=["AAA"])
    await s.start()
    for _ in range(200):
        if len(sleeps) >= 5:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert sleeps[:5] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_jitter_bounds():
    b = Backoff(base=2, cap=30, jitter=0.5)
    vals = [b.next() for _ in range(6)]
    assert all(v >= 0 for v in vals) and max(vals) <= 30 * 1.5
    b.reset()
    assert b.attempt == 0


async def test_duplicate_events_dropped_and_counted():
    t = trade(1)
    s, _ = make_stream([FakeConn([[t, t], [t], [trade(2)], "BLOCK"])])
    await s.start()
    evs = await collect(s, 3)
    await s.stop()
    assert [e.trade_id for e in evs if isinstance(e, Trade)] == ["1", "2"]
    assert s.health.duplicate_events == 2


async def test_corrected_bar_is_not_treated_as_duplicate():
    b0 = Bar("AAA", T0, 10, 10.1, 9.9, 10.0, 100)
    b1 = Bar("AAA", T0, 10, 10.2, 9.9, 10.1, 150, revision=1, corrected=True)
    s, _ = make_stream([FakeConn([[b0, b0, b1], "BLOCK"])])
    await s.start()
    evs = await collect(s, 3)
    await s.stop()
    bars = [e for e in evs if isinstance(e, Bar)]
    assert [b.revision for b in bars] == [0, 1] and s.health.corrections == 1


async def test_late_ticks_counted():
    s, _ = make_stream([FakeConn([[trade(60), trade(1, price=10.1)], "BLOCK"])])
    await s.start()
    await collect(s, 3)
    await s.stop()
    assert s.health.late_events == 1


async def test_gap_diagnostics_for_missing_minutes():
    b = lambda m: Bar("AAA", T0 + timedelta(minutes=m), 10, 10, 10, 10, 100)  # noqa: E731
    s, _ = make_stream([FakeConn([[b(0), b(1), b(4)], "BLOCK"])])
    await s.start()
    evs = await collect(s, 5)
    await s.stop()
    gaps = [e for e in evs if isinstance(e, ProviderEvent) and e.kind_ == "gap"]
    assert len(gaps) == 1 and "2 missing" in gaps[0].detail and s.health.gaps == 2


async def test_stale_stream_triggers_reconnect_and_flags_health():
    c1 = FakeConn(["BLOCK"])
    c2 = FakeConn([[trade(1)], "BLOCK"])
    s, _ = make_stream([c1, c2], stale_after=0.05)
    await s.start()
    evs = await collect(s, 5, timeout=3)
    await s.stop()
    kinds = [e.kind_ for e in evs if isinstance(e, ProviderEvent)]
    assert "stale" in kinds and kinds.count("connected") == 2 and s.health.reconnects >= 1


async def test_rate_limit_backs_off_at_least_retry_after():
    c1 = FakeConn([[RateLimited(30.0)]])

    def parse(raw):
        return raw

    s, sleeps = make_stream([c1, FakeConn([[trade(1)], "BLOCK"])], parse=parse)
    await s.start()
    evs = await collect(s, 4)
    await s.stop()
    assert sleeps[0] >= 30.0 and s.health.rate_limited == 1
    assert any(isinstance(e, ProviderEvent) and e.kind_ == "rate_limited" for e in evs)


async def test_repeated_auth_failure_stops_retrying_and_never_logs_secret(caplog):
    caplog.set_level(logging.DEBUG)
    conns = [FakeConn([[AuthError("auth failed")]]) for _ in range(5)]
    s, sleeps = make_stream(conns)
    await s.start()
    for _ in range(300):
        if s.fatal:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert s.fatal and "authentication" in s.fatal and len(sleeps) == 2  # 3 attempts -> 2 waits, then fatal
    assert SECRET not in caplog.text and SECRET not in json.dumps(s.health.as_dict())


def test_event_buffer_backpressure_drops_oldest_ticks_never_bars():
    buf = EventBuffer(tick_capacity=3)
    q = lambda i: Quote("AAA", T0 + timedelta(seconds=i), 10, 10.01)  # noqa: E731
    results = [buf.put(q(i)) for i in range(6)]
    assert results == [True, True, True, False, False, False] and buf.dropped_ticks == 3
    assert [x.ts for _, x in buf.ticks] == [T0 + timedelta(seconds=i) for i in (3, 4, 5)]
    for i in range(50):
        assert buf.put(Bar("AAA", T0 + timedelta(minutes=i), 10, 10, 10, 10, 1))
    assert len(buf.critical) == 50


async def test_buffer_preserves_arrival_order_across_lanes():
    buf = EventBuffer(tick_capacity=10)
    buf.put(Quote("AAA", T0, 10, 10.01))
    buf.put(SessionEvent("halt", T0, "AAA"))
    buf.put(Quote("AAA", T0 + timedelta(seconds=1), 10, 10.01))
    order = [type(await buf.get()).__name__ for _ in range(3)]
    assert order == ["Quote", "SessionEvent", "Quote"]


async def test_stream_records_dropped_events_in_health():
    s, _ = make_stream(
        [FakeConn([[Quote("AAA", T0 + timedelta(seconds=i), 10, 10.01) for i in range(10)], "BLOCK"])],
        tick_capacity=4,
    )
    await s.start()
    await asyncio.sleep(0.1)
    await s.stop()
    assert s.health.dropped_events >= 6
    bp = [e for e in s.buffer.critical if isinstance(e[1], ProviderEvent) and e[1].kind_ == "backpressure"]
    assert len(bp) == 1  # reported once, not per drop


async def test_incremental_subscribe_on_live_connection():
    c = FakeConn([[trade(1)], "BLOCK"])
    s, _ = make_stream([c])
    await s.subscribe(trades=["AAA"])
    await s.start()
    await collect(s, 2)
    await s.subscribe(trades=["ZZZ"])
    await s.stop()
    assert c.sent[-1]["trades"] == ["ZZZ"] and s.subs.trades == {"AAA", "ZZZ"}


# ----------------------------------------------------------------------------- alpaca schema
def test_alpaca_parse_trade_quote_bar():
    msgs = [
        {
            "T": "t",
            "S": "AAPL",
            "i": 96921,
            "x": "D",
            "p": 126.55,
            "s": 1,
            "c": ["@", "I"],
            "z": "C",
            "t": "2026-09-28T14:01:02.123456789Z",
        },
        {
            "T": "q",
            "S": "AMD",
            "bx": "U",
            "bp": 87.66,
            "bs": 1,
            "ax": "Q",
            "ap": 87.68,
            "as": 4,
            "c": ["R"],
            "z": "B",
            "t": "2026-09-28T14:01:02.5Z",
        },
        {
            "T": "b",
            "S": "SPY",
            "o": 100.0,
            "h": 101.0,
            "l": 99.5,
            "c": 100.5,
            "v": 12345,
            "t": "2026-09-28T14:01:00Z",
            "n": 300,
            "vw": 100.2,
        },
        {
            "T": "u",
            "S": "SPY",
            "o": 100.0,
            "h": 101.5,
            "l": 99.5,
            "c": 101.0,
            "v": 15000,
            "t": "2026-09-28T14:01:00Z",
        },
        {"T": "success", "msg": "connected"},
        {"T": "subscription", "trades": ["AAPL"]},
    ]
    ev = parse_messages(msgs)
    assert [type(e).__name__ for e in ev] == ["Trade", "Quote", "Bar", "Bar"]
    assert ev[0].price == 126.55 and ev[0].conditions == ("@", "I") and ev[0].ts.microsecond == 123456
    assert ev[1].bid == 87.66 and ev[1].ask == 87.68 and ev[1].ask_size == 4
    assert ev[2].vwap == 100.2 and ev[2].revision == 0 and not ev[2].corrected
    assert ev[3].revision == 1 and ev[3].corrected  # 'u' = updated bar
    assert ev[2].ts.tzinfo is not None and ev[0].ingest_ts >= ev[0].ts - timedelta(days=1000)


def test_alpaca_parse_status_halt_resume_and_corrections():
    ev = parse_messages(
        [
            {"T": "s", "S": "XYZ", "sc": "H", "sm": "Trading Halt", "z": "C", "t": "2026-09-28T14:05:00Z"},
            {
                "T": "s",
                "S": "XYZ",
                "sc": "T",
                "sm": "Trading Resumption",
                "z": "C",
                "t": "2026-09-28T14:10:00Z",
            },
            {
                "T": "c",
                "S": "AAPL",
                "x": "D",
                "oi": 1,
                "op": 10.0,
                "os": 1,
                "ci": 2,
                "cp": 10.5,
                "cs": 1,
                "cc": ["@"],
                "t": "2026-09-28T14:05:00Z",
            },
        ]
    )
    assert (
        [e.kind_ for e in ev[:2]] == ["halt", "resume"] and isinstance(ev[2], Trade) and ev[2].price == 10.5
    )


def test_alpaca_parse_errors_are_mapped():
    auth = parse_messages([{"T": "error", "code": 402, "msg": "auth failed"}])[0]
    assert isinstance(auth, AuthError) and "auth failed" not in str(auth)
    assert isinstance(
        parse_messages([{"T": "error", "code": 406, "msg": "connection limit exceeded"}])[0], RateLimited
    )
    assert isinstance(parse_messages([{"T": "error", "code": 407, "msg": "slow client"}])[0], ConnectionError)


def test_alpaca_malformed_records_dropped_not_fatal():
    ev = parse_messages(
        [
            {"T": "t", "S": "A"},
            {"T": "b", "S": "A", "o": 10, "h": 9, "l": 11, "c": 10, "v": 1, "t": "2026-09-28T14:00:00Z"},
            {"T": "q", "S": "A", "bp": 1, "ap": 2, "t": "2026-09-28T14:00:00Z"},
        ]
    )
    assert [type(e).__name__ for e in ev] == ["Quote"]


def test_alpaca_subscribe_message_shape():
    msg = build_subscribe(Subscriptions({"B", "A"}, {"A"}, {"A"}))
    assert msg == [
        {"action": "subscribe", "trades": ["A", "B"], "quotes": ["A"], "bars": ["A"], "updatedBars": ["A"]}
    ]
    assert build_subscribe(Subscriptions()) == []


def alpaca(http=None, connect=None):
    return AlpacaProvider(
        Settings(
            database_url="sqlite://",
            data_provider="alpaca",
            alpaca_data_key_id="KID",
            alpaca_data_secret_key=SECRET,
        ),
        http=http,
        connect=connect,
        backoff=Backoff(base=0.01, cap=0.02, jitter=0),
    )


async def test_alpaca_auth_handshake_and_reconnect_resubscribe():
    conns = []

    def mk(script):
        c = FakeConn(
            [[{"T": "success", "msg": "connected"}], [{"T": "success", "msg": "authenticated"}], *script]
        )
        conns.append(c)
        return c

    scripts = iter(
        [
            mk(
                [
                    [{"T": "t", "S": "AAA", "i": 1, "p": 10, "s": 1, "t": "2026-09-28T14:00:01Z"}],
                    ConnectionError("drop"),
                ]
            ),
            mk([[{"T": "t", "S": "AAA", "i": 2, "p": 10.1, "s": 1, "t": "2026-09-28T14:00:02Z"}], "BLOCK"]),
        ]
    )

    async def connect():
        return next(scripts)

    p = alpaca(connect=connect)
    await p.subscribe_trades(["AAA"])
    await p.subscribe_bars(["AAA"], "1Min")
    await p.start()
    got = []
    async for ev in p.events():
        if isinstance(ev, Trade):
            got.append(ev.trade_id)
        if len(got) == 2:
            break
    await p.stop()
    assert got == ["1", "2"]
    for c in conns:
        assert c.sent[0] == {"action": "auth", "key": "KID", "secret": SECRET}
        assert (
            c.sent[1]["action"] == "subscribe"
            and c.sent[1]["trades"] == ["AAA"]
            and c.sent[1]["bars"] == ["AAA"]
        )
    assert (await p.health()).reconnects == 1


async def test_alpaca_auth_rejected_is_fatal_and_secret_never_in_health_or_logs(caplog):
    caplog.set_level(logging.DEBUG)

    async def connect():
        return FakeConn(
            [[{"T": "success", "msg": "connected"}], [{"T": "error", "code": 402, "msg": "auth failed"}]]
        )

    p = alpaca(connect=connect)
    await p.start()
    for _ in range(300):
        if p.stream.fatal:
            break
        await asyncio.sleep(0.02)
    h = await p.health()
    await p.stop()
    assert p.stream.fatal and SECRET not in json.dumps(h.as_dict()) and SECRET not in caplog.text


async def test_alpaca_requires_credentials():
    p = AlpacaProvider(Settings(database_url="sqlite://", data_provider="alpaca"))
    with pytest.raises(RuntimeError, match="credentials"):
        await p.start()


def test_alpaca_refuses_non_data_hosts():
    from scanalert.config import PaperOnlyViolation

    with pytest.raises(ValueError):
        Settings(alpaca_data_rest_url="https://api.alpaca.markets")
    s = Settings(database_url="sqlite://", alpaca_data_rest_url="https://evil.example.com")
    with pytest.raises(PaperOnlyViolation):
        AlpacaProvider(s)


async def test_alpaca_historical_bars_paginates_and_sends_data_keys_only():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.params.get("page_token") is None:
            body = {
                "bars": {
                    "AAA": [
                        {
                            "t": "2026-09-28T13:30:00Z",
                            "o": 10,
                            "h": 10.2,
                            "l": 9.9,
                            "c": 10.1,
                            "v": 500,
                            "n": 10,
                            "vw": 10.05,
                        }
                    ]
                },
                "next_page_token": "P2",
            }
        else:
            body = {
                "bars": {
                    "AAA": [
                        {"t": "2026-09-28T13:31:00Z", "o": 10.1, "h": 10.3, "l": 10.0, "c": 10.2, "v": 700}
                    ]
                },
                "next_page_token": None,
            }
        return httpx.Response(200, json=body)

    p = alpaca(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    bars = await p.historical_bars(
        HistoricalBarsRequest(["AAA"], T0, T0 + timedelta(days=1), "1Min", feed="iex")
    )
    assert [b.close for b in bars] == [10.1, 10.2] and len(seen) == 2
    assert all(
        r.url.host == "data.alpaca.markets" and r.url.path == "/v2/stocks/bars" and r.method == "GET"
        for r in seen
    )
    assert seen[0].headers["APCA-API-KEY-ID"] == "KID" and seen[0].url.params["timeframe"] == "1Min"


async def test_alpaca_historical_rate_limit_and_auth_errors():
    p = alpaca(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "7"}))
        )
    )
    with pytest.raises(RateLimited) as ei:
        await p.historical_bars(HistoricalBarsRequest(["AAA"], T0, T0 + timedelta(days=1)))
    assert ei.value.retry_after == 7
    p2 = alpaca(http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    with pytest.raises(AuthError):
        await p2.historical_bars(HistoricalBarsRequest(["AAA"], T0, T0 + timedelta(days=1)))
