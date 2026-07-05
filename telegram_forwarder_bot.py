#!/usr/bin/env python3
"""
TELEGRAM TOPIC FORWARDER BOT - FIXED FOR GROUPS & CONFIG
"""

import json
import os
import re
import time
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
import requests

# ============================================
# CONFIG - ENVIRONMENT VARIABLES
# ============================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SOURCE_CHANNEL = os.environ.get("SOURCE_CHANNEL", "")
DESTINATION_GROUP = os.environ.get("DESTINATION_GROUP", "")
START_MESSAGE_ID = int(os.environ.get("START_MESSAGE_ID", "1"))
END_MESSAGE_ID = int(os.environ.get("END_MESSAGE_ID", "100"))

allowed_users_str = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = [int(x.strip()) for x in allowed_users_str.split(",") if x.strip()]

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
DATA_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)

# ============================================
# LOGGER
# ============================================
class Logger:
    @staticmethod
    def log(level: str, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level.upper()}] {msg}"
        print(line)
        sys.stdout.flush()
    @staticmethod
    def info(msg: str) -> None: Logger.log("INFO", msg)
    @staticmethod
    def warn(msg: str) -> None: Logger.log("WARN", msg)
    @staticmethod
    def error(msg: str) -> None: Logger.log("ERROR", msg)

# ============================================
# TELEGRAM CLIENT
# ============================================
class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.retry_limit = 3

    def call(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + method
        data = data or {}
        attempt = 0
        while attempt < self.retry_limit:
            attempt += 1
            try:
                resp = requests.post(url, data=data, timeout=30)
                result = resp.json()
                if result.get("ok", False):
                    return result
                retry_after = result.get("parameters", {}).get("retry_after", 0)
                if retry_after:
                    Logger.warn(f"Rate limited. Sleeping {retry_after}s...")
                    time.sleep(int(retry_after) + 1)
                    continue
                return result
            except Exception as e:
                Logger.warn(f"Error on {method}: {e}")
                time.sleep(2)
        return {"ok": False, "description": "Max retries"}

    def send_message(self, chat_id: int | str, text: str, extra: dict = None) -> dict:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if extra:
            payload.update(extra)
        return self.call("sendMessage", payload)

    def edit_message(self, chat_id: int | str, msg_id: int, text: str) -> dict:
        return self.call("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML"
        })

    def delete_message(self, chat_id: int | str, msg_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    def forward_message(self, to: int | str, frm: int | str, msg_id: int) -> dict:
        return self.call("forwardMessage", {
            "chat_id": to,
            "from_chat_id": frm,
            "message_id": msg_id
        })

    def copy_message(self, to: int | str, thread_id: int, frm: int | str, msg_id: int) -> dict:
        return self.call("copyMessage", {
            "chat_id": to,
            "message_thread_id": thread_id,
            "from_chat_id": frm,
            "message_id": msg_id
        })

    def create_forum_topic(self, chat_id: int | str, name: str) -> dict:
        return self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})

    def get_chat(self, chat_id: int | str) -> dict:
        return self.call("getChat", {"chat_id": chat_id})

    def get_updates(self, offset: int, timeout: int = 30) -> dict:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# ============================================
# SUBJECT DETECTOR
# ============================================
class SubjectDetector:
    PATTERNS = [
        re.compile(r"Tɪᴛʟᴇ\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),
        re.compile(r"Title\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),
        re.compile(r"Subject\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),
    ]
    @staticmethod
    def detect(caption: str) -> str:
        if not caption:
            return "📂 Others"
        for p in SubjectDetector.PATTERNS:
            m = p.search(caption)
            if m:
                return m.group(1).strip()[:50]
        return "📂 Others"

# ============================================
# TOPIC MANAGER
# ============================================
class TopicManager:
    def __init__(self, tg: TelegramClient):
        self.tg = tg
        self.topics = {}

    def get_or_create(self, group_id: int | str, subject: str) -> int:
        if subject in self.topics:
            return self.topics[subject]
        result = self.tg.create_forum_topic(group_id, subject)
        if result.get("ok", False):
            thread_id = int(result["result"]["message_thread_id"])
            self.topics[subject] = thread_id
            Logger.info(f"Created topic: {subject} -> #{thread_id}")
            return thread_id
        Logger.error(f"Topic create failed: {subject}")
        return 0

    def reset(self):
        self.topics = {}

# ============================================
# KEYBOARDS
# ============================================
class Keyboards:
    @staticmethod
    def main() -> dict:
        return {
            "inline_keyboard": [
                [{"text": "📥 Set Source", "callback_data": "set_source"}],
                [{"text": "📤 Set Destination", "callback_data": "set_dest"}],
                [{"text": "🔗 Set Start ID", "callback_data": "set_start"}],
                [{"text": "🔗 Set End ID", "callback_data": "set_end"}],
                [{"text": "🚀 Start Copy", "callback_data": "start"}],
                [{"text": "🛑 Stop", "callback_data": "stop"}],
                [{"text": "📊 Status", "callback_data": "status"}],
                [{"text": "♻️ Reset", "callback_data": "reset"}],
            ]
        }

    @staticmethod
    def confirm(action: str) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "✅ Yes", "callback_data": f"confirm_{action}"},
                {"text": "❌ No", "callback_data": "back_main"}
            ]]
        }

