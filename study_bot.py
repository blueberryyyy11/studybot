import json
import logging
import os
import re
from pathlib import Path
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from telegram.constants import ParseMode
import signal
import sys
from typing import Dict, List
from difflib import get_close_matches

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("Please set TELEGRAM_BOT_TOKEN environment variable in Railway dashboard")

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("study_bot.log", encoding="utf-8")
    ]
)

logger = logging.getLogger(__name__)

# ====== GLOBAL VARIABLES ======
app = None
user_states = {}  # Track user states for multi-step operations

# ====== HELPER FUNCTION FOR MARKDOWN ESCAPING ======
def escape_markdown(text: str, preserve_code: bool = False) -> str:
    """
    Escape special characters for MarkdownV2
    If preserve_code=True, preserves code blocks and inline code
    """
    if not text:
        return text
    
    if not preserve_code:
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text
    
    # Preserve code blocks and inline code by replacing them with placeholders
    code_blocks = []
    inline_codes = []
    
    # Extract code blocks (```code```)
    code_block_pattern = r'```[\s\S]*?```'
    for match in re.finditer(code_block_pattern, text):
        placeholder = f"__CODEBLOCK{len(code_blocks)}__"
        code_blocks.append(match.group())
        text = text.replace(match.group(), placeholder, 1)
    
    # Extract inline code (`code`)
    inline_code_pattern = r'`[^`\n]+`'
    for match in re.finditer(inline_code_pattern, text):
        placeholder = f"__INLINECODE{len(inline_codes)}__"
        inline_codes.append(match.group())
        text = text.replace(match.group(), placeholder, 1)
    
    # Escape special characters in remaining text
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    
    # Restore code blocks (keep backticks unescaped)
    for i, code_block in enumerate(code_blocks):
        text = text.replace(f"__CODEBLOCK{i}__", code_block)
    
    # Restore inline codes (keep backticks unescaped)
    for i, inline_code in enumerate(inline_codes):
        text = text.replace(f"__INLINECODE{i}__", inline_code)
    
    return text

# ====== DATA HELPERS ======
def get_knowledge_file(channel_id: int) -> str:
    """Get knowledge base file for specific channel"""
    knowledge_dir = Path("knowledge_bases")
    knowledge_dir.mkdir(exist_ok=True)
    return str(knowledge_dir / f"knowledge_{abs(channel_id)}.json")

def load_knowledge(channel_id: int = None) -> Dict:
    """Load knowledge base for specific channel"""
    try:
        if channel_id is None:
            filename = "knowledge_base.json"
        else:
            filename = get_knowledge_file(channel_id)
        
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Error loading knowledge base for channel {channel_id}: {e}")
        return {}

def save_knowledge(data: Dict, channel_id: int = None):
    """Save knowledge base for specific channel"""
    try:
        if channel_id is None:
            filename = "knowledge_base.json"
        else:
            filename = get_knowledge_file(channel_id)
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving knowledge base for channel {channel_id}: {e}")

def normalize_term(term: str) -> str:
    """Normalize term for case-insensitive matching"""
    return term.lower().strip()

def extract_definition(text: str, entities: list = None) -> tuple:
    """Extract term and definition from various formats, preserving formatting"""
    # Store original text with entities for formatting preservation
    original_text = text
    
    # Remove markdown formatting for parsing only
    text_clean = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text_clean = re.sub(r'__(.+?)__', r'\1', text_clean)
    
    separators = [' - ', ': ', ' = ', ' – ', ' — ']
    
    for sep in separators:
        if sep in text_clean:
            parts = text_clean.split(sep, 1)
            if len(parts) == 2:
                term = parts[0].strip()
                
                # Find the definition part in original text
                sep_index = original_text.find(sep)
                if sep_index != -1:
                    definition = original_text[sep_index + len(sep):].strip()
                else:
                    definition = parts[1].strip()
                
                if term and definition:
                    return term, definition, entities
    
    return None, None, None

