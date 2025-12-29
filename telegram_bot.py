#!/usr/bin/env python3
"""
VibeTune Telegram Bot - Multi-Bot Server

Telegram bot server that can handle multiple bots simultaneously. Each bot uses its own
token and project configuration.

Setup:
1. Create bots in the frontend (Settings → Bots tab) and get tokens from BotFather
2. Install dependencies: pip install -r requirements.txt
3. Set DATABASE_URL environment variable (or it will use the default)
4. Run: python telegram_bot.py
   - The server will automatically fetch all active bots from the database
   - Each bot will use its associated project configuration (models, tools, etc.)

The bot server:
- Automatically discovers all active bots from the database
- Each bot uses its own token and project configuration
- Fetches tools from database per project and includes them in system prompt
- Saves all messages to database in correct order with source="bot"
- Handles tool calls properly (user message, tool_call, tool_response, natural language response)
- Exposes HTTP webhook endpoint (/sync) for immediate bot discovery when bots are created/updated
- Relies solely on webhook updates from the frontend (no polling)
"""

import os
import logging
import requests
import urllib3
import json
import re
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Optional, Dict, List, Any

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv not installed, skip .env loading
    pass
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from datetime import datetime
import uuid
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# Optional: Single bot token for backward compatibility
# If provided, only that bot will run. Otherwise, all active bots are loaded.
import sys
TELEGRAM_BOT_TOKEN = ""

# Database connection (REQUIRED)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ ERROR: DATABASE_URL environment variable is required")
    exit(1)

# Modal inference endpoint (REQUIRED)
MODAL_INFERENCE_URL = os.getenv("MODAL_INFERENCE_URL")
if not MODAL_INFERENCE_URL:
    print("❌ ERROR: MODAL_INFERENCE_URL environment variable is required")
    exit(1)

# Defaults (work out of the box, can be overridden with environment variables)
DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID", "")
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.8"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))
DEFAULT_TOP_P = float(os.getenv("DEFAULT_TOP_P", "0.95"))
DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", "You are a helpful assistant.")

# Web app API URL for reports and bot lookup (REQUIRED)
APP_URL = os.getenv("APP_URL")
if not APP_URL:
    print("❌ ERROR: APP_URL environment variable is required")
    exit(1)

# SSL verification (default: True for production, can disable for self-signed certs)
SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() == "true"

# Disable SSL warnings only if SSL verification is disabled
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Store bot info cache per token
bot_info_cache: Dict[str, Dict[str, Any]] = {}

# Global bot manager instance (set in main_async)
bot_manager_instance: Optional['BotManager'] = None

# Global requests session for connection pooling (reduces latency)
_http_session: Optional[requests.Session] = None

def get_http_session() -> requests.Session:
    """Get or create a global HTTP session for connection pooling."""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        # Configure connection pooling
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0,  # We handle retries ourselves if needed
        )
        _http_session.mount('http://', adapter)
        _http_session.mount('https://', adapter)
    return _http_session

# Webhook server port (configurable via env var)
# Railway uses PORT env var, otherwise fall back to BOT_WEBHOOK_PORT or default 8888
# The frontend uses BOT_SERVER_URL env var to reach this endpoint
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("BOT_WEBHOOK_PORT", "8888"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Store user preferences (model_id per user)
user_preferences: Dict[int, Dict[str, any]] = {}


def get_db_connection():
    """Get a database connection."""
    conn = psycopg2.connect(DATABASE_URL)
    # Ensure auth tokens table exists
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "TelegramAuthToken" (
                    id serial PRIMARY KEY,
                    "telegramUserId" integer NOT NULL,
                    "botUsername" text NOT NULL,
                    token text NOT NULL,
                    "updatedAt" timestamp DEFAULT now() NOT NULL,
                    UNIQUE("telegramUserId", "botUsername")
                )
            """)
            conn.commit()
    except Exception as e:
        logger.warning(f"⚠️  Could not ensure TelegramAuthToken table exists: {e}")
    return conn


def store_auth_token(telegram_user_id: int, bot_username: str, token: str) -> None:
    """Store or update auth token for a user and bot in the database."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO "TelegramAuthToken" ("telegramUserId", "botUsername", token, "updatedAt")
                VALUES (%s, %s, %s, %s)
                ON CONFLICT ("telegramUserId", "botUsername")
                DO UPDATE SET token = EXCLUDED.token, "updatedAt" = EXCLUDED."updatedAt"
            """, (telegram_user_id, bot_username, token, datetime.now()))
            conn.commit()
            logger.info(f"🔑 [AUTH] Token stored in database for user {telegram_user_id} and bot {bot_username}")
    except Exception as e:
        logger.error(f"❌ [AUTH] Error storing token: {e}")
        conn.rollback()
    finally:
        conn.close()


def get_auth_token(telegram_user_id: int, bot_username: str) -> Optional[str]:
    """Retrieve auth token for a user and bot from the database."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT token FROM "TelegramAuthToken"
                WHERE "telegramUserId" = %s AND "botUsername" = %s
            """, (telegram_user_id, bot_username))
            result = cur.fetchone()
            if result:
                logger.info(f"🔑 [AUTH] Retrieved token from database for user {telegram_user_id} and bot {bot_username}")
                return result["token"]
            else:
                logger.info(f"🔑 [AUTH] No token found in database for user {telegram_user_id} and bot {bot_username}")
                return None
    except Exception as e:
        logger.error(f"❌ [AUTH] Error retrieving token: {e}")
        return None
    finally:
        conn.close()


def get_user_preferences(user_id: int) -> Dict[str, any]:
    """Get user preferences or return defaults."""
    if user_id not in user_preferences:
        user_preferences[user_id] = {
            "model_id": DEFAULT_MODEL_ID,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
    return user_preferences[user_id]


def find_project_by_model_id(model_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Find a project that uses the given trainedModelId."""
    if not model_id:
        return None
    
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Search through all projects' config JSON for trainedModelId
            # Using JSONB operator to check if config->>'trainedModelId' equals model_id
            cur.execute(
                """
                SELECT id, name, description, config, "userId"
                FROM "Project"
                WHERE config->>'trainedModelId' = %s
                LIMIT 1
                """,
                (model_id,)
            )
            project = cur.fetchone()
            if project:
                logger.info(f"✅ Found project {project['id']} for model {model_id}")
                return dict(project)
            return None
    except Exception as e:
        logger.error(f"❌ Error finding project by model ID: {e}")
        return None
    finally:
        conn.close()


def get_project_system_prompt(project_id: str) -> Optional[str]:
    """Get system prompt from ProjectInstruction for a project."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT content
                FROM "ProjectInstruction"
                WHERE "projectId" = %s
                LIMIT 1
                """,
                (project_id,)
            )
            result = cur.fetchone()
            if result:
                return result["content"]
            return None
    except Exception as e:
        logger.error(f"❌ Error fetching system prompt: {e}")
        return None
    finally:
        conn.close()


