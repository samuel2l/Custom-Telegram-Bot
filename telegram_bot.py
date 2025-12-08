#!/usr/bin/env python3
"""
VibeTune Telegram Bot

Simple Telegram bot that uses your Modal finetuned models.

Setup:
1. Get a Telegram bot token from @BotFather
2. Install dependencies: pip install -r requirements.txt
3. Set TELEGRAM_BOT_TOKEN environment variable
4. Run: python telegram_bot.py

The bot works out of the box with defaults. You can optionally set:
- DEFAULT_SYSTEM_PROMPT (default: "delivery company")
- DEFAULT_MODEL_ID (default: your trained model)
- DEFAULT_TEMPERATURE (default: 0.8)
- DEFAULT_MAX_TOKENS (default: 250)
"""

import os
import logging
import requests
from typing import Optional, Dict
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Required: Telegram bot token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8332478522:AAEpwTXfBFlirazWtz93pPAqiHYqb8pf4eo")

# Modal inference endpoint
MODAL_INFERENCE_URL = "https://adamssamuel9955--vibetune-inference-inferenceservice-serve.modal.run"

# Defaults (work out of the box, can be overridden with environment variables)
DEFAULT_MODEL_ID = "training-1764850841135-cmiqe0ncr0001t1rp8sjtt5yc"
DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.8"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "250"))
DEFAULT_TOP_P = float(os.getenv("DEFAULT_TOP_P", "0.95"))
DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", "delivery company")

if not TELEGRAM_BOT_TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN environment variable is required")
    print("   Get a token from @BotFather on Telegram")
    print("   Then run: export TELEGRAM_BOT_TOKEN=your_token_here")
    exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Store user preferences (in production, use a database)
user_preferences: Dict[int, Dict[str, any]] = {}

# Store conversation history per user (in production, use a database)
# Format: {user_id: [{"role": "user"|"assistant", "content": "..."}, ...]}
user_conversations: Dict[int, list] = {}

# Web app API URL for reports (optional, set via APP_URL env var)
# export APP_URL=
APP_URL = os.getenv("APP_URL", "http://localhost:3000")


def get_user_preferences(user_id: int) -> Dict[str, any]:
    """Get user preferences or return defaults."""
    if user_id not in user_preferences:
        user_preferences[user_id] = {
            "model_id": DEFAULT_MODEL_ID,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
        }
    return user_preferences[user_id]


def get_user_conversation(user_id: int) -> list:
    """Get user's conversation history."""
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    return user_conversations[user_id]


def clear_user_conversation(user_id: int) -> None:
    """Clear user's conversation history."""
    user_conversations[user_id] = []
    logger.info(f"🗑️  [Telegram] Cleared conversation history for user {user_id}")


def add_to_conversation(user_id: int, role: str, content: str) -> None:
    """Add a message to user's conversation history."""
    conversation = get_user_conversation(user_id)
    conversation.append({"role": role, "content": content})
    # Keep only last 20 messages to avoid memory issues
    if len(conversation) > 20:
        conversation.pop(0)


