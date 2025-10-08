import json
import logging
import os
import sys
from typing import Dict, List
from difflib import get_close_matches
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    # NOTE: Please ensure TELEGRAM_BOT_TOKEN is set in your environment
    pass

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("flashcard_bot.log", encoding="utf-8")
    ]
)

logger = logging.getLogger(__name__)

# ====== GLOBAL VARIABLES ======
app = None
user_states = {}  # Track user states for multi-step operations
TERMS_DATA_FILE = "terms_base.json"

# ====== HELPER FUNCTION FOR MARKDOWN ESCAPING (Reused from original) ======
def escape_markdown(text: str) -> str:
    """
    Escape special characters for MarkdownV2, primarily used for display text.
    """
    if not text:
        return text
    
    # Escape special characters required by MarkdownV2
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# ====== DATA HELPERS ======
def load_terms() -> Dict:
    """Load the terms base (flashcards) from a JSON file."""
    try:
        with open(TERMS_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Error loading terms base: {e}")
        return {}

def save_terms(data: Dict):
    """Save the terms base (flashcards) to a JSON file."""
    try:
        with open(TERMS_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving terms base: {e}")

def normalize_name(name: str) -> str:
    """Normalize term name for case-insensitive matching."""
    return name.lower().strip()

def extract_term_details(text: str) -> Dict | None:
    """
    Extract term and definition from a string.
    Supports '|', ':', or '-' as separators.
    """
    # Define possible separators
    separators = ['|', ':', '-']
    
    term = None
    definition = None

    for sep in separators:
        # Split only once to allow the separator in the definition
        parts = [p.strip() for p in text.split(sep, 1)] 
        
        if len(parts) == 2 and parts[0] and parts[1]:
            term, definition = parts[0], parts[1]
            break # Found a valid separation, exit loop

    if term and definition:
        return {
            "term": term,
            "definition": definition,
        }
    return None

def search_terms(query: str, terms: Dict) -> List[tuple]:
    """Search for terms matching the query in term name or definition."""
    query_norm = normalize_name(query)
    results = []
    seen_names = set()
    
    # 1. Exact match (by normalized term)
    if query_norm in terms:
        data = terms[query_norm]
        results.append((query_norm, data, 1.0))
        seen_names.add(query_norm)
    
    for term_norm, data in terms.items():
        if term_norm in seen_names:
            continue
            
        definition_norm = normalize_name(data.get("definition", ""))
        
        # 2. Substring match (in term or definition)
        if query_norm in term_norm or query_norm in definition_norm:
            score = 0.8
            results.append((term_norm, data, score))
            seen_names.add(term_norm)

    # 3. Close matches (by term name)
    all_names = list(terms.keys())
    close_matches = get_close_matches(query_norm, all_names, n=3, cutoff=0.6)
    for match in close_matches:
        if match not in seen_names:
            results.append((match, terms[match], 0.6))
            seen_names.add(match)
    
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:5]

# ====== MENU HELPER ======
def get_main_menu():
    """Create the main menu keyboard."""
    keyboard = [
        [KeyboardButton("🔍 Search Term"), KeyboardButton("📚 List All Terms")],
        [KeyboardButton("➕ Add Term"), KeyboardButton("🗑️ Delete Term")],
        [KeyboardButton("ℹ️ Help")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ====== COMMANDS & HANDLERS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - introduction to the bot."""
    msg = (
        "🧠 *Flashcard Study Bot*\n\n"
        "Welcome\\! I help you create and memorize terms and definitions\\.\n\n"
        "🎯 *Quick Start:*\n"
        "• Click *'➕ Add Term'* to create a new flashcard\\.\n"
        "• Click *'🔍 Search Term'* or just type a keyword to find a term or definition\\.\n\n"
        "👇 Use the menu below or just start typing to search\\!"
    )
    # Ensure state is reset on start
    user_states.pop(update.effective_user.id, None) 
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command with detailed instructions."""
    msg = (
        "📖 *How to Use Flashcard Study Bot*\n\n"
        "*➕ Adding Terms:*\n"
        "1\\. Click the *'➕ Add Term'* button\\.\n"
        "2\\. Send the term and definition in a format like this:\n"
        "  `Term Name | Definition`\n"
        "  *or* `Term Name : Definition`\n"
        "  *or* `Term Name - Definition`\n\n"
        "*Example:*\n"
        "`Python : An interpreted, object-oriented, high-level programming language with dynamic semantics\\.`\n\n"
        "*🔍 Searching:*\n"
        "• Just type any part of the term name or definition to search\\.\n"
        "• Example: `interpreted` or `Python`\n\n"
        "*📚 Listing:*\n"
        "• *'📚 List All Terms'* shows all saved flashcards\\.\n\n"
        "*🗑️ Deleting:*\n"
        "1\\. Click *'🗑️ Delete Term'*\\.\n"
        "2\\. Type the exact name of the term to remove it\\.\n\n"
        "*🛑 Canceling:*\n"
        "• Type `/cancel` to stop any pending operation (Add/Delete)\\."
    )
    user_states.pop(update.effective_user.id, None) 
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any ongoing multi-step operation and reset the user state."""
    user_id = update.effective_user.id
    current_state = user_states.pop(user_id, None)
    
    if current_state:
        msg = "🛑 *Canceled*\\! The previous operation was successfully stopped\\."
    else:
        msg = "✨ Nothing to cancel\\! You are currently not in any multi\\-step operation\\."
        
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def add_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate term adding process."""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_term"
    
    msg = (
        "📝 *Add a New Term (Flashcard)*\n\n"
        "Please send the term and definition using one of these separators: *|*, *:* or *\\-*\n"
        "`Term Name \\| Definition`\n"
        "*Example:*\n"
        "`API : Application Programming Interface`\n\n"
        "Send your term now:"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def delete_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate term deletion process."""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_delete_term"
    
    msg = (
        "🗑️ *Delete a Term*\n\n"
        "Type the *exact name* of the term you want to delete\\.\n\n"
        "*Example:* `API`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def search_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for a term by keyword."""
    try:
        query = update.message.text.strip()
        terms = load_terms()
        results = search_terms(query, terms)
        
        if not results:
            msg = (
                f"❌ *No Terms Found*\n\n"
                f"No matches for: *{escape_markdown(query)}*\n\n"
                f"💡 Try different keywords related to the term or definition\\."
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        msg = f"🔍 *Search Results for '{escape_markdown(query)}'*\n\n"
        
        for i, (term_norm, data, score) in enumerate(results, 1):
            original_term = data.get("original_term", term_norm)
            definition = data.get("definition", "N/A")
            
            # Truncate definition for display, while escaping markdown
            display_definition = escape_markdown(definition)
            if len(display_definition) > 100:
                display_definition = display_definition[:100] + '...'
            
            msg += f"*{i}\\. {escape_markdown(original_term)}*\n"
            msg += f"   ➡️ {display_definition}\n\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in search_term: {e}")
        # Note: Do NOT clear state here, as search is the default state
        await update.message.reply_text("❌ Error searching for terms\\. Try again\\.", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def list_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all terms."""
    try:
        terms = load_terms()
        
        if not terms:
            msg = (
                "📭 *No Saved Terms*\n\n"
                "Your flashcard deck is empty\\! Click '➕ Add Term' to get started\\."
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # Sort by term name
        sorted_terms = sorted(terms.items(), key=lambda item: item[0])
        
        msg = "📚 *All Saved Terms* \\(" + str(len(sorted_terms)) + " total\\)\n\n"
        
        for term_norm, data in sorted_terms:
            original_term = data.get("original_term", term_norm)
            definition = data.get("definition", "N/A")
            
            # Truncate definition for display, while escaping markdown
            display_definition = escape_markdown(definition)
            if len(display_definition) > 100:
                display_definition = display_definition[:100] + '...'
            
            msg += f"*{escape_markdown(original_term)}*\n"
            msg += f"   ➡️ {display_definition}\n\n"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in list_terms: {e}")
        await update.message.reply_text("❌ Error listing terms", reply_markup=get_main_menu())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct messages based on user state."""
    user_id = update.effective_user.id
    try:
        if not update.message or not update.message.text:
            return
        
        if update.message.text.startswith('/'):
            return
        
        text = update.message.text.strip()
        if not text:
            return
        user_state = user_states.get(user_id)
        
        # Handle menu button clicks - always clear state for menu buttons before proceeding
        # This prevents accidental state persistence if an error occurred during a previous action.
        
        if text == "🔍 Search Term":
            user_states[user_id] = None
            msg = "🔍 Just type the term or keyword you're looking for\\!\n\nFor example: `API` or `programming language`"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        elif text == "📚 List All Terms":
            user_states[user_id] = None
            await list_terms(update, context)
            return
        
        elif text == "➕ Add Term":
            user_states[user_id] = None # Reset state, then call add_term which sets it to awaiting_term
            await add_term(update, context)
            return
        
        elif text == "🗑️ Delete Term":
            user_states[user_id] = None # Reset state, then call delete_term which sets it to awaiting_delete_term
            await delete_term(update, context)
            return
        
        elif text == "ℹ️ Help":
            user_states[user_id] = None
            await help_command(update, context)
            return
        
        # Handle state-based inputs
        if user_state == "awaiting_term":
            # User is adding a term
            term_details = extract_term_details(text)
            
            if not term_details:
                msg = (
                    "❌ I couldn't parse that\\.\n\n"
                    "Please use the required format with one of these separators: *|*, *:* or *\\-*"
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            terms = load_terms()
            term_norm = normalize_name(term_details["term"])
            
            # Check for existing term
            if term_norm in terms:
                msg = (
                    f"⚠️ *Term Already Exists*\\!\\n\\n"
                    f"The term '*{escape_markdown(term_details['term'])}*' is already in your flashcards\\.\n"
                    f"To replace it, please use the format again or type `/cancel`\\."
                )
                # DO NOT clear state here, allow the user to immediately send the input again to overwrite
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return
            
            terms[term_norm] = {
                "original_term": term_details["term"],
                "definition": term_details["definition"],
                "added": str(update.message.date)
            }
            
            save_terms(terms)
            user_states[user_id] = None # Clear state upon success
            
            msg = f"✅ *Term Added Successfully\\!*\n\n"
            msg += f"*{escape_markdown(term_details['term'])}*\n"
            
            # Truncate definition for display, while escaping markdown
            display_definition = escape_markdown(term_details['definition'])
            if len(display_definition) > 100:
                display_definition = display_definition[:100] + '...'
                
            msg += f"   ➡️ {display_definition}"
            
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"Manual add - term: {term_details['term']}")
            return
        
        elif user_state == "awaiting_delete_term":
            # User is deleting a term
            name = text
            name_norm = normalize_name(name)
            
            terms = load_terms()
            
            if name_norm in terms:
                original_term = terms[name_norm].get("original_term", name)
                del terms[name_norm]
                save_terms(terms)
                
                msg = f"✅ *Term Deleted\\!*\n\n"
                msg += f"🗑️ Removed '*{escape_markdown(original_term)}*' from your flashcards\\."
                logger.info(f"Deleted term: {name}")
            else:
                msg = f"❌ *Term Not Found*\n\n'*{escape_markdown(name)}*' doesn't exist in your flashcards\\."
            
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # If not in a special state and not a menu button, treat as search
        await search_term(update, context)
        
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        # Remove the state only if an error occurred during a multi-step process
        user_states.pop(user_id, None)
        await update.message.reply_text(
            "❌ An internal error occurred\\. Your current operation has been canceled\\. Please try again\\.",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.MARKDOWN_V2
        )

# ====== MAIN ======
def main():
    """Main function to run the bot"""
    global app
    
    # Check if TOKEN is set, exit if not for safety in production
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)

    logger.info("Starting Flashcard Study Bot...")
    
    try:
        # Create application
        app = Application.builder().token(TOKEN).build()

        # Add command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("cancel", cancel_command)) # NEW CANCEL COMMAND
        app.add_handler(CommandHandler("add", add_term))
        app.add_handler(CommandHandler("delete", delete_term))
        app.add_handler(CommandHandler("list", list_terms))
        
        # Handle direct messages (text that is not a command)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))

        # Start the bot
        logger.info("Flashcard Study Bot started successfully")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise

if __name__ == "__main__":
    main()
