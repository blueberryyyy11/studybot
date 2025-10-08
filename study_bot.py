import json
import logging
import os
import re
from pathlib import Path
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown as telegram_escape
import signal
import sys
from typing import Dict, List, Optional, Tuple
from difflib import get_close_matches
from datetime import datetime

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
def safe_escape(text: str) -> str:
    """Safely escape text for MarkdownV2, preserving user formatting"""
    if not text:
        return text
    
    # Characters that need escaping in MarkdownV2
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    
    result = text
    for char in special_chars:
        result = result.replace(char, f'\\{char}')
    
    return result

def format_markdown_text(text: str, entities: list = None) -> str:
    """Convert Telegram entities to MarkdownV2 format"""
    if not text:
        return ""
    
    if not entities:
        return safe_escape(text)
    
    # Build the result by processing entities in order
    result_parts = []
    last_pos = 0
    
    # Sort entities by offset
    sorted_entities = sorted(entities, key=lambda e: e.offset)
    
    for entity in sorted_entities:
        start = entity.offset
        end = entity.offset + entity.length
        
        # Add unformatted text before this entity
        if start > last_pos:
            result_parts.append(safe_escape(text[last_pos:start]))
        
        entity_text = text[start:end]
        
        # Apply formatting based on entity type
        if entity.type == "bold":
            result_parts.append(f"*{safe_escape(entity_text)}*")
        elif entity.type == "italic":
            result_parts.append(f"_{safe_escape(entity_text)}_")
        elif entity.type == "code":
            result_parts.append(f"`{entity_text}`")
        elif entity.type == "pre":
            result_parts.append(f"```{entity_text}```")
        elif entity.type == "underline":
            result_parts.append(f"__{safe_escape(entity_text)}__")
        elif entity.type == "strikethrough":
            result_parts.append(f"~{safe_escape(entity_text)}~")
        else:
            result_parts.append(safe_escape(entity_text))
        
        last_pos = end
    
    # Add remaining text
    if last_pos < len(text):
        result_parts.append(safe_escape(text[last_pos:]))
    
    return ''.join(result_parts)

# ====== DATA HELPERS ======
def get_knowledge_file(channel_id: int) -> str:
    """Get knowledge base file for specific channel"""
    knowledge_dir = Path("knowledge_bases")
    knowledge_dir.mkdir(exist_ok=True)
    return str(knowledge_dir / f"knowledge_{abs(channel_id)}.json")

def load_knowledge(channel_id: Optional[int] = None) -> Dict:
    """Load knowledge base for specific channel"""
    try:
        if channel_id is None:
            filename = "knowledge_base.json"
        else:
            filename = get_knowledge_file(channel_id)
        
        if not Path(filename).exists():
            return {}
        
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # Validate and clean data structure
        cleaned_data = {}
        for key, value in data.items():
            if isinstance(value, dict):
                cleaned_data[key] = value
            else:
                # Fix corrupted entries
                logger.warning(f"Fixing corrupted entry for term: {key}")
                cleaned_data[key] = {
                    "original_term": key,
                    "definition": str(value),
                    "added": str(datetime.now())
                }
        
        return cleaned_data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in {filename}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error loading knowledge base for channel {channel_id}: {e}")
        return {}

def save_knowledge(data: Dict, channel_id: Optional[int] = None):
    """Save knowledge base for specific channel"""
    try:
        if channel_id is None:
            filename = "knowledge_base.json"
        else:
            filename = get_knowledge_file(channel_id)
        
        # Ensure directory exists
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving knowledge base for channel {channel_id}: {e}")

def normalize_term(term: str) -> str:
    """Normalize term for case-insensitive matching"""
    return term.lower().strip()

