#!/usr/bin/env python3
# =============================================================================
#  STORM HOSTING BOT - DATABASE FIXED
#  Added proper error handling and logging
# =============================================================================

import os
import sys
import time
import json
import sqlite3
import logging
import hashlib
import zipfile
import threading
import subprocess
import traceback
import shutil
import re
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from collections import defaultdict

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# =============================================================================
#  CONFIGURATION
# =============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8217100195:AAHeIQKkLWqFzPL2yDO8EPS-z9YdSVhKMlk")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "7007475122")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

DB_PATH = "storm_hosting.db"
UPLOADS_DIR = "uploads"
LOGS_DIR = "logs"
MAX_FILE_SIZE_MB = 5
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSIONS = {".py", ".js", ".zip"}
FREE_SLOTS = 1
PREMIUM_SLOTS = 20
RATE_LIMIT_SECONDS = 2
BOT_START_TIME = time.time()
UPDATES_CHANNEL = os.environ.get("UPDATES_CHANNEL", "@parth_hereee")

# Security DISABLED
DANGEROUS_PATTERNS = []

# =============================================================================
#  LOGGING SETUP
# =============================================================================

Path(LOGS_DIR).mkdir(exist_ok=True)
Path(UPLOADS_DIR).mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more details
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"{LOGS_DIR}/storm_hosting.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("StormHosting")

# =============================================================================
#  BOT INSTANCE
# =============================================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# =============================================================================
#  IN-MEMORY STATE
# =============================================================================

running_processes: dict[int, dict[str, subprocess.Popen]] = defaultdict(dict)
rate_limit_tracker: dict[int, float] = {}
bot_locked = False
force_join_channel: str | None = None
user_states: dict[int, str] = {}
pending_data: dict[int, dict] = {}

state_lock = threading.Lock()
process_lock = threading.Lock()

# =============================================================================
#  DATABASE SETUP
# =============================================================================

def get_db() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