def search_knowledge(query: str, knowledge: Dict) -> List[tuple]:
    """Search for terms matching the query"""
    query_norm = normalize_term(query)
    results = []
    seen_terms = set()
    
    if query_norm in knowledge:
        results.append((query_norm, knowledge[query_norm], 1.0))
        seen_terms.add(query_norm)
    
    for term, data in knowledge.items():
        if term in seen_terms:
            continue
        if query_norm in term or term in query_norm:
            score = 0.8
            results.append((term, data, score))
            seen_terms.add(term)
    
    all_terms = list(knowledge.keys())
    close_matches = get_close_matches(query_norm, all_terms, n=3, cutoff=0.6)
    for match in close_matches:
        if match not in seen_terms:
            results.append((match, knowledge[match], 0.6))
            seen_terms.add(match)
    
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:5]

def get_all_channels() -> List[tuple]:
    """Get list of all channels with knowledge bases"""
    channels = []
    knowledge_dir = Path("knowledge_bases")
    
    if knowledge_dir.exists():
        for kb_file in knowledge_dir.glob("knowledge_*.json"):
            try:
                channel_id = int(kb_file.stem.split("_")[1])
                knowledge = load_knowledge(channel_id)
                if knowledge:
                    first_term = next(iter(knowledge.values()), {})
                    channel_name = first_term.get("channel", f"Channel {channel_id}")
                    term_count = len(knowledge)
                    channels.append((channel_id, channel_name, term_count))
            except Exception as e:
                logger.error(f"Error processing {kb_file}: {e}")
    
    return sorted(channels, key=lambda x: x[2], reverse=True)

