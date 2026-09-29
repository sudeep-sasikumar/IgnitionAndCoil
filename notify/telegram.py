"""Telegram Bot API client: signal-only messages (no buttons) + text commands.

Private chat only: messages go to TELEGRAM_CHAT_ID and commands from any other chat are
ignored. Every message is recorded in alerts_log with a dedupe key, so a restart never
re-sends an alert. Without a token/chat id, messages are printed to the console instead.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("telegram")

CommandHandler = Callable[[str, str], Awaitable[str]]


class Telegram:
    def __init__(self, cfg, token: str, chat_id: str, db):
        t = cfg.telegram
        self.cfg = t
        self.token, self.chat_id, self.db = token, str(chat_id).strip(), db
        self.enabled = bool(t.enabled and token and self.chat_id)
        self.client = httpx.AsyncClient(timeout=t.poll_timeout_s + 10)
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self.on_command: CommandHandler | None = None

    def _url(self, method: str) -> str:
        return f"{self.cfg.api_base}/bot{self.token}/{method}"

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, text: str, *, dedupe_key: str, event: str, signal_id: str | None = None,
                   reply_to: int | None = None) -> int | None:
        """Send once per dedupe_key. Returns the Telegram message id (or None)."""
        prev = self.db.alert_sent(dedupe_key)
        if prev is not None:
            return prev.message_id
        if not self.enabled:
            print(f"\n[telegram not configured - console only]\n{text}\n", flush=True)
            self.db.log_alert(dedupe_key=dedupe_key, event=event, status="console", text=text, signal_id=signal_id)
            return None
        msg_id = await self._send_raw(text, reply_to)
        self.db.log_alert(dedupe_key=dedupe_key, event=event, status="sent" if msg_id else "failed", text=text,
                          signal_id=signal_id, chat_id=self.chat_id, message_id=msg_id, reply_to=reply_to)
        return msg_id

    async def _send_raw(self, text: str, reply_to: int | None = None) -> int | None:
        payload: dict = {"chat_id": self.chat_id, "text": text[:4096],
                         "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        async with self._lock:
            for attempt in range(5):
                wait = self.cfg.min_send_interval_s - (time.monotonic() - self._last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self.client.post(self._url("sendMessage"), json=payload)
                    self._last_send = time.monotonic()
                    j = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("telegram send failed (%s), retry %d", e, attempt + 1)
                    await asyncio.sleep(2 ** attempt)
                    continue
                if j.get("ok"):
                    return int(j["result"]["message_id"])
                if j.get("error_code") == 429:
                    await asyncio.sleep(float(j.get("parameters", {}).get("retry_after", 5)))
                    continue
                log.error("telegram rejected message: %s", j.get("description"))
                return None
        return None

    async def reply_plain(self, text: str) -> None:
        if self.enabled:
            await self._send_raw(text)
        else:
            print(f"[telegram reply] {text}")

    async def poll_commands(self) -> None:
        """Long-poll getUpdates and dispatch /commands from the configured chat only."""
        if not (self.enabled and self.cfg.poll_commands):
            return
        offset = self.db.get_state("tg_update_offset", 0)
        while True:
            try:
                r = await self.client.get(self._url("getUpdates"), params={
                    "offset": offset, "timeout": self.cfg.poll_timeout_s, "allowed_updates": '["message"]'})
                j = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(5)
                continue
            if not j.get("ok"):
                log.warning("getUpdates error: %s", j.get("description"))
                await asyncio.sleep(10)
                continue
            for upd in j.get("result", []):
                offset = int(upd["update_id"]) + 1
                self.db.set_state("tg_update_offset", offset)
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat != self.chat_id or not text.startswith("/"):
                    continue  # private chat only: ignore everyone else
                cmd, _, arg = text.partition(" ")
                cmd = cmd.split("@")[0].lower()
                if self.on_command:
                    try:
                        reply = await self.on_command(cmd, arg.strip())
                    except Exception as e:  # noqa: BLE001
                        log.exception("command %s failed", cmd)
                        reply = f"Command failed: {e}"
                    if reply:
                        await self._send_raw(reply)