def lookup_bot_by_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Look up bot information by token from the API.
    This replaces the old get_or_create_telegram_project function.
    
    Args:
        token: The Telegram bot token
    
    Returns:
        Bot info dict with project info, or None if not found
    """
    global bot_info_cache
    
    # Use cached info if available
    if token in bot_info_cache:
        return bot_info_cache[token]
    
    try:
        lookup_url = f"{APP_URL}/api/bots/lookup"
        logger.info(f"🔍 Looking up bot at: {lookup_url}")
        session = get_http_session()
        response = session.post(
            lookup_url,
            json={"token": token},
            headers={"Content-Type": "application/json"},
            timeout=(5, 10),  # (connect timeout, read timeout)
            verify=SSL_VERIFY,
        )
        
        logger.info(f"📡 Response status: {response.status_code}")
        if not response.ok:
            logger.error(f"❌ Failed to lookup bot: {response.status_code} - {response.text[:200]}")
            logger.error(f"❌ Full response headers: {dict(response.headers)}")
            return None
        
        data = response.json()
        bot_data = data.get("bot")
        
        if not bot_data:
            logger.error(f"❌ Bot not found in API response")
            return None
        
        # Cache the bot info
        bot_info = {
            "token": token,
            "botId": bot_data["id"],
            "username": bot_data["username"],
            "projectId": bot_data["projectId"],
            "project": bot_data["project"],
        }
        bot_info_cache[token] = bot_info
        
        logger.info(f"✅ Found bot @{bot_data['username']} for project {bot_data['projectId']}")
        return bot_info
        
    except Exception as e:
        logger.error(f"❌ Error looking up bot by token: {e}")
        return None


def fetch_all_active_bots() -> List[Dict[str, Any]]:
    """
    Fetch all active bots from the database.
    
    Returns:
        List of bot dicts with token, username, projectId, etc.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, username, token, "projectId", "isActive"
                FROM "TelegramBot"
                WHERE "isActive" = true
                ORDER BY "createdAt" DESC
                """
            )
            bots = cur.fetchall()
            logger.info(f"📋 Found {len(bots)} active bot(s) in database")
            return [dict(bot) for bot in bots]
    except Exception as e:
        logger.error(f"❌ Error fetching active bots: {e}")
        return []
    finally:
        conn.close()


def create_new_conversation(
    telegram_user_id: int, 
    bot_username: str, 
    project_id: str, 
    telegram_bot_id: str,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None
) -> str:
    """
    Create a new conversation for a Telegram user (always creates new, never reuses).
    Used for /clear and /report commands to start fresh conversations.
    
    Args:
        telegram_user_id: The Telegram user's numeric ID
        bot_username: The bot's username
        project_id: The project ID
        telegram_bot_id: The TelegramBot record ID
        telegram_username: The Telegram user's @username (optional)
        telegram_first_name: The Telegram user's first name (optional)
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Always create a new conversation
            conversation_id = str(uuid.uuid4())
            
            # Build a descriptive title with available user info
            # Add timestamp to ensure uniqueness (required by DB constraint)
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            title_parts = [f"Telegram: {telegram_user_id}"]
            if telegram_username:
                title_parts.append(f"@{telegram_username}")
            if telegram_first_name:
                title_parts.append(telegram_first_name)
            title_parts.append(f"(via @{bot_username})")
            # Add timestamp to make title unique
            base_title = " | ".join(title_parts[:3]) if len(title_parts) > 3 else " | ".join(title_parts)
            title = f"{base_title} - {timestamp_str}"
            cur.execute(
                """
                INSERT INTO "Conversation" (id, title, "projectId", source, "telegramBotId", "createdAt", "updatedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, title, project_id, "bot", telegram_bot_id, datetime.now(), datetime.now())
            )
            conn.commit()
            logger.info(f"📝 Created new bot conversation {conversation_id} for Telegram user {telegram_user_id}")
            return conversation_id
    except Exception as e:
        logger.error(f"❌ Error creating conversation: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def get_or_create_conversation(
    telegram_user_id: int, 
    bot_username: str, 
    project_id: str, 
    telegram_bot_id: str,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None
) -> str:
    """
    Get or create a conversation for a Telegram user, stored in database.
    Now tags conversations with source="bot" and telegramBotId.
    Reuses existing conversation if found, otherwise creates new.
    
    Args:
        telegram_user_id: The Telegram user's numeric ID
        bot_username: The bot's username
        project_id: The project ID
        telegram_bot_id: The TelegramBot record ID
        telegram_username: The Telegram user's @username (optional)
        telegram_first_name: The Telegram user's first name (optional)
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Try to find existing conversation for this user
            # We'll use a special title format: "Telegram: {userId} | @{username} | {firstName}"
            cur.execute(
                """
                SELECT id FROM "Conversation"
                WHERE "projectId" = %s
                AND "telegramBotId" = %s
                AND title LIKE %s
                ORDER BY "createdAt" DESC
                LIMIT 1
                """,
                (project_id, telegram_bot_id, f"Telegram: {telegram_user_id}%")
            )
            conversation = cur.fetchone()
            
            if conversation:
                return conversation["id"]
            
            # Create a new conversation with source="bot" and telegramBotId
            # Include Telegram user info in title for display
            conversation_id = str(uuid.uuid4())
            
            # Build a descriptive title with available user info
            title_parts = [f"Telegram: {telegram_user_id}"]
            if telegram_username:
                title_parts.append(f"@{telegram_username}")
            if telegram_first_name:
                title_parts.append(telegram_first_name)
            title_parts.append(f"(via @{bot_username})")
            title = " | ".join(title_parts[:3]) if len(title_parts) > 3 else " | ".join(title_parts)
            cur.execute(
                """
                INSERT INTO "Conversation" (id, title, "projectId", source, "telegramBotId", "createdAt", "updatedAt")
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, title, project_id, "bot", telegram_bot_id, datetime.now(), datetime.now())
            )
            conn.commit()
            logger.info(f"📝 Created new bot conversation {conversation_id} for Telegram user {telegram_user_id}")
            return conversation_id
    except Exception as e:
        logger.error(f"❌ Error creating conversation: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_tools_from_database(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch enabled tools from the database.
    If project_id is provided, only returns tools associated with that project.
    If project_id is not provided, returns all enabled tools.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if project_id:
                # Fetch tools associated with the specific project
                cur.execute(
                    """
                    SELECT DISTINCT t.id, t.name, t.description, t.icon, t.enabled, t.category, t."typeName", 
                           t.endpoint, t.method, t.parameters, t."responseSchema"
                    FROM "Tool" t
                    INNER JOIN "_ProjectToTool" pt ON t.id = pt."B"
                    WHERE t.enabled = true AND pt."A" = %s
                    ORDER BY t.name
                    """,
                    (project_id,)
                )
                logger.info(f"🔧 Fetching tools for project {project_id}...")
            else:
                # Fetch all enabled tools (backward compatibility)
                cur.execute(
                    """
                    SELECT id, name, description, icon, enabled, category, "typeName", endpoint, method, parameters, "responseSchema"
                    FROM "Tool"
                    WHERE enabled = true
                    ORDER BY name
                    """
                )
                logger.info(f"🔧 Fetching all enabled tools (no project filter)...")
            
            tools = cur.fetchall()
            logger.info(f"✅ Fetched {len(tools)} enabled tools from database" + (f" for project {project_id}" if project_id else ""))
            
            # Log details of each tool retrieved
            if tools:
                logger.info(f"📋 Tools retrieved:")
                for idx, tool in enumerate(tools, 1):
                    logger.info(f"   [{idx}] {tool['name']} (id: {tool['id']}, endpoint: {tool['endpoint']}, method: {tool.get('method', 'GET')})")
            else:
                logger.warning(f"⚠️  No tools found" + (f" for project {project_id}" if project_id else ""))
            
            return [dict(tool) for tool in tools]
    except Exception as e:
        logger.error(f"❌ Error fetching tools: {e}")
        return []
    finally:
        conn.close()


def format_parameter(key: str, param_def: Any, indent: str = "    ") -> str:
    """Recursively format a parameter definition, handling nested objects and arrays."""
    # Handle JSON string parameters
    param_info = param_def
    if isinstance(param_def, str):
        try:
            param_info = json.loads(param_def)
        except:
            return f"{indent}- {key}: {param_def}"
    
    if not isinstance(param_info, dict):
        return f"{indent}- {key}: {param_info}"
    
    param_type = param_info.get("type", "string")
    required = "(required)" if param_info.get("required") else "(optional)"
    description = param_info.get("description", "")
    default_val = f" [default: {param_info.get('default')}]" if param_info.get("default") else ""
    
    # Handle nested object with properties
    if param_type == "object" and param_info.get("properties"):
        props = "\n".join([
            format_parameter(prop_key, prop_value, indent + "  ")
            for prop_key, prop_value in param_info["properties"].items()
        ])
        return f"{indent}- {key}: object {required}{default_val} - {description}\n{props}"
    
    # Handle array with items schema
    if param_type == "array" and param_info.get("items"):
        items = param_info["items"]
        if isinstance(items, dict) and items.get("type") == "object" and items.get("properties"):
            items_desc = f"object with properties:\n" + "\n".join([
                format_parameter(item_key, item_value, indent + "    ")
                for item_key, item_value in items["properties"].items()
            ])
        else:
            items_desc = items.get("type", "any") if isinstance(items, dict) else str(items)
        return f"{indent}- {key}: array {required}{default_val} - {description}\n{indent}  Each item: {items_desc}"
    
    # Simple parameter
    return f"{indent}- {key}: {param_type} {required}{default_val} - {description}"


def format_tools_for_system_prompt(tools: List[Dict[str, Any]]) -> str:
    """Format tools into a system prompt section that the model can understand."""
    if not tools:
        return ""
    
    tool_names = ", ".join([f'"{tool["name"]}"' for tool in tools])
    
    tool_descriptions = "\n".join([
        f"""
Tool Name (use EXACTLY this in tool calls): "{tool["name"]}"
Description: {tool["description"]}
Parameters:
{chr(10).join([format_parameter(key, value) for key, value in (tool.get("parameters") or {}).items()])}"""
        for tool in tools
    ])
    
    return f"""
## AVAILABLE TOOLS

You have access to the following tools. When a user request requires external data or actions, you MUST use the appropriate tool.

{tool_descriptions}

## TOOL USAGE FORMAT

When you need to use a tool, output it in this EXACT format:

<tool_call>{{"name": "tool_name", "arguments": {{"param1": "value1", "param2": "value2"}}}}</tool_call>

⚠️ **CRITICAL: You MUST include BOTH the opening <tool_call> tag AND the closing </tool_call> tag. The closing tag is REQUIRED.**

CRITICAL RULES:
1. **ONLY use tools from the AVAILABLE TOOLS list above** - NEVER invent or use tool names that are not listed
2. Use <tool_call> tags for ALL external data requests or actions
3. **ALWAYS close your tool call with </tool_call> - this is MANDATORY**
4. **NEVER generate <tool_response> tags** - these are ONLY generated by the system AFTER your tool calls complete. You can ONLY generate <tool_call> tags, never <tool_response> tags.
5. **NEVER fabricate or guess tool results** - you MUST make the actual tool call first and wait for the real response from the system
6. After the system provides <tool_response> (which you will receive, not generate), provide a natural language summary of the results
7. If a tool returns an error or empty results, acknowledge it honestly
8. **ONLY include arguments that the user EXPLICITLY mentioned** - do NOT guess or add fields the user didn't specify
9. All arguments are OPTIONAL - only include what the user actually asked for
10. **For nested objects and arrays**: Extract the data from the user's request and structure it according to the parameter schema. For example:
   - If a parameter is an object with properties like "name" and "location_description", extract those fields from the user's message
   - If a parameter is an array of objects, create an array with objects matching the item schema
   - Always match the exact structure shown in the parameter definition
   - Pay attention to nested JSON structures - extract and format them correctly

Available tool names (use EXACTLY this name in tool calls - copy it character by character):
{tool_names}

CRITICAL: The tool name in your tool call MUST match one of the names above EXACTLY. Do NOT modify, normalize, or change the tool name in any way.

## EXAMPLE: NESTED JSON EXTRACTION

When a user provides natural language that needs to be structured as nested JSON, extract and format it correctly:

**User Request:**
"create delivery from Santasi Roundabout, Kumasi, Ghana under the name Kobi Long to Kotei, Kumasi, Ghana and the recipient is Theresa Achiamaa"

**Correct Tool Call (NOTE: Must include closing </tool_call> tag):**
<tool_call>{{"name": "create_delivery", "arguments": {{
  "sender": {{
    "name": "Kobi Long",
    "location_description": "Santasi Roundabout, Kumasi, Ghana"
  }},
  "recipients": [
    {{
      "name": "Theresa Achiamaa",
      "location_description": "Kotei, Kumasi, Ghana"
    }}
  ]
}}}}</tool_call>

**Key Points:**
- Extract "Kobi Long" → sender.name
- Extract "Santasi Roundabout, Kumasi, Ghana" → sender.location_description
- Extract "Theresa Achiamaa" → recipients[0].name
- Extract "Kotei, Kumasi, Ghana" → recipients[0].location_description
- Structure matches the parameter schema exactly (nested object for sender, array of objects for recipients)

**IMPORTANT:** Always match the exact structure shown in the parameter definition above. If a parameter is an object, create an object. If it's an array, create an array.

## ⚠️ CRITICAL REMINDER

**YOU MUST USE <tool_call> TAGS TO CALL TOOLS. NEVER GENERATE <tool_response> TAGS - THOSE ARE ONLY CREATED BY THE SYSTEM AFTER YOUR TOOL CALLS EXECUTE.**

When the user asks you to verify an OTP code, you MUST call the verify_otp tool using <tool_call> tags with the code and phone_number from the user's message or conversation history. Do NOT fabricate a response - make the actual tool call first.
"""


def fetch_conversation_history(conversation_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch the last N messages from a conversation, excluding tool calls and tool responses.
    Returns a list of messages with 'role' and 'content' fields.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content
                FROM "Message"
                WHERE "conversationId" = %s
                  AND "isToolCall" = false
                  AND "isToolResponse" = false
                ORDER BY "createdAt" DESC
                LIMIT %s
                """,
                (conversation_id, limit)
            )
            rows = cur.fetchall()
            # Reverse to get chronological order (oldest first)
            messages = [{"role": row[0], "content": row[1]} for row in reversed(rows)]
            logger.info(f"📚 Fetched {len(messages)} messages from conversation history (excluding tool calls/responses)")
            return messages
    except Exception as e:
        logger.error(f"❌ Error fetching conversation history: {e}")
        return []
    finally:
        conn.close()


def build_prompt(system_prompt: str, user_message: str, conversation_history: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Build a prompt in the same format as the frontend.
    Uses the chat template: <|im_start|>system, <|im_start|>user, <|im_start|>assistant
    
    Args:
        system_prompt: The system prompt
        user_message: The current user message
        conversation_history: Optional list of previous messages (excluding the current one)
                             Each message should have 'role' ('user' or 'assistant') and 'content'
    """
    prompt = f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
    
    # Add conversation history if provided (excluding the current user message)
    if conversation_history:
        logger.info(f"📚 [build_prompt] Including {len(conversation_history)} message(s) from conversation history:")
        for idx, msg in enumerate(conversation_history, 1):
            role = msg.get("role", "").lower()
            content = msg.get("content", "")
            if role in ["user", "assistant"] and content:
                # Log a preview of each message
                content_preview = content[:100] + "..." if len(content) > 100 else content
                logger.info(f"   {idx}. [{role.upper()}] {content_preview}")
                prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"
    else:
        logger.info(f"📚 [build_prompt] No conversation history provided - model will only see current message")
    
    # Add current user message
    prompt += f"<|im_start|>user\n{user_message}<|im_end|>\n"
    prompt += "<|im_start|>assistant\n"
    logger.info(f"📝 [build_prompt] Current user message: {user_message[:100]}{'...' if len(user_message) > 100 else ''}")
    return prompt


def contains_tool_call(response_text: str) -> bool:
    """Check if response contains a tool call."""
    # Check for opening tag, closing tag, or tool_calls pattern
    has_opening_tag = "<tool_call>" in response_text
    has_closing_tag = "</tool_call>" in response_text
    has_tool_calls_pattern = '"tool_calls"' in response_text and '"name"' in response_text
    # Also check if response looks like a tool call (JSON with "name" and "arguments" followed by </tool_call>)
    looks_like_tool_call = ('"name"' in response_text and '"arguments"' in response_text and has_closing_tag)
    
    return has_opening_tag or has_closing_tag or has_tool_calls_pattern or looks_like_tool_call


def parse_tool_calls(response_text: str) -> List[Dict[str, Any]]:
    """Parse tool calls from model response."""
    logger.info(f"🔍 [parse_tool_calls] Starting to parse tool calls")
    logger.info(f"📄 [parse_tool_calls] Response text length: {len(response_text)}")
    logger.info(f"📄 [parse_tool_calls] Response preview (first 300 chars): {response_text[:300]}")
    
    tool_calls = []
    
    # Match <tool_call>...</tool_call> format (with closing tag)
    logger.info(f"🔍 [parse_tool_calls] Searching for <tool_call> tags...")
    tool_call_matches = re.findall(r"<tool_call>([\s\S]*?)</tool_call>", response_text)
    logger.info(f"🔍 [parse_tool_calls] Found {len(tool_call_matches)} complete <tool_call> tag(s) with closing tag")
    
    # If no complete tags found, try to find unclosed <tool_call> tags
    if not tool_call_matches and "<tool_call>" in response_text:
        logger.warning(f"⚠️  [parse_tool_calls] Found <tool_call> but no closing </tool_call> tag - attempting to extract anyway")
        # Find all <tool_call> positions
        open_tag_positions = [m.start() for m in re.finditer(r"<tool_call>", response_text)]
        for pos in open_tag_positions:
            # Extract everything after <tool_call> until end of response
            content_start = pos + len("<tool_call>")
            potential_content = response_text[content_start:].strip()
            logger.info(f"📝 [parse_tool_calls] Found unclosed tag at position {pos}, content: {potential_content[:200]}...")
            # Add as a match to process
            tool_call_matches.append(potential_content)
    
    # If still no matches but we have a closing tag without opening tag (model forgot opening tag)
    if not tool_call_matches and "</tool_call>" in response_text:
        logger.warning(f"⚠️  [parse_tool_calls] Found </tool_call> closing tag but no opening <tool_call> tag - attempting to extract anyway")
        # Find the closing tag position
        closing_tag_match = re.search(r"</tool_call>", response_text)
        if closing_tag_match:
            # Extract everything from the start until the closing tag
            potential_content = response_text[:closing_tag_match.start()].strip()
            logger.info(f"📝 [parse_tool_calls] Found closing tag without opening, content: {potential_content[:200]}...")
            tool_call_matches.append(potential_content)
    
    logger.info(f"🔍 [parse_tool_calls] Total matches to process: {len(tool_call_matches)}")
    
    for idx, match in enumerate(tool_call_matches):
        logger.info(f"🔍 [parse_tool_calls] Processing tool call #{idx + 1}")
        logger.info(f"📝 [parse_tool_calls] Raw content inside <tool_call> tags: {match[:200]}...")
        
        # Extract JSON from the match - handle cases where there's extra text after JSON
        json_content = match.strip()
        
        # Try to find and extract just the JSON object/array
        # Look for the first { or [ and find its matching closing bracket
        json_start = -1
        
        for i, char in enumerate(json_content):
            if char == '{':
                json_start = i
                break
            elif char == '[':
                json_start = i
                break
        
        if json_start >= 0:
            # Find the matching closing bracket using bracket counting
            # This handles nested objects/arrays correctly
            bracket_count = 0
            in_string = False
            escape_next = False
            json_end = -1
            
            for i in range(json_start, len(json_content)):
                char = json_content[i]
                
                if escape_next:
                    escape_next = False
                    continue
                
                if char == '\\':
                    escape_next = True
                    continue
                
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                
                if not in_string:
                    if char == '{' or char == '[':
                        bracket_count += 1
                    elif char == '}' or char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            json_end = i + 1
                            break
            
            if json_end > json_start:
                json_content = json_content[json_start:json_end]
                logger.info(f"📝 [parse_tool_calls] Extracted JSON (removed extra text): {json_content[:200]}...")
            else:
                logger.warning(f"⚠️  [parse_tool_calls] Could not find matching closing bracket, using full content")
        else:
            logger.warning(f"⚠️  [parse_tool_calls] No JSON object/array found, using full content as-is")
        
        try:
            parsed = json.loads(json_content)
            logger.info(f"✅ [parse_tool_calls] Successfully parsed JSON: {json.dumps(parsed, indent=2)}")
            
            # Handle array format
            if isinstance(parsed, list):
                logger.info(f"📋 [parse_tool_calls] Detected array format with {len(parsed)} items")
                for tc_idx, tc in enumerate(parsed):
                    if tc.get("name"):
                        raw_args = tc.get("arguments") or tc.get("parameters") or {}
                        logger.info(f"   [{tc_idx + 1}] Tool: \"{tc['name']}\"")
                        logger.info(f"   [{tc_idx + 1}] Raw arguments: {json.dumps(raw_args, indent=2)}")
                        tool_calls.append({
                            "name": tc["name"],
                            "arguments": raw_args
                        })
            # Handle object format
            elif parsed.get("name"):
                logger.info(f"📋 [parse_tool_calls] Detected object format")
                logger.info(f"   Tool Name: \"{parsed['name']}\"")
                raw_args = parsed.get("arguments") or parsed.get("parameters") or {}
                logger.info(f"   Raw arguments: {json.dumps(raw_args, indent=2)}")
                tool_calls.append({
                    "name": parsed["name"],
                    "arguments": raw_args
                })
            # Handle nested tool_calls format
            elif parsed.get("tool_calls") and isinstance(parsed["tool_calls"], list):
                logger.info(f"📋 [parse_tool_calls] Detected nested tool_calls format with {len(parsed['tool_calls'])} items")
                for tc_idx, tc in enumerate(parsed["tool_calls"]):
                    if tc.get("name"):
                        raw_args = tc.get("arguments") or tc.get("parameters") or {}
                        logger.info(f"   [{tc_idx + 1}] Tool: \"{tc['name']}\"")
                        logger.info(f"   [{tc_idx + 1}] Raw arguments: {json.dumps(raw_args, indent=2)}")
                        tool_calls.append({
                            "name": tc["name"],
                            "arguments": raw_args
                        })
        except Exception as e:
            logger.warning(f"❌ [parse_tool_calls] Failed to parse tool call #{idx + 1}")
            logger.warning(f"   Full match content: {match}")
            logger.warning(f"   Extracted JSON content: {json_content}")
            logger.warning(f"   Error: {e}")
            # Try to continue with other tool calls even if one fails
            continue
    
    logger.info(f"✅ [parse_tool_calls] Parsing complete. Found {len(tool_calls)} tool call(s)")
    if tool_calls:
        for idx, tc in enumerate(tool_calls):
            logger.info(f"   [{idx + 1}] Tool: \"{tc['name']}\" with {len(tc.get('arguments', {}))} argument(s)")
    
    return tool_calls


def normalize_tool_arguments(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize tool arguments, parsing JSON strings recursively."""
    logger.info(f"🔍 [normalize_tool_arguments] Normalizing arguments for tool: \"{tool_name}\"")
    logger.info(f"📥 [normalize_tool_arguments] Input arguments: {json.dumps(args, indent=2)}")
    
    def try_parse_json(value):
        if not isinstance(value, str):
            return value
        try:
            trimmed = value.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or \
               (trimmed.startswith("[") and trimmed.endswith("]")):
                parsed = json.loads(trimmed)
                logger.info(f"   ✅ Parsed JSON string: {value[:50]}... -> {type(parsed).__name__}")
                return parsed
        except Exception as e:
            logger.debug(f"   ⚠️  Could not parse as JSON: {value[:50]}... ({e})")
        return value
    
    normalized = {}
    for key, value in args.items():
        if value is None or value == "":
            logger.debug(f"   ⏭️  Skipping empty value for key: {key}")
            continue
        
        logger.info(f"   🔍 Processing argument: {key} = {json.dumps(value) if not isinstance(value, str) or len(value) < 100 else value[:100] + '...'}")
        parsed_value = try_parse_json(value)
        
        if isinstance(parsed_value, dict):
            logger.info(f"   📦 Detected nested object for {key}, parsing recursively...")
            nested = {k: try_parse_json(v) for k, v in parsed_value.items() if v is not None and v != ""}
            normalized[key] = nested
            logger.info(f"   ✅ Normalized nested object: {json.dumps(nested, indent=2)}")
        elif isinstance(parsed_value, list):
            logger.info(f"   📦 Detected array for {key} with {len(parsed_value)} items, parsing items...")
            nested = [try_parse_json(item) for item in parsed_value if item is not None and item != ""]
            normalized[key] = nested
            logger.info(f"   ✅ Normalized array: {json.dumps(nested, indent=2)}")
        else:
            normalized[key] = parsed_value
            logger.info(f"   ✅ Kept as-is: {key} = {parsed_value}")
    
    logger.info(f"✅ [normalize_tool_arguments] Normalization complete for \"{tool_name}\"")
    logger.info(f"📤 [normalize_tool_arguments] Output arguments: {json.dumps(normalized, indent=2)}")
    return normalized


def execute_tool_call(tool_call: Dict[str, Any], tools: List[Dict[str, Any]], telegram_user_id: Optional[int] = None, auth_token: Optional[str] = None) -> Dict[str, Any]:
    """Execute a tool call and return the result."""
    logger.info(f"🔧 [execute_tool_call] ========== Starting tool execution ==========")
    logger.info(f"🔧 [execute_tool_call] Tool: \"{tool_call['name']}\"")
    logger.info(f"📋 [execute_tool_call] Available tools: {[t['name'] for t in tools]}")
    if telegram_user_id is not None:
        logger.info(f"👤 [execute_tool_call] Telegram user ID: {telegram_user_id}")
    if auth_token:
        logger.info(f"🔑 [execute_tool_call] Authentication token provided (length: {len(auth_token)})")
    else:
        logger.info(f"🔑 [execute_tool_call] No authentication token - request may require auth")
    
    # Find the tool definition
    logger.info(f"🔍 [execute_tool_call] Searching for tool definition...")
    tool = None
    for t in tools:
        if t["name"] == tool_call["name"] or t["typeName"] == tool_call["name"]:
            tool = t
            logger.info(f"✅ [execute_tool_call] Found tool definition!")
            break
    
    if not tool:
        logger.error(f"❌ [execute_tool_call] Tool \"{tool_call['name']}\" not found in available tools")
        return {
            "success": False,
            "data": None,
            "error": f'Tool "{tool_call["name"]}" not found'
        }
    
    logger.info(f"📋 [execute_tool_call] Tool definition:")
    logger.info(f"   - Name: {tool['name']}")
    logger.info(f"   - Endpoint: {tool['endpoint']}")
    logger.info(f"   - Method: {tool.get('method', 'GET')}")
    logger.info(f"   - Description: {tool.get('description', 'N/A')}")
    
    try:
        method = (tool.get("method") or "GET").upper()
        logger.info(f"🔍 [execute_tool_call] Using HTTP method: {method}")
        
        # Arguments should already be normalized, but log them
        logger.info(f"📥 [execute_tool_call] Tool call arguments: {json.dumps(tool_call.get('arguments', {}), indent=2)}")
        normalized_args = tool_call.get("arguments", {})
        
        # Build params
        logger.info(f"🔍 [execute_tool_call] Building request parameters...")
        params = {}
        for key, value in normalized_args.items():
            if value is not None and value != "":
                if isinstance(value, list) and method == "GET":
                    params[key] = ",".join(str(v) for v in value)
                    logger.info(f"   - {key}: array -> joined string for GET: {params[key]}")
                else:
                    params[key] = value
                    logger.info(f"   - {key}: {json.dumps(value) if not isinstance(value, str) or len(str(value)) < 100 else str(value)[:100] + '...'}")
        
        # Always include Telegram user ID if provided
        if telegram_user_id is not None:
            params["telegramUserId"] = telegram_user_id
            logger.info(f"   - telegramUserId: {telegram_user_id}")
        
        logger.info(f"📤 [execute_tool_call] Final params for API call: {json.dumps(params, indent=2)}")
        
        # Prepare headers
        headers = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            logger.info(f"🔑 [execute_tool_call] Adding Authorization header with Bearer token")
        
        # Make the API call
        session = get_http_session()
        if method == "GET":
            url_params = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
            url = f"{tool['endpoint']}?{url_params}" if url_params else tool["endpoint"]
            logger.info(f"🌐 [execute_tool_call] Making GET request to: {url}")
            logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
            response = session.get(url, headers=headers, timeout=(5, 30))  # (connect timeout, read timeout)
        elif method == "POST":
            logger.info(f"🌐 [execute_tool_call] Making POST request to: {tool['endpoint']}")
            logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
            logger.info(f"📦 [execute_tool_call] Request body: {json.dumps(params, indent=2)}")
            response = session.post(
                tool["endpoint"],
                json=params,
                headers=headers,
                timeout=(5, 150)  # (connect timeout, read timeout)
            )
        elif method == "PUT":
            logger.info(f"🌐 [execute_tool_call] Making PUT request to: {tool['endpoint']}")
            logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
            logger.info(f"📦 [execute_tool_call] Request body: {json.dumps(params, indent=2)}")
            response = session.put(
                tool["endpoint"],
                json=params,
                headers=headers,
                timeout=(5, 150)
            )
        elif method == "PATCH":
            logger.info(f"🌐 [execute_tool_call] Making PATCH request to: {tool['endpoint']}")
            logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
            logger.info(f"📦 [execute_tool_call] Request body: {json.dumps(params, indent=2)}")
            response = session.patch(
                tool["endpoint"],
                json=params,
                headers=headers,
                timeout=(5, 150)
            )
        elif method == "DELETE":
            # For DELETE, params can be in query string or body depending on API design
            # We'll support both: query params if it's a simple request, body if params are complex
            if params and len(params) > 0:
                # Check if we should use query params (simple key-value pairs) or body
                has_complex_values = any(isinstance(v, (dict, list)) for v in params.values())
                if has_complex_values:
                    # Use body for complex data
                    logger.info(f"🌐 [execute_tool_call] Making DELETE request to: {tool['endpoint']} (with body)")
                    logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
                    logger.info(f"📦 [execute_tool_call] Request body: {json.dumps(params, indent=2)}")
                    response = session.delete(
                        tool["endpoint"],
                        json=params,
                        headers=headers,
                        timeout=(5, 150)
                    )
                else:
                    # Use query params for simple data
                    url_params = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
                    url = f"{tool['endpoint']}?{url_params}" if url_params else tool["endpoint"]
                    logger.info(f"🌐 [execute_tool_call] Making DELETE request to: {url}")
                    logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
                    response = session.delete(url, headers=headers, timeout=(5, 150))
            else:
                # No params, just delete the endpoint
                logger.info(f"🌐 [execute_tool_call] Making DELETE request to: {tool['endpoint']}")
                logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
                response = session.delete(tool["endpoint"], headers=headers, timeout=(5, 150))
        else:
            logger.warning(f"⚠️  [execute_tool_call] Unsupported HTTP method: {method}, defaulting to POST")
            logger.info(f"🌐 [execute_tool_call] Making POST request to: {tool['endpoint']}")
            logger.info(f"🌐 [execute_tool_call] Request headers: {headers}")
            logger.info(f"📦 [execute_tool_call] Request body: {json.dumps(params, indent=2)}")
            response = session.post(
                tool["endpoint"],
                json=params,
                headers=headers,
                timeout=(5, 150)
            )
        
        logger.info(f"📡 [execute_tool_call] Response status: {response.status_code} {response.reason}")
        logger.info(f"📡 [execute_tool_call] Response headers: {dict(response.headers)}")
        
        # Check for authentication errors
        if response.status_code == 401:
            try:
                error_data = response.json() if response.text else {}
                error_message = error_data.get("error", response.text)
                logger.warning(f"🔒 [execute_tool_call] Authentication required (401) for tool: {tool_call['name']}")
                logger.warning(f"🔒 [execute_tool_call] Error message: {error_message}")
                logger.info(f"🔒 [execute_tool_call] User needs to authenticate before using this tool")
            except:
                logger.warning(f"🔒 [execute_tool_call] Authentication required (401) for tool: {tool_call['name']}")
        
        if not response.ok:
            error_text = response.text
            logger.error(f"❌ [execute_tool_call] API error: {response.status_code} - {error_text}")
            
            # Try to parse error message from JSON response
            error_data = {}
            actual_error_message = None
            try:
                if error_text:
                    error_data = json.loads(error_text)
                    # Extract error message from various possible fields
                    actual_error_message = (
                        error_data.get("error") or 
                        error_data.get("message") or 
                        error_data.get("detail") or
                        error_text
                    )
            except:
                # If not JSON, use the text as-is
                actual_error_message = error_text
            
            return {
                "success": False,
                "data": {"error": actual_error_message or f"API error: {response.status_code}"},
                "error": actual_error_message or error_text
            }
        
        # Parse response
        content_type = response.headers.get("content-type", "")
        logger.info(f"📥 [execute_tool_call] Response content-type: {content_type}")
        
        if content_type.startswith("application/json"):
            data = response.json()
            logger.info(f"✅ [execute_tool_call] Parsed JSON response: {json.dumps(data, indent=2)}")
        else:
            data = {"result": response.text}
            logger.info(f"✅ [execute_tool_call] Text response (first 500 chars): {response.text[:500]}")
        
        logger.info(f"✅ [execute_tool_call] Tool execution successful: {tool_call['name']}")
        logger.info(f"✅ [execute_tool_call] ========== Tool execution complete ==========")
        return {"success": True, "data": data}
        
    except Exception as e:
        logger.error(f"❌ [execute_tool_call] Error executing tool {tool_call['name']}: {e}", exc_info=True)
        logger.error(f"❌ [execute_tool_call] ========== Tool execution failed ==========")
        return {
            "success": False,
            "data": None,
            "error": str(e)
        }


def format_tool_call_for_training(tool_call: Dict[str, Any]) -> str:
    """Format tool call with tags for training."""
    return f'<tool_call>{json.dumps({"name": tool_call["name"], "arguments": tool_call.get("arguments", {})})}</tool_call>'


def format_tool_response(tool_name: str, result: Any) -> str:
    """Format tool response with tags."""
    return f"<tool_response>{json.dumps(result)}</tool_response>"


def save_message_to_db(
    conversation_id: str,
    role: str,
    content: str,
    bot_username: Optional[str] = None,
    is_tool_call: bool = False,
    is_tool_response: bool = False,
    tool_calls: Optional[Dict] = None,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None
) -> Optional[str]:
    """Save a message to the database."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "Message" 
                (id, content, role, "conversationId", "createdAt", "updatedAt", "isToolCall", "isToolResponse", "toolCalls", "tokensInput", "tokensOutput", "botUsername")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(uuid.uuid4()),
                    content,
                    role,
                    conversation_id,
                    datetime.now(),
                    datetime.now(),
                    is_tool_call,
                    is_tool_response,
                    json.dumps(tool_calls) if tool_calls else None,
                    tokens_input,
                    tokens_output,
                    bot_username
                )
            )
            message_id = cur.fetchone()[0]
            conn.commit()
            logger.info(f"💾 Saved {role} message to database (isToolCall={is_tool_call}, isToolResponse={is_tool_response}, botUsername={bot_username})")
            return message_id
    except Exception as e:
        logger.error(f"❌ Error saving message to database:")
        logger.error(f"   - Role: {role}")
        logger.error(f"   - Content length: {len(content)} chars")
        logger.error(f"   - Conversation ID: {conversation_id}")
        logger.error(f"   - Bot Username: {bot_username}")
        logger.error(f"   - Is Tool Call: {is_tool_call}")
        logger.error(f"   - Is Tool Response: {is_tool_response}")
        logger.error(f"   - Error: {e}")
        logger.error(f"   - Error type: {type(e).__name__}")
        import traceback
        logger.error(f"   - Traceback: {traceback.format_exc()}")
        conn.rollback()
        return None
    finally:
        conn.close()


def call_modal_inference(
    prompt: str,
    model_id: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    top_p: float = DEFAULT_TOP_P,
) -> Dict[str, any]:
    """
    Call Modal inference endpoint with a specific model.
    
    Returns:
        dict with 'text', 'tokens', and optionally 'error'
    """
    import time
    
    inference_url = f"{MODAL_INFERENCE_URL}/inference"
    
    payload = {
        "prompt": prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
    }
    
    if model_id:
        payload["modelId"] = model_id
        logger.info(f"✅ [Modal] Using FINETUNED MODEL: {model_id}")
    else:
        logger.info("⚠️  [Modal] Using BASE MODEL (no finetuning)")
    
    logger.info(f"📡 [Modal] Endpoint: {inference_url}")
    logger.info(f"🌡️  [Modal] Temperature: {temperature}, Max Tokens: {max_tokens}, Top P: {top_p}")
    logger.info(f"📝 [Modal] Prompt preview: {prompt[:150]}...")
    logger.info(f"📦 [Modal] Request payload: {json.dumps({**payload, 'prompt': prompt[:200] + '...' if len(prompt) > 200 else prompt}, indent=2)}")
    
    # Get session for connection pooling
    session = get_http_session()
    
    try:
        # Time the request
        request_start = time.time()
        logger.info(f"⏱️  [Modal] Starting request at {datetime.now().isoformat()}")
        
        response = session.post(
            inference_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(10, 150),  # (connect timeout, read timeout) - 10s to connect, 60s to read
        )
        
        request_end = time.time()
        request_duration = request_end - request_start
        logger.info(f"⏱️  [Modal] Request completed in {request_duration:.2f}s (connect + read time)")
        
        logger.info(f"📡 [Modal] Response status: {response.status_code}")
        logger.info(f"📡 [Modal] Response headers: {dict(response.headers)}")
        
        if not response.ok:
            error_text = response.text
            logger.error(f"❌ [Modal] API error: {response.status_code} - {error_text}")
            return {
                "text": "",
                "tokens": 0,
                "error": f"Modal API error: {response.status_code} - {error_text}",
            }
        
        # Log raw response before parsing
        raw_response_text = response.text
        logger.info(f"📥 [Modal] Raw response text (first 500 chars): {raw_response_text[:500]}")
        logger.info(f"📥 [Modal] Raw response length: {len(raw_response_text)} chars")
        
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"❌ [Modal] Failed to parse JSON response: {e}")
            logger.error(f"❌ [Modal] Raw response: {raw_response_text}")
            return {
                "text": "",
                "tokens": 0,
                "error": f"Invalid JSON response from Modal: {str(e)}",
            }
        
        # Log the full response for debugging
        logger.info(f"📥 [Modal] Full response JSON: {json.dumps(data, indent=2)}")
        logger.info(f"📥 [Modal] Response keys: {list(data.keys())}")
        
        # Check for errors in response
        if "error" in data:
            error_msg = data.get("error", "Unknown error")
            logger.error(f"❌ [Modal] Error in response: {error_msg}")
            return {
                "text": "",
                "tokens": 0,
                "error": f"Modal API returned error: {error_msg}",
            }
        
        tokens = data.get("tokens", 0)
        text = data.get("text", "")
        
        # Check for alternative response formats
        if not text:
            # Try alternative field names
            text = data.get("response", "") or data.get("output", "") or data.get("generated_text", "")
        
        # If still no text, use fallback
        if not text:
            text = "No response generated"
            logger.warning(f"⚠️  [Modal] No text in response! Response data: {json.dumps(data, indent=2)}")
            logger.warning(f"⚠️  [Modal] This might indicate the model hit a stop token immediately or the prompt format is incorrect")
        
        logger.info(f"✅ [Modal] Response received: {tokens} tokens")
        logger.info(f"📄 [Modal] Response preview: {text[:100]}...")
        
        return {
            "text": text,
            "tokens": tokens,
        }
        
    except requests.exceptions.Timeout:
        logger.error("❌ [Modal] Request timeout")
        return {
            "text": "",
            "tokens": 0,
            "error": "Request timeout - Modal endpoint took too long to respond",
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ [Modal] Request error: {e}")
        return {
            "text": "",
            "tokens": 0,
            "error": f"Failed to connect to Modal: {str(e)}",
        }
    except Exception as e:
        logger.error(f"❌ [Modal] Unexpected error: {e}")
        return {
            "text": "",
            "tokens": 0,
            "error": f"Unexpected error: {str(e)}",
        }


def format_model_id(model_id: Optional[str]) -> str:
    """Format model ID for display."""
    return model_id or "base model"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    bot_token = context.bot_data.get("bot_token")
    if not bot_token:
        await update.message.reply_text("❌ Bot configuration error. Please contact support.")
        return
    
    user_id = update.effective_user.id
    prefs = get_user_preferences(user_id)
    current_model = format_model_id(prefs["model_id"])
    
    welcome_message = f"""
🤖 Welcome to VibeTune Bot!

I'm powered by your finetuned Modal model. Just send me a message!

Current Model: {current_model}

Commands:
/status - Show current settings
/model <modelId> - Switch to a different model
/base - Use the base model
/report <description> - Report a problem
/clear - Clear chat history
/help - Show all commands
    """
    
    await update.message.reply_text(welcome_message.strip())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_message = """
📚 Available Commands:

/model <modelId> - Switch to a trained model
  Example: /model training-12345

/base - Switch back to the base model

/status - Show current model and settings

/report <description> - Report a problem
  Example: /report The bot is not responding correctly

/clear - Clear chat history and start fresh

/help - Show this help message

💬 Just send a regular message to chat!
    """
    await update.message.reply_text(help_message.strip())


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model <modelId> command."""
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "❌ Please provide a model ID\n\n"
            "Example: /model training-12345"
        )
        return
    
    model_id = context.args[0].strip()
    
    if not re.match(r"^[a-zA-Z0-9\-_]+$", model_id):
        await update.message.reply_text(
            "❌ Invalid model ID format. Model IDs can only contain letters, numbers, dashes, and underscores."
        )
        return
    
    prefs = get_user_preferences(user_id)
    prefs["model_id"] = model_id
    user_preferences[user_id] = prefs
    
    logger.info(f"🔄 [Telegram] User {user_id} switched to model: {model_id}")
    
    await update.message.reply_text(
        f"✅ Switched to model: `{model_id}`\n\n"
        "Now send me a message to test it!",
        parse_mode="Markdown",
    )


async def base_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /base command."""
    user_id = update.effective_user.id
    
    prefs = get_user_preferences(user_id)
    prefs["model_id"] = None
    user_preferences[user_id] = prefs
    
    logger.info(f"🔄 [Telegram] User {user_id} switched to base model")
    
    await update.message.reply_text(
        "✅ Switched to base model\n\n" "Now send me a message to test it!"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    bot_token = context.bot_data.get("bot_token")
    if not bot_token:
        await update.message.reply_text("❌ Bot configuration error. Please contact support.")
        return
    
    user_id = update.effective_user.id
    prefs = get_user_preferences(user_id)
    current_model = format_model_id(prefs["model_id"])
    
    # Look up bot info
    bot_data = lookup_bot_by_token(bot_token)
    if not bot_data:
        await update.message.reply_text(
            "❌ Bot configuration not found. Please configure this bot in the frontend first."
        )
        return
    
    bot_username = bot_data["username"]
    project_id = bot_data["projectId"]
    project_data = bot_data["project"]
    
    # Find project by model_id (if user selected one)
    project = find_project_by_model_id(prefs["model_id"])
    project_info = f"{project_data['name']} ({project_id[:8]}...)" if project_data else "None"
    system_prompt_preview = project_data.get("systemPrompt", "You are a helpful assistant.")[:50] + "..." if project_data.get("systemPrompt") else "You are a helpful assistant."
    
    if project:
        project_info = f"{project['name']} ({project['id'][:8]}...)"
        project_system_prompt = get_project_system_prompt(project["id"])
        if project_system_prompt:
            system_prompt_preview = project_system_prompt[:50] + "..."
        elif project.get("description"):
            system_prompt_preview = project["description"][:50] + "..."
    
    # Get conversation
    bot_id = bot_data["botId"]
    telegram_username = update.effective_user.username
    telegram_first_name = update.effective_user.first_name
    conversation_id = get_or_create_conversation(
        user_id, bot_username, project_id, bot_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name
    )
    
    status_message = f"""
📊 Current Settings:

🤖 Model: {current_model}
📋 Project: {project_info}
🌡️ Temperature: {prefs['temperature']}
📝 Max Tokens: {prefs['max_tokens']}
🎯 Top P: {DEFAULT_TOP_P}
💬 System Prompt: {system_prompt_preview}
💬 Conversation ID: {conversation_id[:8]}...

🔗 Modal Endpoint: {MODAL_INFERENCE_URL}
    """
    
    await update.message.reply_text(status_message.strip())


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report command - Report a problem and create a new conversation."""
    bot_token = context.bot_data.get("bot_token")
    if not bot_token:
        await update.message.reply_text("❌ Bot configuration error. Please contact support.")
        return
    
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "📝 Please describe the problem after /report\n\n"
            "Example: /report The bot is not responding correctly"
        )
        return
    
    report_text = " ".join(context.args).strip()
    
    if not report_text:
        await update.message.reply_text(
            "❌ Report text cannot be empty. Please describe the problem."
        )
        return
    
    # Look up bot info
    bot_data = lookup_bot_by_token(bot_token)
    if not bot_data:
        await update.message.reply_text(
            "❌ Bot configuration not found. Please configure this bot in the frontend first."
        )
        return
    
    bot_id = bot_data["botId"]
    bot_username = bot_data["username"]
    project_id = bot_data["projectId"]
    
    telegram_username = update.effective_user.username
    telegram_first_name = update.effective_user.first_name
    
    # Get conversation history BEFORE creating new conversation (from the existing conversation)
    conversation_history = None
    try:
        # Get the existing conversation to fetch its history
        existing_conversation_id = get_or_create_conversation(
            user_id, bot_username, project_id, bot_id,
            telegram_username=telegram_username,
            telegram_first_name=telegram_first_name
        )
        # Fetch last 10 messages from existing conversation
        history_messages = fetch_conversation_history(existing_conversation_id, limit=10)
        if history_messages:
            conversation_history = history_messages
            logger.info(f"📚 [Report] Including {len(history_messages)} messages in report context")
    except Exception as hist_error:
        logger.warning(f"⚠️ [Report] Could not fetch conversation history: {hist_error}")
    
    # Create a NEW conversation after report (old one stays in DB)
    # This ensures the frontend shows separate conversations before/after report
    conversation_id = create_new_conversation(
        user_id, bot_username, project_id, bot_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name
    )
    
    logger.info(f"📝 [Telegram] User {user_id} created new conversation after report (new conversation: {conversation_id})")
    
    # Send report to API
    if APP_URL:
        try:
            reports_url = f"{APP_URL}/api/reports"
            
            payload = {
                "telegramUserId": user_id,
                "username": username,
                "botUsername": bot_username,  # Include bot username so reports can be filtered by project
                "reportText": report_text,
                "conversationHistory": conversation_history,  # Include conversation context
            }
            
            logger.info(f"📤 [Report] Sending report from user {user_id} (@{username}) via bot @{bot_username} to {reports_url}")
            
            session = get_http_session()
            response = session.post(
                reports_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(5, 10),  # (connect timeout, read timeout)
                verify=SSL_VERIFY,
            )
            
            if response.ok:
                logger.info(f"✅ [Report] Report successfully saved for user {user_id}")
                await update.message.reply_text(
                    "✅ Thank you for your report! It has been saved and will be reviewed.\n\n"
                    "You're starting with a fresh conversation. Send me a message to begin!"
                )
            else:
                logger.error(f"❌ [Report] Failed to save report: {response.status_code} - {response.text}")
                await update.message.reply_text(
                    "⚠️ Your report was received, but there was an issue saving it.\n\n"
                    "You're starting with a fresh conversation. Send me a message to begin!"
                )
        except Exception as e:
            logger.error(f"❌ [Report] Error sending report: {e}", exc_info=True)
            await update.message.reply_text(
                "⚠️ There was an error sending your report, but it has been logged locally.\n\n"
                "You're starting with a fresh conversation. Send me a message to begin!"
            )
    else:
        logger.warning(f"📝 [Report] Report from user {user_id} (@{username}): {report_text}")
        await update.message.reply_text(
            "✅ Your report has been logged. Thank you!\n\n"
            "You're starting with a fresh conversation. Send me a message to begin!"
        )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command - Clear chat history by creating a new conversation."""
    bot_token = context.bot_data.get("bot_token")
    if not bot_token:
        await update.message.reply_text("❌ Bot configuration error. Please contact support.")
        return
    
    user_id = update.effective_user.id
    
    # Look up bot info
    bot_data = lookup_bot_by_token(bot_token)
    if not bot_data:
        await update.message.reply_text(
            "❌ Bot configuration not found. Please configure this bot in the frontend first."
        )
        return
    
    bot_id = bot_data["botId"]
    bot_username = bot_data["username"]
    project_id = bot_data["projectId"]
    
    # Create a NEW conversation (old one stays in DB, but we start fresh)
    # This ensures the frontend shows separate conversations before/after clear
    telegram_username = update.effective_user.username
    telegram_first_name = update.effective_user.first_name
    conversation_id = create_new_conversation(
        user_id, bot_username, project_id, bot_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name
    )
    
    logger.info(f"🗑️  [Telegram] User {user_id} cleared their conversation (new conversation: {conversation_id})")
    
    await update.message.reply_text(
        "✅ Chat history cleared!\n\n"
        "You're starting with a fresh conversation. Send me a message to begin!"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages."""
    bot_token = context.bot_data.get("bot_token")
    if not bot_token:
        await update.message.reply_text("❌ Bot configuration error. Please contact support.")
        return
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if not text:
        await update.message.reply_text("📝 Please send a text message")
        return
    
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    
    try:
        # Look up bot by token to get associated project
        bot_data = lookup_bot_by_token(bot_token)
        if not bot_data:
            logger.error(f"❌ Bot not found in database. Please create the bot in the frontend first.")
            await update.message.reply_text(
                "❌ Bot configuration not found. Please configure this bot in the frontend first."
            )
            return
        
        bot_id = bot_data["botId"]
        bot_username = bot_data["username"]
        project_id = bot_data["projectId"]
        project_data = bot_data["project"]
        
        logger.info(f"✅ Using bot @{bot_username} for project {project_id}")
        
        # Get system prompt from project
        system_prompt = project_data.get("systemPrompt") or project_data.get("description") or "You are a helpful assistant."
        
        # Get the project's config including trained model ID
        # This ensures the bot uses the same finetuned model as the project
        project_config = project_data.get("config") or {}
        if not isinstance(project_config, dict):
            project_config = {}
        
        trained_model_id = project_config.get("trainedModelId")
        
        # Use project's trained model, or fall back to base model if not trained yet
        if trained_model_id:
            model_id = trained_model_id
            logger.info(f"🎯 Using project's TRAINED model: {model_id}")
        else:
            model_id = None  # Will use base model
            logger.info(f"📋 Project has no trained model - using BASE model")
        
        # Get inference parameters from project config (with defaults)
        temperature = project_config.get("temperature", DEFAULT_TEMPERATURE)
        max_tokens = project_config.get("maxTokens", DEFAULT_MAX_TOKENS)
        top_p = project_config.get("topP", DEFAULT_TOP_P)
        
        logger.info(f"📋 Using bot's associated project {project_id}")
        logger.info(f"🌡️  Inference params: temperature={temperature}, max_tokens={max_tokens}, top_p={top_p}")
        
        # Get or create conversation for this user (tagged with source="bot")
        # Include Telegram user info for better display in UI
        telegram_username = update.effective_user.username
        telegram_first_name = update.effective_user.first_name
        conversation_id = get_or_create_conversation(
            user_id, bot_username, project_id, bot_id,
            telegram_username=telegram_username,
            telegram_first_name=telegram_first_name
        )
        
        # Fetch tools from database - use the project ID
        logger.info(f"🔧 [Telegram] Fetching tools for project: {project_id}")
        tools = fetch_tools_from_database(project_id)
        logger.info(f"🔧 [Telegram] Retrieved {len(tools)} tool(s) for this conversation")
        tools_section = format_tools_for_system_prompt(tools)
        
        # Build enhanced system prompt with tools
        enhanced_system_prompt = f"{system_prompt}\n\n{tools_section}" if tools_section else system_prompt
        
        logger.info("=" * 60)
        logger.info(f"📨 [Telegram] New message from user {user_id}")
        logger.info(f"💬 [Telegram] Message: {text[:100]}...")
        logger.info(f"🤖 [Telegram] Model ID: {model_id or 'BASE MODEL'}")
        logger.info(f"📋 [Telegram] Project ID: {project_id or 'None (using fallback)'}")
        logger.info(f"🔧 [Telegram] Available tools: {len(tools)}")
        
        # Fetch conversation history for context (excluding tool calls/responses)
        # This helps the model see previous messages and extract missing parameters
        # TEMPORARILY DISABLED FOR TESTING - set limit=0 to disable, limit=10 to re-enable
        conversation_history = fetch_conversation_history(conversation_id, limit=10)
        # Exclude the current message from history (it will be added separately)
        if conversation_history:
            # Remove the last message if it's the same as current (shouldn't happen, but safety check)
            conversation_history = [msg for msg in conversation_history if msg.get("content", "").strip() != text.strip()]
        
        logger.info("=" * 60)
        logger.info(f"📚 CONTEXT ANALYSIS: Conversation History")
        logger.info(f"   - Fetched {len(conversation_history)} message(s) from conversation history")
        if conversation_history:
            logger.info(f"   - History messages (for context extraction):")
            for idx, msg in enumerate(conversation_history, 1):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                preview = content[:150] + "..." if len(content) > 150 else content
                logger.info(f"     {idx}. [{role.upper()}] {preview}")
        else:
            logger.info(f"   - No previous messages - model will only see current message")
        logger.info("=" * 60)
        
        # MESSAGE SAVING ORDER (CRITICAL - must match this exact sequence):
        # 1. Original query (user message)
        # 2. Tool call message (if tool call detected)
        # 3. Tool response message (if tool call detected)
        # 4. Natural language response (if tool call detected, otherwise just assistant response)
        
        # STEP 1: Save original query (user message)
        logger.info(f"💾 STEP 1: Saving user message to database...")
        user_message_id = save_message_to_db(
            conversation_id=conversation_id,
            role="user",
            content=text,
            bot_username=bot_username,
            is_tool_call=False,
            is_tool_response=False
        )
        if user_message_id:
            logger.info(f"✅ STEP 1: User message saved to database (ID: {user_message_id})")
        else:
            logger.error(f"❌ STEP 1: FAILED to save user message to database!")
        
        # Build formatted prompt with conversation history
        formatted_prompt = build_prompt(enhanced_system_prompt, text, conversation_history)
        
        logger.info(f"📝 [Telegram] Formatted prompt length: {len(formatted_prompt)} chars")
        
        # First inference call
        # Use higher max_tokens if tools are available (tool calls can be long with nested JSON)
        max_tokens_for_inference = max_tokens
        if tools:
            # Increase max_tokens for tool calls to ensure closing tags aren't cut off
            max_tokens_for_inference = max(max_tokens, 500)
            logger.info(f"🔧 Tools available - using max_tokens={max_tokens_for_inference} (increased from {max_tokens}) to ensure tool calls complete")
        
        result = call_modal_inference(
            formatted_prompt,
            model_id,
            temperature,
            max_tokens_for_inference,
            top_p,
        )
        
        if result.get("error"):
            logger.error(f"❌ [Telegram] Error: {result['error']}")
            error_message = f"❌ Sorry, I encountered an error:\n\n`{result['error']}`\n\nPlease try again later or check your Modal endpoint."
            
            # Save error message to database so it appears in web app
            logger.info(f"💾 Saving error message to database...")
            error_message_id = save_message_to_db(
                conversation_id=conversation_id,
                role="assistant",
                content=error_message,
                bot_username=bot_username,
                is_tool_call=False,
                is_tool_response=False,
                tokens_output=0
            )
            if error_message_id:
                logger.info(f"✅ Error message saved to database (ID: {error_message_id})")
            else:
                logger.error(f"❌ FAILED to save error message to database!")
            
            await update.message.reply_text(
                error_message,
                parse_mode="Markdown",
            )
            return
        
        if not result.get("text") or not result["text"].strip():
            empty_response_message = "⚠️ The model generated an empty response. Please try rephrasing your message."
            
            # Save empty response message to database
            logger.info(f"💾 Saving empty response message to database...")
            empty_message_id = save_message_to_db(
                conversation_id=conversation_id,
                role="assistant",
                content=empty_response_message,
                bot_username=bot_username,
                is_tool_call=False,
                is_tool_response=False,
                tokens_output=0
            )
            if empty_message_id:
                logger.info(f"✅ Empty response message saved to database (ID: {empty_message_id})")
            else:
                logger.error(f"❌ FAILED to save empty response message to database!")
            
            await update.message.reply_text(empty_response_message)
            return
        
        response_text = result["text"]
        tokens = result.get("tokens", 0)
        
        logger.info("=" * 60)
        logger.info(f"🔍 STEP 1: Checking if response contains tool call")
        logger.info(f"📄 Response text (first 500 chars): {response_text[:500]}")
        logger.info(f"📊 Response length: {len(response_text)} chars, tokens: {tokens}")
        
        # Check if response contains tool call
        if contains_tool_call(response_text) and tools:
            logger.info(f"✅ STEP 1: Tool call DETECTED in response")
            logger.info(f"🔧 Available tools: {[t['name'] for t in tools]}")
            
            # Parse tool calls
            logger.info(f"🔍 STEP 2: Parsing tool calls from response...")
            tool_calls = parse_tool_calls(response_text)
            logger.info(f"📋 STEP 2: Parsed {len(tool_calls)} tool call(s)")
            
            if tool_calls:
                tool_call = tool_calls[0]  # Handle first tool call
                
                logger.info(f"📋 STEP 3: Tool call details:")
                logger.info(f"   - Tool Name: \"{tool_call['name']}\"")
                logger.info(f"   - Raw Arguments (from model): {json.dumps(tool_call.get('arguments', {}), indent=2)}")
                
                # Analyze parameter extraction from context
                logger.info("=" * 60)
                logger.info(f"🔍 PARAMETER EXTRACTION ANALYSIS")
                logger.info(f"   - Tool: \"{tool_call['name']}\"")
                
                # Find tool definition to check required parameters
                tool_definition = None
                for t in tools:
                    if t["name"] == tool_call["name"] or t.get("typeName") == tool_call["name"]:
                        tool_definition = t
                        break
                
                if tool_definition:
                    # Get parameter schema
                    parameters = tool_definition.get("parameters", {})
                    if isinstance(parameters, str):
                        try:
                            parameters = json.loads(parameters)
                        except:
                            parameters = {}
                    
                    properties = parameters.get("properties", {})
                    required_params = parameters.get("required", [])
                    provided_params = list(tool_call.get("arguments", {}).keys())
                    
                    logger.info(f"   - Required parameters: {required_params}")
                    logger.info(f"   - Parameters provided by model: {provided_params}")
                    
                    # Check which required parameters are missing
                    missing_required = [p for p in required_params if p not in provided_params or not tool_call.get("arguments", {}).get(p)]
                    if missing_required:
                        logger.warning(f"   ⚠️  MISSING required parameters: {missing_required}")
                        logger.info(f"   - Model should have extracted these from conversation history")
                        if conversation_history:
                            logger.info(f"   - Conversation history was available ({len(conversation_history)} messages)")
                            logger.info(f"   - Checking if missing params can be found in history...")
                            for param in missing_required:
                                found_in_history = False
                                for msg in reversed(conversation_history):
                                    content = msg.get("content", "").lower()
                                    param_lower = param.lower()
                                    # Simple check if param name or value appears in history
                                    if param_lower in content or any(char.isdigit() for char in content if 'phone' in param_lower or 'number' in param_lower):
                                        logger.info(f"     - '{param}' might be in history: {msg.get('content', '')[:80]}...")
                                        found_in_history = True
                                        break
                                if not found_in_history:
                                    logger.warning(f"     - '{param}' NOT found in conversation history")
                        else:
                            logger.warning(f"   - No conversation history available - model couldn't extract missing params")
                    else:
                        logger.info(f"   ✅ All required parameters provided by model")
                    
                    # Check for parameters that might have been extracted from context
                    if conversation_history and provided_params:
                        logger.info(f"   - Analyzing if parameters were extracted from context:")
                        for param in provided_params:
                            value = tool_call.get("arguments", {}).get(param)
                            # Check if this value appears in conversation history
                            value_str = str(value).lower() if value else ""
                            found_in_current = value_str in text.lower() if value else False
                            found_in_history = False
                            if value and not found_in_current:
                                for msg in conversation_history:
                                    if value_str in msg.get("content", "").lower():
                                        logger.info(f"     ✅ '{param}' = '{value}' found in history message")
                                        found_in_history = True
                                        break
                            if found_in_current:
                                logger.info(f"     ✅ '{param}' = '{value}' found in current message")
                            elif found_in_history:
                                logger.info(f"     ✅ '{param}' = '{value}' successfully extracted from conversation history!")
                            else:
                                logger.info(f"     ℹ️  '{param}' = '{value}' (not found in visible context)")
                else:
                    logger.warning(f"   ⚠️  Tool definition not found - cannot analyze parameters")
                
                logger.info("=" * 60)
                
                # Normalize arguments
                logger.info(f"🔍 STEP 4: Normalizing tool arguments...")
                normalized_args = normalize_tool_arguments(tool_call["name"], tool_call.get("arguments", {}))
                logger.info(f"✅ STEP 4: Normalized arguments: {json.dumps(normalized_args, indent=2)}")
                
                # Update tool_call with normalized args
                tool_call["arguments"] = normalized_args
                
                # STEP 2: Save tool call message
                logger.info(f"💾 STEP 2: Saving tool call message to database...")
                tool_call_content = format_tool_call_for_training(tool_call)
                logger.info(f"📝 Tool call content to save: {tool_call_content}")
                tool_call_message_id = save_message_to_db(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=tool_call_content,
                    bot_username=bot_username,
                    is_tool_call=True,
                    is_tool_response=False,
                    tool_calls={"name": tool_call["name"], "arguments": tool_call.get("arguments", {})},
                    tokens_output=tokens
                )
                if tool_call_message_id:
                    logger.info(f"✅ STEP 2: Tool call message saved to database (ID: {tool_call_message_id})")
                else:
                    logger.error(f"❌ STEP 2: FAILED to save tool call message to database!")
                
                # Execute tool call
                logger.info(f"🔧 STEP 6: Executing tool call...")
                # Retrieve stored auth token for this user and bot if available
                auth_token = None
                if user_id and bot_username:
                    auth_token = get_auth_token(user_id, bot_username)
                    if not auth_token:
                        logger.info(f"🔑 [AUTH] No token found for user {user_id} and bot {bot_username}")
                tool_result = execute_tool_call(tool_call, tools, telegram_user_id=user_id, auth_token=auth_token)
                logger.info(f"✅ STEP 6: Tool execution completed")
                logger.info(f"   - Success: {tool_result['success']}")
                logger.info(f"   - Result data: {json.dumps(tool_result.get('data'), indent=2)}")
                if tool_result.get("error"):
                    logger.error(f"   - Error: {tool_result['error']}")
                
                # STEP 3: Save tool response message
                logger.info(f"💾 STEP 3: Saving tool response message to database...")
                response_data = tool_result["data"] if tool_result["success"] else {
                    "success": False,
                    "error": tool_result.get("error", "An unknown error occurred")
                }
                
                # Extract and store auth token if response contains a token (generic approach)
                if tool_result["success"] and user_id and bot_username:
                    token = response_data.get("token")
                    if token:
                        store_auth_token(user_id, bot_username, token)
                        logger.info(f"🔑 [AUTH] Token extracted and stored for user {user_id} and bot {bot_username} (length: {len(token)})")
                
                tool_response_content = format_tool_response(tool_call["name"], response_data)
                logger.info(f"📝 Tool response content to save: {tool_response_content[:200]}...")
                tool_response_message_id = save_message_to_db(
                    conversation_id=conversation_id,
                    role="system",
                    content=tool_response_content,
                    bot_username=bot_username,
                    is_tool_call=False,
                    is_tool_response=True
                )
                if tool_response_message_id:
                    logger.info(f"✅ STEP 3: Tool response message saved to database (ID: {tool_response_message_id})")
                else:
                    logger.error(f"❌ STEP 3: FAILED to save tool response message to database!")
                
                # Build follow-up prompt for natural language response
                logger.info(f"🔄 STEP 8: Building follow-up prompt for natural language response...")
                
                # Extract error message if there's an error
                error_message = None
                if not tool_result["success"]:
                    response_data = tool_result.get("data", {})
                    # Try to extract the actual error message from various possible structures
                    if isinstance(response_data, dict):
                        error_message = (
                            response_data.get("error") or 
                            response_data.get("message") or 
                            response_data.get("detail") or
                            tool_result.get("error")
                        )
                    else:
                        error_message = tool_result.get("error")
                
                # Format results for the prompt
                results_data = tool_result["data"] if tool_result["success"] else {
                    "error": error_message or "An unknown error occurred"
                }
                
                summary_instruction = f"""
IMPORTANT: The tool has been executed and returned results. You MUST now provide a natural language summary of the results below. Do NOT make another tool call. Do NOT output JSON. Write a friendly, conversational response presenting the data.

Tool: {tool_call["name"]}
Results: {json.dumps(results_data, indent=2)}

Now respond naturally to the user based on these results:"""
                
                summary_system_prompt = f"""You are a helpful assistant.

Your task now is to present the tool results to the user in a friendly, natural way.

CRITICAL RULES:
- Do NOT output any tool calls or JSON
- Do NOT use <tool_call> tags
- Write in plain conversational English
- Present the information clearly
- Be helpful and professional
- If there's an error, explain what actually went wrong in simple terms - extract the actual error message and explain it naturally
- Do NOT mention HTTP status codes (like 400, 500, etc.) - they are technical details the user doesn't need
- Focus on what the error means for the user and what they can do about it
- If the error message says something specific (like "Driver is too far from delivery locations"), explain that exact issue clearly"""
                
                # Build follow-up prompt (include conversation history for context)
                follow_up_prompt = build_prompt(summary_system_prompt, f"{text}\n\n{summary_instruction}", conversation_history)
                logger.info(f"📝 Follow-up prompt length: {len(follow_up_prompt)} chars")
                
                logger.info(f"🔄 STEP 8: Running follow-up inference for natural response...")
                logger.info(f"   - Model ID: {model_id or 'BASE MODEL'}")
                logger.info(f"   - Temperature: {temperature}")
                logger.info(f"   - Max Tokens: {max_tokens}")
                
                # Run follow-up inference
                follow_up_result = call_modal_inference(
                    follow_up_prompt,
                    model_id,
                    temperature,
                    max_tokens,
                    top_p,
                )
                
                logger.info(f"📥 Follow-up inference result:")
                logger.info(f"   - Tokens: {follow_up_result.get('tokens', 0)}")
                if follow_up_result.get("error"):
                    logger.error(f"   - Error: {follow_up_result['error']}")
                    # Fallback to showing tool result
                    final_response = f"I executed the tool '{tool_call['name']}' and got these results:\n\n{json.dumps(tool_result['data'], indent=2)}"
                    logger.info(f"⚠️  Using fallback response (tool execution failed)")
                else:
                    final_response = follow_up_result.get("text", "").strip()
                    logger.info(f"✅ Natural language response received ({len(final_response)} chars)")
                    logger.info(f"📄 Response preview: {final_response[:200]}...")
                
                # STEP 4: Save natural language response
                logger.info(f"💾 STEP 4: Saving natural language response to database...")
                logger.info(f"📝 Natural language response (first 200 chars): {final_response[:200]}...")
                natural_response_message_id = save_message_to_db(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=final_response,
                    bot_username=bot_username,
                    is_tool_call=False,
                    is_tool_response=False,
                    tokens_output=follow_up_result.get("tokens", 0)
                )
                if natural_response_message_id:
                    logger.info(f"✅ STEP 4: Natural language response saved to database (ID: {natural_response_message_id})")
                else:
                    logger.error(f"❌ STEP 4: FAILED to save natural language response to database!")
                
                logger.info(f"✅ [Telegram] Complete tool call flow finished - Message save status:")
                logger.info(f"   1. {'✅' if user_message_id else '❌'} Original query (user message) - ID: {user_message_id or 'FAILED'}")
                logger.info(f"   2. {'✅' if tool_call_message_id else '❌'} Tool call message - ID: {tool_call_message_id or 'FAILED'}")
                logger.info(f"   3. {'✅' if tool_response_message_id else '❌'} Tool response message - ID: {tool_response_message_id or 'FAILED'}")
                logger.info(f"   4. {'✅' if natural_response_message_id else '❌'} Natural language response - ID: {natural_response_message_id or 'FAILED'}")
                logger.info("=" * 60)
                
                await update.message.reply_text(final_response)
                return
        
        # No tool call - just save assistant response (only 2 messages: user query + assistant response)
        logger.info(f"💾 Saving assistant response (no tool call)...")
        assistant_message_id = save_message_to_db(
            conversation_id=conversation_id,
            role="assistant",
            content=response_text,
            bot_username=bot_username,
            is_tool_call=False,
            is_tool_response=False,
            tokens_output=tokens
        )
        if assistant_message_id:
            logger.info(f"✅ Assistant response saved to database (ID: {assistant_message_id})")
        else:
            logger.error(f"❌ FAILED to save assistant response to database!")
        
        logger.info(f"✅ [Telegram] Response generated: {tokens} tokens")
        logger.info(f"📄 [Telegram] Response: {response_text[:100]}...")
        logger.info("=" * 60)
        
        await update.message.reply_text(response_text)
            
    except Exception as e:
        logger.error(f"❌ [Telegram] Unexpected error: {e}", exc_info=True)
        error_message = f"❌ An unexpected error occurred:\n\n`{str(e)}`\n\nPlease try again later."
        
        # Try to save error message to database if we have conversation context
        try:
            # Try to get conversation_id and bot_username from context if available
            bot_token = context.bot_data.get("bot_token")
            if bot_token:
                bot_data = lookup_bot_by_token(bot_token)
                if bot_data:
                    bot_username = bot_data["username"]
                    project_id = bot_data["projectId"]
                    bot_id = bot_data["botId"]
                    user_id = update.effective_user.id
                    
                    # Try to get existing conversation
                    conversation_id = get_or_create_conversation(
                        user_id, bot_username, project_id, bot_id,
                        telegram_username=update.effective_user.username,
                        telegram_first_name=update.effective_user.first_name
                    )
                    
                    # Save error message to database
                    logger.info(f"💾 Saving exception error message to database...")
                    error_message_id = save_message_to_db(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=error_message,
                        bot_username=bot_username,
                        is_tool_call=False,
                        is_tool_response=False,
                        tokens_output=0
                    )
                    if error_message_id:
                        logger.info(f"✅ Exception error message saved to database (ID: {error_message_id})")
                    else:
                        logger.error(f"❌ FAILED to save exception error message to database!")
        except Exception as save_error:
            logger.error(f"❌ Failed to save exception error to database: {save_error}")
        
        await update.message.reply_text(
            error_message,
            parse_mode="Markdown",
        )


