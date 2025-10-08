#!/usr/bin/env python3
"""
Flashcard Study Bot - clean reimplementation

Features:
 - /start, /help, /cancel, /add, /delete, /list
 - Reply keyboard for quick actions
 - Add term with "Term | Definition" (supports |, :, -)
 - Search by typing keywords (searches term name and definition)
 - Case-insensitive term keys, preserves original formatting
 - Atomic save/load of JSON file
 - Simple concurrency protection for file operations
 - Robust MarkdownV2 escaping for replies
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- Configuration ----------
DATA_DIR = Path("data")
TERMS_FILE = DATA_DIR / "terms_base.json"
LOG_FILE = "flashcard_bot.log"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # must be set in environment

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------- Data structures ----------
@dataclass
class Term:
    original_term: str
    definition: str
    added: str  # ISO timestamp

# Terms map normalized_name -> Term
TermsMap = Dict[str, Term]

# ---------- In-memory state ----------
_user_states: Dict[int, Optional[str]] = {}  # user_id -> state string or None
_file_lock = asyncio.Lock()  # protects load/save

# ---------- Helpers ----------
def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def normalize_name(name: str) -> str:
    return name.lower().strip()

def escape_markdown_v2(text: str) -> str:
    """
    Escapes text for MarkdownV2. Keep this self-contained to avoid version issues.
    """
    if text is None:
        return ""
    # According to Telegram MarkdownV2, the following characters must be escaped:
    special = r"_*[]()~`>#+-=|{}.!\\"
    # Escape backslash first
    text = text.replace("\\", "\\\\")
    out = []
    for ch in text:
        if ch in special:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)

def parse_term_input(text: str) -> Optional[Tuple[str, str]]:
    """
    Parse "Term Separator Definition" allowing separators | : -
    Returns (term, definition) or None if cannot parse.
    """
    if not text or not text.strip():
        return None
    separators = ["|", ":", "-"]
    for sep in separators:
        if sep in text:
            left, right = text.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return None

async def load_terms() -> TermsMap:
    """Load terms from JSON file (returns empty dict if missing)."""
    _ensure_data_dir()
    async with _file_lock:
        if not TERMS_FILE.exists():
            return {}
        try:
            # Use blocking file I/O inside executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, TERMS_FILE.read_text, "utf-8")
            data = json.loads(raw)
            terms: TermsMap = {}
            for key, obj in data.items():
                # Validation/fallbacks
                if not isinstance(obj, dict):
                    continue
                original = obj.get("original_term") or key
                definition = obj.get("definition", "")
                added = obj.get("added", "")
                terms[key] = Term(original_term=original, definition=definition, added=added)
            return terms
        except Exception as e:
            logger.exception("Failed to load terms file")
            return {}

async def save_terms(terms: TermsMap) -> None:
    """Atomically save terms to JSON file."""
    _ensure_data_dir()
    async with _file_lock:
        try:
            serializable = {k: asdict(v) for k, v in terms.items()}
            tmpfd, tmppath = tempfile.mkstemp(prefix="terms_", suffix=".json", dir=str(DATA_DIR))
            os.close(tmpfd)
            loop = asyncio.get_running_loop()
            # write via executor
            def _write():
                with open(tmppath, "w", encoding="utf-8") as f:
                    json.dump(serializable, f, ensure_ascii=False, indent=2)
                # atomic replace
                os.replace(tmppath, TERMS_FILE)
            await loop.run_in_executor(None, _write)
        except Exception:
            logger.exception("Failed to save terms")

def get_main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🔍 Search Term"), KeyboardButton("📚 List All Terms")],
        [KeyboardButton("➕ Add Term"), KeyboardButton("🗑️ Delete Term")],
        [KeyboardButton("ℹ️ Help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def search_terms(query: str, terms: TermsMap, limit: int = 5) -> List[Tuple[str, Term, float]]:
    """
    Return up to `limit` results: (term_norm, Term, score)
    Strategy:
     - exact normalized match -> score 1.0
     - substring in term or definition -> score 0.8
     - difflib close matches -> score 0.6
    """
    query_norm = normalize_name(query)
    if not query_norm:
        return []

    results: List[Tuple[str, Term, float]] = []
    seen = set()

    # Exact match
    if query_norm in terms:
        results.append((query_norm, terms[query_norm], 1.0))
        seen.add(query_norm)

    # Substring matches
    for name_norm, term in terms.items():
        if name_norm in seen:
            continue
        if query_norm in name_norm or query_norm in normalize_name(term.definition):
            results.append((name_norm, term, 0.8))
            seen.add(name_norm)

    # fuzzy matches on names
    all_names = list(terms.keys())
    matches = get_close_matches(query_norm, all_names, n=limit, cutoff=0.6)
    for m in matches:
        if m not in seen:
            results.append((m, terms[m], 0.6))
            seen.add(m)

    results.sort(key=lambda x: x[2], reverse=True)
    return results[:limit]

# ---------- Bot Handlers ----------
async def _send_text_safe(update: Update, text: str, reply_markup=None):
    """
    Send text with MarkdownV2 safely. Log and avoid crashing if Telegram rejects formatting.
    """
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    except TelegramError:
        # Fallback: send plain text without MarkdownV2
        try:
            await update.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send message to user")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _user_states.pop(user_id, None)
    msg = (
        "🧠 *Flashcard Study Bot*\n\n"
        "Welcome! I help you create and memorize terms and definitions.\n\n"
        "🎯 *Quick Start:*\n"
        "• Tap *'➕ Add Term'* to create a new flashcard.\n"
        "• Tap *'🔍 Search Term'* or just type a keyword to find a term or definition.\n\n"
        "👇 Use the menu below or start typing to search!"
    )
    await _send_text_safe(update, msg, reply_markup=get_main_menu())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _user_states.pop(user_id, None)
    msg = (
        "📖 *How to Use Flashcard Study Bot*\n\n"
        "*➕ Adding Terms:*\n"
        "1. Tap *'➕ Add Term'*\n"
        "2. Send the term and definition in one line using one of these separators: `|`, `:` or `-`\n\n"
        "Example:\n"
        "`Python : An interpreted, high-level programming language.`\n\n"
        "*🔍 Searching:*\n"
        "• Type any part of the term name or definition to search.\n\n"
        "*📚 Listing:*\n"
        "• Tap *'📚 List All Terms'* to see your saved flashcards.\n\n"
        "*🗑️ Deleting:*\n"
        "• Tap *'🗑️ Delete Term'* then type the exact term name to remove it.\n\n"
        "*🛑 Cancel:*\n"
        "• Use `/cancel` to stop a pending operation."
    )
    await _send_text_safe(update, msg, reply_markup=get_main_menu())

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prev = _user_states.pop(user_id, None)
    if prev:
        msg = "🛑 *Canceled.* The previous operation has been stopped."
    else:
        msg = "✨ Nothing to cancel. No pending operation."
    await _send_text_safe(update, msg, reply_markup=get_main_menu())

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _user_states[user_id] = "awaiting_term"
    msg = (
        "📝 *Add a New Term*\n\n"
        "Send the term and definition on one line using `|`, `:` or `-`.\n"
        "Example:\n"
        "`API : Application Programming Interface`\n\n"
        "Send your term now:"
    )
    await _send_text_safe(update, msg)

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    _user_states[user_id] = "awaiting_delete_term"
    msg = (
        "🗑️ *Delete a Term*\n\n"
        "Type the *exact name* of the term you want to delete.\n\n"
        "Example: `API`"
    )
    await _send_text_safe(update, msg)

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    terms = await load_terms()
    if not terms:
        msg = "📭 *No Saved Terms*\n\nYour flashcard deck is empty. Tap '➕ Add Term' to get started."
        await _send_text_safe(update, msg, reply_markup=get_main_menu())
        return

    items = sorted(terms.items(), key=lambda it: it[0])
    msg_lines = [f"📚 *All Saved Terms* ({len(items)} total)\n"]
    for _, term in items:
        definition = term.definition
        # Truncate for display
        display_def = (definition[:200] + "...") if len(definition) > 200 else definition
        msg_lines.append(f"*{escape_markdown_v2(term.original_term)}*\n   ➡️ {escape_markdown_v2(display_def)}\n")
    msg = "\n".join(msg_lines)
    await _send_text_safe(update, msg, reply_markup=get_main_menu())

async def _perform_search_and_reply(update: Update, query: str):
    terms = await load_terms()
    results = search_terms(query, terms)
    if not results:
        msg = (
            f"❌ *No Terms Found*\n\n"
            f"No matches for: *{escape_markdown_v2(query)}*\n\n"
            "💡 Try different keywords related to the term or definition."
        )
        await _send_text_safe(update, msg, reply_markup=get_main_menu())
        return

    msg_lines = [f"🔍 *Search Results for '{escape_markdown_v2(query)}'*\n"]
    for i, (name_norm, term, score) in enumerate(results, 1):
        display_def = term.definition
        if len(display_def) > 200:
            display_def = display_def[:200] + "..."
        msg_lines.append(
            f"*{i}. {escape_markdown_v2(term.original_term)}*\n   ➡️ {escape_markdown_v2(display_def)}\n"
        )
    await _send_text_safe(update, "\n".join(msg_lines), reply_markup=get_main_menu())

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles free text from users and keyboard button presses.
    Controls a simple per-user state machine:
      - None: default (search mode)
      - "awaiting_term": next message should be "Term | Definition"
      - "awaiting_delete_term": next message should be exact term name to delete
    """
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Short-circuit commands typed as text
    if text.startswith("/"):
        return

    # Menu buttons
    if text == "🔍 Search Term":
        _user_states[user_id] = None
        await _send_text_safe(update, "🔍 Just type the term or keyword you're looking for!\nExample: `API`", reply_markup=get_main_menu())
        return
    if text == "📚 List All Terms":
        _user_states[user_id] = None
        await list_command(update, context)
        return
    if text == "➕ Add Term":
        _user_states[user_id] = None
        await add_command(update, context)
        return
    if text == "🗑️ Delete Term":
        _user_states[user_id] = None
        await delete_command(update, context)
        return
    if text == "ℹ️ Help":
        _user_states[user_id] = None
        await help_command(update, context)
        return

    # State-based handling
    state = _user_states.get(user_id)

    try:
        if state == "awaiting_term":
            parsed = parse_term_input(text)
            if not parsed:
                await _send_text_safe(
                    update,
                    "❌ *Couldn't parse your input.*\n\nPlease use the format `Term | Definition` (supporting `|`, `:` or `-`)."
                )
                return
            term_name, definition = parsed
            terms = await load_terms()
            key = normalize_name(term_name)
            if key in terms:
                # If exists, overwrite (user must resend to replace)
                # We'll explicitly inform them and overwrite if they send again immediately with same input.
                # To keep UX simple: overwrite without extra prompts.
                logger.info("Overwriting existing term: %s", term_name)

            terms[key] = Term(original_term=term_name, definition=definition, added=datetime.utcnow().isoformat())
            await save_terms(terms)
            _user_states[user_id] = None
            msg = (
                "✅ *Term Added Successfully!*\n\n"
                f"*{escape_markdown_v2(term_name)}*\n   ➡️ {escape_markdown_v2(definition if len(definition) < 500 else definition[:500] + '...')}"
            )
            await _send_text_safe(update, msg, reply_markup=get_main_menu())
            return

        if state == "awaiting_delete_term":
            name = text
            key = normalize_name(name)
            terms = await load_terms()
            if key in terms:
                original = terms[key].original_term
                del terms[key]
                await save_terms(terms)
                _user_states[user_id] = None
                msg = f"✅ *Term Deleted!*\n\n🗑️ Removed '*{escape_markdown_v2(original)}*' from your flashcards."
                await _send_text_safe(update, msg, reply_markup=get_main_menu())
            else:
                msg = f"❌ *Term Not Found*\n\n'*{escape_markdown_v2(name)}*' doesn't exist in your flashcards."
                await _send_text_safe(update, msg, reply_markup=get_main_menu())
            return

        # Default: treat the text as a search query
        await _perform_search_and_reply(update, text)

    except Exception:
        # In case something odd happens, clear state to avoid leaving user stuck
        _user_states.pop(user_id, None)
        logger.exception("Unhandled error in message_handler")
        await _send_text_safe(update, "❌ An internal error occurred. Your current operation was canceled.", reply_markup=get_main_menu())

# ---------- Entry point ----------
def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)

    logger.info("Starting Flashcard Study Bot (clean version)...")
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("list", list_command))

    # Messages (private chats only)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, message_handler))

    # Run
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("Shutting down (keyboard interrupt)")
    except Exception:
        logger.exception("Bot crashed unexpectedly")
        raise

if __name__ == "__main__":
    main()