def extract_definition(text: str, entities: list = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract term and definition from various formats"""
    if not text:
        return None, None, None
    
    # Try different separators
    separators = [' - ', ': ', ' = ', ' – ', ' — ', '- ', ': ']
    
    for sep in separators:
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) == 2:
                term = parts[0].strip()
                definition = parts[1].strip()
                
                if term and definition:
                    # Format definition with entities if available
                    if entities:
                        # Calculate offset for definition part
                        def_start = text.index(sep) + len(sep)
                        def_entities = []
                        
                        for entity in entities:
                            if entity.offset >= def_start:
                                # Adjust entity offset for definition
                                new_entity = type('Entity', (), {
                                    'type': entity.type,
                                    'offset': entity.offset - def_start,
                                    'length': entity.length
                                })()
                                def_entities.append(new_entity)
                        
                        if def_entities:
                            formatted_def = format_markdown_text(definition, def_entities)
                        else:
                            formatted_def = safe_escape(definition)
                    else:
                        formatted_def = safe_escape(definition)
                    
                    return term, formatted_def, entities
    
    return None, None, None

def search_knowledge(query: str, knowledge: Dict) -> List[Tuple]:
    """Search for terms matching the query"""
    if not query or not knowledge:
        return []
    
    query_norm = normalize_term(query)
    results = []
    seen_terms = set()
    
    # Exact match
    if query_norm in knowledge:
        data = knowledge[query_norm]
        if isinstance(data, dict):
            results.append((query_norm, data, 1.0))
            seen_terms.add(query_norm)
    
    # Partial matches
    for term, data in knowledge.items():
        if term in seen_terms or not isinstance(data, dict):
            continue
        
        if query_norm in term or term in query_norm:
            score = 0.8
            results.append((term, data, score))
            seen_terms.add(term)
    
    # Fuzzy matches
    all_terms = [t for t in knowledge.keys() if isinstance(knowledge[t], dict)]
    close_matches = get_close_matches(query_norm, all_terms, n=5, cutoff=0.6)
    
    for match in close_matches:
        if match not in seen_terms:
            data = knowledge[match]
            if isinstance(data, dict):
                results.append((match, data, 0.6))
                seen_terms.add(match)
    
    # Sort by score
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:10]

def get_all_channels() -> List[Tuple]:
    """Get list of all channels with knowledge bases"""
    channels = []
    knowledge_dir = Path("knowledge_bases")
    
    if not knowledge_dir.exists():
        return channels
    
    for kb_file in knowledge_dir.glob("knowledge_*.json"):
        try:
            channel_id = int(kb_file.stem.split("_")[1])
            knowledge = load_knowledge(channel_id)
            
            if knowledge:
                # Get channel name from first term
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
        "• Fuzzy matching finds similar terms\n\n"
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
        "`Algorithm \\- A step\\-by\\-step procedure for solving a problem`\n\n"
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
        
        if not query:
            await update.message.reply_text(
                "❌ Please enter a search term\\.",
                reply_markup=get_main_menu(),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        all_results = []
        
        # Search in default knowledge base
        default_knowledge = load_knowledge()
        if default_knowledge:
            default_results = search_knowledge(query, default_knowledge)
            for term, data, score in default_results:
                all_results.append((term, data, score, "Manual", 0))
        
        # Search in channel knowledge bases
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            for kb_file in knowledge_dir.glob("knowledge_*.json"):
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    if knowledge:
                        channel_results = search_knowledge(query, knowledge)
                        
                        # Get channel name
                        first_term = next(iter(knowledge.values()), {})
                        channel_name = first_term.get("channel", f"Channel {channel_id}")
                        
                        for term, data, score in channel_results:
                            all_results.append((term, data, score, channel_name, channel_id))
                except Exception as e:
                    logger.error(f"Error searching {kb_file}: {e}")
        
        if not all_results:
            msg = (
                f"❌ *No Results Found*\n\n"
                f"No matches for: *{safe_escape(query)}*\n\n"
                f"💡 Try:\n"
                f"• Different keywords\n"
                f"• Checking spelling\n"
                f"• Adding the term using 'Add Term' button"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # Sort and deduplicate
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
        
        # Build response with proper escaping
        msg = f"🔍 *Search Results for '{safe_escape(query)}'*\n\n"
        msg += f"Found {len(unique_results)} result{'s' if len(unique_results) != 1 else ''}:\n\n"
        
        for i, (term, data, score, channel_name, channel_id) in enumerate(unique_results, 1):
            try:
                original = data.get("original_term", term)
                
                msg += f"*{i}\\. {safe_escape(original)}*"
                if channel_name != "Manual":
                    msg += f" 📺 _{safe_escape(channel_name)}_"
                msg += "\n"
                
                # Handle multiple definitions
                if "definitions" in data and isinstance(data["definitions"], list):
                    definitions = data["definitions"]
                    for j, def_item in enumerate(definitions[:3], 1):  # Show max 3
                        try:
                            if isinstance(def_item, dict):
                                def_text = def_item.get("text", "")
                            else:
                                def_text = str(def_item)
                            
                            # Ensure definition is properly escaped
                            # Check if it's already in markdown format (contains unescaped markdown chars)
                            if any(md in def_text for md in ['*', '_', '`', '[']):
                                # Already formatted, use as-is
                                safe_def = def_text
                            else:
                                # Plain text, escape it
                                safe_def = safe_escape(def_text)
                            
                            if len(definitions) > 1:
                                msg += f"   {j}\\. {safe_def}\n"
                            else:
                                msg += f"   {safe_def}\n"
                        except Exception as e:
                            logger.error(f"Error formatting definition {j}: {e}")
                            msg += f"   _Definition error_\n"
                    
                    if len(definitions) > 3:
                        msg += f"   _\\.\\.\\.and {len(definitions) - 3} more_\n"
                else:
                    # Single definition
                    try:
                        definition = data.get("definition", "No definition")
                        
                        # Check if already formatted
                        if any(md in definition for md in ['*', '_', '`', '[']):
                            safe_def = definition
                        else:
                            safe_def = safe_escape(definition)
                        
                        msg += f"   {safe_def}\n"
                    except Exception as e:
                        logger.error(f"Error formatting single definition: {e}")
                        msg += f"   _Definition error_\n"
                
                msg += "\n"
            except Exception as e:
                logger.error(f"Error formatting result {i}: {e}")
                msg += f"   _Error displaying this result_\n\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in search_term: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Error searching\\. Try again\\.",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def list_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all terms from all channels"""
    try:
        all_terms = {}
        
        # Load default knowledge
        default_knowledge = load_knowledge()
        for term, data in default_knowledge.items():
            if isinstance(data, dict):
                original = data.get("original_term", term)
                all_terms[original] = "Manual"
        
        # Load channel knowledge
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            for kb_file in knowledge_dir.glob("knowledge_*.json"):
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    if knowledge:
                        first_term = next(iter(knowledge.values()), {})
                        channel_name = first_term.get("channel", f"Channel {channel_id}")
                        
                        for term, data in knowledge.items():
                            if isinstance(data, dict):
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
        
        msg = f"📚 *All Terms* \\({len(sorted_terms)} total\\)\n\n"
        
        # Group by source
        manual_terms = [(t, s) for t, s in sorted_terms if s == "Manual"]
        channel_terms = [(t, s) for t, s in sorted_terms if s != "Manual"]
        
        if manual_terms:
            msg += "*Manual Terms:*\n"
            for term, _ in manual_terms[:20]:
                msg += f"• {safe_escape(term)}\n"
            if len(manual_terms) > 20:
                msg += f"_\\.\\.\\.and {len(manual_terms) - 20} more_\n"
            msg += "\n"
        
        if channel_terms:
            # Group by channel
            from itertools import groupby
            channel_terms_sorted = sorted(channel_terms, key=lambda x: x[1])
            
            for channel, terms in groupby(channel_terms_sorted, key=lambda x: x[1]):
                terms_list = list(terms)
                msg += f"*{safe_escape(channel)}:*\n"
                for term, _ in terms_list[:15]:
                    msg += f"• {safe_escape(term)}\n"
                if len(terms_list) > 15:
                    msg += f"_\\.\\.\\.and {len(terms_list) - 15} more_\n"
                msg += "\n"
        
        msg += "💡 Type any term name to search for it\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in list_terms: {e}", exc_info=True)
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
        
        msg = f"📺 *Active Channels* \\({len(channels)}\\)\n\n"
        
        for i, (channel_id, channel_name, term_count) in enumerate(channels, 1):
            msg += f"*{i}\\. {safe_escape(channel_name)}*\n"
            msg += f"   📚 {term_count} term{'s' if term_count != 1 else ''}\n"
            msg += f"   🆔 `{channel_id}`\n\n"
        
        msg += "💡 Add me to more channels to expand the knowledge base\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in show_channels: {e}", exc_info=True)
        await update.message.reply_text("❌ Error showing channels", reply_markup=get_main_menu())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overall statistics"""
    try:
        total_terms = 0
        total_definitions = 0
        total_channels = 0
        
        # Count default knowledge
        default_knowledge = load_knowledge()
        if default_knowledge:
            total_terms += len(default_knowledge)
            for data in default_knowledge.values():
                if isinstance(data, dict):
                    if "definitions" in data:
                        total_definitions += len(data["definitions"])
                    elif "definition" in data:
                        total_definitions += 1
        
        # Count channel knowledge
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            channel_files = list(knowledge_dir.glob("knowledge_*.json"))
            total_channels = len(channel_files)
            
            for kb_file in channel_files:
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    total_terms += len(knowledge)
                    for data in knowledge.values():
                        if isinstance(data, dict):
                            if "definitions" in data:
                                total_definitions += len(data["definitions"])
                            elif "definition" in data:
                                total_definitions += 1
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
        logger.error(f"Error in stats: {e}", exc_info=True)
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
        
        term, definition, _ = extract_definition(text, entities)
        
        if term and definition:
            knowledge = load_knowledge(channel_id)
            term_norm = normalize_term(term)
            
            timestamp = str(update.channel_post.date)
            
            if term_norm in knowledge:
                # Add to existing term
                if "definitions" not in knowledge[term_norm]:
                    old_def = knowledge[term_norm].get("definition", "")
                    old_added = knowledge[term_norm].get("added", timestamp)
                    knowledge[term_norm]["definitions"] = [{"text": old_def, "added": old_added}]
                
                knowledge[term_norm]["definitions"].append({
                    "text": definition,
                    "added": timestamp,
                    "channel": channel_name
                })
                logger.info(f"[{channel_name}] Added definition #{len(knowledge[term_norm]['definitions'])} for term: {term}")
            else:
                # Create new term
                knowledge[term_norm] = {
                    "original_term": term,
                    "definitions": [{"text": definition, "added": timestamp, "channel": channel_name}],
                    "added": timestamp,
                    "channel": channel_name,
                    "related": []
                }
                logger.info(f"[{channel_name}] Auto-learned new term: {term}")
            
            save_knowledge(knowledge, channel_id)
        
    except Exception as e:
        logger.error(f"Error handling channel message: {e}", exc_info=True)

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
            term, definition, _ = extract_definition(text, update.message.entities)
            
            if not term or not definition:
                msg = (
                    "❌ I couldn't parse that\\.\n\n"
                    "Please use format: `Term \\- Definition`\n\n"
                    "Examples:\n"
                    "• `Algorithm \\- Step\\-by\\-step procedure`\n"
                    "• `Python: Programming language`\n"
                    "• `API \\= Application Programming Interface`"
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            knowledge = load_knowledge()
            term_norm = normalize_term(term)
            timestamp = str(update.message.date)
            
            if term_norm in knowledge:
                # Add to existing term
                if "definitions" not in knowledge[term_norm]:
                    old_def = knowledge[term_norm].get("definition", "")
                    old_added = knowledge[term_norm].get("added", timestamp)
                    knowledge[term_norm]["definitions"] = [{"text": old_def, "added": old_added}]
                
                knowledge[term_norm]["definitions"].append({
                    "text": definition,
                    "added": timestamp,
                    "source": "manual"
                })
                
                msg = f"✅ *Added Another Definition\\!*\n\n"
                msg += f"📚 *{safe_escape(term)}*\n"
                msg += f"📊 Now has {len(knowledge[term_norm]['definitions'])} definitions"
            else:
                # Create new term
                knowledge[term_norm] = {
                    "original_term": term,
                    "definitions": [{"text": definition, "added": timestamp, "source": "manual"}],
                    "added": timestamp,
                    "related": []
                }
                
                msg = f"✅ *Term Added Successfully\\!*\n\n"
                msg += f"📚 *{safe_escape(term)}*\n"
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
            
            # Delete from default knowledge
            default_knowledge = load_knowledge()
            if term_norm in default_knowledge:
                original = default_knowledge[term_norm].get("original_term", term)
                del default_knowledge[term_norm]
                save_knowledge(default_knowledge)
                deleted_from.append("Manual")
            
            # Delete from channel knowledge bases
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
                msg += f"🗑️ Removed '*{safe_escape(term)}*' from:\n"
                for source in deleted_from:
                    msg += f"   • {safe_escape(source)}\n"
                logger.info(f"Deleted term: {term} from {', '.join(deleted_from)}")
            else:
                msg = f"❌ *Term Not Found*\n\n'{safe_escape(term)}' doesn't exist in the knowledge base\\."
            
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # If not in a special state and not a menu button, treat as search
        await search_term(update, context)
        
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        user_states.pop(user_id, None)
        await update.message.reply_text(
            "❌ An error occurred\\. Please try again\\.",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)

# ====== GRACEFUL SHUTDOWN ======
def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info("Received shutdown signal, stopping bot...")
    if app and app.running:
        app.stop()
    sys.exit(0)

# ====== MAIN ======
def main():
    """Main function to run the bot"""
    global app
    
    logger.info("Starting enhanced multi-channel study bot...")
    
    try:
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
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

        # Add error handler
        app.add_error_handler(error_handler)

        # Set bot commands for menu
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help message"),
            BotCommand("search", "Search for a term"),
            BotCommand("add", "Add a new term"),
            BotCommand("list", "List all terms"),
            BotCommand("channels", "Show active channels"),
            BotCommand("delete", "Delete a term"),
            BotCommand("stats", "Show statistics")
        ]
        
        # Start the bot
        logger.info("Enhanced multi-channel study bot started successfully")
        logger.info("Bot is ready to receive messages...")
        
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "channel_post"]
        )
        
    except Exception as e:
        logger.error(f"Error running bot: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()