class BotManager:
    """Manages multiple bot instances with dynamic discovery."""
    
    def __init__(self):
        self.running_bots: Dict[str, Any] = {}  # bot_id -> {"application": Application, "task": Task, "bot_info": dict}
        self._shutdown_event = asyncio.Event()
    
    async def start_bot(self, bot_info: Dict[str, Any]) -> bool:
        """
        Start a single bot instance.
        
        Args:
            bot_info: Bot info dict with token, username, projectId, etc.
        
        Returns:
            True if bot started successfully, False otherwise
        """
        token = bot_info["token"]
        username = bot_info["username"]
        bot_id = bot_info["id"]
        
        # Skip if already running
        if bot_id in self.running_bots:
            logger.debug(f"⏭️  Bot @{username} (ID: {bot_id}) is already running")
            return True
        
        logger.info(f"🚀 Starting bot instance: @{username} (ID: {bot_id})")
        
        try:
            # Verify bot is still active and get full config
            bot_data = lookup_bot_by_token(token)
            if not bot_data:
                logger.error(f"❌ Failed to lookup bot @{username} - skipping")
                return False
            
            # Create application with bot token
            application = Application.builder().token(token).build()
            
            # Store bot token in application context for handlers
            application.bot_data["bot_token"] = token
            application.bot_data["bot_id"] = bot_id
            application.bot_data["bot_username"] = username
            
            # Add handlers
            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("model", model_command))
            application.add_handler(CommandHandler("base", base_command))
            application.add_handler(CommandHandler("status", status_command))
            application.add_handler(CommandHandler("report", report_command))
            application.add_handler(CommandHandler("clear", clear_command))
            
            application.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
            )
            
            # Initialize and start polling
            await application.initialize()
            await application.start()
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
            # Create a task that waits for the updater to stop
            async def polling_wrapper():
                try:
                    await application.updater.idle()
                except asyncio.CancelledError:
                    pass
            
            polling_task = asyncio.create_task(polling_wrapper())
            
            # Store bot instance
            self.running_bots[bot_id] = {
                "application": application,
                "task": polling_task,
                "bot_info": bot_info,
            }
            
            logger.info(f"✅ Bot @{username} started successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error starting bot @{username}: {e}", exc_info=True)
            return False
    
    async def stop_bot(self, bot_id: str) -> bool:
        """
        Stop a bot instance.
        
        Args:
            bot_id: The bot ID to stop
        
        Returns:
            True if bot stopped successfully, False otherwise
        """
        if bot_id not in self.running_bots:
            return False
        
        bot_data = self.running_bots[bot_id]
        username = bot_data["bot_info"]["username"]
        
        logger.info(f"🛑 Stopping bot @{username} (ID: {bot_id})")
        
        try:
            application = bot_data["application"]
            task = bot_data["task"]
            
            # Stop the updater
            await application.updater.stop()
            
            # Stop the application
            await application.stop()
            await application.shutdown()
            
            # Cancel the polling task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            
            # Remove from running bots
            del self.running_bots[bot_id]
            
            logger.info(f"✅ Bot @{username} stopped successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error stopping bot @{username}: {e}", exc_info=True)
            # Remove from running bots even if shutdown failed
            if bot_id in self.running_bots:
                del self.running_bots[bot_id]
            return False
    
    async def sync_bots(self) -> None:
        """Check database for new/deactivated bots and sync running instances."""
        try:
            # Fetch all active bots from database
            active_bots = fetch_all_active_bots()
            active_bot_ids = {bot["id"] for bot in active_bots}
            running_bot_ids = set(self.running_bots.keys())
            
            # Start new bots
            for bot in active_bots:
                if bot["id"] not in running_bot_ids:
                    logger.info(f"🆕 New bot detected: @{bot['username']} (ID: {bot['id']})")
                    await self.start_bot(bot)
            
            # Stop deactivated bots
            for bot_id in running_bot_ids:
                if bot_id not in active_bot_ids:
                    logger.info(f"🔴 Bot deactivated: {bot_id}")
                    await self.stop_bot(bot_id)
            
        except Exception as e:
            logger.error(f"❌ Error syncing bots: {e}", exc_info=True)
    
    async def trigger_sync(self) -> Dict[str, Any]:
        """
        Manually trigger a bot sync (called by webhook).
        
        Returns:
            Dict with sync results
        """
        try:
            before_count = len(self.running_bots)
            await self.sync_bots()
            after_count = len(self.running_bots)
            
            return {
                "success": True,
                "message": "Bot sync completed",
                "bots_before": before_count,
                "bots_after": after_count,
                "bots_added": after_count - before_count,
            }
        except Exception as e:
            logger.error(f"❌ Error in manual sync: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
            }
    
    async def shutdown(self) -> None:
        """Shutdown all bots and stop monitoring."""
        logger.info("🛑 Shutting down bot manager...")
        
        # Signal shutdown
        self._shutdown_event.set()
        
        # Stop all bots
        bot_ids = list(self.running_bots.keys())
        for bot_id in bot_ids:
            await self.stop_bot(bot_id)
        
        logger.info("✅ Bot manager shut down complete")


