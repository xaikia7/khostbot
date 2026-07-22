# -*- coding: utf-8 -*-
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import signal
import threading
import re
import sys
import atexit
import requests

# --- Flask Keep Alive ---
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "I'am Marco File Host"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("Flask Keep-Alive server started.")
# --- End Flask Keep Alive ---

# --- Configuration ---
TOKEN = os.environ.get("TOKEN")
OWNER_ID = 7007475122
ADMIN_ID = 7007475122
YOUR_USERNAME = '@ghostof1975'
UPDATE_CHANNEL = 'https://t.me/LootSenaOfficial'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

FREE_USER_LIMIT = 1        # Free users: 1 file (enforced by approval system)
SUBSCRIBED_USER_LIMIT = 1  # Subscribed: 1 file at a time (same approval gate)
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# --- Data structures ---
bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
force_join_channels = {}   # {channel_id_str: channel_name}
pending_approvals = {}     # {user_id: [(file_name, file_type)]} — waiting admin approval
approved_files = {}        # {user_id: [(file_name, file_type)]}  — admin approved

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Command Button Layouts ---
COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["📦 Manual Install"],
    ["📞 Contact Owner"]
]
ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💳 Subscriptions", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["📦 Manual Install"],
    ["👑 Admin Panel", "📞 Contact Owner"]
]

