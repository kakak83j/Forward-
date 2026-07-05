#!/usr/bin/env python3
"""
TELEGRAM TOPIC FORWARDER BOT - RAILWAY DEPLOYMENT READY
"""

import json
import os
import re
import time
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
import requests

# ============================================
# CONFIG - RAILWAY ENVIRONMENT VARIABLES SE LOAD
# ============================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SOURCE_CHANNEL = os.environ.get("SOURCE_CHANNEL", "")
DESTINATION_GROUP = os.environ.get("DESTINATION_GROUP", "")
START_MESSAGE_ID = int(os.environ.get("START_MESSAGE_ID", "1"))
END_MESSAGE_ID = int(os.environ.get("END_MESSAGE_ID", "100"))

# Parse ALLOWED_USERS from comma-separated string
allowed_users_str = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = [int(x.strip()) for x in allowed_users_str.split(",") if x.strip()]

# Paths - Railway pe /app/data use karega
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
TOPICS_FILE = DATA_DIR / "topics.json"
LOG_FILE = DATA_DIR / "bot.log"

# ============================================
# BOOTSTRAP
# ============================================
DATA_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)

for f in (CONFIG_FILE, TOPICS_FILE):
    if not f.exists():
        f.write_text(json.dumps({}, indent=2))

# ============================================
# LOGGER - Railway logs ke liye simple
# ============================================
class Logger:
    @staticmethod
    def log(level: str, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level.upper()}] {message}"
        print(line)
        sys.stdout.flush()

    @staticmethod
    def info(msg: str) -> None:
        Logger.log("INFO", msg)

    @staticmethod
    def warn(msg: str) -> None:
        Logger.log("WARN", msg)

    @staticmethod
    def error(msg: str) -> None:
        Logger.log("ERROR", msg)

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
            except (requests.RequestException, ValueError):
                Logger.warn(f"Network error on {method} (attempt {attempt})")
                time.sleep(2)
                continue

            if not result.get("ok", False):
                retry_after = result.get("parameters", {}).get("retry_after", 0)
                if retry_after:
                    Logger.warn(f"Rate limited. Sleeping {retry_after}s...")
                    time.sleep(int(retry_after) + 1)
                    continue

            return result

        Logger.error(f"Failed after {self.retry_limit} attempts: {method}")
        return {"ok": False, "description": "Max retries exceeded"}

    def send_message(self, chat_id: int | str, text: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        payload.update(extra or {})
        return self.call("sendMessage", payload)

    def edit_message(self, chat_id: int | str, msg_id: int, text: str) -> dict[str, Any]:
        return self.call("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
        })

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def copy_message(self, to: int | str, thread_id: int, frm: int | str, msg_id: int) -> dict[str, Any]:
        return self.call("copyMessage", {
            "chat_id": to,
            "message_thread_id": thread_id,
            "from_chat_id": frm,
            "message_id": msg_id,
        })

    def delete_message(self, chat_id: int | str, msg_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    def create_forum_topic(self, chat_id: int | str, name: str) -> dict[str, Any]:
        return self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})

    def get_updates(self, offset: int, timeout: int = 30) -> dict[str, Any]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout})

# ============================================
# TOPIC MANAGER
# ============================================
class TopicManager:
    def __init__(self, tg: TelegramClient) -> None:
        self.tg = tg
        self.topics = {}

    def get_or_create(self, group_id: int | str, subject: str) -> int:
        # Simple cache - no file locking needed on Railway
        if subject in self.topics:
            return self.topics[subject]

        result = self.tg.create_forum_topic(group_id, subject)

        if not result.get("ok", False):
            Logger.error(f"Could not create topic [{subject}]: {json.dumps(result)}")
            return 0

        thread_id = int(result["result"]["message_thread_id"])
        self.topics[subject] = thread_id
        Logger.info(f"Created topic [{subject}] -> thread #{thread_id}")
        return thread_id

    def reset(self) -> None:
        self.topics = {}
        Logger.info("Topics reset.")

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
        if not caption.strip():
            return "📂 Others"

        title = ""
        for pattern in SubjectDetector.PATTERNS:
            m = pattern.search(caption)
            if m:
                title = m.group(1).strip()
                break

        if title == "":
            return "📂 Others"

        title = re.sub(r"\s+", " ", title).strip()
        title = title[:50]
        return title if title != "" else "📂 Others"