async def run_bot_instance(bot_info: Dict[str, Any]) -> None:
    """
    Initialize and run a single bot instance (legacy function for backward compatibility).
    
    Args:
        bot_info: Bot info dict with token, username, projectId, etc.
    """
    token = bot_info["token"]
    username = bot_info["username"]
    bot_id = bot_info["id"]
    
    logger.info(f"🚀 Starting bot instance: @{username} (ID: {bot_id})")
    
    try:
        # Verify bot is still active and get full config
        bot_data = lookup_bot_by_token(token)
        if not bot_data:
            logger.error(f"❌ Failed to lookup bot @{username} - skipping")
            return
        
        # Create application with bot token
        application = Application.builder().token(token).build()
        
        # Store bot token in application context for handlers
        application.bot_data["bot_token"] = token
        application.bot_data["bot_id"] = bot_id
        application.bot_data["bot_username"] = username
        
        # Add handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("model", model_command))
        application.add_handler(CommandHandler("base", base_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("report", report_command))
        application.add_handler(CommandHandler("clear", clear_command))
        
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
        )
        
        logger.info(f"✅ Bot @{username} initialized successfully")
        
        # Run polling
        await application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Error running bot @{username}: {e}", exc_info=True)


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for webhook endpoints."""
    
    def do_POST(self):
        """Handle POST requests."""
        if self.path == '/sync':
            self.handle_sync()
        else:
            self.send_error(404)
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/sync':
            self.handle_sync()
        else:
            self.send_error(404)
    
    def handle_sync(self):
        """Handle sync webhook."""
        global bot_manager_instance
        
        if bot_manager_instance is None:
            self.send_json_response(
                {"success": False, "error": "Bot manager not initialized"},
                503
            )
            return
        
        try:
            # Run sync in async context
            # Get or create event loop for this thread
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Run the sync
            result = loop.run_until_complete(bot_manager_instance.trigger_sync())
            
            status = 200 if result.get("success") else 500
            self.send_json_response(result, status)
        except Exception as e:
            logger.error(f"❌ Error in webhook handler: {e}", exc_info=True)
            self.send_json_response(
                {"success": False, "error": str(e)},
                500
            )
    
    def send_json_response(self, data: Dict[str, Any], status: int = 200):
        """Send JSON response."""
        json_data = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(json_data)))
        self.end_headers()
        self.wfile.write(json_data)
    
    def log_message(self, format, *args):
        """Override to use our logger instead of default."""
        logger.info(f"HTTP {format % args}")


def start_webhook_server_thread(bot_manager: 'BotManager') -> Thread:
    """Start HTTP server in a separate thread."""
    global bot_manager_instance
    bot_manager_instance = bot_manager
    
    def run_server():
        server = HTTPServer(('0.0.0.0', WEBHOOK_PORT), WebhookHandler)
        logger.info(f"🌐 Webhook server started on port {WEBHOOK_PORT}")
        logger.info(f"   POST http://0.0.0.0:{WEBHOOK_PORT}/sync - Trigger bot sync")
        server.serve_forever()
    
    thread = Thread(target=run_server, daemon=True)
    thread.start()
    return thread


async def main_async() -> None:
    """Main async function to run all bots with dynamic discovery."""
    print("🤖 VibeTune Telegram Bot Server - Multi-Bot Mode")
    print("═══════════════════════════════════════")
    print(f"📡 Modal Endpoint: {MODAL_INFERENCE_URL}")
    print(f"🎯 Default Model: {DEFAULT_MODEL_ID}")
    print(f"📋 System Prompt: {DEFAULT_SYSTEM_PROMPT}")
    print(f"🌡️  Temperature: {DEFAULT_TEMPERATURE}")
    print(f"📝 Max Tokens: {DEFAULT_MAX_TOKENS}")
    print(f"🎯 Top P: {DEFAULT_TOP_P}")
    print(f"💾 Database: Connected")
    print("═══════════════════════════════════════")
    
    # Initialize bot manager
    bot_manager = BotManager()
    
    # Fetch all active bots
    if TELEGRAM_BOT_TOKEN:
        # Backward compatibility: run single bot if token provided
        logger.info(f"🔑 Single bot mode: Using provided token")
        # Verify bot exists in database
        bot_data = lookup_bot_by_token(TELEGRAM_BOT_TOKEN)
        if not bot_data:
            logger.error("❌ Bot not found in database. Please create the bot in the frontend first.")
            print("")
            print("To create a bot:")
            print("  1. Go to Settings → Bots tab in the frontend")
            print("  2. Create a bot and paste the token from BotFather")
            print("  3. Make sure the bot is set to 'Active'")
            print("")
            return
        bots = [{"token": TELEGRAM_BOT_TOKEN, "id": bot_data["botId"], "username": bot_data["username"]}]
        
        # Start the single bot
        for bot in bots:
            await bot_manager.start_bot(bot)
        
        # In single bot mode, just run the bot (no monitoring)
        if bots:
            await bot_manager.running_bots[bots[0]["id"]]["task"]
    else:
        # Multi-bot mode: fetch all active bots from database
        logger.info(f"🔍 Multi-bot mode: Fetching active bots from database...")
        bots = fetch_all_active_bots()
        
        if not bots:
            logger.warning("⚠️  No active bots found in database!")
            print("")
            print("To create a bot:")
            print("  1. Go to Settings → Bots tab in the frontend")
            print("  2. Create a bot and get token from BotFather")
            print("  3. The bot will automatically appear here when active")
            print("")
            print("💡 Bots are discovered via webhook when created in the frontend")
            print("")
        
        # Start initial bots
        for bot in bots:
            await bot_manager.start_bot(bot)
        
        if bots:
            print(f"✅ Started {len(bots)} bot(s) initially")
            print("")
            for idx, bot in enumerate(bots, 1):
                print(f"  [{idx}] @{bot.get('username', 'unknown')} (ID: {bot['id']})")
            print("")
        
        print("🔄 Dynamic bot discovery enabled")
        print("💡 New bots created in the frontend will be automatically picked up via webhook!")
        print(f"🌐 Webhook endpoint: http://0.0.0.0:{WEBHOOK_PORT}/sync")
        print("💡 Send /start to any bot to begin")
        print("")
        
        # Start webhook server in background thread (for immediate sync on bot creation)
        webhook_thread = start_webhook_server_thread(bot_manager)
        
        # Keep the main thread alive - bots are managed via webhook updates
        try:
            # Wait indefinitely until shutdown signal
            while not bot_manager._shutdown_event.is_set():
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("🛑 Received shutdown signal...")
            bot_manager._shutdown_event.set()
        finally:
            # Shutdown gracefully
            await bot_manager.shutdown()


def main() -> None:
    """Start the bot server."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down bot server...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    main()