def build_prompt(system_prompt: str, user_message: str) -> str:
    """
    Build a prompt in the same format as the frontend.
    Uses the chat template: <|im_start|>system, <|im_start|>user, <|im_start|>assistant
    """
    prompt = f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
    prompt += f"<|im_start|>user\n{user_message}<|im_end|>\n"
    prompt += "<|im_start|>assistant\n"
    return prompt


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
    inference_url = f"{MODAL_INFERENCE_URL}/inference"
    
    payload = {
        "prompt": prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
    }
    
    # Only include modelId if it's not None (base model)
    if model_id:
        payload["modelId"] = model_id
        logger.info(f"✅ [Modal] Using FINETUNED MODEL: {model_id}")
    else:
        logger.info("⚠️  [Modal] Using BASE MODEL (no finetuning)")
    
    logger.info(f"📡 [Modal] Endpoint: {inference_url}")
    logger.info(f"📦 [Modal] Payload keys: {list(payload.keys())}")
    logger.info(f"🌡️  [Modal] Temperature: {temperature}, Max Tokens: {max_tokens}, Top P: {top_p}")
    logger.info(f"📝 [Modal] Prompt preview: {prompt[:150]}...")
    
    try:
        response = requests.post(
            inference_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=180,
        )
        
        if not response.ok:
            error_text = response.text
            logger.error(f"❌ [Modal] API error: {response.status_code} - {error_text}")
            return {
                "text": "",
                "tokens": 0,
                "error": f"Modal API error: {response.status_code} - {error_text}",
            }
        
        data = response.json()
        tokens = data.get("tokens", 0)
        text = data.get("text", "No response generated")
        
        logger.info(f"✅ [Modal] Response received: {tokens} tokens")
        logger.info(f"📄 [Modal] Response preview: {text[:100]}...")
        
        # Verify model was used
        if model_id:
            logger.info(f"✅ [Modal] CONFIRMED: Finetuned model {model_id} was used")
        else:
            logger.info("⚠️  [Modal] CONFIRMED: Base model was used")
        
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
    
    import re
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
    user_id = update.effective_user.id
    prefs = get_user_preferences(user_id)
    current_model = format_model_id(prefs["model_id"])
    conversation = get_user_conversation(user_id)
    
    status_message = f"""
📊 Current Settings:

🤖 Model: {current_model}
🌡️ Temperature: {prefs['temperature']}
📝 Max Tokens: {prefs['max_tokens']}
🎯 Top P: {DEFAULT_TOP_P}
📋 System Prompt: {prefs['system_prompt'][:50]}...
💬 Messages in conversation: {len(conversation)}

🔗 Modal Endpoint: {MODAL_INFERENCE_URL}
    """
    
    await update.message.reply_text(status_message.strip())


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report command - Report a problem."""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Get report text from command arguments
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "📝 Please describe the problem after /report\n\n"
            "Example: /report The bot is giving incorrect responses\n\n"
            "Or use: /report and then send your report in the next message"
        )
        return
    
    report_text = " ".join(context.args).strip()
    
    if not report_text:
        await update.message.reply_text(
            "❌ Report text cannot be empty. Please describe the problem."
        )
        return
    
    # Get conversation history for context
    conversation = get_user_conversation(user_id)
    
    # Log conversation history details for debugging
    logger.info(f"📋 [Report] Conversation history for user {user_id}:")
    logger.info(f"   - History length: {len(conversation)} messages")
    if conversation:
        logger.info(f"   - First message: {conversation[0] if conversation else 'N/A'}")
        logger.info(f"   - Last message: {conversation[-1] if conversation else 'N/A'}")
        logger.info(f"   - Full history: {conversation}")
    else:
        logger.warning(f"   ⚠️  No conversation history found for user {user_id}")
        logger.warning(f"   - This might be because:")
        logger.warning(f"     * User sent /report without any prior messages")
        logger.warning(f"     * User cleared conversation with /clear")
        logger.warning(f"     * Bot was restarted (conversation history is in-memory)")
    
    # Get bot username
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username if bot_info else None
    logger.info(f"🤖 [Report] Bot username: {bot_username}")
    
    # Send report to API
    if APP_URL:
        try:
            reports_url = f"{APP_URL}/api/reports"
            payload = {
                "telegramUserId": user_id,
                "username": username,
                "botUsername": bot_username,
                "reportText": report_text,
                "conversationHistory": conversation if conversation else None,
            }
            
            logger.info(f"📤 [Report] Sending report from user {user_id} to {reports_url}")
            logger.info(f"📦 [Report] Payload conversationHistory: {payload['conversationHistory']}")
            
            response = requests.post(
                reports_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            
            if response.ok:
                logger.info(f"✅ [Report] Report successfully saved for user {user_id}")
                await update.message.reply_text(
                    "✅ Thank you for your report! It has been saved and will be reviewed.\n\n"
                    "🔄 Starting a fresh conversation..."
                )
                # Clear conversation history after reporting
                clear_user_conversation(user_id)
            else:
                logger.error(f"❌ [Report] Failed to save report: {response.status_code} - {response.text}")
                await update.message.reply_text(
                    "⚠️ Your report was received, but there was an issue saving it.\n\n"
                    "The report text has been logged. Please try again if the problem persists."
                )
                # Still clear conversation to start fresh
                clear_user_conversation(user_id)
        except Exception as e:
            logger.error(f"❌ [Report] Error sending report: {e}", exc_info=True)
            await update.message.reply_text(
                "⚠️ There was an error sending your report, but it has been logged locally.\n\n"
                "🔄 Starting a fresh conversation..."
            )
            # Still clear conversation to start fresh
            clear_user_conversation(user_id)
    else:
        # No APP_URL configured, just log locally
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username if bot_info else "unknown"
        logger.warning(f"📝 [Report] Report from user {user_id} (@{username}) to bot @{bot_username}: {report_text}")
        logger.warning(f"📝 [Report] Conversation history: {conversation}")
        await update.message.reply_text(
            "✅ Your report has been logged. Thank you!\n\n"
            "🔄 Starting a fresh conversation..."
        )
        # Clear conversation history after reporting
        clear_user_conversation(user_id)


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command - Clear chat history."""
    user_id = update.effective_user.id
    
    clear_user_conversation(user_id)
    
    logger.info(f"🗑️  [Telegram] User {user_id} cleared their conversation")
    
    await update.message.reply_text(
        "✅ Chat history cleared!\n\n"
        "You're starting with a fresh conversation. Send me a message to begin!"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages."""
    user_id = update.effective_user.id
    text = update.message.text
    
    if not text:
        await update.message.reply_text("📝 Please send a text message")
        return
    
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    
    try:
        prefs = get_user_preferences(user_id)
        model_id = prefs["model_id"]
        system_prompt = prefs.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        
        # Add user message to conversation history
        add_to_conversation(user_id, "user", text)
        current_conv = get_user_conversation(user_id)
        logger.debug(f"💬 [Conversation] Added user message. Total messages: {len(current_conv)}")
        
        logger.info("=" * 60)
        logger.info(f"📨 [Telegram] New message from user {user_id}")
        logger.info(f"💬 [Telegram] Message: {text[:100]}...")
        logger.info(f"🤖 [Telegram] Model ID: {model_id or 'BASE MODEL'}")
        logger.info(f"📋 [Telegram] System Prompt: {system_prompt[:50]}...")
        
        # Build formatted prompt (same format as frontend)
        formatted_prompt = build_prompt(system_prompt, text)
        
        logger.info(f"📝 [Telegram] Formatted prompt length: {len(formatted_prompt)} chars")
        
        result = call_modal_inference(
            formatted_prompt,
            model_id,
            prefs["temperature"],
            prefs["max_tokens"],
            DEFAULT_TOP_P,
        )
        
        if result.get("error"):
            logger.error(f"❌ [Telegram] Error: {result['error']}")
            await update.message.reply_text(
                f"❌ Sorry, I encountered an error:\n\n"
                f"`{result['error']}`\n\n"
                f"Please try again later or check your Modal endpoint.",
                parse_mode="Markdown",
            )
            return
        
        if not result.get("text") or not result["text"].strip():
            await update.message.reply_text(
                "⚠️ The model generated an empty response. Please try rephrasing your message."
            )
            return
        
        response_text = result["text"]
        tokens = result.get("tokens", 0)
        
        # Add assistant response to conversation history
        add_to_conversation(user_id, "assistant", response_text)
        logger.debug(f"🤖 [Conversation] Added assistant message. Total messages: {len(get_user_conversation(user_id))}")
        
        logger.info(f"✅ [Telegram] Response generated: {tokens} tokens")
        logger.info(f"📄 [Telegram] Response: {response_text[:100]}...")
        logger.info("=" * 60)
        
        await update.message.reply_text(response_text)
            
    except Exception as e:
        logger.error(f"❌ [Telegram] Unexpected error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ An unexpected error occurred:\n\n"
            f"`{str(e)}`\n\n"
            f"Please try again later.",
            parse_mode="Markdown",
        )


def main() -> None:
    """Start the bot."""
    print("🤖 VibeTune Telegram Bot")
    print("═══════════════════════════════════════")
    print(f"📡 Modal Endpoint: {MODAL_INFERENCE_URL}")
    print(f"🎯 Default Model: {DEFAULT_MODEL_ID}")
    print(f"📋 System Prompt: {DEFAULT_SYSTEM_PROMPT}")
    print(f"🌡️  Temperature: {DEFAULT_TEMPERATURE}")
    print(f"📝 Max Tokens: {DEFAULT_MAX_TOKENS}")
    print(f"🎯 Top P: {DEFAULT_TOP_P}")
    print("═══════════════════════════════════════")
    print("✅ Bot is ready to receive messages!")
    print("💡 Send /start to your bot to begin")
    print()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
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
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