# ============================================
# USER SESSIONS (in-memory for Railway)
# ============================================
user_sessions = {}

# ============================================
# FORWARDER ENGINE
# ============================================
class ForwarderEngine:
    def __init__(self, tg: TelegramClient, topics: TopicManager):
        self.tg = tg
        self.topics = topics
        self.running = False
        self.current_pos = START_MESSAGE_ID
        self.end_id = END_MESSAGE_ID
        self.source = SOURCE_CHANNEL
        self.dest = DESTINATION_GROUP

    def run(self, chat_id: int | str, status_msg_id: int):
        self.running = True
        start = self.current_pos
        end = self.end_id
        copied = 0
        skipped = 0
        errors = 0
        total = end - start + 1

        # FIRST - Test if bot can send to group
        test = self.tg.send_message(chat_id, "✅ Bot is working! Starting copy...")
        if not test.get("ok", False):
            Logger.error(f"Cannot send to chat {chat_id}: {test}")
            self.tg.send_message(chat_id, "❌ Bot cannot send messages here! Make bot admin and add to group.")
            self.running = False
            return

        Logger.info(f"Forwarder started: {start}->{end}")

        for msg_id in range(start, end + 1):
            if not self.running:
                break

            # Forward message to get caption
            fwd = self.tg.forward_message(chat_id, self.source, msg_id)
            if not fwd.get("ok", False):
                skipped += 1
                continue

            fwd_msg_id = fwd["result"]["message_id"]
            caption = fwd["result"].get("caption") or fwd["result"].get("text") or ""

            # Delete temp message
            self.tg.delete_message(chat_id, fwd_msg_id)

            # Detect subject
            subject = SubjectDetector.detect(caption)
            thread_id = self.topics.get_or_create(self.dest, subject)

            if thread_id == 0:
                errors += 1
                continue

            # Copy to destination
            copy = self.tg.copy_message(self.dest, thread_id, self.source, msg_id)
            if copy.get("ok", False):
                copied += 1
            else:
                errors += 1
                Logger.warn(f"Copy failed: {copy}")

            # Update progress
            if msg_id % 5 == 0 or msg_id == end:
                done = msg_id - start + 1
                pct = int((done / total) * 100) if total > 0 else 0
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                self.tg.edit_message(
                    chat_id,
                    status_msg_id,
                    f"📨 <b>Copying...</b>\n\n"
                    f"{bar} {pct}%\n"
                    f"📌 {msg_id}/{end}\n"
                    f"📂 {subject}\n"
                    f"✅ {copied} | ⏭ {skipped} | ❌ {errors}"
                )

            time.sleep(0.5)

        self.running = False
        self.tg.edit_message(
            chat_id,
            status_msg_id,
            f"✅ <b>COMPLETED!</b>\nCopied: {copied}\nSkipped: {skipped}\nErrors: {errors}"
        )
        Logger.info(f"Forwarder done: {copied} copied, {skipped} skipped, {errors} errors")

    def stop(self):
        self.running = False

    def reset(self):
        self.stop()
        self.current_pos = START_MESSAGE_ID
        self.topics.reset()

