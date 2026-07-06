#!/usr/bin/env python3
"""
============================================================
 ULTRA PRO MAX - Telegram Topic Forwarder Bot
 Version  : 3.0.0 (Python port)
 Python   : 3.10+
 Standards: PEP 8, type hints, OOP, Security-First
============================================================

 SETUP:
 1. Set BOT_TOKEN, SOURCE_CHANNEL, DESTINATION_GROUP below
    (or, better, via environment variables - see note at bottom).
 2. Set ALLOWED_USERS - only these Telegram user IDs can
    control the bot (find yours via @userinfobot).
 3. Run:  python3 telegram_forwarder_bot.py
============================================================
"""

from __future__ import annotations

import json
import os
import re
import time
import fcntl
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  (edit only this section)
# ─────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")   # <- Replace
SOURCE_CHANNEL: str = os.environ.get("SOURCE_CHANNEL", "@your_source_channel")
DESTINATION_GROUP: str = os.environ.get("DESTINATION_GROUP", "@your_destination_group")
START_MESSAGE_ID: int = 15
END_MESSAGE_ID: int = 2287

# SECURITY: Only these Telegram user IDs can operate the bot
ALLOWED_USERS: list[int] = [
    123456789,   # <- Replace with your Telegram numeric user ID
]

# Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
TOPICS_FILE = DATA_DIR / "topics.json"
LOG_FILE = DATA_DIR / "bot.log"

# ─────────────────────────────────────────────────────────────
#  BOOTSTRAP
# ─────────────────────────────────────────────────────────────
DATA_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)

for f in (CONFIG_FILE, TOPICS_FILE):
    if not f.exists():
        f.write_text(json.dumps({}, indent=2))


# ─────────────────────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────────────────────
class Logger:
    @staticmethod
    def log(level: str, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level.upper()}] {message}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as fp:
            fcntl.flock(fp, fcntl.LOCK_EX)
            fp.write(line)
            fcntl.flock(fp, fcntl.LOCK_UN)
        print(line, end="")

    @staticmethod
    def info(msg: str) -> None:
        Logger.log("INFO", msg)

    @staticmethod
    def warn(msg: str) -> None:
        Logger.log("WARN", msg)

    @staticmethod
    def error(msg: str) -> None:
        Logger.log("ERROR", msg)