# ============================================
# ACCESS GUARD
# ============================================
class AccessGuard:
    def __init__(self, allowed_user_ids: list[int]) -> None:
        self.allowed = allowed_user_ids

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed

# ============================================
# KEYBOARDS
# ============================================
class Keyboards:
    @staticmethod
    def main() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "📥 Source", "callback_data": "set_source"},
                    {"text": "📤 Destination", "callback_data": "set_destination"},
                ],
                [
                    {"text": "🔗 Start ID", "callback_data": "set_startid"},
                    {"text": "🔗 End ID", "callback_data": "set_endid"},
                ],
                [
                    {"text": "🚀 Start Copy", "callback_data": "start"},
                    {"text": "🛑 Stop", "callback_data": "stop"},
                ],
                [
                    {"text": "📊 Status", "callback_data": "status"},
                    {"text": "♻️ Reset", "callback_data": "reset"},
                ],
            ],
        }

    @staticmethod
    def confirm(action: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [[
                {"text": "✅ Yes", "callback_data": f"confirm_{action}"},
                {"text": "❌ No", "callback_data": "back_main"},
            ]],
        }

# ============================================
# FORWARDER ENGINE - RAILWAY OPTIMIZED
# ============================================
class ForwarderEngine:
    def __init__(self, tg: TelegramClient, topics: TopicManager) -> None:
        self.tg = tg
        self.topics = topics
        self.running = False
        self.current_pos = START_MESSAGE_ID
        self.end_id = END_MESSAGE_ID

    def run(self, operator_chat_id: int | str, status_msg_id: int) -> None:
        self.running = True
        source = SOURCE_CHANNEL
        destination = DESTINATION_GROUP
        start = self.current_pos
        end = self.end_id

        copied = 0
        skipped = 0
        errors = 0
        total = end - start + 1
        subject = ""

        Logger.info(f"Forwarder started: msg {start}->{end}")

        msg_id = start
        while msg_id <= end and self.running:
            # Get message info via forward to operator
            temp = self.tg.send_message(operator_chat_id, f"📥 Fetching message {msg_id}...")
            temp_msg_id = temp.get("result", {}).get("message_id", 0)

            # Get message caption via forward
            forward = self.tg.call("forwardMessage", {
                "chat_id": operator_chat_id,
                "from_chat_id": source,
                "message_id": msg_id
            })

            if not forward.get("ok", False):
                skipped += 1
                msg_id += 1
                continue

            temp_forward_id = forward.get("result", {}).get("message_id", 0)
            caption = forward.get("result", {}).get("caption") or ""

            # Delete temp messages
            if temp_msg_id:
                self.tg.delete_message(operator_chat_id, temp_msg_id)
            if temp_forward_id:
                self.tg.delete_message(operator_chat_id, temp_forward_id)

            # Detect subject -> create topic
            subject = SubjectDetector.detect(caption)
            thread_id = self.topics.get_or_create(destination, subject)

            if thread_id == 0:
                errors += 1
                msg_id += 1
                continue

            # Copy to destination topic
            copy = self.tg.copy_message(destination, thread_id, source, msg_id)

            if not copy.get("ok", False):
                errors += 1
                Logger.warn(f"Copy failed for msg {msg_id}")
            else:
                copied += 1

            # Update status every 10 messages
            if msg_id % 10 == 0 or msg_id == end:
                done = msg_id - start + 1
                pct = int(round((done / total) * 100)) if total > 0 else 0
                bar = "█" * int(pct/10) + "░" * (10 - int(pct/10))

                self.tg.edit_message(
                    operator_chat_id,
                    status_msg_id,
                    f"📨 <b>COPYING</b>\n\n"
                    f"{bar} {pct}%\n"
                    f"📌 {msg_id}/{end}\n"
                    f"📂 {subject}\n"
                    f"✅ {copied} | ⏭ {skipped} | ❌ {errors}"
                )

            time.sleep(0.5)
            msg_id += 1

        self.running = False
        Logger.info(f"Forwarder done. Copied:{copied} Skipped:{skipped} Errors:{errors}")
        self.tg.edit_message(
            operator_chat_id,
            status_msg_id,
            f"✅ <b>COMPLETED!</b>\n"
            f"Copied: {copied}\n"
            f"Skipped: {skipped}\n"
            f"Errors: {errors}"
        )

    def stop(self):
        self.running = False
        Logger.info("Stop signal received")

    def reset(self):
        self.stop()
        self.current_pos = START_MESSAGE_ID
        self.topics.reset()
        Logger.info("Reset complete")