# ============================================
# BOT CONTROLLER
# ============================================
class BotController:
    def __init__(self, tg: TelegramClient, engine: ForwarderEngine):
        self.tg = tg
        self.engine = engine
        self.sessions = {}

    def handle_update(self, update: dict):
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _check_access(self, user_id: int, chat_id: int | str) -> bool:
        if user_id in ALLOWED_USERS:
            return True
        self.tg.send_message(chat_id, "🚫 Unauthorized!")
        return False

    def _handle_message(self, msg: dict):
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "").strip()

        if not self._check_access(user_id, chat_id):
            return

        # Handle config input
        session = self.sessions.get(str(chat_id), {})
        step = session.get("step", "")

        if step == "set_source":
            self.engine.source = text
            self.sessions[str(chat_id)] = {}
            self.tg.send_message(chat_id, f"✅ Source set to: {text}")
            self._send_main_menu(chat_id)
            return

        if step == "set_dest":
            self.engine.dest = text
            self.sessions[str(chat_id)] = {}
            self.tg.send_message(chat_id, f"✅ Destination set to: {text}")
            self._send_main_menu(chat_id)
            return

        if step == "set_start":
            try:
                self.engine.current_pos = int(text)
                self.sessions[str(chat_id)] = {}
                self.tg.send_message(chat_id, f"✅ Start ID set to: {text}")
                self._send_main_menu(chat_id)
            except:
                self.tg.send_message(chat_id, "❌ Invalid number!")
            return

        if step == "set_end":
            try:
                self.engine.end_id = int(text)
                self.sessions[str(chat_id)] = {}
                self.tg.send_message(chat_id, f"✅ End ID set to: {text}")
                self._send_main_menu(chat_id)
            except:
                self.tg.send_message(chat_id, "❌ Invalid number!")
            return

        if text == "/start":
            self._send_main_menu(chat_id)
        elif text == "/status":
            self._send_status(chat_id)
        elif text == "/help":
            self.tg.send_message(chat_id, "/start - Menu\n/status - Check status\n/help - This")

    def _handle_callback(self, cb: dict):
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        data = cb["data"]

        if not self._check_access(user_id, chat_id):
            return

        self.tg.answer_callback(cb["id"])

        if data == "set_source":
            self.sessions[str(chat_id)] = {"step": "set_source"}
            self.tg.send_message(chat_id, "📥 Send Source Channel ID or @username:")
        elif data == "set_dest":
            self.sessions[str(chat_id)] = {"step": "set_dest"}
            self.tg.send_message(chat_id, "📤 Send Destination Group ID or @username:")
        elif data == "set_start":
            self.sessions[str(chat_id)] = {"step": "set_start"}
            self.tg.send_message(chat_id, "🔗 Send Start Message ID (number):")
        elif data == "set_end":
            self.sessions[str(chat_id)] = {"step": "set_end"}
            self.tg.send_message(chat_id, "🔗 Send End Message ID (number):")
        elif data == "status":
            self._send_status(chat_id)
        elif data == "start":
            self._do_start(chat_id)
        elif data == "stop":
            self._do_stop(chat_id)
        elif data == "reset":
            self.tg.send_message(chat_id, "♻️ Reset everything?", {"reply_markup": json.dumps(Keyboards.confirm("reset"))})
        elif data == "confirm_reset":
            self._do_reset(chat_id)
        elif data == "back_main":
            self._send_main_menu(chat_id)

    def _send_main_menu(self, chat_id: int | str):
        src = self.engine.source or "NOT SET"
        dst = self.engine.dest or "NOT SET"
        self.tg.send_message(
            chat_id,
            f"🤖 <b>Topic Forwarder Bot</b>\n\n"
            f"📥 Source: <code>{src}</code>\n"
            f"📤 Destination: <code>{dst}</code>\n"
            f"🔢 Range: {self.engine.current_pos} → {self.engine.end_id}\n"
            f"🟢 Running: {self.engine.running}\n\n"
            f"Use buttons below:",
            {"reply_markup": json.dumps(Keyboards.main())}
        )

    def _send_status(self, chat_id: int | str):
        self.tg.send_message(
            chat_id,
            f"📊 <b>STATUS</b>\n"
            f"Running: {self.engine.running}\n"
            f"Position: {self.engine.current_pos}/{self.engine.end_id}\n"
            f"Source: {self.engine.source}\n"
            f"Destination: {self.engine.dest}"
        )

    def _do_start(self, chat_id: int | str):
        if self.engine.running:
            self.tg.send_message(chat_id, "⚠️ Already running!")
            return

        if not self.engine.source or not self.engine.dest:
            self.tg.send_message(chat_id, "❌ Set Source and Destination first!")
            return

        status = self.tg.send_message(chat_id, "🚀 Starting copy...")
        status_msg_id = status.get("result", {}).get("message_id", 0)

        if status_msg_id:
            thread = threading.Thread(target=self.engine.run, args=(chat_id, status_msg_id))
            thread.daemon = True
            thread.start()
            self.tg.send_message(chat_id, "✅ Started! Watch status.")
        else:
            self.tg.send_message(chat_id, "❌ Failed to create status message!")

    def _do_stop(self, chat_id: int | str):
        self.engine.stop()
        self.tg.send_message(chat_id, "🛑 Stopped!")

    def _do_reset(self, chat_id: int | str):
        self.engine.reset()
        self.tg.send_message(chat_id, "♻️ Reset complete!")

# ============================================
# MAIN
# ============================================
def main():
    if not BOT_TOKEN:
        Logger.error("BOT_TOKEN not set!")
        sys.exit(1)

    if not ALLOWED_USERS:
        Logger.error("ALLOWED_USERS not set!")
        sys.exit(1)

    Logger.info("=" * 40)
    Logger.info("  Topic Forwarder Bot v3.1 - FIXED")
    Logger.info(f"  Source: {SOURCE_CHANNEL or 'NOT SET'}")
    Logger.info(f"  Dest: {DESTINATION_GROUP or 'NOT SET'}")
    Logger.info(f"  Users: {ALLOWED_USERS}")
    Logger.info("=" * 40)

    tg = TelegramClient(BOT_TOKEN)
    topics = TopicManager(tg)
    engine = ForwarderEngine(tg, topics)
    controller = BotController(tg, engine)

    # Test bot connection
    test = tg.call("getMe")
    if test.get("ok", False):
        Logger.info(f"✅ Bot connected: @{test['result']['username']}")
    else:
        Logger.error("❌ Bot connection failed!")

    update_id = 0
    while True:
        try:
            updates = tg.get_updates(update_id + 1, 30)
            if updates.get("ok", False):
                for u in updates.get("result", []):
                    update_id = u["update_id"]
                    controller.handle_update(u)
            time.sleep(1)
        except Exception as e:
            Logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