# ─────────────────────────────────────────────────────────────
#  TELEGRAM API CLIENT
# ─────────────────────────────────────────────────────────────
class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.retry_limit = 3

    def call(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call any Telegram Bot API method with automatic retry on 429."""
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

            if not isinstance(result, dict):
                Logger.warn(f"Invalid JSON from {method}")
                return {"ok": False, "description": "Invalid JSON"}

            if not result.get("ok", False):
                retry_after = result.get("parameters", {}).get("retry_after", 0)
                if retry_after:
                    Logger.warn(f"Rate limited. Sleeping {retry_after}s...")
                    time.sleep(int(retry_after) + 1)
                    continue

            return result

        Logger.error(f"Failed after {self.retry_limit} attempts: {method}")
        return {"ok": False, "description": "Max retries exceeded"}

    # ── Convenience wrappers ──────────────────────────────────

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

    def forward_message(self, to: int | str, frm: int | str, msg_id: int) -> dict[str, Any]:
        return self.call("forwardMessage", {
            "chat_id": to,
            "from_chat_id": frm,
            "message_id": msg_id,
        })

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


# ─────────────────────────────────────────────────────────────
#  CONFIG STORE  (atomic read/write with file locking)
# ─────────────────────────────────────────────────────────────
class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        with open(self.path, "r+", encoding="utf-8") as fp:
            fcntl.flock(fp, fcntl.LOCK_SH)
            try:
                content = fp.read()
                data = json.loads(content) if content.strip() else {}
            finally:
                fcntl.flock(fp, fcntl.LOCK_UN)
        return data

    def save(self, data: dict[str, Any]) -> None:
        with open(self.path, "w", encoding="utf-8") as fp:
            fcntl.flock(fp, fcntl.LOCK_EX)
            try:
                fp.truncate(0)
                fp.seek(0)
                fp.write(json.dumps(data, indent=2, ensure_ascii=False))
                fp.flush()
            finally:
                fcntl.flock(fp, fcntl.LOCK_UN)

    @staticmethod
    def get(data: dict[str, Any], key: str, default: Any = None) -> Any:
        return data.get(key, default)


# ─────────────────────────────────────────────────────────────
#  TOPIC MANAGER
# ─────────────────────────────────────────────────────────────
class TopicManager:
    def __init__(self, tg: TelegramClient, store: ConfigStore) -> None:
        self.tg = tg
        self.store = store

    def get_or_create(self, group_id: int | str, subject: str) -> int:
        """Return existing forum topic thread ID or create a new one."""
        topics = self.store.load()

        if subject in topics:
            return int(topics[subject])

        result = self.tg.create_forum_topic(group_id, subject)

        if not result.get("ok", False):
            Logger.error(f"Could not create topic [{subject}]: {json.dumps(result)}")
            return 0

        thread_id = int(result["result"]["message_thread_id"])
        topics[subject] = thread_id
        self.store.save(topics)

        Logger.info(f"Created topic [{subject}] -> thread #{thread_id}")
        return thread_id

    def reset(self) -> None:
        self.store.save({})
        Logger.info("Topics reset.")


# ─────────────────────────────────────────────────────────────
#  SUBJECT DETECTOR
# ─────────────────────────────────────────────────────────────
class SubjectDetector:
    # Zero-width / invisible characters that "fancy text" caption generators
    # often sprinkle between letters (ZWSP, ZWJ, ZWNJ, word-joiner, BOM,
    # soft hyphen, various Unicode spaces). These silently break exact-text
    # regex matching, which is the #1 cause of "everything falls into one
    # topic" when captions look fine to the human eye.
    _INVISIBLE_CHARS = re.compile(
        "[\u200B\u200C\u200D\u200E\u200F\u2060\uFEFF\u00AD"
        "\u2000-\u200A\u202F\u205F\u3000]"
    )

    PATTERNS = [
        re.compile(r"T\s*ɪ\s*ᴛ\s*ʟ\s*ᴇ\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),  # small-caps "Tɪᴛʟᴇ" (spaced-out safe)
        re.compile(r"T\s*i\s*t\s*l\s*e\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),   # plain "Title"
        re.compile(r"S\s*u\s*b\s*j\s*e\s*c\s*t\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),
        re.compile(r"ɴ\s*ᴀ\s*ᴍ\s*ᴇ\s*:\s*(.+)", re.IGNORECASE | re.UNICODE),       # small-caps "Name"
    ]

    @staticmethod
    def _clean(text: str) -> str:
        """Strip invisible/zero-width characters that break exact matching."""
        return SubjectDetector._INVISIBLE_CHARS.sub("", text)

    @staticmethod
    def detect(caption: str) -> str:
        """Extract a clean subject/title from a message caption."""
        if not caption.strip():
            return "📂 Others"

        cleaned = SubjectDetector._clean(caption)

        title = ""
        for pattern in SubjectDetector.PATTERNS:
            m = pattern.search(cleaned)
            if m:
                title = m.group(1).strip()
                break

        if title == "":
            # Log the raw caption so misses are debuggable from bot.log
            # instead of silently dumping everything into one topic.
            preview = caption.replace("\n", " ⏎ ")[:120]
            Logger.warn(f"SubjectDetector: no title pattern matched. Caption preview: {preview}")
            return "📂 Others"

        # Clean up dates, part numbers, excess whitespace
        title = re.sub(r"\d{2}-\d{2}-\d{4}", "", title)
        title = re.sub(r"PART[-\s]*\d+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip()
        title = title[:50]

        return title if title != "" else "📂 Others"


# ─────────────────────────────────────────────────────────────
#  ACCESS GUARD
# ─────────────────────────────────────────────────────────────
class AccessGuard:
    def __init__(self, allowed_user_ids: list[int]) -> None:
        self.allowed = allowed_user_ids

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed


# ─────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────
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
                    {"text": "🔗 Start Link", "callback_data": "set_startlink"},
                    {"text": "🔗 End Link", "callback_data": "set_endlink"},
                ],
                [
                    {"text": "⚡ Speed", "callback_data": "menu_speed"},
                    {"text": "📊 Status", "callback_data": "status"},
                ],
                [
                    {"text": "🚀 Start Copy", "callback_data": "start"},
                    {"text": "🛑 Stop", "callback_data": "stop"},
                ],
                [
                    {"text": "♻️ Reset All", "callback_data": "reset"},
                ],
            ],
        }

    @staticmethod
    def speed() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🐢 Slow   (3s delay)", "callback_data": "speed_3000000"}],
                [{"text": "⚡ Medium (0.5s delay)", "callback_data": "speed_500000"}],
                [{"text": "🚀 Fast   (0.15s delay)", "callback_data": "speed_150000"}],
                [{"text": "⬅️ Back", "callback_data": "back_main"}],
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


# ─────────────────────────────────────────────────────────────
#  FORWARDER ENGINE
# ─────────────────────────────────────────────────────────────
class ForwarderEngine:
    def __init__(self, tg: TelegramClient, topics: TopicManager, config_store: ConfigStore) -> None:
        self.tg = tg
        self.topics = topics
        self.config_store = config_store

    def run(self, operator_chat_id: int | str, status_msg_id: int, session: dict[str, Any]) -> None:
        source = session["source"]
        destination = session["destination"]
        start = int(session["current"])
        end = int(session["end_id"])
        speed = int(session.get("speed", 500_000))  # microseconds, matches PHP usleep()
        chat_id_key = str(operator_chat_id)

        copied = 0
        skipped = 0
        errors = 0
        total = end - start + 1
        subject = ""

        Logger.info(f"Forwarder started: msg {start}->{end} from {source} to {destination}")

        msg_id = start
        while msg_id <= end:
            # ── Stop signal check ─────────────────────────────
            cfg = self.config_store.load()
            if cfg.get(chat_id_key, {}).get("stop", False) is True:
                self.tg.edit_message(
                    operator_chat_id,
                    status_msg_id,
                    f"🛑 <b>STOPPED</b> at message <code>{msg_id}</code>\n"
                    f"✅ Copied: {copied} | ⏭ Skipped: {skipped} | ❌ Errors: {errors}",
                )
                Logger.info(f"Forwarder stopped at msg {msg_id}.")
                return

            # ── Progress save ─────────────────────────────────
            cfg.setdefault(chat_id_key, {})["current"] = msg_id + 1
            self.config_store.save(cfg)

            # ── Forward to operator (to read caption/text) ────
            temp = self.tg.forward_message(operator_chat_id, source, msg_id)

            if not temp.get("ok", False):
                skipped += 1
                msg_id += 1
                continue

            temp_msg_id = int(temp.get("result", {}).get("message_id", 0))
            caption = temp.get("result", {}).get("caption") or temp.get("result", {}).get("text") or ""

            # ── Clean up temp message ─────────────────────────
            if temp_msg_id > 0:
                self.tg.delete_message(operator_chat_id, temp_msg_id)

            # ── Detect subject → get/create topic ────────────
            subject = SubjectDetector.detect(caption)
            thread_id = self.topics.get_or_create(destination, subject)

            if thread_id == 0:
                errors += 1
                msg_id += 1
                continue

            # ── Copy to destination topic ─────────────────────
            copy = self.tg.copy_message(destination, thread_id, source, msg_id)

            if not copy.get("ok", False):
                errors += 1
                Logger.warn(f"Copy failed for msg {msg_id}: {json.dumps(copy)}")
            else:
                copied += 1

            # ── Update status every 5 messages ───────────────
            if msg_id % 5 == 0 or msg_id == end:
                done = msg_id - start + 1
                pct = int(round((done / total) * 100)) if total > 0 else 0
                bar = self._progress_bar(pct)

                self.tg.edit_message(
                    operator_chat_id,
                    status_msg_id,
                    f"📨 <b>COPYING IN PROGRESS</b>\n\n"
                    f"{bar} {pct}%\n\n"
                    f"📌 Message: <code>{msg_id}</code> / <code>{end}</code>\n"
                    f"📂 Topic: <b>{_html_escape(subject)}</b>\n\n"
                    f"✅ Copied: {copied} | ⏭ Skipped: {skipped} | ❌ Errors: {errors}",
                )

            time.sleep(speed / 1_000_000)  # PHP usleep() takes microseconds
            msg_id += 1

        # ── Completion ────────────────────────────────────────
        self.tg.edit_message(
            operator_chat_id,
            status_msg_id,
            f"🎉 <b>COPY COMPLETED!</b>\n\n"
            f"✅ Copied: {copied}\n"
            f"⏭ Skipped: {skipped}\n"
            f"❌ Errors: {errors}\n\n"
            f"📥 Source: <code>{source}</code>\n"
            f"📤 Destination: <code>{destination}</code>",
        )

        Logger.info(f"Forwarder done. Copied:{copied} Skipped:{skipped} Errors:{errors}")

    @staticmethod
    def _progress_bar(pct: int) -> str:
        filled = int(round(pct / 10))
        return "█" * filled + "░" * (10 - filled)


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────
#  BOT CONTROLLER
# ─────────────────────────────────────────────────────────────
class BotController:
    def __init__(self, tg: TelegramClient, config: ConfigStore, guard: AccessGuard, engine: ForwarderEngine) -> None:
        self.tg = tg
        self.config = config
        self.guard = guard
        self.engine = engine

    # ── Entry point ───────────────────────────────────────────
    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    # ── Access check helper ───────────────────────────────────
    def _check_access(self, user_id: int, chat_id: int | str) -> bool:
        if self.guard.is_allowed(user_id):
            return True
        self.tg.send_message(chat_id, "🚫 <b>Unauthorized.</b> You are not allowed to use this bot.")
        Logger.warn(f"Unauthorized access attempt by user {user_id}")
        return False

    # ── Session helpers ───────────────────────────────────────
    def _load_session(self, chat_id: int | str) -> dict[str, Any]:
        cfg = self.config.load()
        key = str(chat_id)

        if key not in cfg:
            cfg[key] = {
                "source": SOURCE_CHANNEL,
                "destination": DESTINATION_GROUP,
                "start_id": START_MESSAGE_ID,
                "end_id": END_MESSAGE_ID,
                "current": START_MESSAGE_ID,
                "speed": 500_000,
                "step": "",
                "stop": False,
            }
            self.config.save(cfg)

        return cfg[key]

    def _save_session(self, chat_id: int | str, session: dict[str, Any]) -> None:
        cfg = self.config.load()
        cfg[str(chat_id)] = session
        self.config.save(cfg)

    # ── Callback handler ──────────────────────────────────────
    def _handle_callback(self, cb: dict[str, Any]) -> None:
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        data = cb["data"]

        self.tg.answer_callback(cb["id"])

        if not self._check_access(user_id, chat_id):
            return

        session = self._load_session(chat_id)

        if data == "back_main":
            self._send_main_menu(chat_id, session)
        elif data == "menu_speed":
            self.tg.send_message(chat_id, "⚡ <b>Select copy speed:</b>", {"reply_markup": json.dumps(Keyboards.speed())})
        elif data == "status":
            self._send_status(chat_id, session)
        elif data == "set_source":
            self._prompt_step(chat_id, session, "source", "📥 Send the <b>Source Channel</b> ID or @username:")
        elif data == "set_destination":
            self._prompt_step(chat_id, session, "destination", "📤 Send the <b>Destination Forum Group</b> ID or @username:")
        elif data == "set_startlink":
            self._prompt_step(chat_id, session, "startlink", "🔗 Send the <b>Start Message Link</b> (e.g. https://t.me/chan/15):")
        elif data == "set_endlink":
            self._prompt_step(chat_id, session, "endlink", "🔗 Send the <b>End Message Link</b>:")
        elif data == "stop":
            self._do_stop(chat_id, session)
        elif data == "reset":
            self.tg.send_message(chat_id, "♻️ <b>Reset all config?</b> This will also clear all topics.", {"reply_markup": json.dumps(Keyboards.confirm("reset"))})
        elif data == "confirm_reset":
            self._do_reset(chat_id)
        elif data == "start":
            self._do_start(chat_id, session)
        elif data.startswith("speed_"):
            self._set_speed(chat_id, session, data)

    # ── Message handler ───────────────────────────────────────
    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = (msg.get("text") or "").strip()

        if not self._check_access(user_id, chat_id):
            return

        if text == "/start":
            session = self._load_session(chat_id)
            self._send_main_menu(chat_id, session)
            return

        if text == "/status":
            session = self._load_session(chat_id)
            self._send_status(chat_id, session)
            return

        if text == "/help":
            self._send_help(chat_id)
            return

        # Handle pending step input
        session = self._load_session(chat_id)
        step = session.get("step", "")

        if step == "":
            return

        if step == "source":
            self._save_field(chat_id, session, "source", text, "📥 Source saved")
        elif step == "destination":
            self._save_field(chat_id, session, "destination", text, "📤 Destination saved")
        elif step == "startlink":
            self._save_link_field(chat_id, session, "start_id", "current", text, "✅ Start ID saved")
        elif step == "endlink":
            self._save_link_field(chat_id, session, "end_id", None, text, "✅ End ID saved")

    # ── Action methods ────────────────────────────────────────
    def _send_main_menu(self, chat_id: int | str, session: dict[str, Any]) -> None:
        src = _html_escape(session["source"])
        dst = _html_escape(session["destination"])
        cur = session["current"]
        end = session["end_id"]

        self.tg.send_message(
            chat_id,
            "🤖 <b>ULTRA PRO MAX — Topic Forwarder Bot</b>\n\n"
            f"📥 Source: <code>{src}</code>\n"
            f"📤 Destination: <code>{dst}</code>\n"
            f"🔢 Range: <code>{cur}</code> → <code>{end}</code>\n\n"
            "Use the buttons below to configure and control the bot.",
            {"reply_markup": json.dumps(Keyboards.main())},
        )

    def _send_status(self, chat_id: int | str, session: dict[str, Any]) -> None:
        cur = session["current"]
        end = session["end_id"]
        start = session["start_id"]
        total = max(1, end - start + 1)
        done = max(0, cur - start)
        pct = int(round((done / total) * 100))
        filled = int(round(pct / 10))
        bar = "█" * filled + "░" * (10 - filled)
        src = _html_escape(session["source"])
        dst = _html_escape(session["destination"])
        speed_ms = round(session["speed"] / 1000)

        self.tg.send_message(
            chat_id,
            "📊 <b>STATUS</b>\n\n"
            f"{bar} <b>{pct}%</b>\n\n"
            f"📌 Current: <code>{cur}</code> / <code>{end}</code>\n"
            f"📥 Source: <code>{src}</code>\n"
            f"📤 Destination: <code>{dst}</code>\n"
            f"⚡ Speed delay: <code>{speed_ms} ms</code>",
            {"reply_markup": json.dumps(Keyboards.main())},
        )

    def _send_help(self, chat_id: int | str) -> None:
        self.tg.send_message(
            chat_id,
            "📖 <b>HELP</b>\n\n"
            "/start  — Open main menu\n"
            "/status — Show copy progress\n"
            "/help   — Show this message\n\n"
            "<b>How to use:</b>\n"
            "1. Set Source channel\n"
            "2. Set Destination forum group\n"
            "3. Set Start and End message links\n"
            "4. Select Speed\n"
            "5. Press 🚀 Start Copy\n\n"
            "<b>Bot auto-creates forum topics</b> based on the <code>Title:</code> field in captions.",
        )

    def _prompt_step(self, chat_id: int | str, session: dict[str, Any], step: str, prompt: str) -> None:
        session["step"] = step
        self._save_session(chat_id, session)
        self.tg.send_message(chat_id, prompt)

    def _save_field(self, chat_id: int | str, session: dict[str, Any], field_name: str, value: str, confirm: str) -> None:
        session[field_name] = value
        session["step"] = ""
        self._save_session(chat_id, session)
        self.tg.send_message(chat_id, f"✅ {confirm}: <code>{_html_escape(value)}</code>", {"reply_markup": json.dumps(Keyboards.main())})

    def _save_link_field(self, chat_id: int | str, session: dict[str, Any], id_field: str, current_field: str | None, text: str, confirm: str) -> None:
        # Accept raw ID or full message link
        m = re.search(r"/(\d+)$", text)
        if m:
            msg_id = int(m.group(1))
        elif text.isdigit():
            msg_id = int(text)
        else:
            self.tg.send_message(chat_id, "❌ Invalid format. Send a message link like <code>https://t.me/chan/100</code> or just the numeric ID.")
            return

        session[id_field] = msg_id
        if current_field is not None:
            session[current_field] = msg_id
        session["step"] = ""
        self._save_session(chat_id, session)
        self.tg.send_message(chat_id, f"✅ {confirm}: <code>{msg_id}</code>", {"reply_markup": json.dumps(Keyboards.main())})

    def _set_speed(self, chat_id: int | str, session: dict[str, Any], data: str) -> None:
        parts = data.split("_", 1)
        speed = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 500_000
        session["speed"] = speed
        self._save_session(chat_id, session)
        if speed >= 3_000_000:
            label = "🐢 Slow"
        elif speed >= 500_000:
            label = "⚡ Medium"
        else:
            label = "🚀 Fast"
        self.tg.send_message(chat_id, f"✅ Speed set to <b>{label}</b>", {"reply_markup": json.dumps(Keyboards.main())})

    def _do_stop(self, chat_id: int | str, session: dict[str, Any]) -> None:
        session["stop"] = True
        self._save_session(chat_id, session)
        self.tg.send_message(chat_id, "🛑 <b>Stop signal sent.</b> Current message will finish, then copy will halt.", {"reply_markup": json.dumps(Keyboards.main())})

    def _do_reset(self, chat_id: int | str) -> None:
        cfg = self.config.load()
        cfg.pop(str(chat_id), None)
        self.config.save(cfg)

        # Also reset topics
        TOPICS_FILE.write_text(json.dumps({}, indent=2))

        self.tg.send_message(chat_id, "♻️ <b>Reset complete!</b> All config and topics cleared.", {"reply_markup": json.dumps(Keyboards.main())})
        Logger.info(f"Config and topics reset by {chat_id}.")

    def _do_start(self, chat_id: int | str, session: dict[str, Any]) -> None:
        session["stop"] = False
        self._save_session(chat_id, session)

        src = _html_escape(session["source"])
        dst = _html_escape(session["destination"])
        start = session["current"]
        end = session["end_id"]

        status_msg = self.tg.send_message(
            chat_id,
            "🚀 <b>COPY STARTED</b>\n\n"
            f"📥 From: <code>{src}</code>\n"
            f"📤 To: <code>{dst}</code>\n"
            f"🔢 Messages: <code>{start}</code> → <code>{end}</code>\n\n"
            "⏳ Initializing…",
        )

        status_msg_id = int(status_msg.get("result", {}).get("message_id", 0))

        if status_msg_id == 0:
            Logger.error("Could not send status message.")
            return

        self.engine.run(chat_id, status_msg_id, session)


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def main() -> None:
    # Validate config
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set your BOT_TOKEN in the configuration section.")
        raise SystemExit(1)

    if not ALLOWED_USERS:
        print("❌ ERROR: ALLOWED_USERS must not be empty. Add your Telegram user ID.")
        raise SystemExit(1)

    # Boot
    tg = TelegramClient(BOT_TOKEN)
    config_store = ConfigStore(CONFIG_FILE)
    topics_store = ConfigStore(TOPICS_FILE)
    topic_mgr = TopicManager(tg, topics_store)
    guard = AccessGuard(ALLOWED_USERS)
    engine = ForwarderEngine(tg, topic_mgr, config_store)
    controller = BotController(tg, config_store, guard, engine)

    Logger.info("═══════════════════════════════════════════")
    Logger.info("  ULTRA PRO MAX — Topic Forwarder Bot v3.0")
    Logger.info(f"  Source      : {SOURCE_CHANNEL}")
    Logger.info(f"  Destination : {DESTINATION_GROUP}")
    Logger.info(f"  Msg Range   : {START_MESSAGE_ID} → {END_MESSAGE_ID}")
    Logger.info("═══════════════════════════════════════════")

    update_id = 0

    while True:
        try:
            updates = tg.get_updates(update_id + 1, 30)

            if not updates.get("ok", False):
                time.sleep(2)
                continue

            for update in updates.get("result", []):
                update_id = int(update["update_id"])
                controller.handle_update(update)
        except Exception as e:  # noqa: BLE001 - top-level guard, mirrors PHP's catch (Throwable)
            Logger.error(f"Unhandled exception: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