def init_db():
    try:
        with get_db() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL DEFAULT '',
                username    TEXT    DEFAULT '',
                bio         TEXT    DEFAULT '',
                join_date   TEXT    NOT NULL,
                last_active TEXT    NOT NULL,
                is_premium  INTEGER NOT NULL DEFAULT 0,
                premium_expiry TEXT DEFAULT NULL,
                is_banned   INTEGER NOT NULL DEFAULT 0,
                is_admin    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                file_id     TEXT    NOT NULL,
                filename    TEXT    NOT NULL,
                filepath    TEXT    NOT NULL,
                filetype    TEXT    NOT NULL,
                filesize    INTEGER NOT NULL DEFAULT 0,
                upload_date TEXT    NOT NULL,
                is_running  INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                level       TEXT    NOT NULL DEFAULT 'INFO',
                category    TEXT    NOT NULL,
                user_id     INTEGER DEFAULT NULL,
                message     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS broadcast_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER NOT NULL,
                message     TEXT    NOT NULL,
                sent_at     TEXT    NOT NULL,
                success_count INTEGER DEFAULT 0,
                fail_count  INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('bot_locked', '0');
            INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('force_join_channel', '');
            """)
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

def load_settings():
    global bot_locked, force_join_channel
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
            settings = {r["key"]: r["value"] for r in rows}
        bot_locked = settings.get("bot_locked", "0") == "1"
        fc = settings.get("force_join_channel", "")
        force_join_channel = fc if fc else None
        logger.info(f"Settings loaded. Locked={bot_locked}, ForceJoin={force_join_channel}")
    except Exception as e:
        logger.error(f"Error loading settings: {e}")

def save_setting(key: str, value: str):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                (key, value)
            )
    except Exception as e:
        logger.error(f"Error saving setting {key}: {e}")

# =============================================================================
#  DATABASE HELPERS
# =============================================================================

def db_get_user(user_id: int):
    try:
        with get_db() as conn:
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None

def db_register_user(user_id: int, name: str, username: str, bio: str = ""):
    try:
        now = datetime.now().isoformat()
        with get_db() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO users
                (user_id, name, username, bio, join_date, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, name, username or "", bio, now, now))
        logger.info(f"Registered user {user_id} ({name})")
    except Exception as e:
        logger.error(f"Error registering user {user_id}: {e}")

def db_update_last_active(user_id: int):
    try:
        now = datetime.now().isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET last_active = ? WHERE user_id = ?",
                (now, user_id)
            )
    except Exception as e:
        logger.error(f"Error updating last_active for {user_id}: {e}")

def db_update_user_info(user_id: int, name: str, username: str):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET name = ?, username = ? WHERE user_id = ?",
                (name, username or "", user_id)
            )
    except Exception as e:
        logger.error(f"Error updating user info for {user_id}: {e}")

def db_get_all_users() -> list:
    try:
        with get_db() as conn:
            return conn.execute("SELECT * FROM users").fetchall()
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []

def db_ban_user(user_id: int):
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")

def db_unban_user(user_id: int):
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.error(f"Error unbanning user {user_id}: {e}")

def db_set_premium(user_id: int, days: int):
    try:
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET is_premium = 1, premium_expiry = ? WHERE user_id = ?",
                (expiry, user_id)
            )
    except Exception as e:
        logger.error(f"Error setting premium for {user_id}: {e}")

def db_remove_premium(user_id: int):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET is_premium = 0, premium_expiry = NULL WHERE user_id = ?",
                (user_id,)
            )
    except Exception as e:
        logger.error(f"Error removing premium for {user_id}: {e}")

def db_add_file(user_id: int, file_id: str, filename: str, filepath: str,
                filetype: str, filesize: int) -> int:
    try:
        now = datetime.now().isoformat()
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO files (user_id, file_id, filename, filepath, filetype, filesize, upload_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, file_id, filename, filepath, filetype, filesize, now))
            return cur.lastrowid
    except Exception as e:
        logger.error(f"Error adding file for {user_id}: {e}")
        return -1

def db_get_user_files(user_id: int) -> list:
    try:
        with get_db() as conn:
            return conn.execute(
                "SELECT * FROM files WHERE user_id = ? ORDER BY upload_date DESC",
                (user_id,)
            ).fetchall()
    except Exception as e:
        logger.error(f"Error getting files for {user_id}: {e}")
        return []

def db_get_file(file_id: str):
    try:
        with get_db() as conn:
            return conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    except Exception as e:
        logger.error(f"Error getting file {file_id}: {e}")
        return None

def db_delete_file(file_id: str):
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
    except Exception as e:
        logger.error(f"Error deleting file {file_id}: {e}")

def db_set_file_running(file_id: str, running: bool):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE files SET is_running = ? WHERE file_id = ?",
                (1 if running else 0, file_id)
            )
    except Exception as e:
        logger.error(f"Error setting file running {file_id}: {e}")

def db_get_all_files() -> list:
    try:
        with get_db() as conn:
            return conn.execute(
                "SELECT f.*, u.name, u.username FROM files f JOIN users u ON f.user_id = u.user_id ORDER BY f.upload_date DESC"
            ).fetchall()
    except Exception as e:
        logger.error(f"Error getting all files: {e}")
        return []

def db_get_running_files() -> list:
    try:
        with get_db() as conn:
            return conn.execute(
                "SELECT f.*, u.name, u.username FROM files f JOIN users u ON f.user_id = u.user_id WHERE f.is_running = 1"
            ).fetchall()
    except Exception as e:
        logger.error(f"Error getting running files: {e}")
        return []

def db_add_log(category: str, message: str, user_id: int = None, level: str = "INFO"):
    try:
        now = datetime.now().isoformat()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO logs (timestamp, level, category, user_id, message) VALUES (?, ?, ?, ?, ?)",
                (now, level, category, user_id, message)
            )
    except Exception as e:
        logger.error(f"Error adding log: {e}")

def db_get_logs(limit: int = 50) -> list:
    try:
        with get_db() as conn:
            return conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return []

def db_get_stats() -> dict:
    try:
        with get_db() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            running_files = conn.execute("SELECT COUNT(*) FROM files WHERE is_running = 1").fetchone()[0]
            premium_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1").fetchone()[0]
            banned_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
        return {
            "total_users": total_users,
            "total_files": total_files,
            "running_files": running_files,
            "premium_users": premium_users,
            "banned_users": banned_users,
        }
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return {"total_users": 0, "total_files": 0, "running_files": 0, "premium_users": 0, "banned_users": 0}

# =============================================================================
#  KEYBOARD BUILDERS
# =============================================================================

def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📂 Check Files"),
        KeyboardButton("📤 Upload File"),
    )
    kb.add(
        KeyboardButton("📊 Statistics"),
        KeyboardButton("⚡ Bot Speed"),
    )
    kb.add(
        KeyboardButton("💳 My Plan"),
        KeyboardButton("📢 Updates Channel"),
    )
    kb.add(KeyboardButton("📞 Contact Owner"))
    if user_id in ADMIN_IDS or is_admin_in_db(user_id):
        kb.add(KeyboardButton("👑 Admin Panel"))
    return kb

def admin_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📂 All Files"),
        KeyboardButton("📤 Upload Logs"),
    )
    kb.add(
        KeyboardButton("📊 Statistics"),
        KeyboardButton("⚡ Running All Code"),
    )
    kb.add(
        KeyboardButton("📢 Broadcast"),
        KeyboardButton("🚫 Ban User"),
    )
    kb.add(
        KeyboardButton("🔓 Unban User"),
        KeyboardButton("💳 Subscriptions"),
    )
    kb.add(
        KeyboardButton("⏳ Set Force Join"),
        KeyboardButton("🔒 Lock Bot"),
    )
    kb.add(
        KeyboardButton("🧾 Logs"),
        KeyboardButton("🔙 Back"),
    )
    return kb

def file_action_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("▶️ Run File"),
        KeyboardButton("⏹ Stop File"),
    )
    kb.add(
        KeyboardButton("🗑 Delete File"),
        KeyboardButton("🔙 Back to Files"),
    )
    kb.add(KeyboardButton("🏠 Main Menu"))
    return kb

def cancel_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("❌ Cancel"))
    return kb

def duration_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("⏱ 7 Days"),
        KeyboardButton("⏱ 15 Days"),
    )
    kb.add(
        KeyboardButton("⏱ 30 Days"),
        KeyboardButton("⏱ 60 Days"),
    )
    kb.add(KeyboardButton("❌ Cancel"))
    return kb

def subscription_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("➕ Add Premium"),
        KeyboardButton("➖ Remove Premium"),
    )
    kb.add(KeyboardButton("🔙 Back to Admin"))
    return kb

# =============================================================================
#  UTILITY HELPERS
# =============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or is_admin_in_db(user_id)

def is_admin_in_db(user_id: int) -> bool:
    user = db_get_user(user_id)
    return bool(user and user["is_admin"])

def is_banned(user_id: int) -> bool:
    user = db_get_user(user_id)
    return bool(user and user["is_banned"])

def is_premium(user_id: int) -> bool:
    user = db_get_user(user_id)
    if not user:
        return False
    if not user["is_premium"]:
        return False
    if user["premium_expiry"]:
        expiry = datetime.fromisoformat(user["premium_expiry"])
        if datetime.now() > expiry:
            db_remove_premium(user_id)
            return False
    return True

def get_user_slot_limit(user_id: int) -> int:
    return PREMIUM_SLOTS if is_premium(user_id) else FREE_SLOTS

def get_user_running_count(user_id: int) -> int:
    with process_lock:
        return len([p for p in running_processes.get(user_id, {}).values()
                    if p.poll() is None])

def generate_file_id(user_id: int, filename: str) -> str:
    raw = f"{user_id}_{filename}_{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024*1024):.1f} MB"

def format_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

def safe_send(chat_id: int, text: str, reply_markup=None, **kwargs):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None

def set_state(user_id: int, state: str, data: dict = None):
    with state_lock:
        user_states[user_id] = state
        if data is not None:
            pending_data[user_id] = data
        elif user_id in pending_data:
            pending_data.pop(user_id, None)

def get_state(user_id: int) -> str:
    with state_lock:
        return user_states.get(user_id, "")

def get_pending(user_id: int) -> dict:
    with state_lock:
        return pending_data.get(user_id, {})

def clear_state(user_id: int):
    with state_lock:
        user_states.pop(user_id, None)
        pending_data.pop(user_id, None)

# =============================================================================
#  RATE LIMITING
# =============================================================================

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    last = rate_limit_tracker.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return False
    rate_limit_tracker[user_id] = now
    return True

# =============================================================================
#  FORCE JOIN CHECK
# =============================================================================

def check_force_join(user_id: int) -> bool:
    global force_join_channel
    if not force_join_channel:
        return True
    try:
        member = bot.get_chat_member(force_join_channel, user_id)
        return member.status not in ["left", "kicked"]
    except Exception:
        return True

# =============================================================================
#  PROCESS MANAGEMENT
# =============================================================================

def kill_all_user_processes(user_id: int):
    with process_lock:
        procs = running_processes.get(user_id, {})
        for fid, proc in list(procs.items()):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                logger.info(f"Killed process for user {user_id}, file {fid}")
            except Exception as e:
                logger.error(f"Error killing process {fid}: {e}")
        running_processes[user_id] = {}
    with get_db() as conn:
        conn.execute(
            "UPDATE files SET is_running = 0 WHERE user_id = ?",
            (user_id,)
        )

def kill_single_process(user_id: int, file_id: str) -> bool:
    with process_lock:
        proc = running_processes.get(user_id, {}).get(file_id)
        if proc is None:
            return False
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            running_processes[user_id].pop(file_id, None)
        except Exception as e:
            logger.error(f"Error killing process {file_id}: {e}")
            return False
    db_set_file_running(file_id, False)
    return True

def start_file_process(user_id: int, file_id: str, filepath: str, filetype: str) -> tuple[bool, str]:
    running_count = get_user_running_count(user_id)
    limit = get_user_slot_limit(user_id)
    if running_count >= limit:
        return False, f"❌ Slot limit reached ({running_count}/{limit}). Stop another file first."

    if not os.path.exists(filepath):
        return False, "❌ File not found on server."

    try:
        log_file_path = f"{LOGS_DIR}/proc_{user_id}_{file_id}.log"
        log_file = open(log_file_path, "w")

        if filetype == ".py":
            cmd = [sys.executable, filepath]
        elif filetype == ".js":
            cmd = ["node", filepath]
        else:
            return False, "❌ Cannot run .zip files directly."

        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            cwd=os.path.dirname(filepath),
        )
        with process_lock:
            if user_id not in running_processes:
                running_processes[user_id] = {}
            running_processes[user_id][file_id] = proc

        db_set_file_running(file_id, True)
        db_add_log("RUN", f"Started file {file_id} for user {user_id}", user_id)
        return True, f"✅ Process started (PID {proc.pid})."
    except FileNotFoundError as e:
        return False, f"❌ Runtime not found: {e}. Make sure python/node is installed."
    except Exception as e:
        db_add_log("ERROR", f"Failed to start {file_id}: {e}", user_id, "ERROR")
        return False, f"❌ Failed to start: {e}"

def is_process_alive(user_id: int, file_id: str) -> bool:
    with process_lock:
        proc = running_processes.get(user_id, {}).get(file_id)
        if proc is None:
            return False
        return proc.poll() is None

# =============================================================================
#  FILE SECURITY SCANNER - DISABLED
# =============================================================================

def scan_file_for_dangerous_code(filepath: str, filetype: str) -> tuple[bool, str]:
    return True, "Security check disabled"

def scan_zip_for_safety(filepath: str) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            for name in zf.namelist():
                if ".." in name or name.startswith("/") or name.startswith("\\"):
                    return False, f"Zip contains unsafe path: {name}"
                _, ext = os.path.splitext(name.lower())
                if ext in (".exe", ".bat", ".sh", ".cmd", ".ps1", ".dll", ".so"):
                    return False, f"Zip contains dangerous file type: {name}"
        return True, ""
    except zipfile.BadZipFile:
        return False, "Invalid or corrupted zip file."
    except Exception as e:
        return False, f"Zip inspection failed: {e}"

# =============================================================================
#  USER REGISTRATION & PROFILE
# =============================================================================

def register_and_notify(message):
    user = message.from_user
    user_id = user.id
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = user.username or ""

    existing = db_get_user(user_id)
    if existing:
        db_update_user_info(user_id, name, username)
        db_update_last_active(user_id)
        return False

    db_register_user(user_id, name, username)
    db_add_log("JOIN", f"New user {user_id} (@{username}) registered", user_id)

    notify_text = (
        f"<b>STORM HOSTING:</b>\n"
        f"🎉 <b>New user!</b>\n"
        f"👤 Name: <b>{name}</b>\n"
        f"✳️ User: @{username}\n"
        f"🆔 ID: <code>{user_id}</code>"
    )

    for admin_id in ADMIN_IDS:
        try:
            safe_send(admin_id, notify_text)
        except Exception as e:
            logger.error(f"Error notifying admin {admin_id}: {e}")

    return True

# =============================================================================
#  FILE UPLOAD HANDLER - FIXED WITH PROPER ERROR HANDLING
# =============================================================================

def handle_file_upload(message):
    """Process uploaded file from user with proper error handling."""
    try:
        user_id = message.from_user.id
        logger.info(f"📁 File upload initiated by user {user_id}")
        
        # Check if user exists in database, if not register them
        user = db_get_user(user_id)
        if not user:
            logger.info(f"User {user_id} not found in DB, registering...")
            name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
            username = message.from_user.username or ""
            db_register_user(user_id, name, username)
            user = db_get_user(user_id)
            if not user:
                logger.error(f"Failed to register user {user_id}")
                safe_send(user_id, "❌ Error: Could not register user. Please try /start")
                return

        doc = message.document
        if not doc:
            logger.warning(f"User {user_id} sent message without document")
            safe_send(user_id, "❌ Please send a document file.", reply_markup=main_menu(user_id))
            return

        filename = doc.file_name or "unknown"
        filesize = doc.file_size or 0
        _, ext = os.path.splitext(filename.lower())
        
        logger.info(f"📄 File: {filename}, Size: {filesize}, Extension: {ext}")

        if ext not in ALLOWED_EXTENSIONS:
            logger.warning(f"User {user_id} sent invalid file type: {ext}")
            safe_send(user_id, f"❌ File type <b>{ext}</b> not allowed.\n\nAllowed: .py, .js, .zip",
                      reply_markup=main_menu(user_id))
            return

        if filesize > MAX_FILE_SIZE_BYTES:
            logger.warning(f"User {user_id} sent file too large: {filesize}")
            safe_send(user_id, f"❌ File too large ({format_size(filesize)}).\nMax size: {MAX_FILE_SIZE_MB}MB",
                      reply_markup=main_menu(user_id))
            return

        current_files = db_get_user_files(user_id)
        slot_limit = get_user_slot_limit(user_id)
        if len(current_files) >= slot_limit * 3:
            safe_send(user_id, f"❌ You have too many files ({len(current_files)}). Delete some first.",
                      reply_markup=main_menu(user_id))
            return

        file_id = generate_file_id(user_id, filename)
        user_dir = Path(UPLOADS_DIR) / str(user_id) / file_id
        user_dir.mkdir(parents=True, exist_ok=True)
        dest_path = user_dir / filename

        try:
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)
            with open(dest_path, "wb") as f:
                f.write(downloaded)
            logger.info(f"✅ File {filename} downloaded successfully for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to download file for {user_id}: {e}")
            safe_send(user_id, f"❌ Failed to download file: {e}", reply_markup=main_menu(user_id))
            return

        if ext == ".zip":
            ok, reason = scan_zip_for_safety(str(dest_path))
            if not ok:
                shutil.rmtree(str(user_dir), ignore_errors=True)
                safe_send(user_id, f"❌ Zip file rejected: {reason}", reply_markup=main_menu(user_id))
                return
            extract_dir = user_dir / "extracted"
            extract_dir.mkdir(exist_ok=True)
            try:
                with zipfile.ZipFile(str(dest_path), "r") as zf:
                    zf.extractall(str(extract_dir))
                logger.info(f"✅ Zip extracted for user {user_id}")
            except Exception as e:
                shutil.rmtree(str(user_dir), ignore_errors=True)
                safe_send(user_id, f"❌ Failed to extract zip: {e}", reply_markup=main_menu(user_id))
                return

        db_add_file(user_id, file_id, filename, str(dest_path), ext, filesize)
        db_add_log("UPLOAD", f"File '{filename}' uploaded by {user_id}", user_id)
        logger.info(f"✅ File {filename} uploaded successfully by user {user_id}")

        safe_send(user_id,
                  f"✅ <b>File uploaded successfully!</b>\n\n"
                  f"📁 <b>{filename}</b>\n"
                  f"📦 Size: {format_size(filesize)}\n"
                  f"🆔 ID: <code>{file_id}</code>\n\n"
                  f"Use <b>📂 Check Files</b> to manage it.",
                  reply_markup=main_menu(user_id))

        clear_state(user_id)
        
    except Exception as e:
        logger.error(f"🔥 CRITICAL ERROR in handle_file_upload: {e}\n{traceback.format_exc()}")
        try:
            safe_send(message.from_user.id, f"❌ Error uploading file: {str(e)[:200]}")
        except:
            pass

# =============================================================================
#  GUARD DECORATOR
# =============================================================================

def guard(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
        username = message.from_user.username or ""

        if not check_rate_limit(user_id):
            try:
                bot.send_message(user_id, "⏳ Slow down! Please wait a moment.")
            except Exception:
                pass
            return

        if bot_locked and not is_admin(user_id):
            safe_send(user_id, "🔒 The bot is currently locked by the admin. Please try again later.")
            return

        db_update_last_active(user_id)
        db_update_user_info(user_id, name, username)

        if is_banned(user_id):
            safe_send(user_id, "🚫 You are banned from using this bot.")
            return

        if not check_force_join(user_id) and not is_admin(user_id):
            safe_send(user_id,
                      f"⚠️ You must join our channel first:\n{force_join_channel}\n\nThen press /start")
            return

        return func(message, *args, **kwargs)
    return wrapper

# =============================================================================
#  /START COMMAND
# =============================================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    username = message.from_user.username or ""

    kill_all_user_processes(user_id)
    is_new = register_and_notify(message)
    clear_state(user_id)

    welcome = (
        f"⚡ <b>Welcome to STORM HOSTING{'!' if not is_new else ', ' + name + '!'}</b>\n\n"
        f"🌩️ <i>Fast, reliable bot hosting right inside Telegram.</i>\n\n"
        f"📂 Upload your .py or .js bots and run them instantly.\n"
        f"💳 Free plan: <b>{FREE_SLOTS} slot</b> | Premium: <b>{PREMIUM_SLOTS} slots</b>\n\n"
        f"Use the menu below to get started:"
    )
    safe_send(user_id, welcome, reply_markup=main_menu(user_id))

# =============================================================================
#  DOCUMENT UPLOAD HANDLER - NO GUARD DECORATOR
# =============================================================================

@bot.message_handler(content_types=["document"])
def route_document(message):
    """Handle file uploads - NO GUARD to prevent interference"""
    try:
        logger.info(f"📎 Document received from {message.from_user.id}")
        handle_file_upload(message)
    except Exception as e:
        logger.error(f"🔥 Error in route_document: {e}\n{traceback.format_exc()}")
        try:
            safe_send(message.from_user.id, f"❌ Error: {str(e)[:200]}")
        except:
            pass

# =============================================================================
#  MAIN MESSAGE ROUTER
# =============================================================================

@bot.message_handler(func=lambda m: m.content_type == "text")
@guard
def route_text(message):
    text = message.text.strip()
    user_id = message.from_user.id
    state = get_state(user_id)

    # ---- CANCEL ----
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=main_menu(user_id))
        return

    # ---- STATE-BASED ROUTING ----
    if state == "awaiting_upload":
        safe_send(user_id, "📎 Please send a file (not text).", reply_markup=cancel_menu())
        return

    if state == "awaiting_ban_id":
        handle_ban_input(message)
        return

    if state == "awaiting_unban_id":
        handle_unban_input(message)
        return

    if state == "awaiting_broadcast":
        handle_broadcast_input(message)
        return

    if state == "awaiting_force_join":
        handle_force_join_input(message)
        return

    if state == "awaiting_premium_user_id":
        handle_premium_user_id_input(message)
        return

    if state == "awaiting_premium_duration":
        handle_premium_duration_input(message)
        return

    if state == "awaiting_remove_premium_id":
        handle_remove_premium_input(message)
        return

    if state.startswith("file_action:"):
        handle_file_action_text(message, state)
        return

    # ---- MAIN MENU ----
    if text == "📂 Check Files":
        show_user_files(message)
    elif text == "📤 Upload File":
        handle_upload_request(message)
    elif text == "📊 Statistics":
        show_user_stats(message)
    elif text == "⚡ Bot Speed":
        show_bot_speed(message)
    elif text == "💳 My Plan":
        show_my_plan(message)
    elif text == "📢 Updates Channel":
        show_updates_channel(message)
    elif text == "📞 Contact Owner":
        show_contact_owner(message)
    elif text == "👑 Admin Panel":
        open_admin_panel(message)

    # ---- ADMIN MENU ----
    elif text == "📂 All Files":
        admin_all_files(message)
    elif text == "📤 Upload Logs":
        admin_upload_logs(message)
    elif text == "📊 Statistics" and is_admin(user_id):
        admin_statistics(message)
    elif text == "⚡ Running All Code":
        admin_running_all(message)
    elif text == "📢 Broadcast":
        admin_broadcast_start(message)
    elif text == "🚫 Ban User":
        admin_ban_start(message)
    elif text == "🔓 Unban User":
        admin_unban_start(message)
    elif text == "💳 Subscriptions":
        admin_subscriptions(message)
    elif text == "⏳ Set Force Join":
        admin_set_force_join_start(message)
    elif text == "🔒 Lock Bot":
        admin_toggle_lock(message)
    elif text == "🧾 Logs":
        admin_view_logs(message)
    elif text == "🔙 Back":
        go_back_to_main(message)

    # ---- SUBSCRIPTION SUBMENU ----
    elif text == "➕ Add Premium":
        admin_add_premium_start(message)
    elif text == "➖ Remove Premium":
        admin_remove_premium_start(message)
    elif text == "🔙 Back to Admin":
        open_admin_panel(message)

    # ---- FILE ACTION SUBMENU ----
    elif text == "▶️ Run File":
        handle_file_run(message)
    elif text == "⏹ Stop File":
        handle_file_stop(message)
    elif text == "🗑 Delete File":
        handle_file_delete(message)
    elif text == "🔙 Back to Files":
        show_user_files(message)
    elif text == "🏠 Main Menu":
        clear_state(user_id)
        safe_send(user_id, "🏠 Main menu.", reply_markup=main_menu(user_id))

    # ---- DURATION SUBMENU ----
    elif text in ("⏱ 7 Days", "⏱ 15 Days", "⏱ 30 Days", "⏱ 60 Days"):
        handle_premium_duration_input(message)

    # ---- FILE SELECTION ----
    elif text.isdigit():
        handle_file_selection_by_number(message)

    elif admin_extended_route(message, text):
        pass

    else:
        safe_send(user_id, "❓ Unknown command. Use the menu buttons below.",
                  reply_markup=main_menu(user_id))

# =============================================================================
#  MAIN MENU HANDLERS
# =============================================================================

def handle_upload_request(message):
    user_id = message.from_user.id
    set_state(user_id, "awaiting_upload")
    safe_send(user_id,
              "📤 <b>Upload a file</b>\n\n"
              "Supported: <b>.py</b>, <b>.js</b>, <b>.zip</b>\n"
              f"Max size: <b>{MAX_FILE_SIZE_MB}MB</b>\n\n"
              "Send your file now, or press Cancel.",
              reply_markup=cancel_menu())

def show_user_files(message):
    user_id = message.from_user.id
    clear_state(user_id)
    files = db_get_user_files(user_id)
    if not files:
        safe_send(user_id, "📂 You have no uploaded files yet.\n\nUse <b>📤 Upload File</b> to get started.",
                  reply_markup=main_menu(user_id))
        return

    lines = ["<b>📂 Your Files:</b>\n"]
    for i, f in enumerate(files, 1):
        running = is_process_alive(user_id, f["file_id"]) or bool(f["is_running"])
        status = "🟢 Running" if running else "🔴 Stopped"
        lines.append(
            f"<b>{i}.</b> <code>{f['filename']}</code>\n"
            f"   {status} | {format_size(f['filesize'])} | {f['filetype']}\n"
            f"   📅 {f['upload_date'][:10]}"
        )

    lines.append("\n<i>Enter a file number to manage it:</i>")
    set_state(user_id, "selecting_file")
    safe_send(user_id, "\n\n".join(lines), reply_markup=main_menu(user_id))

def handle_file_selection_by_number(message):
    user_id = message.from_user.id
    state = get_state(user_id)
    if state not in ("selecting_file",):
        return

    num = int(message.text.strip())
    files = db_get_user_files(user_id)
    if num < 1 or num > len(files):
        safe_send(user_id, f"❌ Invalid number. Choose 1–{len(files)}.",
                  reply_markup=main_menu(user_id))
        return

    selected = files[num - 1]
    file_id = selected["file_id"]
    running = is_process_alive(user_id, file_id) or bool(selected["is_running"])
    status = "🟢 Running" if running else "🔴 Stopped"

    info = (
        f"<b>📁 {selected['filename']}</b>\n\n"
        f"Status: {status}\n"
        f"Size: {format_size(selected['filesize'])}\n"
        f"Type: {selected['filetype']}\n"
        f"Uploaded: {selected['upload_date'][:10]}\n"
        f"ID: <code>{file_id}</code>\n\n"
        f"Choose an action:"
    )
    set_state(user_id, f"file_action:{file_id}", {"file_id": file_id, "filename": selected["filename"]})
    safe_send(user_id, info, reply_markup=file_action_menu())

def handle_file_action_text(message, state):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == "▶️ Run File":
        handle_file_run(message)
    elif text == "⏹ Stop File":
        handle_file_stop(message)
    elif text == "🗑 Delete File":
        handle_file_delete(message)
    elif text == "🔙 Back to Files":
        clear_state(user_id)
        show_user_files(message)
    elif text == "🏠 Main Menu":
        clear_state(user_id)
        safe_send(user_id, "🏠 Main menu.", reply_markup=main_menu(user_id))
    else:
        safe_send(user_id, "❓ Use the action buttons.", reply_markup=file_action_menu())

def handle_file_run(message):
    user_id = message.from_user.id
    data = get_pending(user_id)
    file_id = data.get("file_id") if data else None

    if not file_id:
        safe_send(user_id, "❌ No file selected.", reply_markup=main_menu(user_id))
        clear_state(user_id)
        return

    f = db_get_file(file_id)
    if not f:
        safe_send(user_id, "❌ File not found in database.", reply_markup=main_menu(user_id))
        clear_state(user_id)
        return

    if f["filetype"] == ".zip":
        safe_send(user_id, "❌ Cannot run .zip files directly. Upload a .py or .js file.",
                  reply_markup=file_action_menu())
        return

    success, msg = start_file_process(user_id, file_id, f["filepath"], f["filetype"])
    safe_send(user_id, msg, reply_markup=file_action_menu())

def handle_file_stop(message):
    user_id = message.from_user.id
    data = get_pending(user_id)
    file_id = data.get("file_id") if data else None

    if not file_id:
        safe_send(user_id, "❌ No file selected.", reply_markup=main_menu(user_id))
        clear_state(user_id)
        return

    killed = kill_single_process(user_id, file_id)
    if killed:
        safe_send(user_id, "⏹ <b>Process stopped.</b>", reply_markup=file_action_menu())
        db_add_log("STOP", f"File {file_id} stopped by user {user_id}", user_id)
    else:
        safe_send(user_id, "ℹ️ File was not running.", reply_markup=file_action_menu())

def handle_file_delete(message):
    user_id = message.from_user.id
    data = get_pending(user_id)
    file_id = data.get("file_id") if data else None
    filename = data.get("filename", "file") if data else "file"

    if not file_id:
        safe_send(user_id, "❌ No file selected.", reply_markup=main_menu(user_id))
        clear_state(user_id)
        return

    kill_single_process(user_id, file_id)
    f = db_get_file(file_id)
    if f:
        file_dir = Path(UPLOADS_DIR) / str(user_id) / file_id
        shutil.rmtree(str(file_dir), ignore_errors=True)

    db_delete_file(file_id)
    db_add_log("DELETE", f"File {file_id} ({filename}) deleted by user {user_id}", user_id)
    clear_state(user_id)
    safe_send(user_id, f"🗑 <b>{filename}</b> deleted.", reply_markup=main_menu(user_id))

def show_user_stats(message):
    user_id = message.from_user.id
    stats = db_get_stats()
    uptime = format_uptime(time.time() - BOT_START_TIME)
    safe_send(user_id,
              f"<b>📊 Statistics</b>\n\n"
              f"👥 Total Users: <b>{stats['total_users']}</b>\n"
              f"📁 Total Files: <b>{stats['total_files']}</b>\n"
              f"🟢 Running Processes: <b>{stats['running_files']}</b>\n"
              f"💎 Premium Users: <b>{stats['premium_users']}</b>\n"
              f"🚫 Banned Users: <b>{stats['banned_users']}</b>\n"
              f"⏱ Bot Uptime: <b>{uptime}</b>",
              reply_markup=main_menu(user_id))

def show_bot_speed(message):
    user_id = message.from_user.id
    t_start = time.time()
    sent = safe_send(user_id, "⚡ Measuring speed...")
    t_end = time.time()
    latency_ms = int((t_end - t_start) * 1000)
    uptime = format_uptime(time.time() - BOT_START_TIME)
    if sent:
        bot.edit_message_text(
            f"<b>⚡ Bot Speed</b>\n\n"
            f"📶 Telegram API Latency: <b>{latency_ms}ms</b>\n"
            f"⏱ Bot Uptime: <b>{uptime}</b>\n"
            f"🕒 Server Time: <b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</b>\n"
            f"🌐 Status: <b>🟢 Online</b>",
            user_id,
            sent.message_id
        )

def show_my_plan(message):
    user_id = message.from_user.id
    user = db_get_user(user_id)
    if not user:
        return

    premium = is_premium(user_id)
    files = db_get_user_files(user_id)
    running = get_user_running_count(user_id)
    slot_limit = get_user_slot_limit(user_id)
    plan_name = "💎 Premium" if premium else "🆓 Free"

    expiry_text = ""
    if premium and user["premium_expiry"]:
        expiry = datetime.fromisoformat(user["premium_expiry"])
        days_left = (expiry - datetime.now()).days
        expiry_text = f"\n⏳ Expires in: <b>{days_left} days</b> ({expiry.strftime('%Y-%m-%d')})"

    safe_send(user_id,
              f"<b>💳 My Plan</b>\n\n"
              f"Plan: <b>{plan_name}</b>{expiry_text}\n"
              f"📁 Files: <b>{len(files)}</b>\n"
              f"🟢 Running: <b>{running}/{slot_limit}</b>\n\n"
              f"<b>Plan Limits:</b>\n"
              f"Free: {FREE_SLOTS} running slot\n"
              f"Premium: {PREMIUM_SLOTS} running slots\n\n"
              f"Contact the owner to upgrade: /start",
              reply_markup=main_menu(user_id))

def show_updates_channel(message):
    user_id = message.from_user.id
    safe_send(user_id,
              f"📢 <b>Updates Channel</b>\n\n"
              f"Stay updated with the latest news and features:\n{UPDATES_CHANNEL}",
              reply_markup=main_menu(user_id))

def show_contact_owner(message):
    user_id = message.from_user.id
    admins = ADMIN_IDS
    admin_text = "\n".join([f"👤 Admin ID: <code>{a}</code>" for a in admins])
    safe_send(user_id,
              f"📞 <b>Contact Owner</b>\n\n"
              f"👑 Owner: @ghostof1975\n"
              f"{admin_text}\n\n"
              f"For support, upgrades, or issues please message the admin directly.",
              reply_markup=main_menu(user_id))

def open_admin_panel(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        safe_send(user_id, "❌ You are not an admin.", reply_markup=main_menu(user_id))
        return
    clear_state(user_id)
    safe_send(user_id, "👑 <b>Admin Panel</b>\n\nWelcome, Admin. Choose an action:",
              reply_markup=admin_menu())

def go_back_to_main(message):
    user_id = message.from_user.id
    clear_state(user_id)
    safe_send(user_id, "🏠 Back to main menu.", reply_markup=main_menu(user_id))

# =============================================================================
#  ADMIN FUNCTIONS
# =============================================================================

def admin_all_files(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    files = db_get_all_files()
    if not files:
        safe_send(user_id, "📂 No files on the server.", reply_markup=admin_menu())
        return

    lines = [f"<b>📂 All Files ({len(files)} total):</b>\n"]
    for i, f in enumerate(files[:30], 1):
        running = is_process_alive(f["user_id"], f["file_id"]) or bool(f["is_running"])
        status = "🟢" if running else "🔴"
        lines.append(
            f"{i}. {status} <code>{f['filename']}</code>\n"
            f"   👤 {f['name']} (<code>{f['user_id']}</code>) | {format_size(f['filesize'])}"
        )
    if len(files) > 30:
        lines.append(f"\n<i>... and {len(files) - 30} more</i>")

    safe_send(user_id, "\n".join(lines), reply_markup=admin_menu())

def admin_upload_logs(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    logs = db_get_logs(50)
    upload_logs = [l for l in logs if l["category"] == "UPLOAD"]
    if not upload_logs:
        safe_send(user_id, "📤 No upload logs found.", reply_markup=admin_menu())
        return

    lines = [f"<b>📤 Upload Logs (last {len(upload_logs)}):</b>\n"]
    for l in upload_logs[:20]:
        lines.append(
            f"• {l['timestamp'][:16]} | UID: <code>{l['user_id']}</code>\n"
            f"  {l['message']}"
        )
    safe_send(user_id, "\n".join(lines), reply_markup=admin_menu())

def admin_statistics(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    stats = db_get_stats()
    uptime = format_uptime(time.time() - BOT_START_TIME)
    active_procs = sum(
        len([p for p in procs.values() if p.poll() is None])
        for procs in running_processes.values()
    )
    safe_send(user_id,
              f"<b>📊 Admin Statistics</b>\n\n"
              f"👥 Total Users: <b>{stats['total_users']}</b>\n"
              f"💎 Premium Users: <b>{stats['premium_users']}</b>\n"
              f"🚫 Banned Users: <b>{stats['banned_users']}</b>\n"
              f"📁 Total Files: <b>{stats['total_files']}</b>\n"
              f"🟢 DB Running: <b>{stats['running_files']}</b>\n"
              f"⚙️ Live Processes: <b>{active_procs}</b>\n"
              f"⏱ Uptime: <b>{uptime}</b>\n"
              f"🕒 Server Time: <b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</b>",
              reply_markup=admin_menu())

def admin_running_all(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return

    lines = ["<b>⚡ All Running Processes:</b>\n"]
    found = False
    for uid, procs in running_processes.items():
        for fid, proc in procs.items():
            if proc.poll() is None:
                f = db_get_file(fid)
                fname = f["filename"] if f else "unknown"
                lines.append(
                    f"👤 User: <code>{uid}</code>\n"
                    f"📁 File: <code>{fname}</code> (PID: {proc.pid})\n"
                    f"🆔 FID: <code>{fid}</code>"
                )
                found = True

    if not found:
        lines.append("No processes are currently running.")
    safe_send(user_id, "\n\n".join(lines), reply_markup=admin_menu())

def admin_broadcast_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    set_state(user_id, "awaiting_broadcast")
    safe_send(user_id,
              "📢 <b>Broadcast</b>\n\nSend the message you want to broadcast to all users:",
              reply_markup=cancel_menu())

def handle_broadcast_input(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    if message.text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Broadcast cancelled.", reply_markup=admin_menu())
        return

    broadcast_text = message.text
    clear_state(user_id)

    users = db_get_all_users()
    success, fail = 0, 0
    for u in users:
        if u["is_banned"]:
            continue
        result = safe_send(u["user_id"],
                           f"📢 <b>Announcement from Storm Hosting:</b>\n\n{broadcast_text}")
        if result:
            success += 1
        else:
            fail += 1
        time.sleep(0.05)

    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO broadcast_log (admin_id, message, sent_at, success_count, fail_count) VALUES (?, ?, ?, ?, ?)",
            (user_id, broadcast_text, now, success, fail)
        )
    db_add_log("BROADCAST", f"Admin {user_id} broadcasted. Success: {success}, Fail: {fail}", user_id)
    safe_send(user_id,
              f"✅ <b>Broadcast complete!</b>\n\n"
              f"✅ Sent: <b>{success}</b>\n"
              f"❌ Failed: <b>{fail}</b>",
              reply_markup=admin_menu())

def admin_ban_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    set_state(user_id, "awaiting_ban_id")
    safe_send(user_id, "🚫 <b>Ban User</b>\n\nEnter the user ID to ban:",
              reply_markup=cancel_menu())

def handle_ban_input(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = message.text.strip()
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=admin_menu())
        return

    if not text.isdigit():
        safe_send(user_id, "❌ Invalid ID. Enter a numeric user ID.", reply_markup=cancel_menu())
        return

    target_id = int(text)
    if target_id in ADMIN_IDS:
        safe_send(user_id, "❌ Cannot ban an admin.", reply_markup=admin_menu())
        clear_state(user_id)
        return

    db_ban_user(target_id)
    kill_all_user_processes(target_id)
    db_add_log("BAN", f"Admin {user_id} banned user {target_id}", user_id)
    safe_send(user_id, f"✅ User <code>{target_id}</code> has been <b>banned</b>.", reply_markup=admin_menu())
    safe_send(target_id, "🚫 You have been banned from Storm Hosting.")
    clear_state(user_id)

def admin_unban_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    set_state(user_id, "awaiting_unban_id")
    safe_send(user_id, "🔓 <b>Unban User</b>\n\nEnter the user ID to unban:",
              reply_markup=cancel_menu())

def handle_unban_input(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = message.text.strip()
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=admin_menu())
        return

    if not text.isdigit():
        safe_send(user_id, "❌ Invalid ID.", reply_markup=cancel_menu())
        return

    target_id = int(text)
    db_unban_user(target_id)
    db_add_log("UNBAN", f"Admin {user_id} unbanned user {target_id}", user_id)
    safe_send(user_id, f"✅ User <code>{target_id}</code> has been <b>unbanned</b>.", reply_markup=admin_menu())
    safe_send(target_id, "✅ You have been unbanned from Storm Hosting. Welcome back!")
    clear_state(user_id)

def admin_subscriptions(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    safe_send(user_id, "💳 <b>Subscriptions</b>\n\nChoose an action:",
              reply_markup=subscription_menu())

def admin_add_premium_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    set_state(user_id, "awaiting_premium_user_id", {"action": "add"})
    safe_send(user_id, "➕ <b>Add Premium</b>\n\nEnter the user ID to grant premium:",
              reply_markup=cancel_menu())

def handle_premium_user_id_input(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = message.text.strip()
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=admin_menu())
        return

    if not text.isdigit():
        safe_send(user_id, "❌ Invalid user ID.", reply_markup=cancel_menu())
        return

    data = get_pending(user_id)
    action = data.get("action", "add")
    target_id = int(text)

    if action == "add":
        set_state(user_id, "awaiting_premium_duration", {"action": "add", "target_id": target_id})
        safe_send(user_id,
                  f"➕ Grant premium to user <code>{target_id}</code>\n\nSelect duration:",
                  reply_markup=duration_menu())
    else:
        db_remove_premium(target_id)
        db_add_log("PREMIUM", f"Admin {user_id} removed premium from {target_id}", user_id)
        safe_send(user_id, f"✅ Premium removed from user <code>{target_id}</code>.",
                  reply_markup=admin_menu())
        safe_send(target_id, "ℹ️ Your Storm Hosting premium has been removed.")
        clear_state(user_id)

def handle_premium_duration_input(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = message.text.strip()
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=admin_menu())
        return

    duration_map = {
        "⏱ 7 Days": 7,
        "⏱ 15 Days": 15,
        "⏱ 30 Days": 30,
        "⏱ 60 Days": 60,
    }
    if text not in duration_map:
        safe_send(user_id, "❌ Please select a valid duration.", reply_markup=duration_menu())
        return

    days = duration_map[text]
    data = get_pending(user_id)
    target_id = data.get("target_id")
    if not target_id:
        safe_send(user_id, "❌ Session error. Try again.", reply_markup=admin_menu())
        clear_state(user_id)
        return

    db_set_premium(target_id, days)
    db_add_log("PREMIUM", f"Admin {user_id} granted {days}d premium to {target_id}", user_id)
    expiry = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    safe_send(user_id,
              f"✅ <b>Premium granted!</b>\n\nUser: <code>{target_id}</code>\nDuration: <b>{days} days</b>\nExpires: <b>{expiry}</b>",
              reply_markup=admin_menu())
    safe_send(target_id,
              f"🎉 <b>Congratulations!</b>\nYou now have <b>Premium</b> access!\n"
              f"⏳ Duration: <b>{days} days</b>\n"
              f"📅 Expires: <b>{expiry}</b>\n"
              f"🚀 Enjoy <b>{PREMIUM_SLOTS} running slots</b>!")
    clear_state(user_id)

def admin_remove_premium_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    set_state(user_id, "awaiting_premium_user_id", {"action": "remove"})
    safe_send(user_id, "➖ <b>Remove Premium</b>\n\nEnter the user ID to remove premium from:",
              reply_markup=cancel_menu())

def handle_remove_premium_input(message):
    handle_premium_user_id_input(message)

def admin_set_force_join_start(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    current = force_join_channel or "Not set"
    set_state(user_id, "awaiting_force_join")
    safe_send(user_id,
              f"⏳ <b>Set Force Join</b>\n\n"
              f"Current channel: <b>{current}</b>\n\n"
              "Enter channel username (e.g. @MyChannel) or send <b>disable</b> to remove:",
              reply_markup=cancel_menu())

def handle_force_join_input(message):
    global force_join_channel
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    text = message.text.strip()
    if text == "❌ Cancel":
        clear_state(user_id)
        safe_send(user_id, "✅ Cancelled.", reply_markup=admin_menu())
        return

    if text.lower() == "disable":
        force_join_channel = None
        save_setting("force_join_channel", "")
        db_add_log("SETTING", f"Admin {user_id} disabled force join", user_id)
        safe_send(user_id, "✅ Force join disabled.", reply_markup=admin_menu())
    else:
        if not text.startswith("@"):
            text = "@" + text
        force_join_channel = text
        save_setting("force_join_channel", text)
        db_add_log("SETTING", f"Admin {user_id} set force join to {text}", user_id)
        safe_send(user_id, f"✅ Force join set to <b>{text}</b>.", reply_markup=admin_menu())
    clear_state(user_id)

def admin_toggle_lock(message):
    global bot_locked
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    bot_locked = not bot_locked
    save_setting("bot_locked", "1" if bot_locked else "0")
    status = "🔒 Locked" if bot_locked else "🔓 Unlocked"
    db_add_log("SETTING", f"Admin {user_id} toggled bot lock: {status}", user_id)
    safe_send(user_id, f"Bot is now <b>{status}</b>.", reply_markup=admin_menu())

def admin_view_logs(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    logs = db_get_logs(30)
    if not logs:
        safe_send(user_id, "🧾 No logs found.", reply_markup=admin_menu())
        return

    lines = [f"<b>🧾 System Logs (last {len(logs)}):</b>\n"]
    for l in logs:
        icon = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌"}.get(l["level"], "•")
        uid_str = f" UID:{l['user_id']}" if l["user_id"] else ""
        lines.append(f"{icon} [{l['category']}]{uid_str}\n{l['timestamp'][:16]}: {l['message']}")

    full_text = "\n\n".join(lines)
    if len(full_text) > 3800:
        full_text = full_text[:3800] + "\n\n<i>... truncated</i>"
    safe_send(user_id, full_text, reply_markup=admin_menu())

def admin_extended_route(message, text: str):
    return False

# =============================================================================
#  BACKGROUND THREADS
# =============================================================================

def thread_premium_expiry():
    while True:
        try:
            with get_db() as conn:
                users = conn.execute(
                    "SELECT user_id, premium_expiry FROM users WHERE is_premium = 1 AND premium_expiry IS NOT NULL"
                ).fetchall()
            now = datetime.now()
            for u in users:
                try:
                    expiry = datetime.fromisoformat(u["premium_expiry"])
                    if now > expiry:
                        db_remove_premium(u["user_id"])
                        db_add_log("PREMIUM", f"Premium expired for user {u['user_id']}", u["user_id"])
                        safe_send(u["user_id"],
                                  "⏳ <b>Your premium has expired.</b>\nContact the admin to renew.")
                        logger.info(f"Premium expired for user {u['user_id']}")
                except Exception as e:
                    logger.error(f"Error processing premium expiry for {u['user_id']}: {e}")
        except Exception as e:
            logger.error(f"Premium expiry thread error: {e}")
        time.sleep(3600)

def thread_process_monitor():
    while True:
        try:
            with process_lock:
                for uid, procs in list(running_processes.items()):
                    for fid, proc in list(procs.items()):
                        if proc.poll() is not None:
                            db_set_file_running(fid, False)
                            del running_processes[uid][fid]
                            db_add_log("PROC_DIED", f"Process {fid} for user {uid} exited", uid)
                            logger.info(f"Process {fid} for user {uid} exited naturally")
        except Exception as e:
            logger.error(f"Process monitor thread error: {e}")
        time.sleep(10)

def thread_auto_restart():
    logger.info("Auto-restart: checking for files to restart...")
    try:
        running_files = db_get_running_files()
        for f in running_files:
            user_id = f["user_id"]
            file_id = f["file_id"]
            filepath = f["filepath"]
            filetype = f["filetype"]

            if filetype == ".zip":
                db_set_file_running(file_id, False)
                continue

            if not os.path.exists(filepath):
                db_set_file_running(file_id, False)
                logger.warning(f"Auto-restart: file {filepath} not found, skipping")
                continue

            success, msg = start_file_process(user_id, file_id, filepath, filetype)
            if success:
                logger.info(f"Auto-restarted file {file_id} for user {user_id}")
            else:
                db_set_file_running(file_id, False)
                logger.error(f"Auto-restart failed for {file_id}: {msg}")
    except Exception as e:
        logger.error(f"Auto-restart error: {e}")

def thread_health_check():
    while True:
        try:
            stats = db_get_stats()
            active_procs = sum(
                len([p for p in procs.values() if p.poll() is None])
                for procs in running_processes.values()
            )
            logger.info(
                f"Health: users={stats['total_users']}, files={stats['total_files']}, "
                f"live_procs={active_procs}, uptime={format_uptime(time.time() - BOT_START_TIME)}"
            )
        except Exception as e:
            logger.error(f"Health check error: {e}")
        time.sleep(300)

def start_background_threads():
    threads = [
        threading.Thread(target=thread_premium_expiry, daemon=True, name="PremiumExpiry"),
        threading.Thread(target=thread_process_monitor, daemon=True, name="ProcessMonitor"),
        threading.Thread(target=thread_health_check, daemon=True, name="HealthCheck"),
    ]
    for t in threads:
        t.start()
        logger.info(f"Started background thread: {t.name}")

# =============================================================================
#  ERROR HANDLER
# =============================================================================

@bot.message_handler(content_types=["photo", "video", "audio", "voice", "sticker", "animation"])
def handle_unsupported(message):
    user_id = message.from_user.id
    safe_send(user_id,
              "❌ Unsupported content type.\n\nPlease use the menu buttons or upload a .py, .js, or .zip file.",
              reply_markup=main_menu(user_id))

# =============================================================================
#  GRACEFUL SHUTDOWN
# =============================================================================

def cleanup_on_exit():
    logger.info("Bot shutting down — killing all processes...")
    with process_lock:
        for uid, procs in running_processes.items():
            for fid, proc in procs.items():
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2)
                except Exception:
                    pass
    logger.info("All processes killed. Goodbye.")

# =============================================================================
#  MAIN ENTRY POINT
# =============================================================================

def main():
    import atexit
    atexit.register(cleanup_on_exit)

    logger.info("=== STORM HOSTING BOT STARTING ===")

    init_db()
    load_settings()

    Path(UPLOADS_DIR).mkdir(exist_ok=True)
    Path(LOGS_DIR).mkdir(exist_ok=True)

    thread_auto_restart()
    start_background_threads()

    logger.info(f"Bot configured. Admin IDs: {ADMIN_IDS}")
    logger.info(f"Bot locked: {bot_locked}, Force join: {force_join_channel}")
    logger.info("Starting polling...")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                allowed_updates=["message"],
                skip_pending=False,
            )
        except telebot.apihelper.ApiException as e:
            logger.error(f"Telegram API error: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Polling error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

if __name__ == "__main__":
    main()
