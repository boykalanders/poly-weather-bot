"""Thread-safe bridge from the engine's worker threads to Telegram."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading

log = logging.getLogger(__name__)

MAX_LEN = 3900


class Notifier:
    """The engine calls `send()` from plain threads; delivery happens on the
    bot's asyncio loop.  Messages are queued so nothing is lost during startup
    or a transient Telegram outage."""

    def __init__(self, chat_ids: list[str]):
        self.chat_ids = [c for c in chat_ids if c]
        self._q: queue.Queue[str] = queue.Queue(maxsize=500)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot = None
        self._lock = threading.Lock()

    def bind(self, bot, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._bot = bot
            self._loop = loop
        self._drain()

    def send(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            bot, loop = self._bot, self._loop
        if bot is None or loop is None or loop.is_closed():
            self._enqueue(text)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._deliver(text), loop)
        except RuntimeError:
            self._enqueue(text)

    def _enqueue(self, text: str) -> None:
        try:
            self._q.put_nowait(text)
        except queue.Full:
            log.warning("notifier queue full, dropping message")

    def _drain(self) -> None:
        while True:
            try:
                self.send(self._q.get_nowait())
            except queue.Empty:
                return

    async def _deliver(self, text: str) -> None:
        text = text[:MAX_LEN]
        for chat_id in self.chat_ids:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram send to %s failed: %s", chat_id, e)
