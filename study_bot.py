#!/usr/bin/env python3
"""
Flashcard Multi-Channel Bot
- Per-channel and global knowledge bases (JSON)
- Safe MarkdownV2 escaping (preserves code blocks / inline code)
- Add / delete / list / search / stats / channels
- Auto-ingest channel posts with "Term - Definition" (or :, =)
- Robust file I/O with atomic replace and simple repairs for corrupted JSON
"""

from __future__ import annotations
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
    sys.exit(1)

DATA_DIR = Path("knowledge_data")
GLOBAL_FILE = DATA_DIR / "global.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ====== GLOBAL STATE ======
_user_states: Dict[int, Optional[str]] = {}  # user_id -> None | "awaiting_term" | "awaiting_delete"
_file_lock = Lock()  # simple process-level lock for file I/O

# ====== UTILITIES ======
MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"

def escape_markdown_v2(text: str, preserve_code: bool = True) -> str:
    """
    Escape text for Telegram MarkdownV2.
    If preserve_code is True, code blocks (```...```) and inline code (`...`) remain unescaped inside backticks.
    """
    if text is None:
        return ""
    # Quick path for empty
    if text == "":
        return ""

    if not preserve_code:
        # Escape everything
        return "".join("\\" + ch if ch in MDV2_SPECIAL else ch for ch in text)

    # We will replace code blocks and inline code with placeholders, escape the rest, then restore
    code_block_pattern = re.compile(r"```[\s\S]*?```")
    inline_code_pattern = re.compile(r"`[^`\n]+`")

    code_blocks = []
    def repl_block(m):
        code_blocks.append(m.group(0))
        return f"__CODEBLOCK_{len(code_blocks)-1}__"

    text_tmp = code_block_pattern.sub(repl_block, text)

    inline_codes = []
    def repl_inline(m):
        inline_codes.append(m.group(0))
        return f"__INLINE_{len(inline_codes)-1}__"

    text_tmp = inline_code_pattern.sub(repl_inline, text_tmp)

    # Escape remaining
    escaped = []
    for ch in text_tmp:
        if ch in MDV2_SPECIAL:
            escaped.append("\\" + ch)
        else:
            escaped.append(ch)
    escaped_text = "".join(escaped)

    # restore inline codes (keep backticks, do NOT escape inside)
    for i, code in enumerate(inline_codes):
        escaped_text = escaped_text.replace(f"__INLINE_{i}__", code)

    # restore code blocks
    for i, block in enumerate(code_blocks):
        escaped_text = escaped_text.replace(f"__CODEBLOCK_{i}__", block)

    return escaped_text

def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically using a temp file and os.replace."""
    with _file_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="tmp_kb_", dir=str(path.parent))
        os.close(tmp_fd)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            logger.exception(f"Failed atomic write to {path}")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def load_json_file(path: Path) -> Dict:
    """Load JSON safely; repair simple corruption (non-dict root or non-dict entries)."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning(f"{path} root is not a dict, resetting to empty dict.")
            return {}
        # repair entries that are not dicts
        repaired = False
        for k, v in list(data.items()):
            if not isinstance(v, dict):
                data[k] = {"definition": str(v)}
                repaired = True
        if repaired:
            # try to persist repair
            try:
                atomic_write_json(path, data)
            except Exception:
                logger.exception("Failed to persist repair")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in {path}: {e}. Renaming corrupted file and returning empty dict.")
        # move corrupted file aside
        try:
            corrupt_name = path.with_suffix(path.suffix + ".corrupt")
            os.replace(str(path), str(corrupt_name))
            logger.info(f"Moved corrupted file to {corrupt_name}")
        except Exception:
            logger.exception("Failed to move corrupted file")
        return {}
    except Exception:
        logger.exception(f"Unexpected error loading {path}")
        return {}

def get_channel_file(channel_id: Optional[int]) -> Path:
    """Return Path for a channel-specific knowledge file, or global file if channel_id is None"""
    if channel_id is None or channel_id == 0:
        return GLOBAL_FILE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"knowledge_{abs(channel_id)}.json"

