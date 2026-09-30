"""Alpaca MARKET-DATA adapter (WebSocket stream + REST historical bars).

Scope: market data only. This module never imports or calls any order/trading API and never reads
trading credentials. Credential env names are ``ALPACA_DATA_KEY_ID`` / ``ALPACA_DATA_SECRET_KEY`` (data
keys), deliberately different from the SDK's ``APCA_*`` trading variables.

Schema notes: message shapes follow Alpaca's public streaming documentation
(https://docs.alpaca.markets/us/docs/streaming-market-data, .../real-time-stock-pricing-data), written from
the documented contract. They have NOT been verified against a live feed in this repository because no data
credential/egress was available; see STATUS.md.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets

from ..config import Settings, validate_data_url
from ..models import (
    Bar,
    HistoricalBarsRequest,
    MarketEvent,
    ProviderHealth,
    Quote,
    SessionEvent,
    Trade,
    iso,
    parse_ts,
)
from .stream import AuthError, Backoff, Connection, RateLimited, ResilientStream, Subscriptions

# Documented stream error codes (subset).
ERR_AUTH = {401, 402, 404, 409}
ERR_RATE = {406, 429}


class WsConnection:
    """Adapts a ``websockets`` client connection to the :class:`Connection` protocol (JSON frames)."""

    def __init__(self, ws: Any):
        self._ws = ws

    async def send(self, message: Any) -> None:
        await self._ws.send(json.dumps(message))

    async def recv(self) -> Any:
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()


def parse_messages(msgs: Any) -> list[MarketEvent | Exception]:
    """Normalise one Alpaca frame (a JSON array of messages) into events; errors are returned, not raised."""
    out: list[MarketEvent | Exception] = []
    if isinstance(msgs, dict):
        msgs = [msgs]
    for m in msgs or []:
        t = m.get("T")
        try:
            if t == "t":
                out.append(
                    Trade(
                        m["S"],
                        parse_ts(m["t"]),
                        float(m["p"]),
                        float(m["s"]),
                        str(m.get("i", "")),
                        tuple(m.get("c") or ()),
                    )
                )
            elif t == "q":
                out.append(
                    Quote(
                        m["S"],
                        parse_ts(m["t"]),
                        float(m["bp"]),
                        float(m["ap"]),
                        float(m.get("bs", 0)),
                        float(m.get("as", 0)),
                    )
                )
            elif t in ("b", "u"):
                bar = Bar(
                    m["S"],
                    parse_ts(m["t"]),
                    float(m["o"]),
                    float(m["h"]),
                    float(m["l"]),
                    float(m["c"]),
                    float(m["v"]),
                    vwap=float(m["vw"]) if m.get("vw") is not None else None,
                    trade_count=int(m["n"]) if m.get("n") is not None else None,
                    revision=1 if t == "u" else 0,
                    corrected=t == "u",
                )
                bar.validate()
                out.append(bar)
            elif t == "c":  # trade correction: emit the corrected print
                out.append(
                    Trade(
                        m["S"],
                        parse_ts(m["t"]),
                        float(m["cp"]),
                        float(m["cs"]),
                        f"{m.get('ci', '')}-c",
                        tuple(m.get("cc") or ()),
                    )
                )
            elif t == "s":
                msg = str(m.get("sm", "")).lower()
                if "resum" in msg:
                    out.append(SessionEvent("resume", parse_ts(m["t"]), m["S"], m.get("sm", "")))
                elif "halt" in msg:
                    out.append(SessionEvent("halt", parse_ts(m["t"]), m["S"], m.get("sm", "")))
            elif t == "error":
                code = int(m.get("code", 0))
                if code in ERR_AUTH:
                    out.append(AuthError(f"stream error {code}"))  # message text deliberately omitted
                elif code in ERR_RATE:
                    out.append(RateLimited(30.0, f"stream error {code}"))
                else:
                    out.append(ConnectionError(f"stream error {code}: {str(m.get('msg', ''))[:80]}"))
            # success/subscription/x/l/d frames carry nothing to ingest
        except (KeyError, ValueError, TypeError) as exc:
            out.append(ValueError(f"malformed {t!r} message dropped: {exc}"))
    # A malformed record must not tear down the stream: only transport/auth/rate errors are raised.
    return [e for e in out if not (isinstance(e, ValueError) and not isinstance(e, ConnectionError))]


def build_subscribe(subs: Subscriptions) -> list[dict[str, Any]]:
    msg: dict[str, Any] = {"action": "subscribe"}
    if subs.trades:
        msg["trades"] = sorted(subs.trades)
    if subs.quotes:
        msg["quotes"] = sorted(subs.quotes)
    if subs.bars:
        msg["bars"] = sorted(subs.bars)
        msg["updatedBars"] = sorted(subs.bars)
    return [msg] if len(msg) > 1 else []


class AlpacaProvider:
    name = "alpaca"

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        connect: Any = None,
        backoff: Backoff | None = None,
    ):
        self.s = settings
        ws_url = f"{settings.alpaca_data_ws_url.rstrip('/')}/{settings.alpaca_data_feed}"
        validate_data_url(ws_url)
        validate_data_url(settings.alpaca_data_rest_url)
        self._http = http
        self._key, self._secret = settings.alpaca_data_key_id, settings.alpaca_data_secret_key
        self._ws_url = ws_url
        self._connect_override = connect
        self.stream = ResilientStream(
            "alpaca",
            self._connect,
            self._authenticate,
            build_subscribe,
            parse_messages,
            feed=settings.alpaca_data_feed,
            backoff=backoff,
            stale_after=60.0,
            tick_capacity=settings.queue_maxsize,
        )

    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    # ---- stream plumbing -----------------------------------------------------
    async def _connect(self) -> Connection:
        if self._connect_override is not None:
            return await self._connect_override()
        return WsConnection(await websockets.connect(self._ws_url, open_timeout=10, ping_interval=20))

    async def _authenticate(self, conn: Connection) -> None:
        first = await conn.recv()  # [{"T":"success","msg":"connected"}]
        if not any(m.get("T") == "success" for m in (first if isinstance(first, list) else [first])):
            for ev in parse_messages(first):
                if isinstance(ev, Exception):
                    raise ev
            raise ConnectionError("unexpected first frame from data stream")
        await conn.send({"action": "auth", "key": self._key, "secret": self._secret})
        reply = await conn.recv()
        frames = reply if isinstance(reply, list) else [reply]
        if any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in frames):
            return
        for ev in parse_messages(reply):
            if isinstance(ev, Exception):
                raise ev
        raise AuthError("authentication was not acknowledged")

    # ---- provider API --------------------------------------------------------
    async def start(self) -> None:
        if not self.has_credentials():
            self.stream.health.detail = "ALPACA_DATA_KEY_ID / ALPACA_DATA_SECRET_KEY not set"
            raise RuntimeError(
                "Alpaca data credentials are required for DATA_PROVIDER=alpaca (market data keys only)"
            )
        await self.stream.start()

    async def stop(self) -> None:
        await self.stream.stop()
        if self._http:
            await self._http.aclose()

    async def subscribe_trades(self, symbols: list[str]) -> None:
        await self.stream.subscribe(trades=symbols)

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        await self.stream.subscribe(quotes=symbols)

    async def subscribe_bars(self, symbols: list[str], timeframe: str = "1Min") -> None:
        if timeframe != "1Min":
            raise ValueError("only 1Min bars are subscribed")
        await self.stream.subscribe(bars=symbols)

    def events(self) -> AsyncIterator[MarketEvent]:
        return self.stream.events()

    async def health(self) -> ProviderHealth:
        h = self.stream.health
        if self.stream.fatal:
            h.detail = self.stream.fatal
        return h

    async def historical_bars(self, request: HistoricalBarsRequest) -> list[Bar]:
        if request.timeframe != "1Min":
            raise ValueError("only 1Min historical bars are supported")
        client = self._http or httpx.AsyncClient(timeout=30.0)
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}
        base = self.s.alpaca_data_rest_url.rstrip("/")
        bars: list[Bar] = []
        token: str | None = None
        try:
            while True:
                params: dict[str, Any] = {
                    "symbols": ",".join(request.symbols),
                    "timeframe": "1Min",
                    "start": iso(request.start.astimezone(UTC)),
                    "end": iso(request.end.astimezone(UTC)),
                    "adjustment": "raw" if request.adjustment == "raw" else request.adjustment,
                    "feed": request.feed or self.s.alpaca_data_feed,
                    "limit": 10000,
                    "sort": "asc",
                }
                if token:
                    params["page_token"] = token
                r = await client.get(f"{base}/v2/stocks/bars", params=params, headers=headers)
                if r.status_code == 429:
                    raise RateLimited(float(r.headers.get("retry-after", 30)), "historical bars rate limited")
                if r.status_code in (401, 403):
                    raise AuthError("historical data credentials rejected")
                r.raise_for_status()
                body = r.json()
                for sym, rows in (body.get("bars") or {}).items():
                    for row in rows:
                        b = Bar(
                            sym,
                            parse_ts(row["t"]),
                            float(row["o"]),
                            float(row["h"]),
                            float(row["l"]),
                            float(row["c"]),
                            float(row["v"]),
                            vwap=row.get("vw"),
                            trade_count=row.get("n"),
                        )
                        b.validate()
                        bars.append(b)
                token = body.get("next_page_token")
                if not token:
                    break
        finally:
            if self._http is None:
                await client.aclose()
        return sorted(bars, key=lambda b: (b.ts, b.symbol))


def utc_from(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