# ============================================
# BOT CONTROLLER
# ============================================
class BotController:
    def __init__(self, tg: TelegramClient, guard: AccessGuard, engine: ForwarderEngine):
        self.tg = tg
        self.guard = guard
        self.engine = engine
        self.status_msg_id = None
        self.chat_id = None

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _check_access(self, user_id: int, chat_id: int | str) -> bool:
        if self.guard.is_allowed(user_id):
            self.chat_id = chat_id
            return True
        self.tg.send_message(chat_id, "🚫 Unauthorized")
        return False

    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "").strip()

        if not self._check_access(user_id, chat_id):
            return

        if text == "/start":
            self._send_main_menu(chat_id)
        elif text == "/status":
            self._send_status(chat_id)
        elif text == "/help":
            self._send_help(chat_id)

    def _handle_callback(self, cb: dict[str, Any]) -> None:
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        data = cb["data"]

        self.tg.answer_callback(cb["id"])

        if not self._check_access(user_id, chat_id):
            return

        if data == "back_main":
            self._send_main_menu(chat_id)
        elif data == "set_source":
            self.tg.send_message(chat_id, "📥 Send new Source channel ID or @username (restart bot to apply)")
        elif data == "set_destination":
            self.tg.send_message(chat_id, "📤 Send new Destination group ID (restart bot to apply)")
        elif data == "set_startid":
            self.tg.send_message(chat_id, "🔗 Send Start Message ID (number)")
        elif data == "set_endid":
            self.tg.send_message(chat_id, "🔗 Send End Message ID (number)")
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

    def _send_main_menu(self, chat_id: int | str) -> None:
        self.tg.send_message(
            chat_id,
            f"🤖 <b>Topic Forwarder Bot</b>\n"
            f"📥 Source: {SOURCE_CHANNEL}\n"
            f"📤 Dest: {DESTINATION_GROUP}\n"
            f"🔢 Range: {START_MESSAGE_ID}→{END_MESSAGE_ID}\n"
            f"🟢 Running: {self.engine.running}",
            {"reply_markup": json.dumps(Keyboards.main())}
        )

    def _send_status(self, chat_id: int | str) -> None:
        self.tg.send_message(
            chat_id,
            f"📊 <b>STATUS</b>\n"
            f"Running: {self.engine.running}\n"
            f"Position: {self.engine.current_pos}/{self.engine.end_id}\n"
            f"Source: {SOURCE_CHANNEL}\n"
            f"Destination: {DESTINATION_GROUP}"
        )

    def _send_help(self, chat_id: int | str) -> None:
        self.tg.send_message(chat_id, "📖 /start - Menu\n/status - Check\n/help - This")

    def _do_start(self, chat_id: int | str) -> None:
        if self.engine.running:
            self.tg.send_message(chat_id, "⚠️ Already running!")
            return

        status = self.tg.send_message(chat_id, "🚀 Starting...")
        self.status_msg_id = status.get("result", {}).get("message_id", 0)

        if self.status_msg_id:
            import threading
            thread = threading.Thread(target=self.engine.run, args=(chat_id, self.status_msg_id))
            thread.daemon = True
            thread.start()
            self.tg.send_message(chat_id, "✅ Started! Check status.")

    def _do_stop(self, chat_id: int | str) -> None:
        self.engine.stop()
        self.tg.send_message(chat_id, "🛑 Stopped!")

    def _do_reset(self, chat_id: int | str) -> None:
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

    Logger.info("═══════════════════════════════")
    Logger.info("  Topic Forwarder Bot v3.0")
    Logger.info(f"  Source: {SOURCE_CHANNEL}")
    Logger.info(f"  Dest: {DESTINATION_GROUP}")
    Logger.info(f"  Range: {START_MESSAGE_ID}→{END_MESSAGE_ID}")
    Logger.info(f"  Users: {ALLOWED_USERS}")
    Logger.info("═══════════════════════════════")

    tg = TelegramClient(BOT_TOKEN)
    topics = TopicManager(tg)
    guard = AccessGuard(ALLOWED_USERS)
    engine = ForwarderEngine(tg, topics)
    controller = BotController(tg, guard, engine)

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