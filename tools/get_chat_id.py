"""Print your Telegram chat ID.

1. Put TELEGRAM_BOT_TOKEN in .env
2. Open your bot in Telegram and send it any message (e.g. "hi")
3. Run:  .venv\\Scripts\\python.exe tools\\get_chat_id.py
4. Copy the number into TELEGRAM_CHAT_ID in .env
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config, load_env  # noqa: E402


def main() -> int:
    cfg = load_config()
    token = load_env()["TELEGRAM_BOT_TOKEN"]
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty in .env - create a bot with @BotFather first.")
        return 1
    base = f"{cfg.telegram.api_base}/bot{token}"
    me = httpx.get(f"{base}/getMe", timeout=20).json()
    if not me.get("ok"):
        print(f"Token rejected by Telegram: {me.get('description')}")
        return 1
    print(f"Bot: @{me['result']['username']}")
    j = httpx.get(f"{base}/getUpdates", timeout=20).json()
    if not j.get("ok"):
        print(f"getUpdates failed: {j.get('description')}")
        return 1
    chats = {}
    for u in j["result"]:
        chat = (u.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat
    if not chats:
        print("No messages yet. Send your bot a message in Telegram, then run this again.")
        return 1
    for cid, chat in chats.items():
        name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
        print(f"chat id {cid}  ({chat.get('type')}, {name})")
    print("\nPut your PRIVATE chat id in .env as TELEGRAM_CHAT_ID=<number>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