# --- Database Setup ---
def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        # Force-join channels: admin sets required channels here
        c.execute('''CREATE TABLE IF NOT EXISTS force_join_channels
                     (channel_id TEXT PRIMARY KEY, channel_name TEXT)''')
        # File approval system
        c.execute('''CREATE TABLE IF NOT EXISTS file_approvals
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      status TEXT DEFAULT 'pending',
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                logger.warning(f"⚠️ Invalid expiry date for user {user_id}.")
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        # Load force-join channels
        c.execute('SELECT channel_id, channel_name FROM force_join_channels')
        for channel_id, channel_name in c.fetchall():
            force_join_channels[channel_id] = channel_name
        # Load file approvals
        c.execute('SELECT user_id, file_name, file_type, status FROM file_approvals')
        for user_id, file_name, file_type, status in c.fetchall():
            if status == 'pending':
                if user_id not in pending_approvals:
                    pending_approvals[user_id] = []
                pending_approvals[user_id].append((file_name, file_type))
            elif status == 'approved':
                if user_id not in approved_files:
                    approved_files[user_id] = []
                approved_files[user_id].append((file_name, file_type))
        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(user_subscriptions)} subscriptions, "
                    f"{len(admin_ids)} admins, {len(force_join_channels)} force-join channels.")
    except Exception as e:
        logger.error(f"❌ Error loading data: {e}", exc_info=True)

init_db()
load_data()
# --- End Database Setup ---

# --- Helper Functions ---
def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if script_info and script_info.get('process'):
        try:
            proc = psutil.Process(script_info['process'].pid)
            is_running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not is_running:
                if 'log_file' in script_info and hasattr(script_info['log_file'], 'close') and not script_info['log_file'].closed:
                    try: script_info['log_file'].close()
                    except Exception: pass
                if script_key in bot_scripts: del bot_scripts[script_key]
            return is_running
        except psutil.NoSuchProcess:
            if 'log_file' in script_info and hasattr(script_info['log_file'], 'close') and not script_info['log_file'].closed:
                try: script_info['log_file'].close()
                except Exception: pass
            if script_key in bot_scripts: del bot_scripts[script_key]
            return False
        except Exception as e:
            logger.error(f"Error checking process for {script_key}: {e}", exc_info=True)
            return False
    return False

def has_any_running_script(user_id):
    """Check if user already has any script running."""
    files_list = user_files.get(user_id, [])
    for file_name, _ in files_list:
        if is_bot_running(user_id, file_name):
            return True
    return False

def kill_process_tree(process_info):
    pid = None
    script_key = process_info.get('script_key', 'N/A')
    try:
        if 'log_file' in process_info and hasattr(process_info['log_file'], 'close') and not process_info['log_file'].closed:
            try: process_info['log_file'].close()
            except Exception as log_e: logger.error(f"Error closing log file for {script_key}: {log_e}")
        process = process_info.get('process')
        if process and hasattr(process, 'pid'):
            pid = process.pid
            if pid:
                try:
                    parent = psutil.Process(pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        try: child.terminate()
                        except psutil.NoSuchProcess: pass
                        except Exception:
                            try: child.kill()
                            except Exception: pass
                    gone, alive = psutil.wait_procs(children, timeout=1)
                    for p in alive:
                        try: p.kill()
                        except Exception: pass
                    try:
                        parent.terminate()
                        try: parent.wait(timeout=1)
                        except psutil.TimeoutExpired: parent.kill()
                    except psutil.NoSuchProcess: pass
                    except Exception:
                        try: parent.kill()
                        except Exception: pass
                except psutil.NoSuchProcess: pass
    except Exception as e:
        logger.error(f"❌ Error killing process {pid or 'N/A'} ({script_key}): {e}", exc_info=True)

# --- Force Join System ---
def check_force_join(user_id):
    """Returns list of channels the user has NOT joined yet."""
    if user_id in admin_ids:
        return []  # Admins bypass force join
    not_joined = []
    for channel_id, channel_name in force_join_channels.items():
        try:
            member = bot.get_chat_member(channel_id, user_id)
            if member.status in ['left', 'kicked', 'restricted']:
                not_joined.append((channel_id, channel_name))
        except Exception as e:
            logger.warning(f"Could not check membership for {user_id} in {channel_id}: {e}")
            not_joined.append((channel_id, channel_name))
    return not_joined

def send_force_join_message(chat_id, not_joined_channels):
    """Send message with join buttons for required channels."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel_id, channel_name in not_joined_channels:
        invite_link = channel_id if channel_id.startswith('http') else f"https://t.me/{channel_id.lstrip('@')}"
        markup.add(types.InlineKeyboardButton(f"📢 {channel_name}", url=invite_link))
    markup.add(types.InlineKeyboardButton("✅ Maine Join Kar Liya!", callback_data='check_joined'))
    bot.send_message(
        chat_id,
        "⚠️ *Bot use karne ke liye pehle neeche ke channels join karo:*\n\n"
        "Join karne ke baad '✅ Maine Join Kar Liya!' dabao.",
        parse_mode='Markdown',
        reply_markup=markup
    )

# --- File Approval System ---
DB_LOCK = threading.Lock()

def save_file_approval(user_id, file_name, file_type, status='pending'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO file_approvals (user_id, file_name, file_type, status) VALUES (?, ?, ?, ?)',
                      (user_id, file_name, file_type, status))
            conn.commit()
            # Update in-memory dicts
            if status == 'pending':
                if user_id not in pending_approvals:
                    pending_approvals[user_id] = []
                pending_approvals[user_id] = [(fn, ft) for fn, ft in pending_approvals.get(user_id, []) if fn != file_name]
                pending_approvals[user_id].append((file_name, file_type))
                # Remove from approved if it was there
                if user_id in approved_files:
                    approved_files[user_id] = [(fn, ft) for fn, ft in approved_files.get(user_id, []) if fn != file_name]
            elif status == 'approved':
                if user_id not in approved_files:
                    approved_files[user_id] = []
                approved_files[user_id] = [(fn, ft) for fn, ft in approved_files.get(user_id, []) if fn != file_name]
                approved_files[user_id].append((file_name, file_type))
                # Remove from pending
                if user_id in pending_approvals:
                    pending_approvals[user_id] = [(fn, ft) for fn, ft in pending_approvals.get(user_id, []) if fn != file_name]
            elif status == 'rejected':
                if user_id in pending_approvals:
                    pending_approvals[user_id] = [(fn, ft) for fn, ft in pending_approvals.get(user_id, []) if fn != file_name]
                if user_id in approved_files:
                    approved_files[user_id] = [(fn, ft) for fn, ft in approved_files.get(user_id, []) if fn != file_name]
        except sqlite3.Error as e:
            logger.error(f"❌ SQLite error saving approval for {user_id},{file_name}: {e}")
        finally:
            conn.close()

def remove_file_approval_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM file_approvals WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.commit()
            if user_id in pending_approvals:
                pending_approvals[user_id] = [(fn, ft) for fn, ft in pending_approvals.get(user_id, []) if fn != file_name]
            if user_id in approved_files:
                approved_files[user_id] = [(fn, ft) for fn, ft in approved_files.get(user_id, []) if fn != file_name]
        except sqlite3.Error as e:
            logger.error(f"❌ SQLite error removing approval for {user_id},{file_name}: {e}")
        finally:
            conn.close()

def get_file_approval_status(user_id, file_name):
    """Returns: 'pending', 'approved', 'rejected', or None"""
    if user_id in approved_files and any(fn == file_name for fn, _ in approved_files.get(user_id, [])):
        return 'approved'
    if user_id in pending_approvals and any(fn == file_name for fn, _ in pending_approvals.get(user_id, [])):
        return 'pending'
    return None

def has_approved_file(user_id):
    """Check if user already has an approved file (blocks new uploads)."""
    if user_id in admin_ids:
        return False  # Admins not blocked
    return bool(approved_files.get(user_id))

def has_pending_file(user_id):
    """Check if user has a pending approval file."""
    if user_id in admin_ids:
        return False
    return bool(pending_approvals.get(user_id))

# --- Force Join Channel DB Operations ---
def add_force_channel_db(channel_id, channel_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO force_join_channels (channel_id, channel_name) VALUES (?, ?)',
                      (channel_id, channel_name))
            conn.commit()
            force_join_channels[channel_id] = channel_name
            logger.info(f"Added force-join channel: {channel_id} ({channel_name})")
        except sqlite3.Error as e:
            logger.error(f"❌ SQLite error adding force channel {channel_id}: {e}")
        finally:
            conn.close()

def remove_force_channel_db(channel_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM force_join_channels WHERE channel_id = ?', (channel_id,))
            conn.commit()
            force_join_channels.pop(channel_id, None)
            logger.info(f"Removed force-join channel: {channel_id}")
        except sqlite3.Error as e:
            logger.error(f"❌ SQLite error removing force channel {channel_id}: {e}")
        finally:
            conn.close()

# --- Other Database Operations ---
def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)',
                      (user_id, file_name, file_type))
            conn.commit()
            if user_id not in user_files: user_files[user_id] = []
            user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != file_name]
            user_files[user_id].append((file_name, file_type))
        except sqlite3.Error as e: logger.error(f"❌ SQLite error saving file {file_name} for {user_id}: {e}")
        finally: conn.close()

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
                if not user_files[user_id]: del user_files[user_id]
        except sqlite3.Error as e: logger.error(f"❌ SQLite error removing file {file_name} for {user_id}: {e}")
        finally: conn.close()

def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
        except sqlite3.Error as e: logger.error(f"❌ SQLite error adding active user {user_id}: {e}")
        finally: conn.close()

def save_subscription(user_id, expiry):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                      (user_id, expiry.isoformat()))
            conn.commit()
            user_subscriptions[user_id] = {'expiry': expiry}
        except sqlite3.Error as e: logger.error(f"❌ SQLite error saving sub for {user_id}: {e}")
        finally: conn.close()

def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
            conn.commit()
            if user_id in user_subscriptions: del user_subscriptions[user_id]
        except sqlite3.Error as e: logger.error(f"❌ SQLite error removing sub for {user_id}: {e}")
        finally: conn.close()

def add_admin_db(admin_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (admin_id,))
            conn.commit()
            admin_ids.add(admin_id)
        except sqlite3.Error as e: logger.error(f"❌ SQLite error adding admin {admin_id}: {e}")
        finally: conn.close()

def remove_admin_db(admin_id):
    if admin_id == OWNER_ID: return False
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        removed = False
        try:
            c.execute('SELECT 1 FROM admins WHERE user_id = ?', (admin_id,))
            if c.fetchone():
                c.execute('DELETE FROM admins WHERE user_id = ?', (admin_id,))
                conn.commit()
                removed = c.rowcount > 0
                if removed: admin_ids.discard(admin_id)
            else:
                admin_ids.discard(admin_id)
            return removed
        except sqlite3.Error as e: logger.error(f"❌ SQLite error removing admin {admin_id}: {e}"); return False
        finally: conn.close()

# --- Automatic Package Installation & Script Running ---
TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'aiogram': 'aiogram',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'telethon.sync': 'telethon',
    'bs4': 'beautifulsoup4',
    'requests': 'requests',
    'pillow': 'Pillow',
    'cv2': 'opencv-python',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil',
    'pandas': 'pandas',
    'numpy': 'numpy',
    'flask': 'Flask',
    'django': 'Django',
    'sqlalchemy': 'SQLAlchemy',
    'psutil': 'psutil',
    'asyncio': None,
    'json': None,
    'datetime': None,
    'os': None,
    'sys': None,
    're': None,
    'time': None,
    'math': None,
    'random': None,
    'logging': None,
    'threading': None,
    'subprocess': None,
    'zipfile': None,
    'tempfile': None,
    'shutil': None,
    'sqlite3': None,
    'atexit': None,
}

def attempt_install_pip(module_name, message):
    package_name = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    if package_name is None:
        return False
    try:
        bot.reply_to(message, f"🐍 Module `{module_name}` not found. Installing `{package_name}`...", parse_mode='Markdown')
        command = [sys.executable, '-m', 'pip', 'install', package_name]
        result = subprocess.run(command, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Package `{package_name}` installed.", parse_mode='Markdown')
            return True
        else:
            error_msg = f"❌ Failed to install `{package_name}`.\nLog:\n```\n{result.stderr or result.stdout}\n```"
            if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (truncated)"
            cb_pkg = package_name[:40]
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(
                f"📦 Force Install `{cb_pkg}`",
                callback_data=f"finstall_{cb_pkg}"
            ))
            bot.reply_to(message, error_msg + "\n\n👑 *Admins can force-install below:*",
                         parse_mode='Markdown', reply_markup=markup)
            return False
    except Exception as e:
        bot.reply_to(message, f"❌ Error installing `{package_name}`: {str(e)}")
        return False

def force_install_pip_callback(call):
    user_id = call.from_user.id
    if user_id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Sirf admins ye kar sakte hain!", show_alert=True)
        return
    package_name = call.data[len("finstall_"):]
    if not package_name:
        bot.answer_callback_query(call.id, "⚠️ Package name missing.", show_alert=True)
        return
    bot.answer_callback_query(call.id, f"⏳ Installing {package_name}...")
    bot.send_message(call.message.chat.id,
                     f"⏳ Force installing `{package_name}` with `--break-system-packages`...",
                     parse_mode='Markdown')
    command = [sys.executable, '-m', 'pip', 'install', package_name, '--break-system-packages']
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.send_message(call.message.chat.id,
                             f"✅ Package `{package_name}` force-installed!\nAb script dobara start karein.",
                             parse_mode='Markdown')
        else:
            err = (result.stderr or result.stdout)[:3000]
            bot.send_message(call.message.chat.id,
                             f"❌ Force install fail hua `{package_name}`.\nLog:\n```\n{err}\n```",
                             parse_mode='Markdown')
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error: {e}")

def attempt_install_npm(module_name, user_folder, message):
    try:
        bot.reply_to(message, f"🟠 Node package `{module_name}` not found. Installing locally...", parse_mode='Markdown')
        command = ['npm', 'install', module_name]
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=user_folder, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Node package `{module_name}` installed.", parse_mode='Markdown')
            return True
        else:
            error_msg = f"❌ Failed to install Node package `{module_name}`.\nLog:\n```\n{result.stderr or result.stdout}\n```"
            if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (truncated)"
            bot.reply_to(message, error_msg, parse_mode='Markdown')
            return False
    except FileNotFoundError:
        bot.reply_to(message, "❌ 'npm' not found. Ensure Node.js is installed.")
        return False
    except Exception as e:
        bot.reply_to(message, f"❌ Error installing Node package `{module_name}`: {str(e)}")
        return False

def run_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    max_attempts = 2
    if attempt > max_attempts:
        bot.reply_to(message_obj_for_reply, f"❌ Failed to run '{file_name}' after {max_attempts} attempts.")
        return
    script_key = f"{script_owner_id}_{file_name}"
    try:
        if not os.path.exists(script_path):
            bot.reply_to(message_obj_for_reply, f"❌ Script '{file_name}' not found!")
            user_files[script_owner_id] = [f for f in user_files.get(script_owner_id, []) if f[0] != file_name]
            remove_user_file_db(script_owner_id, file_name)
            return
        if attempt == 1:
            check_proc = None
            try:
                check_proc = subprocess.Popen(
                    [sys.executable, script_path], cwd=user_folder,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='ignore'
                )
                stdout, stderr = check_proc.communicate(timeout=5)
                if check_proc.returncode != 0 and stderr:
                    match_py = re.search(r"ModuleNotFoundError: No module named '(.+?)'", stderr)
                    if match_py:
                        module_name = match_py.group(1).strip().strip("'\"")
                        if attempt_install_pip(module_name, message_obj_for_reply):
                            bot.reply_to(message_obj_for_reply, f"🔄 Install OK. Retrying '{file_name}'...")
                            time.sleep(2)
                            threading.Thread(target=run_script, args=(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt + 1)).start()
                            return
                        else:
                            bot.reply_to(message_obj_for_reply, f"❌ Install failed. Cannot run '{file_name}'.")
                            return
                    else:
                        error_summary = stderr[:500]
                        bot.reply_to(message_obj_for_reply, f"❌ Script error:\n```\n{error_summary}\n```", parse_mode='Markdown')
                        return
            except subprocess.TimeoutExpired:
                if check_proc and check_proc.poll() is None: check_proc.kill(); check_proc.communicate()
            except FileNotFoundError:
                bot.reply_to(message_obj_for_reply, f"❌ Python interpreter not found.")
                return
            except Exception as e:
                bot.reply_to(message_obj_for_reply, f"❌ Error in pre-check: {e}")
                return
            finally:
                if check_proc and check_proc.poll() is None:
                    check_proc.kill(); check_proc.communicate()

        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None; process = None
        try: log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
            bot.reply_to(message_obj_for_reply, f"❌ Failed to open log file: {e}")
            return
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            process = subprocess.Popen(
                [sys.executable, script_path], cwd=user_folder, stdout=log_file, stderr=log_file,
                stdin=subprocess.PIPE, startupinfo=startupinfo, encoding='utf-8', errors='ignore'
            )
            bot_scripts[script_key] = {
                'process': process, 'log_file': log_file, 'file_name': file_name,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(), 'user_folder': user_folder, 'type': 'py', 'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply, f"✅ Python script '{file_name}' started! (PID: {process.pid})")
        except Exception as e:
            if log_file and not log_file.closed: log_file.close()
            bot.reply_to(message_obj_for_reply, f"❌ Error starting '{file_name}': {str(e)}")
            if script_key in bot_scripts: del bot_scripts[script_key]
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Unexpected error running '{file_name}': {str(e)}")
        if script_key in bot_scripts:
            kill_process_tree(bot_scripts[script_key])
            del bot_scripts[script_key]

def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    max_attempts = 2
    if attempt > max_attempts:
        bot.reply_to(message_obj_for_reply, f"❌ Failed to run '{file_name}' after {max_attempts} attempts.")
        return
    script_key = f"{script_owner_id}_{file_name}"
    try:
        if not os.path.exists(script_path):
            bot.reply_to(message_obj_for_reply, f"❌ Script '{file_name}' not found!")
            user_files[script_owner_id] = [f for f in user_files.get(script_owner_id, []) if f[0] != file_name]
            remove_user_file_db(script_owner_id, file_name)
            return
        if attempt == 1:
            check_proc = None
            try:
                check_proc = subprocess.Popen(
                    ['node', script_path], cwd=user_folder,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='ignore'
                )
                stdout, stderr = check_proc.communicate(timeout=5)
                if check_proc.returncode != 0 and stderr:
                    match_js = re.search(r"Cannot find module '(.+?)'", stderr)
                    if match_js:
                        module_name = match_js.group(1).strip().strip("'\"")
                        if not module_name.startswith('.') and not module_name.startswith('/'):
                            if attempt_install_npm(module_name, user_folder, message_obj_for_reply):
                                bot.reply_to(message_obj_for_reply, f"🔄 NPM Install OK. Retrying '{file_name}'...")
                                time.sleep(2)
                                threading.Thread(target=run_js_script, args=(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt + 1)).start()
                                return
                            else:
                                bot.reply_to(message_obj_for_reply, f"❌ NPM Install failed.")
                                return
                    error_summary = stderr[:500]
                    bot.reply_to(message_obj_for_reply, f"❌ JS Script error:\n```\n{error_summary}\n```\n", parse_mode='Markdown')
                    return
            except subprocess.TimeoutExpired:
                if check_proc and check_proc.poll() is None: check_proc.kill(); check_proc.communicate()
            except FileNotFoundError:
                bot.reply_to(message_obj_for_reply, "❌ 'node' not found. Install Node.js.")
                return
            except Exception as e:
                bot.reply_to(message_obj_for_reply, f"❌ Error in JS pre-check: {e}")
                return
            finally:
                if check_proc and check_proc.poll() is None:
                    check_proc.kill(); check_proc.communicate()

        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None; process = None
        try: log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
            bot.reply_to(message_obj_for_reply, f"❌ Failed to open log file: {e}")
            return
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            process = subprocess.Popen(
                ['node', script_path], cwd=user_folder, stdout=log_file, stderr=log_file,
                stdin=subprocess.PIPE, startupinfo=startupinfo, encoding='utf-8', errors='ignore'
            )
            bot_scripts[script_key] = {
                'process': process, 'log_file': log_file, 'file_name': file_name,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(), 'user_folder': user_folder, 'type': 'js', 'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply, f"✅ JS script '{file_name}' started! (PID: {process.pid})")
        except Exception as e:
            if log_file and not log_file.closed: log_file.close()
            bot.reply_to(message_obj_for_reply, f"❌ Error starting JS '{file_name}': {str(e)}")
            if script_key in bot_scripts: del bot_scripts[script_key]
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Unexpected error running JS '{file_name}': {str(e)}")
        if script_key in bot_scripts:
            kill_process_tree(bot_scripts[script_key])
            del bot_scripts[script_key]

# --- Menu Creation ---
def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL),
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'),
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📦 Manual Install', callback_data='manual_install'),
        types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}')
    ]
    if user_id in admin_ids:
        admin_buttons = [
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscription'),
            types.InlineKeyboardButton('📊 Statistics', callback_data='stats'),
            types.InlineKeyboardButton('🔒 Lock Bot' if not bot_locked else '🔓 Unlock Bot',
                                       callback_data='lock_bot' if not bot_locked else 'unlock_bot'),
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast'),
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'),
            types.InlineKeyboardButton('🟢 Run All User Scripts', callback_data='run_all_scripts')
        ]
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3], admin_buttons[0])
        markup.add(admin_buttons[1], admin_buttons[3])
        markup.add(admin_buttons[2], admin_buttons[5])
        markup.add(buttons[4])
        markup.add(admin_buttons[4])
        markup.add(buttons[5])
    else:
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3])
        markup.add(types.InlineKeyboardButton('📊 Statistics', callback_data='stats'))
        markup.add(buttons[4])
        markup.add(buttons[5])
    return markup

def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout_to_use = ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC if user_id in admin_ids else COMMAND_BUTTONS_LAYOUT_USER_SPEC
    for row_buttons_text in layout_to_use:
        markup.add(*[types.KeyboardButton(text) for text in row_buttons_text])
    return markup

def create_control_buttons(script_owner_id, file_name, is_running=True):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.row(
            types.InlineKeyboardButton("🔴 Stop", callback_data=f'stop_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f'restart_{script_owner_id}_{file_name}')
        )
        markup.row(
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("📜 Logs", callback_data=f'logs_{script_owner_id}_{file_name}')
        )
    else:
        markup.row(
            types.InlineKeyboardButton("🟢 Start", callback_data=f'start_{script_owner_id}_{file_name}'),
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}')
        )
        markup.row(
            types.InlineKeyboardButton("📜 View Logs", callback_data=f'logs_{script_owner_id}_{file_name}')
        )
    markup.add(types.InlineKeyboardButton("🔙 Back to Files", callback_data='check_files'))
    return markup

def create_admin_panel():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Admin', callback_data='add_admin'),
        types.InlineKeyboardButton('➖ Remove Admin', callback_data='remove_admin')
    )
    markup.row(types.InlineKeyboardButton('📋 List Admins', callback_data='list_admins'))
    markup.row(
        types.InlineKeyboardButton('📢 Add Group/Channel', callback_data='add_force_channel'),
        types.InlineKeyboardButton('🗑️ Remove Channel', callback_data='remove_force_channel')
    )
    markup.row(types.InlineKeyboardButton('📋 List Channels', callback_data='list_force_channels'))
    markup.row(
        types.InlineKeyboardButton('✅ Pending Approvals', callback_data='pending_approvals_list')
    )
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup

def create_subscription_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Subscription', callback_data='add_subscription'),
        types.InlineKeyboardButton('➖ Remove Subscription', callback_data='remove_subscription')
    )
    markup.row(types.InlineKeyboardButton('🔍 Check Subscription', callback_data='check_subscription'))
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup

# --- File Handling ---
def handle_zip_file(downloaded_file_content, file_name_zip, message):
    user_id = message.from_user.id
    user_folder = get_user_folder(user_id)
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_zip_")
        zip_path = os.path.join(temp_dir, file_name_zip)
        with open(zip_path, 'wb') as new_file: new_file.write(downloaded_file_content)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                member_path = os.path.abspath(os.path.join(temp_dir, member.filename))
                if not member_path.startswith(os.path.abspath(temp_dir)):
                    raise zipfile.BadZipFile(f"Unsafe path: {member.filename}")
            zip_ref.extractall(temp_dir)
        extracted_items = os.listdir(temp_dir)
        py_files = [f for f in extracted_items if f.endswith('.py')]
        js_files = [f for f in extracted_items if f.endswith('.js')]
        req_file = 'requirements.txt' if 'requirements.txt' in extracted_items else None
        pkg_json = 'package.json' if 'package.json' in extracted_items else None
        if req_file:
            req_path = os.path.join(temp_dir, req_file)
            bot.reply_to(message, f"🔄 Installing Python deps from `{req_file}`...")
            try:
                result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req_path],
                                        capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
                bot.reply_to(message, f"✅ Python deps installed.")
            except subprocess.CalledProcessError as e:
                error_msg = f"❌ Failed to install Python deps.\nLog:\n```\n{e.stderr or e.stdout}\n```"
                if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (truncated)"
                bot.reply_to(message, error_msg, parse_mode='Markdown'); return
        if pkg_json:
            bot.reply_to(message, f"🔄 Installing Node deps from `{pkg_json}`...")
            try:
                result = subprocess.run(['npm', 'install'], capture_output=True, text=True, check=True,
                                        cwd=temp_dir, encoding='utf-8', errors='ignore')
                bot.reply_to(message, f"✅ Node deps installed.")
            except FileNotFoundError:
                bot.reply_to(message, "❌ 'npm' not found."); return
            except subprocess.CalledProcessError as e:
                error_msg = f"❌ Failed to install Node deps.\nLog:\n```\n{e.stderr or e.stdout}\n```"
                if len(error_msg) > 4000: error_msg = error_msg[:4000] + "\n... (truncated)"
                bot.reply_to(message, error_msg, parse_mode='Markdown'); return
        main_script_name = None; file_type = None
        preferred_py = ['main.py', 'bot.py', 'app.py']
        preferred_js = ['index.js', 'main.js', 'bot.js', 'app.js']
        for p in preferred_py:
            if p in py_files: main_script_name = p; file_type = 'py'; break
        if not main_script_name:
            for p in preferred_js:
                if p in js_files: main_script_name = p; file_type = 'js'; break
        if not main_script_name:
            if py_files: main_script_name = py_files[0]; file_type = 'py'
            elif js_files: main_script_name = js_files[0]; file_type = 'js'
        if not main_script_name:
            bot.reply_to(message, "❌ No `.py` or `.js` script found in archive!"); return
        for item_name in os.listdir(temp_dir):
            src_path = os.path.join(temp_dir, item_name)
            dest_path = os.path.join(user_folder, item_name)
            if os.path.isdir(dest_path): shutil.rmtree(dest_path)
            elif os.path.exists(dest_path): os.remove(dest_path)
            shutil.move(src_path, dest_path)
        save_user_file(user_id, main_script_name, file_type)
        # Save as pending approval
        save_file_approval(user_id, main_script_name, file_type, 'pending')
        notify_admins_for_approval(user_id, main_script_name, file_type, message)
        bot.reply_to(message,
            f"✅ Files extracted! Script `{main_script_name}` uploaded.\n\n"
            f"⏳ *Admin approval ka wait karo. Approve hone ke baad hi script run hogi.*",
            parse_mode='Markdown')
    except zipfile.BadZipFile as e:
        bot.reply_to(message, f"❌ Invalid/corrupted ZIP. {e}")
    except Exception as e:
        logger.error(f"❌ Error processing zip for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error processing zip: {str(e)}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try: shutil.rmtree(temp_dir)
            except Exception: pass

def handle_js_file(file_path, script_owner_id, user_folder, file_name, message):
    try:
        save_user_file(script_owner_id, file_name, 'js')
        save_file_approval(script_owner_id, file_name, 'js', 'pending')
        notify_admins_for_approval(script_owner_id, file_name, 'js', message)
        bot.reply_to(message,
            f"✅ JS script `{file_name}` uploaded!\n\n"
            f"⏳ *Admin approval ka wait karo. Approve hone ke baad hi script run hogi.*",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"❌ Error processing JS file {file_name}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error processing JS file: {str(e)}")

def handle_py_file(file_path, script_owner_id, user_folder, file_name, message):
    try:
        save_user_file(script_owner_id, file_name, 'py')
        save_file_approval(script_owner_id, file_name, 'py', 'pending')
        notify_admins_for_approval(script_owner_id, file_name, 'py', message)
        bot.reply_to(message,
            f"✅ Python script `{file_name}` uploaded!\n\n"
            f"⏳ *Admin approval ka wait karo. Approve hone ke baad hi script run hogi.*",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"❌ Error processing Python file {file_name}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error processing Python file: {str(e)}")

def notify_admins_for_approval(user_id, file_name, file_type, message):
    """Notify all admins that a new file needs approval."""
    user_name = message.from_user.first_name
    username = message.from_user.username or "N/A"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("✅ Approve", callback_data=f'approve_{user_id}_{file_name}'),
        types.InlineKeyboardButton("❌ Reject", callback_data=f'reject_{user_id}_{file_name}')
    )
    notification = (
        f"🆕 *New Script Approval Request*\n\n"
        f"👤 User: {user_name} (@{username})\n"
        f"🆔 User ID: `{user_id}`\n"
        f"📄 File: `{file_name}` ({file_type})\n\n"
        f"Approve karein to script run hogi, reject karein to delete ho jayegi."
    )
    for admin_id in admin_ids:
        try:
            bot.send_message(admin_id, notification, parse_mode='Markdown', reply_markup=markup)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} for approval: {e}")

# --- Logic Functions ---
def _logic_send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    user_username = message.from_user.username

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, "⚠️ Bot locked by admin. Try later.")
        return

    # Force Join Check
    if force_join_channels:
        not_joined = check_force_join(user_id)
        if not_joined:
            send_force_join_message(chat_id, not_joined)
            return

    user_bio = "Could not fetch bio"; photo_file_id = None
    try: user_bio = bot.get_chat(user_id).bio or "No bio"
    except Exception: pass
    try:
        user_profile_photos = bot.get_user_profile_photos(user_id, limit=1)
        if user_profile_photos.photos: photo_file_id = user_profile_photos.photos[0][-1].file_id
    except Exception: pass

    if user_id not in active_users:
        add_active_user(user_id)
        try:
            owner_notification = (f"🎉 New user!\n👤 Name: {user_name}\n✳️ User: @{user_username or 'N/A'}\n"
                                  f"🆔 ID: `{user_id}`\n📝 Bio: {user_bio}")
            bot.send_message(OWNER_ID, owner_notification, parse_mode='Markdown')
            if photo_file_id: bot.send_photo(OWNER_ID, photo_file_id, caption=f"Pic of new user {user_id}")
        except Exception as e: logger.error(f"⚠️ Failed to notify owner: {e}")

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""
    if user_id == OWNER_ID: user_status = "👑 Owner"
    elif user_id in admin_ids: user_status = "🛡️ Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            user_status = "⭐ Premium"; days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\n⏳ Subscription expires in: {days_left} days"
        else: user_status = "🆓 Free User (Expired Sub)"; remove_subscription_db(user_id)
    else: user_status = "🆓 Free User"

    # Pending approval info
    pending_info = ""
    if has_pending_file(user_id):
        pending_info = "\n⏳ *Aapka ek script pending approval mein hai.*"
    elif has_approved_file(user_id):
        pending_info = "\n✅ *Aapka script approved hai.*"

    welcome_msg_text = (f"〽️ Welcome, {user_name}!\n\n🆔 Your User ID: `{user_id}`\n"
                        f"✳️ Username: `@{user_username or 'Not set'}`\n"
                        f"🔰 Your Status: {user_status}{expiry_info}\n"
                        f"📁 Files Uploaded: {current_files} / {limit_str}\n"
                        f"{pending_info}\n\n"
                        f"🤖 Host & run Python (`.py`) or JS (`.js`) scripts.\n"
                        f"👇 Use buttons or type commands.")
    main_reply_markup = create_reply_keyboard_main_menu(user_id)
    try:
        if photo_file_id: bot.send_photo(chat_id, photo_file_id)
        bot.send_message(chat_id, welcome_msg_text, reply_markup=main_reply_markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error sending welcome to {user_id}: {e}", exc_info=True)
        try: bot.send_message(chat_id, welcome_msg_text, reply_markup=main_reply_markup, parse_mode='Markdown')
        except Exception: pass

def _force_join_gate(message):
    """Check force join for any handler. Returns True if user may proceed."""
    user_id = message.from_user.id
    if user_id in admin_ids:
        return True
    if force_join_channels:
        not_joined = check_force_join(user_id)
        if not_joined:
            send_force_join_message(message.chat.id, not_joined)
            return False
    return True

def _logic_updates_channel(message):
    if not _force_join_gate(message): return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL))
    bot.reply_to(message, "Visit our Updates Channel:", reply_markup=markup)

def _logic_upload_file(message):
    user_id = message.from_user.id
    if not _force_join_gate(message): return
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked by admin, cannot accept files.")
        return
    # Block if user already has an approved file
    if has_approved_file(user_id):
        bot.reply_to(message,
            "⚠️ *Aapke paas pehle se ek approved script hai!*\n\n"
            "Naya script upload karne ke liye pehle purana approved script delete karo.",
            parse_mode='Markdown')
        return
    # Block if user already has a pending file
    if has_pending_file(user_id):
        bot.reply_to(message,
            "⚠️ *Aapka ek script already admin approval ke liye pending hai.*\n\n"
            "Approval ka wait karo ya pending script delete karo.",
            parse_mode='Markdown')
        return
    # Check one-script-at-a-time for admins only (regular limit check skipped for approval system)
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit and user_id not in admin_ids:
        limit_str = str(file_limit)
        bot.reply_to(message, f"⚠️ File limit ({current_files}/{limit_str}) reached. Delete files first.")
        return
    bot.reply_to(message, "📤 Send your Python (`.py`), JS (`.js`), or ZIP (`.zip`) file.")

def _logic_check_files(message):
    user_id = message.from_user.id
    if not _force_join_gate(message): return
    user_files_list = user_files.get(user_id, [])
    pending_list = pending_approvals.get(user_id, [])
    all_list = list(user_files_list)
    if not all_list and not pending_list:
        bot.reply_to(message, "📂 Your files:\n\n(No files uploaded yet)")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for file_name, file_type in sorted(set(all_list)):
        is_running = is_bot_running(user_id, file_name)
        approval_status = get_file_approval_status(user_id, file_name)
        if approval_status == 'pending':
            status_icon = "⏳ Pending Approval"
        elif approval_status == 'approved':
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        else:
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        btn_text = f"{file_name} ({file_type}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f'file_{user_id}_{file_name}'))
    bot.reply_to(message, "📂 Your files:\nClick to manage.", reply_markup=markup, parse_mode='Markdown')

def _logic_bot_speed(message):
    if not _force_join_gate(message): return
    user_id = message.from_user.id
    chat_id = message.chat.id
    start_time_ping = time.time()
    wait_msg = bot.reply_to(message, "🏃 Testing speed...")
    try:
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start_time_ping) * 1000, 2)
        status = "🔓 Unlocked" if not bot_locked else "🔒 Locked"
        if user_id == OWNER_ID: user_level = "👑 Owner"
        elif user_id in admin_ids: user_level = "🛡️ Admin"
        elif user_id in user_subscriptions and user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now(): user_level = "⭐ Premium"
        else: user_level = "🆓 Free User"
        speed_msg = (f"⚡ Bot Speed & Status:\n\n⏱️ API Response Time: {response_time} ms\n"
                     f"🚦 Bot Status: {status}\n"
                     f"👤 Your Level: {user_level}")
        bot.edit_message_text(speed_msg, chat_id, wait_msg.message_id)
    except Exception as e:
        logger.error(f"Error during speed test: {e}", exc_info=True)
        bot.edit_message_text("❌ Error during speed test.", chat_id, wait_msg.message_id)

def _logic_contact_owner(message):
    if not _force_join_gate(message): return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'))
    bot.reply_to(message, "Click to contact Owner:", reply_markup=markup)

# --- Admin Logic Functions ---
def _logic_subscriptions_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    bot.reply_to(message, "💳 Subscription Management", reply_markup=create_subscription_menu())

def _logic_statistics(message):
    user_id = message.from_user.id
    total_users = len(active_users)
    total_files_records = sum(len(files) for files in user_files.values())
    running_bots_count = 0
    user_running_bots = 0
    for script_key_iter, script_info_iter in list(bot_scripts.items()):
        s_owner_id, _ = script_key_iter.split('_', 1)
        if is_bot_running(int(s_owner_id), script_info_iter['file_name']):
            running_bots_count += 1
            if int(s_owner_id) == user_id:
                user_running_bots += 1
    pending_count = sum(len(v) for v in pending_approvals.values())
    stats_msg_base = (f"📊 Bot Statistics:\n\n"
                      f"👥 Total Users: {total_users}\n"
                      f"📂 Total File Records: {total_files_records}\n"
                      f"🟢 Total Active Bots: {running_bots_count}\n"
                      f"⏳ Pending Approvals: {pending_count}\n")
    if user_id in admin_ids:
        stats_msg = stats_msg_base + (f"🔒 Bot Status: {'🔴 Locked' if bot_locked else '🟢 Unlocked'}\n"
                                      f"📢 Force Channels: {len(force_join_channels)}\n"
                                      f"🤖 Your Running Bots: {user_running_bots}")
    else:
        stats_msg = stats_msg_base + f"🤖 Your Running Bots: {user_running_bots}"
    bot.reply_to(message, stats_msg)

def _logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    msg = bot.reply_to(message, "📢 Send message to broadcast.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)

def _logic_toggle_lock_bot(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    global bot_locked
    bot_locked = not bot_locked
    status = "locked" if bot_locked else "unlocked"
    bot.reply_to(message, f"🔒 Bot has been {status}.")

def _logic_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    bot.reply_to(message, "👑 Admin Panel", reply_markup=create_admin_panel())

def _logic_run_all_scripts(message_or_call):
    if isinstance(message_or_call, telebot.types.Message):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.chat.id
        reply_func = lambda text, **kwargs: bot.reply_to(message_or_call, text, **kwargs)
        admin_message_obj = message_or_call
    elif isinstance(message_or_call, telebot.types.CallbackQuery):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.message.chat.id
        bot.answer_callback_query(message_or_call.id)
        reply_func = lambda text, **kwargs: bot.send_message(admin_chat_id, text, **kwargs)
        admin_message_obj = message_or_call.message
    else:
        return
    if admin_user_id not in admin_ids:
        reply_func("⚠️ Admin permissions required.")
        return
    reply_func("⏳ Starting all approved user scripts...")
    started_count = 0; skipped_files = 0; error_details = []
    all_user_files_snapshot = dict(user_files)
    for target_user_id, files_for_user in all_user_files_snapshot.items():
        if not files_for_user: continue
        user_folder = get_user_folder(target_user_id)
        for file_name, file_type in files_for_user:
            # Only run approved files
            if get_file_approval_status(target_user_id, file_name) != 'approved':
                continue
            if not is_bot_running(target_user_id, file_name):
                file_path = os.path.join(user_folder, file_name)
                if os.path.exists(file_path):
                    try:
                        if file_type == 'py':
                            threading.Thread(target=run_script, args=(file_path, target_user_id, user_folder, file_name, admin_message_obj)).start()
                        elif file_type == 'js':
                            threading.Thread(target=run_js_script, args=(file_path, target_user_id, user_folder, file_name, admin_message_obj)).start()
                        started_count += 1
                        time.sleep(0.7)
                    except Exception as e:
                        error_details.append(f"`{file_name}` (User {target_user_id}) - Error")
                        skipped_files += 1
                else:
                    error_details.append(f"`{file_name}` (User {target_user_id}) - Not found")
                    skipped_files += 1
    summary_msg = (f"✅ Run All Scripts Complete:\n\n"
                   f"▶️ Started: {started_count} scripts.\n")
    if skipped_files > 0:
        summary_msg += f"⚠️ Skipped: {skipped_files}\n"
    reply_func(summary_msg, parse_mode='Markdown')

def _logic_manual_install(message):
    """Manual package install — available to ALL users."""
    if not _force_join_gate(message): return
    user_id = message.from_user.id
    msg = bot.send_message(
        message.chat.id,
        "📦 *Manual Package Install*\n\n"
        "Package ka naam type karo jo install karna hai\n"
        "_(example: `aiohttp`, `requests`, `telethon`)_\n\n"
        "/cancel — wapas jaane ke liye",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, process_manual_install)

def manual_install_init_callback(call):
    """Inline-button handler for Manual Install — ALL users."""
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "📦 *Manual Package Install*\n\n"
        "Package ka naam type karo jo install karna hai\n"
        "_(example: `aiohttp`, `requests`, `telethon`)_\n\n"
        "/cancel — wapas jaane ke liye",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, process_manual_install)

def process_manual_install(message):
    """Process manual install — all users get standard pip, admins get --break-system-packages."""
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    if text.lower() == '/cancel':
        bot.reply_to(message, "❌ Manual install cancel ho gaya.")
        return
    if not text:
        bot.reply_to(message, "⚠️ Koi package naam nahi diya.")
        msg = bot.send_message(message.chat.id, "📦 Package naam bhejo ya /cancel likho:")
        bot.register_next_step_handler(msg, process_manual_install)
        return
    packages = text.split()
    status_msg = bot.reply_to(message, f"⏳ Installing: `{' '.join(packages)}`...", parse_mode='Markdown')
    # Admins get --break-system-packages, users get standard pip
    if user_id in admin_ids:
        command = [sys.executable, '-m', 'pip', 'install'] + packages + ['--break-system-packages']
    else:
        command = [sys.executable, '-m', 'pip', 'install'] + packages
    logger.info(f"Manual install by user {user_id}: {' '.join(command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.edit_message_text(
                f"✅ *Package(s) install ho gaye!*\n`{' '.join(packages)}`",
                message.chat.id, status_msg.message_id, parse_mode='Markdown'
            )
        else:
            err = (result.stderr or result.stdout)[:3000]
            bot.edit_message_text(
                f"❌ *Install fail hua:* `{' '.join(packages)}`\n\nLog:\n```\n{err}\n```",
                message.chat.id, status_msg.message_id, parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Manual install error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error: {e}")

BUTTON_TEXT_TO_LOGIC = {
    "📢 Updates Channel": _logic_updates_channel,
    "📤 Upload File": _logic_upload_file,
    "📂 Check Files": _logic_check_files,
    "⚡ Bot Speed": _logic_bot_speed,
    "📞 Contact Owner": _logic_contact_owner,
    "📊 Statistics": _logic_statistics,
    "💳 Subscriptions": _logic_subscriptions_panel,
    "📢 Broadcast": _logic_broadcast_init,
    "🔒 Lock Bot": _logic_toggle_lock_bot,
    "🟢 Running All Code": _logic_run_all_scripts,
    "📦 Manual Install": _logic_manual_install,
    "👑 Admin Panel": _logic_admin_panel,
}

@bot.message_handler(commands=['start', 'help'])
def command_send_welcome(message): _logic_send_welcome(message)

@bot.message_handler(commands=['status'])
def command_show_status(message): _logic_statistics(message)

@bot.message_handler(func=lambda message: message.text in BUTTON_TEXT_TO_LOGIC)
def handle_button_text(message):
    logic_func = BUTTON_TEXT_TO_LOGIC.get(message.text)
    if logic_func: logic_func(message)

@bot.message_handler(commands=['updateschannel'])
def command_updates_channel(message): _logic_updates_channel(message)
@bot.message_handler(commands=['uploadfile'])
def command_upload_file(message): _logic_upload_file(message)
@bot.message_handler(commands=['checkfiles'])
def command_check_files(message): _logic_check_files(message)
@bot.message_handler(commands=['botspeed'])
def command_bot_speed(message): _logic_bot_speed(message)
@bot.message_handler(commands=['contactowner'])
def command_contact_owner(message): _logic_contact_owner(message)
@bot.message_handler(commands=['subscriptions'])
def command_subscriptions(message): _logic_subscriptions_panel(message)
@bot.message_handler(commands=['statistics'])
def command_statistics(message): _logic_statistics(message)
@bot.message_handler(commands=['broadcast'])
def command_broadcast(message): _logic_broadcast_init(message)
@bot.message_handler(commands=['lockbot'])
def command_lock_bot(message): _logic_toggle_lock_bot(message)
@bot.message_handler(commands=['adminpanel'])
def command_admin_panel(message): _logic_admin_panel(message)
@bot.message_handler(commands=['runningallcode'])
def command_run_all_code(message): _logic_run_all_scripts(message)

@bot.message_handler(commands=['ping'])
def ping(message):
    start_ping_time = time.time()
    msg = bot.reply_to(message, "Pong!")
    latency = round((time.time() - start_ping_time) * 1000, 2)
    bot.edit_message_text(f"Pong! Latency: {latency} ms", message.chat.id, msg.message_id)

# --- Document (File) Handler ---
@bot.message_handler(content_types=['document'])
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc = message.document

    # Force Join check
    if force_join_channels:
        not_joined = check_force_join(user_id)
        if not_joined:
            send_force_join_message(chat_id, not_joined)
            return

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked, cannot accept files.")
        return

    # Check approved file gate (non-admins)
    if user_id not in admin_ids:
        if has_approved_file(user_id):
            bot.reply_to(message,
                "⚠️ *Aapke paas pehle se ek approved script hai!*\n\n"
                "Naya script upload karne ke liye pehle purana approved script delete karo.",
                parse_mode='Markdown')
            return
        if has_pending_file(user_id):
            bot.reply_to(message,
                "⚠️ *Aapka ek script already admin approval ke liye pending hai.*\n\n"
                "Approval ka wait karo ya pending script delete karo.",
                parse_mode='Markdown')
            return

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if user_id not in admin_ids and current_files >= file_limit:
        bot.reply_to(message, f"⚠️ File limit reached. Delete files first.")
        return

    file_name = doc.file_name
    if not file_name: bot.reply_to(message, "⚠️ No file name."); return
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in ['.py', '.js', '.zip']:
        bot.reply_to(message, "⚠️ Only `.py`, `.js`, `.zip` allowed."); return
    max_file_size = 20 * 1024 * 1024
    if doc.file_size > max_file_size:
        bot.reply_to(message, "⚠️ File too large (Max: 20 MB)."); return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, message.message_id)
            bot.send_message(OWNER_ID, f"⬆️ File '{file_name}' from {message.from_user.first_name} (`{user_id}`)", parse_mode='Markdown')
        except Exception as e: logger.error(f"Failed to forward file to owner: {e}")

        download_wait_msg = bot.reply_to(message, f"⏳ Downloading `{file_name}`...")
        file_info_tg = bot.get_file(doc.file_id)
        downloaded_file_content = bot.download_file(file_info_tg.file_path)
        bot.edit_message_text(f"✅ Downloaded `{file_name}`. Processing...", chat_id, download_wait_msg.message_id)
        user_folder = get_user_folder(user_id)

        if file_ext == '.zip':
            handle_zip_file(downloaded_file_content, file_name, message)
        else:
            file_path = os.path.join(user_folder, file_name)
            with open(file_path, 'wb') as f: f.write(downloaded_file_content)
            if file_ext == '.js': handle_js_file(file_path, user_id, user_folder, file_name, message)
            elif file_ext == '.py': handle_py_file(file_path, user_id, user_folder, file_name, message)
    except telebot.apihelper.ApiTelegramException as e:
        if "file is too big" in str(e).lower():
            bot.reply_to(message, f"❌ File too large to download via Telegram API.")
        else:
            bot.reply_to(message, f"❌ Telegram API Error: {str(e)}.")
    except Exception as e:
        logger.error(f"❌ Error handling file for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Unexpected error: {str(e)}")

# --- Callback Query Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"Callback: User={user_id}, Data='{data}'")

    if bot_locked and user_id not in admin_ids and data not in ['back_to_main', 'speed', 'stats', 'check_joined']:
        bot.answer_callback_query(call.id, "⚠️ Bot locked by admin.", show_alert=True)
        return
    try:
        if data == 'check_joined':
            check_joined_callback(call)
        elif data == 'upload':
            upload_callback(call)
        elif data == 'check_files':
            check_files_callback(call)
        elif data.startswith('file_'):
            file_control_callback(call)
        elif data.startswith('start_'):
            start_bot_callback(call)
        elif data.startswith('stop_'):
            stop_bot_callback(call)
        elif data.startswith('restart_'):
            restart_bot_callback(call)
        elif data.startswith('delete_'):
            delete_bot_callback(call)
        elif data.startswith('logs_'):
            logs_bot_callback(call)
        elif data.startswith('approve_'):
            approve_file_callback(call)
        elif data.startswith('reject_'):
            reject_file_callback(call)
        elif data == 'speed':
            speed_callback(call)
        elif data == 'back_to_main':
            back_to_main_callback(call)
        elif data.startswith('confirm_broadcast_'):
            handle_confirm_broadcast(call)
        elif data == 'cancel_broadcast':
            handle_cancel_broadcast(call)
        elif data == 'manual_install':
            manual_install_init_callback(call)
        elif data.startswith('finstall_'):
            force_install_pip_callback(call)
        # Admin callbacks
        elif data == 'subscription':
            admin_required_callback(call, subscription_management_callback)
        elif data == 'stats':
            stats_callback(call)
        elif data == 'lock_bot':
            admin_required_callback(call, lock_bot_callback)
        elif data == 'unlock_bot':
            admin_required_callback(call, unlock_bot_callback)
        elif data == 'run_all_scripts':
            admin_required_callback(call, run_all_scripts_callback)
        elif data == 'broadcast':
            admin_required_callback(call, broadcast_init_callback)
        elif data == 'admin_panel':
            admin_required_callback(call, admin_panel_callback)
        elif data == 'add_admin':
            owner_required_callback(call, add_admin_init_callback)
        elif data == 'remove_admin':
            owner_required_callback(call, remove_admin_init_callback)
        elif data == 'list_admins':
            admin_required_callback(call, list_admins_callback)
        elif data == 'add_force_channel':
            admin_required_callback(call, add_force_channel_callback)
        elif data == 'remove_force_channel':
            admin_required_callback(call, remove_force_channel_callback)
        elif data == 'list_force_channels':
            admin_required_callback(call, list_force_channels_callback)
        elif data == 'pending_approvals_list':
            admin_required_callback(call, pending_approvals_list_callback)
        elif data == 'add_subscription':
            admin_required_callback(call, add_subscription_init_callback)
        elif data == 'remove_subscription':
            admin_required_callback(call, remove_subscription_init_callback)
        elif data == 'check_subscription':
            admin_required_callback(call, check_subscription_init_callback)
        else:
            bot.answer_callback_query(call.id, "Unknown action.")
    except Exception as e:
        logger.error(f"Error handling callback '{data}' for {user_id}: {e}", exc_info=True)
        try: bot.answer_callback_query(call.id, "Error processing request.", show_alert=True)
        except Exception: pass

def admin_required_callback(call, func_to_run):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin permissions required.", show_alert=True)
        return
    func_to_run(call)

def owner_required_callback(call, func_to_run):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⚠️ Owner permissions required.", show_alert=True)
        return
    func_to_run(call)

def check_joined_callback(call):
    """User clicked 'I joined' — re-check membership."""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    not_joined = check_force_join(user_id)
    if not_joined:
        bot.answer_callback_query(call.id, "⚠️ Abhi bhi channels join nahi kiye!", show_alert=True)
        # Update the message with current not-joined channels
        markup = types.InlineKeyboardMarkup(row_width=1)
        for channel_id, channel_name in not_joined:
            invite_link = channel_id if channel_id.startswith('http') else f"https://t.me/{channel_id.lstrip('@')}"
            markup.add(types.InlineKeyboardButton(f"📢 {channel_name}", url=invite_link))
        markup.add(types.InlineKeyboardButton("✅ Maine Join Kar Liya!", callback_data='check_joined'))
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
        except Exception: pass
    else:
        bot.answer_callback_query(call.id, "✅ Shukriya! Ab bot use kar sakte ho.", show_alert=True)
        try: bot.delete_message(chat_id, call.message.message_id)
        except Exception: pass
        # Show welcome
        _logic_send_welcome(call.message)

def approve_file_callback(call):
    """Admin approves a pending script."""
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True)
        return
    try:
        parts = call.data.split('_', 2)
        user_id = int(parts[1])
        file_name = parts[2]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "⚠️ Invalid data.", show_alert=True)
        return

    # Check user has this file
    user_files_list = user_files.get(user_id, [])
    file_info = next((f for f in user_files_list if f[0] == file_name), None)
    if not file_info:
        bot.answer_callback_query(call.id, "⚠️ File not found for this user.", show_alert=True)
        return

    file_type = file_info[1]
    save_file_approval(user_id, file_name, file_type, 'approved')
    bot.answer_callback_query(call.id, f"✅ Script '{file_name}' approved!", show_alert=True)

    # Update admin message
    try:
        bot.edit_message_text(
            f"✅ *Script Approved*\n\n"
            f"🆔 User: `{user_id}`\n"
            f"📄 File: `{file_name}` ({file_type})\n"
            f"✅ Approved by: {call.from_user.first_name}",
            call.message.chat.id, call.message.message_id, parse_mode='Markdown'
        )
    except Exception: pass

    # Auto-run the script for the user
    user_folder = get_user_folder(user_id)
    file_path = os.path.join(user_folder, file_name)

    # Notify user
    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📂 Check Files", callback_data='check_files'))
        bot.send_message(user_id,
            f"🎉 *Aapka script approve ho gaya!*\n\n"
            f"📄 File: `{file_name}`\n"
            f"✅ Script automatically start ho rahi hai...",
            parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} of approval: {e}")

    # Run the script
    if os.path.exists(file_path):
        if file_type == 'py':
            # Create a dummy message object for the runner
            threading.Thread(target=_run_approved_script,
                             args=(file_path, user_id, user_folder, file_name, file_type)).start()
        elif file_type == 'js':
            threading.Thread(target=_run_approved_script,
                             args=(file_path, user_id, user_folder, file_name, file_type)).start()

def _run_approved_script(file_path, user_id, user_folder, file_name, file_type):
    """Run script after admin approval — uses admin chat for feedback."""
    script_key = f"{user_id}_{file_name}"
    log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    log_file = None; process = None
    try:
        log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        cmd = [sys.executable, file_path] if file_type == 'py' else ['node', file_path]
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            cmd, cwd=user_folder, stdout=log_file, stderr=log_file,
            stdin=subprocess.PIPE, startupinfo=startupinfo, encoding='utf-8', errors='ignore'
        )
        bot_scripts[script_key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'chat_id': user_id, 'script_owner_id': user_id,
            'start_time': datetime.now(), 'user_folder': user_folder,
            'type': file_type, 'script_key': script_key
        }
        try:
            bot.send_message(user_id, f"🟢 Script `{file_name}` chal rahi hai! (PID: {process.pid})", parse_mode='Markdown')
        except Exception: pass
    except Exception as e:
        if log_file and not log_file.closed: log_file.close()
        logger.error(f"Error auto-running approved script {file_name} for {user_id}: {e}")
        try: bot.send_message(user_id, f"❌ Script start karne mein error: {e}")
        except Exception: pass

def reject_file_callback(call):
    """Admin rejects a pending script."""
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True)
        return
    try:
        parts = call.data.split('_', 2)
        user_id = int(parts[1])
        file_name = parts[2]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "⚠️ Invalid data.", show_alert=True)
        return

    # Remove file
    user_folder = get_user_folder(user_id)
    file_path = os.path.join(user_folder, file_name)
    if os.path.exists(file_path):
        try: os.remove(file_path)
        except Exception: pass
    remove_user_file_db(user_id, file_name)
    remove_file_approval_db(user_id, file_name)

    bot.answer_callback_query(call.id, f"❌ Script '{file_name}' rejected & deleted.", show_alert=True)
    try:
        bot.edit_message_text(
            f"❌ *Script Rejected*\n\n"
            f"🆔 User: `{user_id}`\n"
            f"📄 File: `{file_name}`\n"
            f"❌ Rejected by: {call.from_user.first_name}",
            call.message.chat.id, call.message.message_id, parse_mode='Markdown'
        )
    except Exception: pass
    try:
        bot.send_message(user_id,
            f"❌ *Aapka script reject kar diya gaya.*\n\n"
            f"📄 File: `{file_name}`\n"
            f"Dobara upload karein ya admin se contact karein.",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to notify user {user_id} of rejection: {e}")

def upload_callback(call):
    user_id = call.from_user.id
    # Force Join check
    if force_join_channels:
        not_joined = check_force_join(user_id)
        if not_joined:
            bot.answer_callback_query(call.id)
            send_force_join_message(call.message.chat.id, not_joined)
            return
    if user_id not in admin_ids:
        if has_approved_file(user_id):
            bot.answer_callback_query(call.id,
                "⚠️ Pehle approved script delete karo, tab naya upload kar sakte ho.", show_alert=True)
            return
        if has_pending_file(user_id):
            bot.answer_callback_query(call.id,
                "⚠️ Ek script already pending approval mein hai.", show_alert=True)
            return
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if user_id not in admin_ids and current_files >= file_limit:
        bot.answer_callback_query(call.id, f"⚠️ File limit reached.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📤 Send your Python (`.py`), JS (`.js`), or ZIP (`.zip`) file.")

def check_files_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    user_files_list = user_files.get(user_id, [])
    if not user_files_list:
        bot.answer_callback_query(call.id, "⚠️ No files uploaded.", show_alert=True)
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
            bot.edit_message_text("📂 Your files:\n\n(No files uploaded)", chat_id, call.message.message_id, reply_markup=markup)
        except Exception: pass
        return
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for file_name, file_type in sorted(set(user_files_list)):
        is_running = is_bot_running(user_id, file_name)
        approval_status = get_file_approval_status(user_id, file_name)
        if approval_status == 'pending':
            status_icon = "⏳ Pending Approval"
        elif approval_status == 'approved':
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        else:
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        btn_text = f"{file_name} ({file_type}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f'file_{user_id}_{file_name}'))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
    try:
        bot.edit_message_text("📂 Your files:\nClick to manage.", chat_id, call.message.message_id,
                              reply_markup=markup, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.error(f"Error editing file list: {e}")

def file_control_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ You can only manage your own files.", show_alert=True)
            return
        user_files_list = user_files.get(script_owner_id, [])
        if not any(f[0] == file_name for f in user_files_list):
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        is_running = is_bot_running(script_owner_id, file_name)
        approval_status = get_file_approval_status(script_owner_id, file_name)
        status_text = '🟢 Running' if is_running else '🔴 Stopped'
        approval_text = ''
        if approval_status == 'pending':
            approval_text = '\n⏳ Status: Pending Admin Approval'
        file_type = next((f[1] for f in user_files_list if f[0] == file_name), '?')
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type}) of User `{script_owner_id}`\n"
                f"Status: {status_text}{approval_text}",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e): raise
    except (ValueError, IndexError) as ve:
        logger.error(f"Error parsing file control callback: {ve}. Data: '{call.data}'")
        bot.answer_callback_query(call.id, "Error: Invalid action data.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in file_control_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "An error occurred.", show_alert=True)

def start_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return

        # Check approval status
        approval_status = get_file_approval_status(script_owner_id, file_name)
        if approval_status == 'pending' and requesting_user_id not in admin_ids:
            bot.answer_callback_query(call.id, "⏳ Script admin approval ka wait kar rahi hai.", show_alert=True)
            return
        if approval_status != 'approved' and requesting_user_id not in admin_ids:
            bot.answer_callback_query(call.id, "⚠️ Script approved nahi hai. Admin se contact karo.", show_alert=True)
            return

        # Check one-script-per-user
        if requesting_user_id not in admin_ids and has_any_running_script(script_owner_id):
            running_scripts = [fn for fn, _ in user_files.get(script_owner_id, []) if is_bot_running(script_owner_id, fn)]
            bot.answer_callback_query(call.id,
                f"⚠️ Aapki ek script pehle se chal rahi hai: {', '.join(running_scripts)}\nPehle use stop karo.",
                show_alert=True)
            return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return

        file_type = file_info[1]
        user_folder = get_user_folder(script_owner_id)
        file_path = os.path.join(user_folder, file_name)

        if not os.path.exists(file_path):
            bot.answer_callback_query(call.id, f"⚠️ File `{file_name}` missing! Re-upload.", show_alert=True)
            remove_user_file_db(script_owner_id, file_name); return

        if is_bot_running(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"⚠️ Already running.", show_alert=True)
            return

        bot.answer_callback_query(call.id, f"⏳ Starting {file_name}...")
        if file_type == 'py':
            threading.Thread(target=run_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        else:
            bot.send_message(chat_id_for_reply, f"❌ Unknown type '{file_type}'."); return

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_name)
        status_text = '🟢 Running' if is_now_running else '🟡 Starting...'
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\nStatus: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_now_running), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e): raise
    except (ValueError, IndexError) as e:
        bot.answer_callback_query(call.id, "Error: Invalid start command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in start_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error starting script.", show_alert=True)

def stop_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return

        file_type = file_info[1]
        script_key = f"{script_owner_id}_{file_name}"

        if not is_bot_running(script_owner_id, file_name):
            bot.answer_callback_query(call.id, f"⚠️ Already stopped.", show_alert=True)
            return

        bot.answer_callback_query(call.id, f"⏳ Stopping {file_name}...")
        process_info = bot_scripts.get(script_key)
        if process_info: kill_process_tree(process_info)
        if script_key in bot_scripts: del bot_scripts[script_key]

        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\nStatus: 🔴 Stopped",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, False), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e): raise
    except (ValueError, IndexError) as e:
        bot.answer_callback_query(call.id, "Error: Invalid stop command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in stop_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error stopping script.", show_alert=True)

def restart_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        file_info = next((f for f in user_files_list if f[0] == file_name), None)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return

        file_type = file_info[1]; user_folder = get_user_folder(script_owner_id)
        file_path = os.path.join(user_folder, file_name); script_key = f"{script_owner_id}_{file_name}"

        if not os.path.exists(file_path):
            bot.answer_callback_query(call.id, f"⚠️ File missing! Re-upload.", show_alert=True)
            remove_user_file_db(script_owner_id, file_name)
            if script_key in bot_scripts: del bot_scripts[script_key]
            return

        bot.answer_callback_query(call.id, f"⏳ Restarting {file_name}...")
        if is_bot_running(script_owner_id, file_name):
            process_info = bot_scripts.get(script_key)
            if process_info: kill_process_tree(process_info)
            if script_key in bot_scripts: del bot_scripts[script_key]
            time.sleep(1.5)

        if file_type == 'py':
            threading.Thread(target=run_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script, args=(file_path, script_owner_id, user_folder, file_name, call.message)).start()
        else:
            bot.send_message(chat_id_for_reply, f"❌ Unknown type '{file_type}'."); return

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_name)
        status_text = '🟢 Running' if is_now_running else '🟡 Starting...'
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\nStatus: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_name, is_now_running), parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e): raise
    except (ValueError, IndexError) as e:
        bot.answer_callback_query(call.id, "Error: Invalid restart command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in restart_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error restarting.", show_alert=True)

def delete_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return

        user_files_list = user_files.get(script_owner_id, [])
        if not any(f[0] == file_name for f in user_files_list):
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return

        bot.answer_callback_query(call.id, f"🗑️ Deleting {file_name}...")
        script_key = f"{script_owner_id}_{file_name}"
        if is_bot_running(script_owner_id, file_name):
            process_info = bot_scripts.get(script_key)
            if process_info: kill_process_tree(process_info)
            if script_key in bot_scripts: del bot_scripts[script_key]
            time.sleep(0.5)

        user_folder = get_user_folder(script_owner_id)
        file_path = os.path.join(user_folder, file_name)
        log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        deleted_disk = []
        if os.path.exists(file_path):
            try: os.remove(file_path); deleted_disk.append(file_name)
            except OSError as e: logger.error(f"Error deleting {file_path}: {e}")
        if os.path.exists(log_path):
            try: os.remove(log_path); deleted_disk.append(os.path.basename(log_path))
            except OSError as e: logger.error(f"Error deleting log {log_path}: {e}")

        remove_user_file_db(script_owner_id, file_name)
        remove_file_approval_db(script_owner_id, file_name)  # Also clear approval record
        deleted_str = ", ".join(f"`{f}`" for f in deleted_disk) if deleted_disk else "associated files"
        try:
            bot.edit_message_text(
                f"🗑️ File `{file_name}` (User `{script_owner_id}`) deleted!\n{deleted_str}",
                chat_id_for_reply, call.message.message_id, reply_markup=None, parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error editing msg after delete: {e}")
            bot.send_message(chat_id_for_reply, f"🗑️ File `{file_name}` deleted.", parse_mode='Markdown')
    except (ValueError, IndexError) as e:
        bot.answer_callback_query(call.id, "Error: Invalid delete command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in delete_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error deleting.", show_alert=True)

def logs_bot_callback(call):
    try:
        _, script_owner_id_str, file_name = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return

        user_folder = get_user_folder(script_owner_id)
        log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, f"⚠️ No logs yet for '{file_name}'.", show_alert=True); return

        bot.answer_callback_query(call.id)
        try:
            log_content = ""
            file_size = os.path.getsize(log_path)
            max_log_kb = 100; max_tg_msg = 4096
            if file_size == 0: log_content = "(Log empty)"
            elif file_size > max_log_kb * 1024:
                with open(log_path, 'rb') as f: f.seek(-max_log_kb * 1024, os.SEEK_END); log_bytes = f.read()
                log_content = f"(Last {max_log_kb} KB)\n...\n" + log_bytes.decode('utf-8', errors='ignore')
            else:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: log_content = f.read()
            if len(log_content) > max_tg_msg:
                log_content = log_content[-max_tg_msg:]
                first_nl = log_content.find('\n')
                if first_nl != -1: log_content = "...\n" + log_content[first_nl+1:]
            if not log_content.strip(): log_content = "(No visible content)"
            bot.send_message(chat_id_for_reply,
                             f"📜 Logs for `{file_name}` (User `{script_owner_id}`):\n```\n{log_content}\n```",
                             parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error reading log {log_path}: {e}", exc_info=True)
            bot.send_message(chat_id_for_reply, f"❌ Error reading log.")
    except (ValueError, IndexError) as e:
        bot.answer_callback_query(call.id, "Error: Invalid logs command.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in logs_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error fetching logs.", show_alert=True)

def speed_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    start_cb_ping_time = time.time()
    try:
        bot.edit_message_text("🏃 Testing speed...", chat_id, call.message.message_id)
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start_cb_ping_time) * 1000, 2)
        status = "🔓 Unlocked" if not bot_locked else "🔒 Locked"
        if user_id == OWNER_ID: user_level = "👑 Owner"
        elif user_id in admin_ids: user_level = "🛡️ Admin"
        elif user_id in user_subscriptions and user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now(): user_level = "⭐ Premium"
        else: user_level = "🆓 Free User"
        speed_msg = (f"⚡ Bot Speed & Status:\n\n⏱️ Response: {response_time} ms\n"
                     f"🚦 Status: {status}\n👤 Level: {user_level}")
        bot.answer_callback_query(call.id)
        bot.edit_message_text(speed_msg, chat_id, call.message.message_id, reply_markup=create_main_menu_inline(user_id))
    except Exception as e:
        logger.error(f"Error during speed test (cb): {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error in speed test.", show_alert=True)

def back_to_main_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""
    if user_id == OWNER_ID: user_status = "👑 Owner"
    elif user_id in admin_ids: user_status = "🛡️ Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            user_status = "⭐ Premium"; days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\n⏳ Sub expires in: {days_left} days"
        else: user_status = "🆓 Free User"
    else: user_status = "🆓 Free User"
    main_menu_text = (f"〽️ Welcome back, {call.from_user.first_name}!\n\n🆔 ID: `{user_id}`\n"
                      f"🔰 Status: {user_status}{expiry_info}\n📁 Files: {current_files} / {limit_str}\n\n"
                      f"👇 Use buttons or type commands.")
    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(main_menu_text, chat_id, call.message.message_id,
                              reply_markup=create_main_menu_inline(user_id), parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e): logger.error(f"API error on back_to_main: {e}")
    except Exception as e: logger.error(f"Error handling back_to_main: {e}", exc_info=True)

# --- Admin Callback Implementations ---
def subscription_management_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("💳 Subscription Management\nSelect action:",
                              call.message.chat.id, call.message.message_id, reply_markup=create_subscription_menu())
    except Exception as e: logger.error(f"Error showing sub menu: {e}")

def stats_callback(call):
    bot.answer_callback_query(call.id)
    _logic_statistics(call.message)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception: pass

def lock_bot_callback(call):
    global bot_locked; bot_locked = True
    bot.answer_callback_query(call.id, "🔒 Bot locked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception: pass

def unlock_bot_callback(call):
    global bot_locked; bot_locked = False
    bot.answer_callback_query(call.id, "🔓 Bot unlocked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception: pass

def run_all_scripts_callback(call):
    _logic_run_all_scripts(call)

def broadcast_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "📢 Send message to broadcast.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)

def process_broadcast_message(message):
    user_id = message.from_user.id
    if user_id not in admin_ids: bot.reply_to(message, "⚠️ Not authorized."); return
    if message.text and message.text.lower() == '/cancel': bot.reply_to(message, "Broadcast cancelled."); return
    broadcast_content = message.text
    if not broadcast_content:
        bot.reply_to(message, "⚠️ Send text or /cancel.")
        msg = bot.send_message(message.chat.id, "📢 Send broadcast message or /cancel.")
        bot.register_next_step_handler(msg, process_broadcast_message)
        return
    target_count = len(active_users)
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✅ Confirm & Send", callback_data=f"confirm_broadcast_{message.message_id}"),
               types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast"))
    preview_text = broadcast_content[:1000].strip()
    bot.reply_to(message, f"⚠️ Confirm Broadcast:\n\n```\n{preview_text}\n```\nTo **{target_count}** users. Sure?",
                 reply_markup=markup, parse_mode='Markdown')

def handle_confirm_broadcast(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    if user_id not in admin_ids: bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    try:
        original_message = call.message.reply_to_message
        if not original_message: raise ValueError("Could not retrieve original message.")
        broadcast_text = original_message.text if original_message.text else None
        broadcast_photo_id = original_message.photo[-1].file_id if original_message.photo else None
        broadcast_video_id = original_message.video.file_id if original_message.video else None
        bot.answer_callback_query(call.id, "🚀 Starting broadcast...")
        bot.edit_message_text(f"📢 Broadcasting to {len(active_users)} users...", chat_id, call.message.message_id)
        threading.Thread(target=execute_broadcast, args=(broadcast_text, broadcast_photo_id, broadcast_video_id,
                         original_message.caption if (broadcast_photo_id or broadcast_video_id) else None, chat_id)).start()
    except Exception as e:
        logger.error(f"Error in broadcast confirm: {e}", exc_info=True)
        bot.edit_message_text("❌ Error starting broadcast.", chat_id, call.message.message_id)

def handle_cancel_broadcast(call):
    bot.answer_callback_query(call.id, "Broadcast cancelled.")
    bot.delete_message(call.message.chat.id, call.message.message_id)

def execute_broadcast(broadcast_text, photo_id, video_id, caption, admin_chat_id):
    sent_count = 0; failed_count = 0; blocked_count = 0
    users_to_broadcast = list(active_users)
    for i, user_id_bc in enumerate(users_to_broadcast):
        try:
            if broadcast_text: bot.send_message(user_id_bc, broadcast_text, parse_mode='Markdown')
            elif photo_id: bot.send_photo(user_id_bc, photo_id, caption=caption)
            elif video_id: bot.send_video(user_id_bc, video_id, caption=caption)
            sent_count += 1
        except telebot.apihelper.ApiTelegramException as e:
            err_desc = str(e).lower()
            if any(s in err_desc for s in ["bot was blocked", "user is deactivated", "chat not found"]):
                blocked_count += 1
            elif "flood control" in err_desc or "too many requests" in err_desc:
                retry_after = 5
                match = re.search(r"retry after (\d+)", err_desc)
                if match: retry_after = int(match.group(1)) + 1
                time.sleep(retry_after)
                try:
                    if broadcast_text: bot.send_message(user_id_bc, broadcast_text, parse_mode='Markdown')
                    sent_count += 1
                except Exception: failed_count += 1
            else: failed_count += 1
        except Exception: failed_count += 1
        if (i + 1) % 25 == 0: time.sleep(1.5)
        elif i % 5 == 0: time.sleep(0.2)
    result_msg = (f"📢 Broadcast Complete!\n\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}\n"
                  f"🚫 Blocked: {blocked_count}\n👥 Targets: {len(users_to_broadcast)}")
    try: bot.send_message(admin_chat_id, result_msg)
    except Exception: pass

def admin_panel_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("👑 Admin Panel", call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel())
    except Exception as e: logger.error(f"Error showing admin panel: {e}")

def add_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "👑 Enter User ID to promote to Admin.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_admin_id)

def process_add_admin_id(message):
    if message.from_user.id != OWNER_ID: bot.reply_to(message, "⚠️ Owner only."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Admin promotion cancelled."); return
    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id == OWNER_ID: bot.reply_to(message, "⚠️ Owner is already Owner."); return
        if new_admin_id in admin_ids: bot.reply_to(message, f"⚠️ User `{new_admin_id}` already Admin."); return
        add_admin_db(new_admin_id)
        bot.reply_to(message, f"✅ User `{new_admin_id}` promoted to Admin.", parse_mode='Markdown')
        try: bot.send_message(new_admin_id, "🎉 Congrats! You are now an Admin.")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "👑 Enter User ID or /cancel.")
        bot.register_next_step_handler(msg, process_add_admin_id)
    except Exception as e: logger.error(f"Error adding admin: {e}"); bot.reply_to(message, "Error.")

def remove_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "👑 Enter User ID of Admin to remove.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_admin_id)

def process_remove_admin_id(message):
    if message.from_user.id != OWNER_ID: bot.reply_to(message, "⚠️ Owner only."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Admin removal cancelled."); return
    try:
        admin_id_remove = int(message.text.strip())
        if admin_id_remove == OWNER_ID: bot.reply_to(message, "⚠️ Owner cannot remove self."); return
        if admin_id_remove not in admin_ids: bot.reply_to(message, f"⚠️ User `{admin_id_remove}` not Admin."); return
        if remove_admin_db(admin_id_remove):
            bot.reply_to(message, f"✅ Admin `{admin_id_remove}` removed.", parse_mode='Markdown')
            try: bot.send_message(admin_id_remove, "ℹ️ You are no longer an Admin.")
            except Exception: pass
        else: bot.reply_to(message, f"❌ Failed to remove admin `{admin_id_remove}`.")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "👑 Enter Admin ID to remove or /cancel.")
        bot.register_next_step_handler(msg, process_remove_admin_id)
    except Exception as e: logger.error(f"Error removing admin: {e}"); bot.reply_to(message, "Error.")

def list_admins_callback(call):
    bot.answer_callback_query(call.id)
    try:
        admin_list_str = "\n".join(f"- `{aid}` {'(Owner)' if aid == OWNER_ID else ''}" for aid in sorted(list(admin_ids)))
        if not admin_list_str: admin_list_str = "(None)"
        bot.edit_message_text(f"👑 Current Admins:\n\n{admin_list_str}", call.message.chat.id,
                              call.message.message_id, reply_markup=create_admin_panel(), parse_mode='Markdown')
    except Exception as e: logger.error(f"Error listing admins: {e}")

# --- Force Join Channel Admin Callbacks ---
def add_force_channel_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        "📢 *Force Join Channel Add Karo*\n\n"
        "Channel username ya ID bhejo (example: `@MyChannel` ya `-100123456789`)\n"
        "Uske baad channel ka naam bhejo.\n\n"
        "Format: `@username Channel Name`\n"
        "/cancel — wapas jaane ke liye",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, process_add_force_channel)

def process_add_force_channel(message):
    if message.from_user.id not in admin_ids: bot.reply_to(message, "⚠️ Admin only."); return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Cancel ho gaya."); return
    try:
        parts = message.text.strip().split(None, 1)
        if len(parts) < 2:
            bot.reply_to(message,
                "⚠️ Format galat hai!\nSahi format: `@username Channel Name`\nDobara try karo ya /cancel.",
                parse_mode='Markdown')
            msg = bot.send_message(message.chat.id, "📢 Channel add karo (format: `@username Name`):")
            bot.register_next_step_handler(msg, process_add_force_channel)
            return
        channel_id = parts[0].strip()
        channel_name = parts[1].strip()
        # Verify channel exists and bot is admin
        try:
            chat_info = bot.get_chat(channel_id)
            channel_name = channel_name or chat_info.title or channel_id
        except Exception:
            bot.reply_to(message,
                f"⚠️ Channel `{channel_id}` nahi mila. Bot ko channel admin banana padega!",
                parse_mode='Markdown')
            return
        add_force_channel_db(channel_id, channel_name)
        bot.reply_to(message,
            f"✅ *Force Join Channel Add Ho Gaya!*\n\n"
            f"📢 Channel: {channel_name}\n"
            f"🆔 ID: `{channel_id}`\n\n"
            f"Ab users ko join karna hoga is channel ko bot use karne ke liye.",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error adding force channel: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error: {e}")

def remove_force_channel_callback(call):
    bot.answer_callback_query(call.id)
    if not force_join_channels:
        bot.send_message(call.message.chat.id, "⚠️ Koi force-join channel set nahi hai.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel_id, channel_name in force_join_channels.items():
        markup.add(types.InlineKeyboardButton(
            f"🗑️ {channel_name} ({channel_id})",
            callback_data=f"del_fchan_{channel_id}"
        ))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='admin_panel'))
    bot.send_message(call.message.chat.id,
        "🗑️ *Kaunsa channel remove karna hai?*",
        parse_mode='Markdown', reply_markup=markup)

def list_force_channels_callback(call):
    bot.answer_callback_query(call.id)
    if not force_join_channels:
        channels_text = "(Koi force-join channel set nahi hai)"
    else:
        channels_text = "\n".join(
            f"• {name} (`{cid}`)"
            for cid, name in force_join_channels.items()
        )
    try:
        bot.edit_message_text(
            f"📢 *Force Join Channels:*\n\n{channels_text}",
            call.message.chat.id, call.message.message_id,
            reply_markup=create_admin_panel(), parse_mode='Markdown'
        )
    except Exception: pass

def pending_approvals_list_callback(call):
    """Show admin a list of all pending approvals."""
    bot.answer_callback_query(call.id)
    all_pending = []
    for user_id, files_list in pending_approvals.items():
        for file_name, file_type in files_list:
            all_pending.append((user_id, file_name, file_type))
    if not all_pending:
        try:
            bot.edit_message_text(
                "✅ *Koi pending approval nahi hai.*",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_admin_panel(), parse_mode='Markdown'
            )
        except Exception: pass
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    msg_parts = ["⏳ *Pending Approvals:*\n"]
    for user_id, file_name, file_type in all_pending[:10]:  # Show max 10
        msg_parts.append(f"👤 User `{user_id}` → `{file_name}` ({file_type})")
        markup.row(
            types.InlineKeyboardButton(f"✅ {file_name}", callback_data=f'approve_{user_id}_{file_name}'),
            types.InlineKeyboardButton(f"❌ Reject", callback_data=f'reject_{user_id}_{file_name}')
        )
    if len(all_pending) > 10:
        msg_parts.append(f"\n...aur {len(all_pending) - 10} aur hain.")
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='admin_panel'))
    try:
        bot.edit_message_text(
            "\n".join(msg_parts),
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error showing pending list: {e}")

# Handle del_fchan_ callbacks
@bot.callback_query_handler(func=lambda call: call.data.startswith('del_fchan_'))
def delete_force_channel_callback(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True)
        return
    channel_id = call.data[len('del_fchan_'):]
    channel_name = force_join_channels.get(channel_id, channel_id)
    remove_force_channel_db(channel_id)
    bot.answer_callback_query(call.id, f"✅ Channel {channel_name} removed!", show_alert=True)
    try:
        bot.edit_message_text(
            f"✅ Force join channel `{channel_name}` remove kar diya gaya.",
            call.message.chat.id, call.message.message_id,
            reply_markup=create_admin_panel(), parse_mode='Markdown'
        )
    except Exception: pass

# --- Subscription Callbacks ---
def add_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID & days (e.g., `12345678 30`).\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_subscription_details)

def process_add_subscription_details(message):
    if message.from_user.id not in admin_ids: bot.reply_to(message, "⚠️ Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        parts = message.text.split()
        if len(parts) != 2: raise ValueError("Incorrect format")
        sub_user_id = int(parts[0].strip()); days = int(parts[1].strip())
        if sub_user_id <= 0 or days <= 0: raise ValueError("Must be positive")
        current_expiry = user_subscriptions.get(sub_user_id, {}).get('expiry')
        start_date = datetime.now()
        if current_expiry and current_expiry > start_date: start_date = current_expiry
        new_expiry = start_date + timedelta(days=days)
        save_subscription(sub_user_id, new_expiry)
        bot.reply_to(message, f"✅ Sub for `{sub_user_id}` by {days} days. Expiry: {new_expiry:%Y-%m-%d}", parse_mode='Markdown')
        try: bot.send_message(sub_user_id, f"🎉 Sub activated! Expires: {new_expiry:%Y-%m-%d}.")
        except Exception: pass
    except ValueError as e:
        bot.reply_to(message, f"⚠️ Invalid: {e}. Format: `ID days`")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID & days, or /cancel.")
        bot.register_next_step_handler(msg, process_add_subscription_details)
    except Exception as e: logger.error(f"Error adding sub: {e}"); bot.reply_to(message, "Error.")

def remove_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to remove sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_subscription_id)

def process_remove_subscription_id(message):
    if message.from_user.id not in admin_ids: bot.reply_to(message, "⚠️ Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        sub_user_id = int(message.text.strip())
        if sub_user_id not in user_subscriptions:
            bot.reply_to(message, f"⚠️ User `{sub_user_id}` has no sub.", parse_mode='Markdown'); return
        remove_subscription_db(sub_user_id)
        bot.reply_to(message, f"✅ Sub for `{sub_user_id}` removed.", parse_mode='Markdown')
        try: bot.send_message(sub_user_id, "ℹ️ Your subscription was removed.")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID or /cancel.")
        bot.register_next_step_handler(msg, process_remove_subscription_id)
    except Exception as e: logger.error(f"Error removing sub: {e}"); bot.reply_to(message, "Error.")

def check_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to check sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_check_subscription_id)

def process_check_subscription_id(message):
    if message.from_user.id not in admin_ids: bot.reply_to(message, "⚠️ Not authorized."); return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        sub_user_id = int(message.text.strip())
        if sub_user_id in user_subscriptions:
            expiry_dt = user_subscriptions[sub_user_id].get('expiry')
            if expiry_dt and expiry_dt > datetime.now():
                days_left = (expiry_dt - datetime.now()).days
                bot.reply_to(message, f"✅ User `{sub_user_id}` has active sub.\nExpires: {expiry_dt:%Y-%m-%d} ({days_left} days left).", parse_mode='Markdown')
            else:
                bot.reply_to(message, f"⚠️ User `{sub_user_id}` sub expired.", parse_mode='Markdown')
                remove_subscription_db(sub_user_id)
        else:
            bot.reply_to(message, f"ℹ️ User `{sub_user_id}` has no sub.", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID or /cancel.")
        bot.register_next_step_handler(msg, process_check_subscription_id)
    except Exception as e: logger.error(f"Error checking sub: {e}"); bot.reply_to(message, "Error.")

# --- Cleanup ---
def cleanup():
    logger.warning("Shutdown. Cleaning up processes...")
    for key in list(bot_scripts.keys()):
        if key in bot_scripts: kill_process_tree(bot_scripts[key])
    logger.warning("Cleanup finished.")
atexit.register(cleanup)

# --- Main Execution ---
if __name__ == '__main__':
    logger.info("="*40 + "\n🤖 Bot Starting Up...\n" +
                f"🐍 Python: {sys.version.split()[0]}\n"
                f"🔧 Base Dir: {BASE_DIR}\n"
                f"📁 Upload Dir: {UPLOAD_BOTS_DIR}\n"
                f"📊 Data Dir: {IROTECH_DIR}\n"
                f"📢 Force Join Channels: {len(force_join_channels)}\n"
                f"🛡️ Admins: {admin_ids}\n" + "="*40)
    keep_alive()
    logger.info("🚀 Starting polling...")
    while True:
        try:
            bot.infinity_polling(logger_level=logging.INFO, timeout=60, long_polling_timeout=30)
        except requests.exceptions.ReadTimeout:
            logger.warning("Polling ReadTimeout. Restarting in 5s...")
            time.sleep(5)
        except requests.exceptions.ConnectionError as ce:
            logger.error(f"Polling ConnectionError: {ce}. Retrying in 15s...")
            time.sleep(15)
        except Exception as e:
            logger.critical(f"💥 Polling error: {e}", exc_info=True)
            logger.info("Restarting polling in 30s...")
            time.sleep(30)