# ====== MENU HELPER ======
def get_main_menu():
    """Create the main menu keyboard"""
    keyboard = [
        [KeyboardButton("🔍 Search"), KeyboardButton("📚 List All")],
        [KeyboardButton("📺 Channels"), KeyboardButton("📊 Statistics")],
        [KeyboardButton("➕ Add Term"), KeyboardButton("🗑️ Delete Term")],
        [KeyboardButton("ℹ️ Help")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_inline_search_results(results: list, query: str) -> InlineKeyboardMarkup:
    """Create inline keyboard for search results"""
    keyboard = []
    for i, (term, data, score, channel_name, channel_id) in enumerate(results[:5]):
        original = data.get("original_term", term)
        callback_data = f"view_{term}_{channel_id if channel_id else 0}"
        keyboard.append([InlineKeyboardButton(f"📖 {original}", callback_data=callback_data)])
    return InlineKeyboardMarkup(keyboard)

# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    msg = (
        "📚 *Multi\\-Channel Study Bot*\n\n"
        "Welcome\\! I help you learn and organize terms and definitions from multiple channels\\.\n\n"
        "🎯 *Quick Start:*\n"
        "• Just type any term to search for it\\!\n"
        "• Use menu buttons for easy navigation\n"
        "• Add terms directly with simple format\n\n"
        "📺 *In Channels:*\n"
        "Post messages in any of these formats:\n"
        "• `Term \\- Definition`\n"
        "• `Term: Definition`\n"
        "• `Term \\= Definition`\n\n"
        "✨ *Formatting preserved\\!* Your text formatting \\(bold, italic, code\\) will be kept\\.\n\n"
        "👇 Use the menu below or just start typing\\!"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command with detailed instructions"""
    msg = (
        "📖 *How to Use This Bot*\n\n"
        "*🔍 Searching:*\n"
        "• Just type any term to search\n"
        "• No commands needed\\!\n"
        "• Results show with clickable buttons\n\n"
        "*➕ Adding Terms:*\n"
        "1\\. Click 'Add Term' button\n"
        "2\\. Send term in format: `Term \\- Definition`\n"
        "3\\. Use *bold*, _italic_, `code` \\- formatting is preserved\\!\n\n"
        "*📚 Viewing Terms:*\n"
        "• 'List All' \\- see all saved terms\n"
        "• 'Channels' \\- view active channels\n"
        "• 'Statistics' \\- detailed stats\n\n"
        "*🗑️ Deleting:*\n"
        "1\\. Click 'Delete Term'\n"
        "2\\. Type the term name\n\n"
        "*📺 Channel Learning:*\n"
        "Add me to channels and I'll learn from posts automatically\\!\n"
        "Supported formats:\n"
        "• `Term \\- Definition`\n"
        "• `Term: Definition`\n"
        "• `Term \\= Definition`\n\n"
        "💡 *Pro Tip:* All your text formatting \\(bold, italic, code blocks\\) is preserved\\!"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def add_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate term adding process"""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_term"
    
    msg = (
        "📝 *Add a New Term*\n\n"
        "Send me the term and definition in this format:\n"
        "`Term \\- Definition`\n\n"
        "✨ You can use:\n"
        "• *Bold text*\n"
        "• _Italic text_\n"
        "• `Code formatting`\n"
        "• ```Code blocks```\n\n"
        "*Example:*\n"
        "`Algorithm \\- A *step\\-by\\-step* procedure for solving a problem`\n\n"
        "Send your term now:"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def delete_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate term deletion process"""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_delete"
    
    msg = (
        "🗑️ *Delete a Term*\n\n"
        "Type the name of the term you want to delete\\.\n\n"
        "*Example:* `Algorithm`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def search_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for a term across all channels"""
    try:
        query = update.message.text.strip()
        
        all_results = []
        knowledge_dir = Path("knowledge_bases")
        
        default_knowledge = load_knowledge()
        if default_knowledge:
            default_results = search_knowledge(query, default_knowledge)
            for term, data, score in default_results:
                all_results.append((term, data, score, "Manual", 0))
        
        if knowledge_dir.exists():
            for kb_file in knowledge_dir.glob("knowledge_*.json"):
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    if knowledge:
                        channel_results = search_knowledge(query, knowledge)
                        channel_name = None
                        if knowledge:
                            first_term = next(iter(knowledge.values()), {})
                            channel_name = first_term.get("channel", f"Channel {channel_id}")
                        
                        for term, data, score in channel_results:
                            all_results.append((term, data, score, channel_name or f"Channel {channel_id}", channel_id))
                except Exception as e:
                    logger.error(f"Error searching {kb_file}: {e}")
        
        if not all_results:
            msg = (
                f"❌ *No Results Found*\n\n"
                f"No matches for: *{escape_markdown(query)}*\n\n"
                f"💡 Try:\n"
                f"• Different keywords\n"
                f"• Checking spelling\n"
                f"• Adding the term using 'Add Term' button"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        all_results.sort(key=lambda x: x[2], reverse=True)
        
        seen_terms = set()
        unique_results = []
        for result in all_results:
            term = result[0]
            if term not in seen_terms:
                unique_results.append(result)
                seen_terms.add(term)
                if len(unique_results) >= 5:
                    break
        
        msg = f"🔍 *Search Results for '{escape_markdown(query)}'*\n\n"
        msg += f"Found {len(unique_results)} result{'s' if len(unique_results) != 1 else ''}:\n\n"
        
        for i, (term, data, score, channel_name, channel_id) in enumerate(unique_results, 1):
            original = data.get("original_term", term)
            
            msg += f"*{i}\\. {escape_markdown(original)}*"
            if channel_name != "Manual":
                msg += f" 📺 _{escape_markdown(channel_name)}_"
            msg += "\n"
            
            if "definitions" in data:
                definitions = data["definitions"]
                for j, def_item in enumerate(definitions, 1):
                    def_text = def_item.get("text", def_item) if isinstance(def_item, dict) else def_item
                    # Preserve original formatting
                    if len(definitions) > 1:
                        msg += f"   {j}\\. {def_text}\n"
                    else:
                        msg += f"   {def_text}\n"
            else:
                definition = data.get("definition", "No definition")
                msg += f"   {definition}\n"
            
            msg += "\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in search_term: {e}")
        await update.message.reply_text("❌ Error searching\\. Try again\\.", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def list_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all terms from all channels"""
    try:
        all_terms = {}
        
        default_knowledge = load_knowledge()
        for term, data in default_knowledge.items():
            original = data.get("original_term", term)
            all_terms[original] = "Manual"
        
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            for kb_file in knowledge_dir.glob("knowledge_*.json"):
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    channel_name = f"Channel {channel_id}"
                    if knowledge:
                        first_term = next(iter(knowledge.values()), {})
                        channel_name = first_term.get("channel", channel_name)
                    
                    for term, data in knowledge.items():
                        original = data.get("original_term", term)
                        if original not in all_terms:
                            all_terms[original] = channel_name
                except Exception as e:
                    logger.error(f"Error loading {kb_file}: {e}")
        
        if not all_terms:
            msg = (
                "📭 *Knowledge Base is Empty*\n\n"
                "No terms found yet\\!\n\n"
                "💡 Get started by:\n"
                "• Clicking 'Add Term' to add manually\n"
                "• Adding me to a channel as admin"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        sorted_terms = sorted(all_terms.items())
        
        # FIX: Changed f-string to concatenation to avoid Python parser bug with backslash and parenthesis.
        msg = "📚 *All Terms* \\(" + str(len(sorted_terms)) + " total\\)\n\n"
        
        # Group by source for better organization
        manual_terms = [(t, s) for t, s in sorted_terms if s == "Manual"]
        channel_terms = [(t, s) for t, s in sorted_terms if s != "Manual"]
        
        if manual_terms:
            msg += "*Manual Terms:*\n"
            for term, _ in manual_terms[:20]:
                msg += f"• {escape_markdown(term)}\n"
            if len(manual_terms) > 20:
                msg += f"_\\.\\.\\.and {len(manual_terms) - 20} more_\n"
            msg += "\n"
        
        if channel_terms:
            # Group by channel
            from itertools import groupby
            channel_terms_sorted = sorted(channel_terms, key=lambda x: x[1])
            
            for channel, terms in groupby(channel_terms_sorted, key=lambda x: x[1]):
                terms_list = list(terms)
                msg += f"*{escape_markdown(channel)}:*\n"
                for term, _ in terms_list[:15]:
                    msg += f"• {escape_markdown(term)}\n"
                if len(terms_list) > 15:
                    msg += f"_\\.\\.\\.and {len(terms_list) - 15} more_\n"
                msg += "\n"
        
        msg += "💡 Type any term name to search for it\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in list_terms: {e}")
        await update.message.reply_text("❌ Error listing terms", reply_markup=get_main_menu())

async def show_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all channels the bot is learning from"""
    try:
        channels = get_all_channels()
        
        if not channels:
            msg = (
                "📭 *No Active Channels*\n\n"
                "Add me to a channel to start learning\\!\n\n"
                "📌 *How to add me:*\n"
                "1\\. Go to your channel\n"
                "2\\. Channel Settings → Administrators\n"
                "3\\. Add this bot as admin\n"
                "4\\. Post terms: `Term \\- Definition`\n\n"
                "✨ I'll learn automatically\\!"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # FIX: Changed f-string to concatenation to avoid Python parser bug with backslash and parenthesis.
        msg = "📺 *Active Channels* \\(" + str(len(channels)) + "\\)\n\n"
        
        for i, (channel_id, channel_name, term_count) in enumerate(channels, 1):
            msg += f"*{i}\\. {escape_markdown(channel_name)}*\n"
            msg += f"   📚 {term_count} term{'s' if term_count != 1 else ''}\n"
            msg += f"   🆔 `{channel_id}`\n\n"
        
        msg += "💡 Add me to more channels to expand the knowledge base\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in show_channels: {e}")
        await update.message.reply_text("❌ Error showing channels", reply_markup=get_main_menu())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overall statistics"""
    try:
        total_terms = 0
        total_definitions = 0
        total_channels = 0
        
        default_knowledge = load_knowledge()
        if default_knowledge:
            total_terms += len(default_knowledge)
            total_definitions += sum(
                len(data.get("definitions", [data.get("definition", "")]))
                for data in default_knowledge.values()
            )
        
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            channel_files = list(knowledge_dir.glob("knowledge_*.json"))
            total_channels = len(channel_files)
            
            for kb_file in channel_files:
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    total_terms += len(knowledge)
                    total_definitions += sum(
                        len(data.get("definitions", [data.get("definition", "")]))
                        for data in knowledge.values()
                    )
                except Exception as e:
                    logger.error(f"Error processing {kb_file}: {e}")
        
        msg = "📊 *Knowledge Base Statistics*\n\n"
        msg += f"📺 Active Channels: *{total_channels}*\n"
        msg += f"📚 Total Terms: *{total_terms}*\n"
        msg += f"📝 Total Definitions: *{total_definitions}*\n"
        
        if total_terms > 0:
            avg = total_definitions / total_terms
            msg += f"📈 Average: *{avg:.1f}* def/term\n"
        
        msg += f"\n💡 Keep learning\\! Your knowledge base is growing\\."
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ Error getting statistics", reply_markup=get_main_menu())

async def handle_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically extract terms from channel messages - preserves formatting"""
    try:
        if not update.channel_post:
            return
        
        channel_id = update.channel_post.chat.id
        channel_name = update.channel_post.chat.title or update.channel_post.chat.username or f"Channel {channel_id}"
        
        text = update.channel_post.text or update.channel_post.caption
        entities = update.channel_post.entities or update.channel_post.caption_entities
        
        if not text:
            return
        
        term, definition, entities = extract_definition(text, entities)
        
        if term and definition:
            knowledge = load_knowledge(channel_id)
            term_norm = normalize_term(term)
            
            if term_norm in knowledge:
                if "definitions" not in knowledge[term_norm]:
                    old_def = knowledge[term_norm].get("definition", "")
                    knowledge[term_norm]["definitions"] = [{"text": old_def, "added": knowledge[term_norm].get("added", "")}]
                
                knowledge[term_norm]["definitions"].append({
                    "text": definition,  # Preserves original formatting
                    "added": str(update.channel_post.date),
                    "channel": channel_name
                })
                logger.info(f"[{channel_name}] Added definition #{len(knowledge[term_norm]['definitions'])} for term: {term}")
            else:
                knowledge[term_norm] = {
                    "original_term": term,
                    "definitions": [{"text": definition, "added": str(update.channel_post.date), "channel": channel_name}],
                    "added": str(update.channel_post.date),
                    "channel": channel_name,
                    "related": []
                }
                logger.info(f"[{channel_name}] Auto-learned new term: {term}")
            
            save_knowledge(knowledge, channel_id)
        
    except Exception as e:
        logger.error(f"Error handling channel message: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct messages based on user state"""
    try:
        if not update.message or not update.message.text:
            return
        
        if update.message.text.startswith('/'):
            return
        
        user_id = update.effective_user.id
        text = update.message.text.strip()
        
        # Check user state
        user_state = user_states.get(user_id)
        
        # Handle menu button clicks
        if text == "🔍 Search":
            msg = "🔍 Just type the term you're looking for\\!\n\nI'll search across all channels\\."
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        elif text == "📚 List All":
            await list_terms(update, context)
            return
        
        elif text == "📺 Channels":
            await show_channels(update, context)
            return
        
        elif text == "📊 Statistics":
            await stats(update, context)
            return
        
        elif text == "➕ Add Term":
            await add_term(update, context)
            return
        
        elif text == "🗑️ Delete Term":
            await delete_term(update, context)
            return
        
        elif text == "ℹ️ Help":
            await help_command(update, context)
            return
        
        # Handle state-based inputs
        if user_state == "awaiting_term":
            # User is adding a term
            term, definition, entities = extract_definition(text, update.message.entities)
            
            if not term or not definition:
                msg = (
                    "❌ I couldn't parse that\\.\n\n"
                    "Please use format: `Term \\- Definition`\n\n"
                    "Or click 'Back' to cancel\\."
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            knowledge = load_knowledge()
            term_norm = normalize_term(term)
            
            if term_norm in knowledge:
                if "definitions" not in knowledge[term_norm]:
                    old_def = knowledge[term_norm].get("definition", "")
                    knowledge[term_norm]["definitions"] = [{"text": old_def, "added": knowledge[term_norm].get("added", "")}]
                
                knowledge[term_norm]["definitions"].append({
                    "text": definition,
                    "added": str(update.message.date),
                    "source": "manual"
                })
                msg = f"✅ *Added Another Definition\\!*\n\n"
                msg += f"📚 *{escape_markdown(term)}*\n"
                msg += f"📊 Now has {len(knowledge[term_norm]['definitions'])} definitions"
            else:
                knowledge[term_norm] = {
                    "original_term": term,
                    "definitions": [{"text": definition, "added": str(update.message.date), "source": "manual"}],
                    "added": str(update.message.date),
                    "related": []
                }
                msg = f"✅ *Term Added Successfully\\!*\n\n"
                msg += f"📚 *{escape_markdown(term)}*\n"
                msg += f"{definition}"
            
            save_knowledge(knowledge)
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"Manual add - term: {term}")
            return
        
        elif user_state == "awaiting_delete":
            # User is deleting a term
            term = text
            term_norm = normalize_term(term)
            deleted_from = []
            
            default_knowledge = load_knowledge()
            if term_norm in default_knowledge:
                original = default_knowledge[term_norm].get("original_term", term)
                del default_knowledge[term_norm]
                save_knowledge(default_knowledge)
                deleted_from.append("Manual")
            
            knowledge_dir = Path("knowledge_bases")
            if knowledge_dir.exists():
                for kb_file in knowledge_dir.glob("knowledge_*.json"):
                    try:
                        channel_id = int(kb_file.stem.split("_")[1])
                        knowledge = load_knowledge(channel_id)
                        
                        if term_norm in knowledge:
                            original = knowledge[term_norm].get("original_term", term)
                            channel_name = knowledge[term_norm].get("channel", f"Channel {channel_id}")
                            del knowledge[term_norm]
                            save_knowledge(knowledge, channel_id)
                            deleted_from.append(channel_name)
                    except Exception as e:
                        logger.error(f"Error processing {kb_file}: {e}")
            
            if deleted_from:
                msg = f"✅ *Term Deleted\\!*\n\n"
                msg += f"🗑️ Removed '*{escape_markdown(term)}*' from:\n"
                msg += "\n".join(f"   • {escape_markdown(source)}" for source in deleted_from)
                logger.info(f"Deleted term: {term} from {', '.join(deleted_from)}")
            else:
                msg = f"❌ *Term Not Found*\n\n'{escape_markdown(term)}' doesn't exist in the knowledge base\\."
            
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # If not in a special state and not a menu button, treat as search
        await search_term(update, context)
        
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        user_states.pop(user_id, None)
        await update.message.reply_text(
            "❌ An error occurred\\. Please try again\\.",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.MARKDOWN_V2
        )

# ====== MAIN ======
def main():
    """Main function to run the bot"""
    global app
    
    logger.info("Starting enhanced multi-channel study bot...")
    
    try:
        # Create application
        app = Application.builder().token(TOKEN).build()

        # Add command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("add", add_term))
        app.add_handler(CommandHandler("search", search_term))
        app.add_handler(CommandHandler("list", list_terms))
        app.add_handler(CommandHandler("channels", show_channels))
        app.add_handler(CommandHandler("delete", delete_term))
        app.add_handler(CommandHandler("stats", stats))

        # Handle channel posts
        app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_message))
        
        # Handle direct messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))

        # Start the bot
        logger.info("Enhanced multi-channel study bot started successfully")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise

if __name__ == "__main__":
    main()