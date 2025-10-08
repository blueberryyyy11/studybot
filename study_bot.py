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
from typing import Dict, List, Tuple, Optional
from difflib import get_close_matches
from itertools import groupby

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

# ====== SMART CONTENT ANALYSIS ======
def extract_keywords(text: str) -> List[str]:
    """Extract important keywords from text"""
    # Remove common words
    common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 
                    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
                    'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                    'could', 'should', 'may', 'might', 'must', 'can', 'this', 'that',
                    'these', 'those', 'it', 'its', 'which', 'what', 'who', 'when', 'where',
                    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'has', 'have', 'had', 'do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must'}
    
    # Extract capitalized phrases and important words
    words = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b|\b[a-z]{4,}\b', text)
    keywords = []
    
    for word in words:
        word_lower = word.lower()
        if word_lower not in common_words and len(word) > 3:
            keywords.append(word_lower)
    
    # Get unique keywords, preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)
    
    return unique_keywords[:15]  # Top 15 keywords

def extract_main_topic(text: str) -> str:
    """Extract the main topic from text using various heuristics"""
    # Look for common topic indicators
    patterns = [
        r'(?:the|about|regarding|concerning)\s+(.+?)(?:\n|:|\.|$)',
        r'^(.+?)(?:\n|:)',
        r'(?:definition|explanation|description) of\s+(.+?)(?:\n|\.|$)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            topic = match.group(1).strip()
            if len(topic) < 100:
                return topic
    
    # Fallback: use first meaningful sentence
    sentences = re.split(r'[.!?]\s+', text)
    if sentences:
        return sentences[0][:100].strip()
    
    return "Untitled Note"

def detect_content_type(text: str) -> Tuple[str, Optional[str], List[str]]:
    """
    Detect the type of content and extract main topic and subtopics
    Returns: (content_type, main_topic, subtopics)
    """
    # Check for term-definition pattern (high priority)
    separators = [' - ', ': ', ' = ', ' – ', ' — ']
    is_definition = any(sep in text for sep in separators) and len(text.split('\n')) <= 2
    
    if is_definition:
        content_type = "definition"
        # Use extract_definition to get the term
        term, _, _ = extract_definition(text)
        main_topic = term if term else extract_main_topic(text)
        subtopics = []
    else:
        # Check for numbered/bulleted lists
        has_numbers = bool(re.search(r'^\d+\.\s+', text, re.MULTILINE))
        has_bullets = bool(re.search(r'^[•\-\*]\s+', text, re.MULTILINE))
        
        # Determine main topic
        main_topic = extract_main_topic(text)
        
        # Extract subtopics (numbered or bulleted items)
        subtopics = []
        if has_numbers:
            # Capture content until a newline or end of string
            subtopics = re.findall(r'^\d+\.\s+(.+?)(?:\n|$)', text, re.MULTILINE)
        elif has_bullets:
            subtopics = re.findall(r'^[•\-\*]\s+(.+?)(?:\n|$)', text, re.MULTILINE)
        
        # Clean subtopics (take first line only)
        subtopics = [s.split('\n')[0].strip() for s in subtopics]
        
        # Determine content type
        if has_numbers or has_bullets:
            content_type = "list"
        elif len(text) > 500:
            content_type = "long_form"
        else:
            content_type = "note"
            
    return content_type, main_topic, subtopics

def create_smart_entry(text: str, channel_name: str = "Manual") -> Dict:
    """Create a smart knowledge entry from free-form text"""
    content_type, main_topic, subtopics = detect_content_type(text)
    keywords = extract_keywords(text)
    
    # For definitions, the 'definition' field will hold the content
    if content_type == "definition":
        _, definition, _ = extract_definition(text)
        entry = {
            "original_term": main_topic,
            "definition": definition or text,
            "content": text,  # Store full original text
            "content_type": content_type,
            "keywords": keywords,
            "subtopics": subtopics,
            "added": "",  # Will be set by caller
            "channel": channel_name,
            "search_terms": [main_topic.lower()] + keywords + [s.lower() for s in subtopics]
        }
    # For all other types, the content field holds the full text
    else:
        entry = {
            "original_term": main_topic,
            "content": text,  # Store full original text
            "content_type": content_type,
            "keywords": keywords,
            "subtopics": subtopics,
            "added": "",  # Will be set by caller
            "channel": channel_name,
            "search_terms": [main_topic.lower()] + keywords + [s.lower() for s in subtopics]
        }
    
    return entry

def smart_search(query: str, knowledge: Dict) -> List[Tuple[str, Dict, float]]:
    """Enhanced search that checks keywords, subtopics, and content"""
    query_norm = normalize_term(query)
    query_words = query_norm.split()
    results = []
    seen_terms = set()
    
    for term, data in knowledge.items():
        if term in seen_terms:
            continue
        
        score = 0.0
        
        # Check main term
        if query_norm == term:
            score = 1.0
        elif query_norm in term or term in query_norm:
            score = max(score, 0.9)
        
        # Check keywords
        keywords = data.get("keywords", [])
        keyword_matches = sum(1 for kw in keywords if query_norm in kw or kw in query_norm)
        if keyword_matches > 0:
            score = max(score, 0.7 + (keyword_matches * 0.05))
        
        # Check subtopics
        subtopics = data.get("subtopics", [])
        for subtopic in subtopics:
            if query_norm in subtopic.lower():
                score = max(score, 0.75)
                break
        
        # Check search terms (includes normalized term, keywords, subtopics)
        search_terms = data.get("search_terms", [])
        for search_term in search_terms:
            if query_norm in search_term:
                score = max(score, 0.7)
                break
        
        # Check content for multi-word queries or high relevance
        if len(query_words) > 1 or score < 0.7:
            content = data.get("content", data.get("definition", "")).lower()
            if query_norm in content:
                # Give a boost if the full query phrase is in the content
                score = max(score, 0.6)
            
            # Check individual query words against content
            word_match_count = sum(1 for word in query_words if word in content)
            if word_match_count > 0:
                 score = max(score, 0.5 + (word_match_count / len(query_words) * 0.1))
        
        if score > 0:
            results.append((term, data, score))
            seen_terms.add(term)
    
    # Also do fuzzy matching on main terms (lower relevance)
    all_terms = list(knowledge.keys())
    close_matches = get_close_matches(query_norm, all_terms, n=3, cutoff=0.6)
    for match in close_matches:
        if match not in seen_terms:
            # Check if term exists, if so append, otherwise continue
            data = knowledge.get(match)
            if data:
                results.append((match, data, 0.55))
                seen_terms.add(match)
    
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:10]

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
    temp_text = text
    for match in re.finditer(code_block_pattern, temp_text):
        placeholder = f"__CODEBLOCK{len(code_blocks)}__"
        code_blocks.append(match.group())
        text = text.replace(match.group(), placeholder, 1)
    
    # Extract inline code (`code`)
    inline_code_pattern = r'`[^`\n]+`'
    temp_text = text
    for match in re.finditer(inline_code_pattern, temp_text):
        placeholder = f"__INLINECODE{len(inline_codes)}__"
        inline_codes.append(match.group())
        # Replace only in the text remaining after code block extraction
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
        [KeyboardButton("📝 Save Note"), KeyboardButton("🗑️ Delete")],
        [KeyboardButton("ℹ️ Help")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    msg = (
        "📚 *Smart Study Bot*\n\n"
        "I intelligently save and organize your notes, definitions, and long\\-form content\\!\n\n"
        "🎯 *Quick Start:*\n"
        "• Just send me any text \\- I'll understand and save it\n"
        "• Type keywords to search\n"
        "• Use menu for quick actions\n\n"
        "✨ *Smart Features:*\n"
        "• Auto\\-detects topics and keywords\n"
        "• Preserves all formatting\n"
        "• Understands lists and structured content\n"
        "• Searches across everything\n\n"
        "📝 *Send me anything:*\n"
        "• Long explanations\n"
        "• Lists and definitions\n"
        "• Notes and concepts\n"
        "• Study materials\n\n"
        "I'll make it easy to find later\\!\n\n"
        "👇 Try sending me some text or use the menu"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command with detailed instructions"""
    msg = (
        "📖 *How to Use Smart Study Bot*\n\n"
        "*📝 Saving Content:*\n"
        "Just send me any text\\! I understand:\n"
        "• Long explanations\n"
        "• Numbered lists \\(1\\. 2\\. 3\\.\\)\n"
        "• Bullet points \\(• \\- \\*\\)\n"
        "• Definitions \\(Term \\- Definition\\)\n"
        "• Any formatted text\n\n"
        "I'll automatically:\n"
        "✓ Detect the main topic\n"
        "✓ Extract keywords\n"
        "✓ Identify subtopics\n"
        "✓ Preserve formatting\n\n"
        "*🔍 Searching:*\n"
        "Type any keyword, topic, or phrase\\!\n"
        "I search through:\n"
        "• Main topics\n"
        "• Keywords\n"
        "• Subtopics\n"
        "• Full content\n\n"
        "*📚 Organization:*\n"
        "• 'List All' \\- see everything\n"
        "• 'Channels' \\- view sources\n"
        "• 'Statistics' \\- see your progress\n\n"
        "*📺 Channels:*\n"
        "Add me to channels and I'll learn automatically\\!\n\n"
        "💡 *Pro Tip:* Use *bold*, _italic_, `code` \\- all formatting is preserved\\!"
    )
    await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate note saving process"""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_note"
    
    msg = (
        "📝 *Save New Content*\n\n"
        "Send me any text and I'll save it smartly\\!\n\n"
        "✨ I understand:\n"
        "• Long explanations and notes\n"
        "• Lists with numbered or bullet points\n"
        "• Definitions and concepts\n"
        "• Any formatted content\n\n"
        "Just paste your content and I'll handle the rest\\!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate entry deletion process"""
    user_id = update.effective_user.id
    user_states[user_id] = "awaiting_delete"
    
    msg = (
        "🗑️ *Delete an Entry*\n\n"
        "Type the name or keyword of what you want to delete\\.\n\n"
        "*Example:* `complex system`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def search_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search for content across all knowledge bases"""
    try:
        query = update.message.text.strip()
        
        all_results = []
        knowledge_dir = Path("knowledge_bases")
        
        # Search in default knowledge base
        default_knowledge = load_knowledge()
        if default_knowledge:
            default_results = smart_search(query, default_knowledge)
            for term, data, score in default_results:
                all_results.append((term, data, score, "Manual", 0))
        
        # Search in channel knowledge bases
        if knowledge_dir.exists():
            for kb_file in knowledge_dir.glob("knowledge_*.json"):
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    if knowledge:
                        channel_results = smart_search(query, knowledge)
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
                f"• Broader search terms\n"
                f"• Saving new content with 'Save Note'"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        all_results.sort(key=lambda x: x[2], reverse=True)
        
        # Remove duplicates
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
            content_type = data.get("content_type", "note")
            
            msg += f"*{i}\\. {escape_markdown(original)}* ({content_type.replace('_', ' ').title()})"
            if channel_name != "Manual":
                msg += f" 📺 _{escape_markdown(channel_name)}_"
            msg += "\n"
            
            # Show content preview
            content = data.get("content") or data.get("definition")
            if content:
                # Show first 200 characters
                preview = content[:200].replace('\n', ' ') # Replace newlines in preview
                if len(content) > 200:
                    preview += "..."
                msg += f"   {escape_markdown(preview, preserve_code=True)}\n"
                
                # Show keywords
                keywords = data.get("keywords", [])
                if keywords:
                    kw_display = ', '.join(keywords[:5])
                    msg += f"   🏷️ {escape_markdown(kw_display)}\n"
            
            msg += "\n"
        
        msg += "💡 Type a result number or name to search for more details"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in search_content: {e}")
        await update.message.reply_text("❌ Error searching\\. Try again\\.", reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)

async def list_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all saved content"""
    try:
        all_entries = {}
        
        default_knowledge = load_knowledge()
        for term, data in default_knowledge.items():
            original = data.get("original_term", term)
            content_type = data.get("content_type", "note")
            all_entries[original] = ("Manual", content_type)
        
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
                        content_type = data.get("content_type", "note")
                        if original not in all_entries:
                            all_entries[original] = (channel_name, content_type)
                except Exception as e:
                    logger.error(f"Error loading {kb_file}: {e}")
        
        if not all_entries:
            msg = (
                "📭 *No Saved Content*\n\n"
                "Start saving content\\!\n\n"
                "💡 Just send me any text and I'll save it for you\\."
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        sorted_entries = sorted(all_entries.items())
        
        msg = f"📚 *All Saved Content* \\({len(sorted_entries)} total\\)\n\n"
        
        # Group by type
        definitions = [(t, s, ct) for t, (s, ct) in sorted_entries if ct == "definition"]
        lists = [(t, s, ct) for t, (s, ct) in sorted_entries if ct == "list"]
        long_forms = [(t, s, ct) for t, (s, ct) in sorted_entries if ct == "long_form"]
        notes = [(t, s, ct) for t, (s, ct) in sorted_entries if ct == "note"]
        
        if definitions:
            msg += "*📝 Definitions:*\n"
            for term, source, _ in definitions[:10]:
                msg += f"• {escape_markdown(term)} _{escape_markdown(source)}_\n"
            if len(definitions) > 10:
                msg += f"_\\.\\.\\.and {len(definitions) - 10} more_\n"
            msg += "\n"

        if lists:
            msg += "*📋 Lists:*\n"
            for term, source, _ in lists[:10]:
                msg += f"• {escape_markdown(term)} _{escape_markdown(source)}_\n"
            if len(lists) > 10:
                msg += f"_\\.\\.\\.and {len(lists) - 10} more_\n"
            msg += "\n"
        
        if long_forms:
            msg += "*📄 Long\\-Form Content:*\n"
            for term, source, _ in long_forms[:10]:
                msg += f"• {escape_markdown(term)} _{escape_markdown(source)}_\n"
            if len(long_forms) > 10:
                msg += f"_\\.\\.\\.and {len(long_forms) - 10} more_\n"
            msg += "\n"
        
        if notes:
            msg += "*🗒️ Notes:*\n"
            for term, source, _ in notes[:10]:
                msg += f"• {escape_markdown(term)} _{escape_markdown(source)}_\n"
            if len(notes) > 10:
                msg += f"_\\.\\.\\.and {len(notes) - 10} more_\n"
            msg += "\n"
        
        msg += "💡 Type any name to search for it\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in list_terms: {e}")
        await update.message.reply_text("❌ Error listing content", reply_markup=get_main_menu())

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
                "4\\. Post any content \\- I'll save it automatically\\!"
            )
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        msg = f"📺 *Active Channels* \\({len(channels)}\\)\n\n"
        
        for i, (channel_id, channel_name, term_count) in enumerate(channels, 1):
            msg += f"*{i}\\. {escape_markdown(channel_name)}*\n"
            msg += f"   📚 {term_count} item{'s' if term_count != 1 else ''}\n"
            msg += f"   🆔 `{channel_id}`\n\n"
        
        msg += "💡 Add me to more channels to expand your knowledge base\\!"
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in show_channels: {e}")
        await update.message.reply_text("❌ Error showing channels", reply_markup=get_main_menu())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overall statistics"""
    try:
        total_entries = 0
        total_channels = 0
        content_types = {"definition": 0, "list": 0, "long_form": 0, "note": 0}
        
        default_knowledge = load_knowledge()
        if default_knowledge:
            total_entries += len(default_knowledge)
            for data in default_knowledge.values():
                ct = data.get("content_type", "note")
                content_types[ct] = content_types.get(ct, 0) + 1
        
        knowledge_dir = Path("knowledge_bases")
        if knowledge_dir.exists():
            channel_files = list(knowledge_dir.glob("knowledge_*.json"))
            total_channels = len(channel_files)
            
            for kb_file in channel_files:
                try:
                    channel_id = int(kb_file.stem.split("_")[1])
                    knowledge = load_knowledge(channel_id)
                    
                    total_entries += len(knowledge)
                    for data in knowledge.values():
                        ct = data.get("content_type", "note")
                        content_types[ct] = content_types.get(ct, 0) + 1
                except Exception as e:
                    logger.error(f"Error processing {kb_file}: {e}")
        
        msg = "📊 *Knowledge Base Statistics*\n\n"
        msg += f"📺 Active Channels: *{total_channels}*\n"
        msg += f"📚 Total Saved Entries: *{total_entries}*\n"
        msg += f"\n*Content Breakdown:*\n"
        msg += f"📝 Definitions: *{content_types.get('definition', 0)}*\n"
        msg += f"📋 Lists: *{content_types.get('list', 0)}*\n"
        msg += f"📄 Long Form: *{content_types.get('long_form', 0)}*\n"
        msg += f"🗒️ Notes: *{content_types.get('note', 0)}*\n"
        
        msg += f"\n💡 Keep learning\\! Your knowledge base is growing\\."
        
        await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        
    except Exception as e:
        logger.error(f"Error in stats: {e}")
        await update.message.reply_text("❌ Error getting statistics", reply_markup=get_main_menu())

async def handle_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically extract content from channel messages - preserves formatting"""
    try:
        if not update.channel_post:
            return
        
        channel_id = update.channel_post.chat.id
        channel_name = update.channel_post.chat.title or update.channel_post.chat.username or f"Channel {channel_id}"
        
        text = update.channel_post.text or update.channel_post.caption
        
        if not text:
            return
        
        # Create a smart entry
        entry = create_smart_entry(text, channel_name)
        
        if entry["original_term"] == "Untitled Note" and entry["content_type"] == "note":
             # Ignore generic untitled notes unless they are very specific definitions
             # For a channel, we'll try to save everything that's not a clear definition if it's long enough
             if len(text.split()) < 10:
                 return # Too short, likely a fragment or chat
        
        main_topic_norm = normalize_term(entry["original_term"])
        
        knowledge = load_knowledge(channel_id)
        
        if main_topic_norm in knowledge:
            # For simplicity in the merged bot, if an entry exists, we just update/add content 
            # (treating it like adding an alternative definition or a related note)
            # This is a simplification; a full bot would handle multiple *distinct* entries better.
            
            # Since the smart entry format is more generalized, let's prioritize the most relevant fields
            
            # If it's a new definition for an existing entry (term)
            if entry["content_type"] == "definition" and knowledge[main_topic_norm].get("content_type") == "definition":
                 if "definitions" not in knowledge[main_topic_norm]:
                    old_def = knowledge[main_topic_norm].pop("definition", "")
                    if old_def:
                        knowledge[main_topic_norm]["definitions"] = [{"text": old_def, "added": knowledge[main_topic_norm].get("added", "")}]
                 
                 knowledge[main_topic_norm]["definitions"].append({
                     "text": entry["definition"],
                     "added": str(update.channel_post.date),
                     "channel": channel_name
                 })
                 logger.info(f"[{channel_name}] Added definition #{len(knowledge[main_topic_norm].get('definitions', []))} for term: {entry['original_term']}")
            
            # If it's a different type of content, or if the original was not a definition
            else:
                # We'll just update the entry with the new content, keywords, etc., as it's the most recent data
                entry["added"] = str(update.channel_post.date)
                knowledge[main_topic_norm] = entry
                logger.info(f"[{channel_name}] Updated existing entry: {entry['original_term']}")

        else:
            entry["added"] = str(update.channel_post.date)
            knowledge[main_topic_norm] = entry
            logger.info(f"[{channel_name}] Auto-learned new content: {entry['original_term']} ({entry['content_type']})")
        
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
            msg = "🔍 Just type the term or keyword you're looking for\\!\n\nI'll search across all your saved content\\."
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
        
        elif text == "📝 Save Note":
            await save_note(update, context)
            return
        
        elif text == "🗑️ Delete":
            await delete_entry(update, context)
            return
        
        elif text == "ℹ️ Help":
            await help_command(update, context)
            return
        
        # Handle state-based inputs
        if user_state == "awaiting_note":
            # User is adding content
            entry = create_smart_entry(text, "Manual")
            term_norm = normalize_term(entry["original_term"])
            
            # Basic validation for content
            if entry["original_term"] in ["Untitled Note", "Untitled note"] and entry["content_type"] in ["note", "long_form"]:
                 msg = (
                    "❌ I couldn't automatically detect a topic\\.\n\n"
                    "Please start your note with a clear title or use the `Term \\- Definition` format\\.\n\n"
                    "*Example:* `My Key Concept: This is a very important idea...`"
                )
                 await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                 return
            
            knowledge = load_knowledge()
            entry["added"] = str(update.message.date)
            
            # Check for existing entry
            if term_norm in knowledge and entry["content_type"] == "definition":
                # Handle adding a new definition to an existing term
                existing_entry = knowledge[term_norm]
                if existing_entry.get("content_type") == "definition":
                     if "definitions" not in existing_entry:
                        old_def = existing_entry.pop("definition", "")
                        if old_def:
                            existing_entry["definitions"] = [{"text": old_def, "added": existing_entry.get("added", ""), "source": existing_entry.get("source", "manual")}]
                     
                     existing_entry["definitions"].append({
                         "text": entry["definition"],
                         "added": str(update.message.date),
                         "source": "manual"
                     })
                     msg = f"✅ *Added Another Definition\\!*\n\n"
                     msg += f"📚 *{escape_markdown(entry['original_term'])}*\n"
                     msg += f"📊 Now has {len(existing_entry.get('definitions', []))} definitions"
                else:
                    # Overwrite/update if the new content is a definition but the old was a note
                    knowledge[term_norm] = entry
                    msg = f"✅ *Content Updated Successfully\\!*\n\n"
                    msg += f"📚 *{escape_markdown(entry['original_term'])}* \\(Definition\\)\n"
                    msg += f"Summary: {escape_markdown(entry.get('definition', entry.get('content', '')[:100]).replace('\n', '...'), preserve_code=True)}"
            else:
                # Add a brand new entry or overwrite an existing one of a different type
                knowledge[term_norm] = entry
                msg = f"✅ *Content Saved Successfully\\!*\n\n"
                msg += f"📚 *{escape_markdown(entry['original_term'])}* \\({entry['content_type'].replace('_', ' ').title()}\\)\n"
                msg += f"Summary: {escape_markdown(entry.get('definition', entry.get('content', '')[:100]).replace('\n', '...'), preserve_code=True)}"

            
            save_knowledge(knowledge)
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"Manual save - content: {entry['original_term']} ({entry['content_type']})")
            return
        
        elif user_state == "awaiting_delete":
            # User is deleting a term
            term = text
            term_norm = normalize_term(term)
            deleted_from = []
            
            # Search and delete from manual knowledge base
            default_knowledge = load_knowledge()
            if term_norm in default_knowledge:
                original = default_knowledge[term_norm].get("original_term", term)
                del default_knowledge[term_norm]
                save_knowledge(default_knowledge)
                deleted_from.append("Manual")
            
            # Search and delete from channel knowledge bases
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
                msg = f"✅ *Entry Deleted\\!*\n\n"
                msg += f"🗑️ Removed '*{escape_markdown(term)}*' from:\n"
                msg += "\n".join(f"   • {escape_markdown(source)}" for source in deleted_from)
                logger.info(f"Deleted entry: {term} from {', '.join(deleted_from)}")
            else:
                msg = f"❌ *Entry Not Found*\n\n'{escape_markdown(term)}' doesn't exist in the knowledge base\\."
            
            user_states[user_id] = None
            await update.message.reply_text(msg, reply_markup=get_main_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        # If not in a special state and not a menu button, treat as search
        await search_content(update, context)
        
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
    
    logger.info("Starting Smart Study Bot (merged and enhanced)...")
    
    try:
        # Create application
        app = Application.builder().token(TOKEN).build()

        # Add command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("save", save_note))
        app.add_handler(CommandHandler("search", search_content))
        app.add_handler(CommandHandler("list", list_terms))
        app.add_handler(CommandHandler("channels", show_channels))
        app.add_handler(CommandHandler("delete", delete_entry))
        app.add_handler(CommandHandler("stats", stats))

        # Handle channel posts
        app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_message))
        
        # Handle direct messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))

        # Start the bot
        logger.info("Smart Study Bot started successfully")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise

if __name__ == "__main__":
    main()