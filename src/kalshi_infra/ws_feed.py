"""Authenticated Kalshi WebSocket feed that keeps a ``BookManager`` current.

On any sequence gap or drift, the affected markets are resubscribed so a fresh snapshot
replaces the suspect state. Consumers read books only through ``BookManager``, which
answers ``None`` while a book is invalid, so trading on a broken book is impossible by
construction rather than by vigilance.

Requires the optional ``websockets`` package.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence

from .auth import Signer
from .book import BookManager

log = logging.getLogger(__name__)

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


class BookFeed:
    def __init__(self, signer: Signer, tickers: Sequence[str], *,
                 books: BookManager | None = None, url: str = WS_URL,
                 reconnect_backoff_s: float = 1.0, max_backoff_s: float = 30.0) -> None:
        self.signer = signer
        self.tickers = list(tickers)
        self.books = books or BookManager()
        self.url = url
        self._backoff0, self._backoff_max = reconnect_backoff_s, max_backoff_s
        self._next_id = 1
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _cmd(self, cmd: str, params: dict) -> str:
        self._next_id += 1
        return json.dumps({"id": self._next_id, "cmd": cmd, "params": params})

    def subscribe_msg(self, tickers: Sequence[str]) -> str:
        return self._cmd("subscribe", {"channels": ["orderbook_delta"],
                                       "market_tickers": list(tickers)})

    async def run(self) -> None:
        import websockets  # optional dependency

        backoff = self._backoff0
        while not self._stop.is_set():
            try:
                headers = self.signer.headers("GET", WS_PATH)
                async with websockets.connect(self.url, additional_headers=headers) as ws:
                    await ws.send(self.subscribe_msg(self.tickers))
                    backoff = self._backoff0
                    pending: set[str] = set(self.tickers)   # awaiting a snapshot
                    async for raw in ws:
                        if self._stop.is_set():
                            return
                        self.books.on_message(json.loads(raw))
                        stale = set(self.books.needs_resnapshot())
                        pending &= stale                     # snapshot arrived: done
                        fresh = sorted(stale - pending)
                        if fresh:
                            # Resubscribing returns a fresh snapshot for each market.
                            await ws.send(self.subscribe_msg(fresh))
                            pending |= set(fresh)
            except (OSError, ValueError) as e:      # includes websockets' connection errors
                log.warning("feed disconnected: %r; reconnecting in %.0fs", e, backoff)
            for b in self.books.books.values():
                b.invalidate("disconnected")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)
