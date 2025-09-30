
import json
import logging
import os
import re
from pathlib import Path
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import signal
import sys
from typing import Dict, List
from difflib import get_close_matches

# ====== CONFIG ======
# Use environment variable for Railway deployment
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

def extract_definition(text: str) -> tuple:
    """
    Extract term and definition from various formats:
    - "Term - definition"
    - "Term: definition"
    - "Term = definition"
    - "**Term** - definition"
    """
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    
    separators = [' - ', ': ', ' = ', ' – ', ' — ']
    
    for sep in separators:
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) == 2:
                term = parts[0].strip()
                definition = parts[1].strip()
                if term and definition:
                    return term, definition
    
    return None, None

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

# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    msg = (
        "📚 **Multi-Channel Study Bot**\n\n"
        "Welcome! I help you learn and organize terms and definitions from multiple channels.\n\n"
        "🎯 **Quick Start:**\n"
        "• Use the menu buttons below to navigate\n"
        "• Or just type any term to search for it!\n\n"
        "📺 **In Channels:**\n"
        "Add me to a channel and post messages like:\n"
        "• Term - Definition\n"
        "• Term: Definition\n"
        "• Term = Definition\n\n"
        "I'll automatically learn from each channel!\n\n"
        "👇 Use the menu below or type /help for more info"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command with detailed instructions"""
    msg = (
        "📖 **How to Use This Bot**\n\n"
        "**🔍 Searching:**\n"
        "• Click 'Search' button or type `/search Term`\n"
        "• Or just type any term directly!\n\n"
        "**➕ Adding Terms:**\n"
        "• Click 'Add Term' button\n"
        "• Or use: `/add Term - Definition`\n\n"
        "**📚 Viewing Terms:**\n"
        "• Click 'List All' to see all terms\n"
        "• Click 'Channels' to see active channels\n"
        "• Click 'Statistics' for detailed stats\n\n"
        "**🗑️ Deleting:**\n"
        "• Click 'Delete Term' button\n"
        "• Or use: `/delete Term`\n\n"
        "**📺 Channel Learning:**\n"
        "Add me as admin to any channel and I'll automatically learn terms from posts in this format:\n"
        "• `Term - Definition`\n"
        "• `Term: Definition`\n"
        "• `Term = Definition`\n\n"
        "💡 **Tip:** You can search across all channels at once!"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu())

async def delete_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a term from all knowledge bases"""
    # The try block here was empty and caused a syntax error, so it has been removed.
    if not context.args:
        await update.message.reply_text(
            "🗑️ **Delete a Term**\n\n"
            "Please type the term you want to delete.\n\n"
            "**Example:** `Algorithm`",
            reply_markup=get_main_menu()
        )
        return
    
    term = " ".join(context.args)
    term_norm = normalize_term(term)
    deleted_from = []
    
    default_knowledge = load_knowledge()
    if term_norm in default_knowledge:
        original = default_knowledge[term_norm].get("original_term", term)
        del default_knowledge[term_norm]
        save_knowledge(default_knowledge)