def normalize_term(term: str) -> str:
    return term.lower().strip()

def parse_term_definition(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse common separators for term and definition.
    Supports: " - ", " : ", " = ", single "-" ":" "=" as fallback.
    Returns (term, definition) or (None, None) if parsing failed (empty or no separator).
    """
    if not text or not text.strip():
        return None, None
    # Prefer separators with spaces to avoid splitting hyphenated words, but fall back
    separators = [" - ", " : ", " = ", " – ", " — ", ":", "-", "="]
    for sep in separators:
        if sep in text:
            left, right = text.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return None, None

# ====== KNOWLEDGE HELPERS ======
def load_knowledge(channel_id: Optional[int] = None) -> Dict:
    path = get_channel_file(channel_id)
    return load_json_file(path)

def save_knowledge(data: Dict, channel_id: Optional[int] = None) -> None:
    path = get_channel_file(channel_id)
    atomic_write_json(path, data)

def get_all_channel_files() -> List[Tuple[int, Path]]:
    """Return list of (channel_id, path) for each channel knowledge file present."""
    files = []
    for f in DATA_DIR.glob("knowledge_*.json"):
        try:
            # filename pattern knowledge_<id>.json
            m = re.match(r"knowledge_(-?\d+)\.json", f.name)
            if m:
                cid = int(m.group(1))
                files.append((cid, f))
        except Exception:
            continue
    return files

# ====== SEARCH ======
def search_in_knowledge(query: str, kb: Dict) -> List[Tuple[str, Dict, float]]:
    """
    Search in a single KB dict (normalized keys -> data dict).
    Returns list of tuples (term_norm, data, score)
    """
    if not query:
        return []
    q = normalize_term(query)
    results = []
    seen = set()

    # exact match
    if q in kb:
        results.append((q, kb[q], 1.0))
        seen.add(q)

    # substring matches in keys or definition
    for term_norm, data in kb.items():
        if term_norm in seen:
            continue
        # ensure data is dict
        if not isinstance(data, dict):
            continue
        definition = normalize_term(data.get("definition", "") or "")
        if q in term_norm or q in definition:
            results.append((term_norm, data, 0.8))
            seen.add(term_norm)

    # fuzzy on term names
    names = list(kb.keys())
    close = get_close_matches(q, names, n=5, cutoff=0.6)
    for c in close:
        if c not in seen:
            results.append((c, kb[c], 0.6))
            seen.add(c)

    results.sort(key=lambda x: x[2], reverse=True)
    return results

def search_all(query: str, include_global: bool = True, channel_scan: bool = True, max_results: int = 10) -> List[Tuple[str, Dict, float, Optional[int]]]:
    """
    Search across global KB and all channel KBs.
    Returns list of (term_norm, data, score, channel_id) where channel_id=None for global.
    Deduplicates terms preferring higher score.
    """
    combined = []
    # global
    if include_global:
        kb = load_knowledge(None)
        for t, d, s in search_in_knowledge(query, kb):
            combined.append((t, d, s, None))
    # channels
    if channel_scan:
        for cid, _path in get_all_channel_files():
            kb = load_knowledge(cid)
            for t, d, s in search_in_knowledge(query, kb):
                combined.append((t, d, s, cid))

    # deduplicate by term_norm keeping highest score; if same score prefer channel-specific (cid not None)
    best = {}
    for term, data, score, cid in combined:
        key = term
        old = best.get(key)
        if not old or score > old[0] or (score == old[0] and (cid is not None and old[1] is None)):
            best[key] = (score, cid, data)

    results = []
    for term, (score, cid, data) in best.items():
        results.append((term, data, score, cid))

    results.sort(key=lambda x: x[2], reverse=True)
    return results[:max_results]

# ====== BOT HELPERS (menus, messages) ======
def main_menu_markup():
    keyboard = [
        [KeyboardButton("🔍 Search Term"), KeyboardButton("📚 List All Terms")],
        [KeyboardButton("➕ Add Term"), KeyboardButton("🗑️ Delete Term")],
        [KeyboardButton("ℹ️ Help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def send_start(update: Update):
    user_id = update.effective_user.id
    _user_states.pop(user_id, None)
    msg = (
        "🧠 *Flashcard Study Bot*\n\n"
        "I store terms per-channel and globally. Use the menu or commands.\n\n"
        "Commands: /addterm, /delete, /listterms, /search, /channels, /stats, /help, /cancel"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

async def send_help(update: Update):
    user_id = update.effective_user.id
    _user_states.pop(user_id, None)
    msg = (
        "📖 *How to use*\n\n"
        "*Adding*: /addterm <term> - <definition>\n"
        "Or tap '➕ Add Term' and send `Term - Definition`.\n\n"
        "*Searching*: type text or use /search <term>\n"
        "*Listing*: /listterms (shows global + channel terms)\n"
        "*Deleting*: /delete <term> or tap '🗑️ Delete Term' and send term name\n\n"
        "Formatting: MarkdownV2 is supported. Code blocks (```...```) and `inline code` are preserved.\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

# ====== COMMAND HANDLERS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_start(update)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_help(update)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prev = _user_states.pop(user_id, None)
    if prev:
        await update.message.reply_text("🛑 Operation canceled.", reply_markup=main_menu_markup())
    else:
        await update.message.reply_text("✨ Nothing to cancel.", reply_markup=main_menu_markup())

async def cmd_addterm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addterm [term - definition]  (if no args: prompt user to send the line)
    If used in a channel post handler, we will add to the channel KB automatically elsewhere.
    """
    user_id = update.effective_user.id
    args = context.args
    if args:
        # join args and parse
        text = " ".join(args)
        term, definition = parse_term_definition(text)
        if not term or not definition:
            await update.message.reply_text("Usage: /addterm Term - Definition (or use the Add Term button).")
            return
        # save into global KB by default for command usage in private chat
        kb = load_knowledge(None)
        key = normalize_term(term)
        kb[key] = {"original_term": term, "definition": definition, "added": datetime.utcnow().isoformat(), "source": "manual"}
        save_knowledge(kb, None)
        await update.message.reply_text(
            f"✅ Added *{escape_markdown_v2(term)}* to global KB.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu_markup()
        )
    else:
        # prompt user to send the Term - Definition
        _user_states[user_id] = "awaiting_term"
        await update.message.reply_text(
            "📝 Send the term and definition in one message using `Term - Definition` (or `Term: Definition`).",
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /delete <term> will delete the term from global KB and all channel KBs.
    If no args: prompt user.
    """
    user_id = update.effective_user.id
    args = context.args
    if args:
        term = " ".join(args).strip()
        if not term:
            await update.message.reply_text("Usage: /delete <term>")
            return
        norm = normalize_term(term)
        deleted_from = []
        # global
        kb = load_knowledge(None)
        if norm in kb:
            del kb[norm]
            save_knowledge(kb, None)
            deleted_from.append("Global")
        # channels
        for cid, _p in get_all_channel_files():
            ck = load_knowledge(cid)
            if norm in ck:
                del ck[norm]
                save_knowledge(ck, cid)
                deleted_from.append(f"Channel {cid}")
        if deleted_from:
            await update.message.reply_text(
                f"✅ Deleted *{escape_markdown_v2(term)}* from:\n" + "\n".join(f"• {escape_markdown_v2(x)}" for x in deleted_from),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=main_menu_markup()
            )
        else:
            await update.message.reply_text("❌ Term not found.")
    else:
        _user_states[user_id] = "awaiting_delete"
        await update.message.reply_text("🗑️ Send the exact term name to delete (global and all channels).")

async def cmd_listterms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    List terms from global and channels. For private chats, show combined summary.
    """
    # gather terms
    all_terms: Dict[str, List[str]] = {}  # original_term -> list of sources
    global_kb = load_knowledge(None)
    for k, v in global_kb.items():
        orig = v.get("original_term", k)
        all_terms.setdefault(orig, []).append("Global")
    for cid, p in get_all_channel_files():
        ck = load_knowledge(cid)
        for k, v in ck.items():
            orig = v.get("original_term", k)
            all_terms.setdefault(orig, []).append(f"Channel {cid}")

    if not all_terms:
        await update.message.reply_text("📭 No saved terms yet.", reply_markup=main_menu_markup())
        return

    # Build output but limit lines to avoid giant messages
    items = sorted(all_terms.items(), key=lambda x: x[0].lower())
    lines = []
    for term, sources in items:
        src = ", ".join(sources)
        lines.append(f"• {escape_markdown_v2(term)} — _{escape_markdown_v2(src)}_")
        if len(lines) >= 100:
            lines.append(f"_... and {len(items) - 100} more ..._")
            break

    await update.message.reply_text("📚 *All Terms*\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = get_all_channel_files()
    if not files:
        await update.message.reply_text("No active channel KBs found.", reply_markup=main_menu_markup())
        return
    lines = []
    for cid, path in files:
        kb = load_knowledge(cid)
        name = kb[next(iter(kb))].get("channel", f"Channel {cid}") if kb else f"Channel {cid}"
        lines.append(f"• {escape_markdown_v2(name)} — `{cid}` — {len(kb)} terms")
    await update.message.reply_text("📺 *Active Channels*\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_terms = 0
    total_definitions = 0
    global_kb = load_knowledge(None)
    total_terms += len(global_kb)
    for v in global_kb.values():
        if isinstance(v, dict) and "definition" in v:
            total_definitions += 1
    for cid, _ in get_all_channel_files():
        kb = load_knowledge(cid)
        total_terms += len(kb)
        for v in kb.values():
            if isinstance(v, dict) and "definition" in v:
                total_definitions += 1
    msg = f"📊 *Statistics*\n\nTotal terms: {total_terms}\nTotal definitions: {total_definitions}\nChannels: {len(get_all_channel_files())}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

# ====== SEARCH HANDLER ======
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    query = " ".join(args).strip() if args else ""
    if not query:
        # If user typed just "🔍 Search Term" or similar we'll prompt
        await update.message.reply_text("Type a word or phrase to search (or use /search <term>).")
        return
    results = search_all(query, include_global=True, channel_scan=True, max_results=10)
    if not results:
        await update.message.reply_text(f"❌ No results for *{escape_markdown_v2(query)}*.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    lines = [f"🔍 *Search results for* _{escape_markdown_v2(query)}_\n"]
    for i, (term_norm, data, score, cid) in enumerate(results, 1):
        orig = data.get("original_term", term_norm)
        definition = data.get("definition", "")
        source = "Global" if cid is None else f"Channel {cid}"
        lines.append(f"*{i}. {escape_markdown_v2(orig)}*  _({escape_markdown_v2(source)})_")
        # truncate definition to reasonable length
        display_def = (definition[:300] + "...") if len(definition) > 300 else definition
        lines.append(f"   {escape_markdown_v2(display_def)}\n")
        if len(lines) >= 40:
            break
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

# ====== CHANNEL POST HANDLER ======
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Auto-detect Term - Definition in channel posts and add to that channel's KB.
    Uses parse_term_definition on channel_post.text.
    """
    msg = update.channel_post
    if not msg or not msg.text:
        return
    cid = msg.chat.id
    text = msg.text.strip()
    term, definition = parse_term_definition(text)
    if not term or not definition:
        # Not a term-definition formatted post; ignore
        return
    kb = load_knowledge(cid)
    key = normalize_term(term)
    timestamp = str(msg.date) if hasattr(msg, "date") else datetime.utcnow().isoformat()
    # keep original formatting inside definition
    kb[key] = {
        "original_term": term,
        "definition": definition,
        "added": timestamp,
        "channel": msg.chat.title if msg.chat and msg.chat.title else f"Channel {cid}",
        "source": "channel"
    }
    save_knowledge(kb, cid)
    logger.info(f"[Channel {cid}] Added term: {term}")

# ====== PRIVATE MESSAGE / MENU HANDLER ======
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles private chat messages and menu button presses.
    State machine for awaiting_term and awaiting_delete.
    """
    user_id = update.effective_user.id
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # Menu shortcuts handling (strings from keyboard)
    if text == "🔍 Search Term":
        await update.message.reply_text("🔍 Type your search phrase or use /search <term>.", reply_markup=main_menu_markup())
        return
    if text == "📚 List All Terms":
        await cmd_listterms(update, context)
        return
    if text == "➕ Add Term":
        _user_states[user_id] = "awaiting_term"
        await update.message.reply_text("📝 Send the term and definition as `Term - Definition`.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    if text == "🗑️ Delete Term":
        _user_states[user_id] = "awaiting_delete"
        await update.message.reply_text("🗑️ Send the exact term name to delete (global + all channels).")
        return
    if text == "ℹ️ Help":
        await send_help(update)
        return

    # If user in awaiting_term state
    state = _user_states.get(user_id)
    if state == "awaiting_term":
        term, definition = parse_term_definition(text)
        if not term or not definition:
            await update.message.reply_text("Couldn't parse. Send in format `Term - Definition`.")
            return
        kb = load_knowledge(None)
        key = normalize_term(term)
        kb[key] = {
            "original_term": term,
            "definition": definition,
            "added": datetime.utcnow().isoformat(),
            "source": "manual"
        }
        save_knowledge(kb, None)
        _user_states[user_id] = None
        await update.message.reply_text(f"✅ Added *{escape_markdown_v2(term)}* to Global.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())
        return

    if state == "awaiting_delete":
        term = text
        norm = normalize_term(term)
        deleted = []
        g = load_knowledge(None)
        if norm in g:
            del g[norm]
            save_knowledge(g, None)
            deleted.append("Global")
        for cid, _ in get_all_channel_files():
            ck = load_knowledge(cid)
            if norm in ck:
                del ck[norm]
                save_knowledge(ck, cid)
                deleted.append(f"Channel {cid}")
        _user_states[user_id] = None
        if deleted:
            await update.message.reply_text("✅ Deleted from:\n" + "\n".join(f"• {x}" for x in deleted), reply_markup=main_menu_markup())
        else:
            await update.message.reply_text("❌ Term not found.", reply_markup=main_menu_markup())
        return

    # If not special state: treat as search
    # This is friendly: typing any word searches across KBs
    results = search_all(text, include_global=True, channel_scan=True, max_results=8)
    if not results:
        await update.message.reply_text(f"❌ No results for *{escape_markdown_v2(text)}*.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())
        return
    lines = [f"🔍 *Search results for* _{escape_markdown_v2(text)}_\n"]
    for i, (term_norm, data, score, cid) in enumerate(results, 1):
        orig = data.get("original_term", term_norm)
        definition = data.get("definition", "")
        source = "Global" if cid is None else f"Channel {cid}"
        lines.append(f"*{i}. {escape_markdown_v2(orig)}*  _({escape_markdown_v2(source)})_")
        lines.append(f"   {escape_markdown_v2(definition[:300] + ('...' if len(definition) > 300 else ''))}\n")
        if len(lines) >= 40:
            break
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_markup())

# ====== ERROR HANDLER ======
async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update caused error", exc_info=context.error)

# ====== MAIN ======
def main():
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("addterm", cmd_addterm))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("listterms", cmd_listterms))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("search", cmd_search))

    # Channel posts (auto-ingest)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, handle_channel_post))

    # Private messages / menu buttons / typed queries
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_private_message))

    app.add_error_handler(on_error)

    logger.info("Starting multi-channel flashcard bot...")
    # allow channel_post events as well as message
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "channel_post"])

if __name__ == "__main__":
    main()