async def add_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually add a term"""
    try:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "📝 **Add a New Term**\n\n"
                "**Format:** Term - Definition\n\n"
                "**Example:**\n"
                "`Algorithm - A step-by-step procedure for solving a problem`\n\n"
                "Please send your term in the correct format:",
                reply_markup=get_main_menu()
            )
            return
        
        text = " ".join(context.args)
        term, definition = extract_definition(text)
        
        if not term or not definition:
            await update.message.reply_text(
                "❌ Could not parse term and definition.\n\n"
                "Please use format: `Term - Definition`",
                reply_markup=get_main_menu()
            )
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
            msg = f"✅ Added another definition for: **{term}**\n\n📊 Total definitions: {len(knowledge[term_norm]['definitions'])}"
        else:
            knowledge[term_norm] = {
                "original_term": term,
                "definitions": [{"text": definition, "added": str(update.message.date), "source": "manual"}],
                "added": str(update.message.date),
                "related": []
            }
            msg = f"✅ **Term Added Successfully!**\n\n📚 **{term}**\n📝 {definition}"
        
        save_knowledge(knowledge)
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        logger.info(f"Manual add - term: {term}")
        
    except Exception as e:
        logger.error(f"Error in add_term: {e}")
        await update.message.reply_text("❌ Error adding term", reply_markup=get_main_menu())

async def search_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for a term across all channels"""
    try:
        if not context.args:
            await update.message.reply_text(
                "🔍 **Search for a Term**\n\n"
                "Please type the term you want to search for.\n\n"
                "**Example:** `Algorithm`",
                reply_markup=get_main_menu()
            )
            return
        
        query = " ".join(context.args)
        
        all_results = []
        knowledge_dir = Path("knowledge_bases")
        
        default_knowledge = load_knowledge()
        if default_knowledge:
            default_results = search_knowledge(query, default_knowledge)
            for term, data, score in default_results:
                all_results.append((term, data, score, "manual", 0))
        
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
            await update.message.reply_text(
                f"❌ **No Results Found**\n\n"
                f"No matches for: **{query}**\n\n"
                f"Try a different search term or add it using 'Add Term' button.",
                reply_markup=get_main_menu()
            )
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
        
        msg = f"🔍 **Search Results for '{query}'**\n\n"
        
        for i, (term, data, score, channel_name, channel_id) in enumerate(unique_results, 1):
            original = data.get("original_term", term)
            
            msg += f"**{i}. {original}**"
            if channel_name != "manual":
                msg += f" 📺 {channel_name}"
            msg += "\n"
            
            if "definitions" in data:
                definitions = data["definitions"]
                for j, def_item in enumerate(definitions, 1):
                    def_text = def_item.get("text", def_item) if isinstance(def_item, dict) else def_item
                    if len(definitions) > 1:
                        msg += f"   {j}. {def_text}\n"
                    else:
                        msg += f"   📝 {def_text}\n"
            else:
                definition = data.get("definition", "No definition")
                msg += f"   📝 {definition}\n"
            
            related = data.get("related", [])
            if related:
                msg += f"   🔗 Related: {', '.join(related)}\n"
            
            msg += "\n"
        
        if len(msg) > 4000:
            msg = msg[:4000] + "...\n\n⚠️ (Results truncated)"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in search_term: {e}")
        await update.message.reply_text("❌ Error searching term", reply_markup=get_main_menu())

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
                        else:
                            all_terms[original] += f", {channel_name}"
                except Exception as e:
                    logger.error(f"Error loading {kb_file}: {e}")
        
        if not all_terms:
            await update.message.reply_text(
                "📭 **Knowledge Base is Empty**\n\n"
                "No terms found. Start adding terms or add me to a channel!",
                reply_markup=get_main_menu()
            )
            return
        
        sorted_terms = sorted(all_terms.items())
        
        if len(sorted_terms) > 50:
            chunk_size = 50
            chunks = [sorted_terms[i:i+chunk_size] for i in range(0, len(sorted_terms), chunk_size)]
            
            for chunk_idx, chunk in enumerate(chunks, 1):
                chunk_msg = f"📚 **All Terms (Part {chunk_idx}/{len(chunks)})**\n\n"
                for i, (term, source) in enumerate(chunk, (chunk_idx-1)*chunk_size + 1):
                    chunk_msg += f"{i}. {term} 📺 {source}\n"
                
                if chunk_idx == len(chunks):
                    await update.message.reply_text(chunk_msg, reply_markup=get_main_menu())
                else:
                    await update.message.reply_text(chunk_msg)
        else:
            msg = f"📚 **All Terms ({len(sorted_terms)} total)**\n\n"
            for i, (term, source) in enumerate(sorted_terms, 1):
                msg += f"{i}. {term} 📺 {source}\n"
            await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in list_terms: {e}")
        await update.message.reply_text("❌ Error listing terms", reply_markup=get_main_menu())

async def show_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all channels the bot is learning from"""
    try:
        channels = get_all_channels()
        
        if not channels:
            await update.message.reply_text(
                "📭 **No Active Channels**\n\n"
                "Add me to a channel as an admin to start learning!\n\n"
                "📌 **How to add me:**\n"
                "1. Go to your channel settings\n"
                "2. Add administrators\n"
                "3. Search for this bot and add it\n"
                "4. Post terms in format: `Term - Definition`",
                reply_markup=get_main_menu()
            )
            return
        
        msg = f"📺 **Active Channels ({len(channels)})**\n\n"
        
        for i, (channel_id, channel_name, term_count) in enumerate(channels, 1):
            msg += f"**{i}. {channel_name}**\n"
            msg += f"   📊 Terms: {term_count}\n"
            msg += f"   🆔 ID: `{channel_id}`\n\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in show_channels: {e}")
        await update.message.reply_text("❌ Error showing channels", reply_markup=get_main_menu())

async def channel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed statistics per channel"""
    try:
        channels = get_all_channels()
        
        if not channels:
            await update.message.reply_text(
                "📭 No channels found",
                reply_markup=get_main_menu()
            )
            return
        
        msg = "📊 **Detailed Channel Statistics**\n\n"
        
        total_terms = 0
        total_definitions = 0
        
        for i, (channel_id, channel_name, term_count) in enumerate(channels, 1):
            knowledge = load_knowledge(channel_id)
            
            def_count = sum(
                len(data.get("definitions", [data.get("definition", "")]))
                for data in knowledge.values()
            )
            
            total_terms += term_count
            total_definitions += def_count
            
            msg += f"**{i}. {channel_name}**\n"
            msg += f"   📚 Terms: {term_count}\n"
            msg += f"   📝 Definitions: {def_count}\n"
            msg += f"   📈 Avg: {def_count/term_count:.1f} def/term\n\n"
        
        msg += f"**📊 Overall Total:**\n"
        msg += f"   📚 Terms: {total_terms}\n"
        msg += f"   📝 Definitions: {total_definitions}\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in channel_stats: {e}")
        await update.message.reply_text("❌ Error getting channel statistics", reply_markup=get_main_menu())

async def delete_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a term from all knowledge bases"""
    try:
        if not context.args:
            await update.message.reply_text(
                "🗑️ **Delete a Term**\n\n"
                "Please type the term you want to delete.\n\n"
                "**Example:** `Algorithm`",
                reply_markup=get_main_menu()
            )
            return
        
        term = " ".join(context.args)
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
            msg = f"✅ **Term Deleted Successfully!**\n\n"
            msg += f"🗑️ Deleted '**{term}**' from:\n"
            msg += "\n".join(f"   • {source}" for source in deleted_from)
            logger.info(f"Deleted term: {term} from {', '.join(deleted_from)}")
        else:
            msg = f"❌ **Term Not Found**\n\nNo matches for: **{term}**"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in delete_term: {e}")
        await update.message.reply_text("❌ Error deleting term", reply_markup=get_main_menu())

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
        
        msg = "📊 **Knowledge Base Statistics**\n\n"
        msg += f"📺 Active Channels: **{total_channels}**\n"
        msg += f"📚 Total Terms: **{total_terms}**\n"
        msg += f"📝 Total Definitions: **{total_definitions}**\n"
        
        if total_terms > 0:
            msg += f"📈 Avg Definitions/Term: **{total_definitions/total_terms:.1f}**\n"
        
        msg += f"\n💡 Keep learning! Add more channels or terms."
        
        await update.message.reply_text(msg, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ Error getting statistics", reply_markup=get_main_menu())

async def handle_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically extract terms from channel messages - multi-channel version"""
    try:
        if not update.channel_post:
            return
        
        channel_id = update.channel_post.chat.id
        channel_name = update.channel_post.chat.title or update.channel_post.chat.username or f"Channel {channel_id}"
        
        text = update.channel_post.text or update.channel_post.caption
        if not text:
            return
        
        term, definition = extract_definition(text)
        
        if term and definition:
            knowledge = load_knowledge(channel_id)
            term_norm = normalize_term(term)
            
            if term_norm in knowledge:
                if "definitions" not in knowledge[term_norm]:
                    old_def = knowledge[term_norm].get("definition", "")
                    knowledge[term_norm]["definitions"] = [{"text": old_def, "added": knowledge[term_norm].get("added", "")}]
                
                knowledge[term_norm]["definitions"].append({
                    "text": definition,
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
    """Handle direct messages - treat as search queries or menu buttons"""
    try:
        if not update.message or not update.message.text:
            return
        
        if update.message.text.startswith('/'):
            return
        
        text = update.message.text.strip()
        
        # Handle menu button clicks
        if text == "🔍 Search":
            await update.message.reply_text(
                "🔍 **Search for a Term**\n\n"
                "Please type the term you want to search for.\n\n"
                "**Example:** `Algorithm`",
                reply_markup=get_main_menu()
            )
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
            await update.message.reply_text(
                "📝 **Add a New Term**\n\n"
                "**Format:** `/add Term - Definition`\n\n"
                "**Example:**\n"
                "`/add Algorithm - A step-by-step procedure for solving a problem`\n\n"
                "Please send your term in the correct format:",
                reply_markup=get_main_menu()
            )
            return
        
        elif text == "🗑️ Delete Term":
            await update.message.reply_text(
                "🗑️ **Delete a Term**\n\n"
                "**Format:** `/delete Term`\n\n"
                "**Example:**\n"
                "`/delete Algorithm`\n\n"
                "Please send the term you want to delete:",
                reply_markup=get_main_menu()
            )
            return
        
        elif text == "ℹ️ Help":
            await help_command(update, context)
            return
        
        # If not a menu button, treat as a search query
        query = text
        context.args = query.split()
        await search_term(update, context)
        
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await update.message.reply_text(
            "❌ An error occurred. Please try again.",
            reply_markup=get_main_menu()
        )

# ====== MAIN ======
def main():
    """Main function to run the bot"""
    global app
    
    logger.info("Starting multi-channel study bot...")
    
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
        app.add_handler(CommandHandler("channel_stats", channel_stats))
        app.add_handler(CommandHandler("delete", delete_term))
        app.add_handler(CommandHandler("stats", stats))

        # Handle channel posts
        app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_message))
        
        # Handle direct messages as search queries
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))

        # Start the bot
        logger.info("Multi-channel study bot started successfully")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise

if __name__ == "__main__":
    main()