#!/usr/bin/env python3
"""
VibeTune Telegram Bot

Full implementation of a Telegram bot that uses your Modal finetuned models.

Setup:
1. Get a Telegram bot token from @BotFather
2. Install dependencies: pip install -r requirements.txt
3. Set TELEGRAM_BOT_TOKEN environment variable
4. Run: python telegram_bot.py

Usage:
- Send any message to chat with your finetuned model
- Use /model <modelId> to switch to a specific trained model
- Use /base to use the base model
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MODAL_INFERENCE_URL = os.getenv(
    "MODAL_INFERENCE_URL",
    
)

DEFAULT_MODEL_ID = os.getenv("DEFAULT_MODEL_ID", None)

DEFAULT_TEMPERATURE = float(os.getenv("DEFAULT_TEMPERATURE", "0.7"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "250"))
DEFAULT_TOP_P = float(os.getenv("DEFAULT_TOP_P", "0.9"))

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


def get_user_preferences(user_id: int) -> Dict[str, any]:
    """Get user preferences or return defaults."""
    if user_id not in user_preferences:
        user_preferences[user_id] = {
            "model_id": DEFAULT_MODEL_ID,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
    return user_preferences[user_id]



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
    
    logger.info(f"[Modal] Calling inference endpoint: {inference_url}")
    logger.info(f"[Modal] Model ID: {model_id or 'base'}")
    logger.info(f"[Modal] Prompt: {prompt[:100]}...")
    
    try:
        response = requests.post(
            inference_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=180,  
        )
        
        if not response.ok:
            error_text = response.text
            logger.error(f"[Modal] API error: {response.status_code} - {error_text}")
            return {
                "text": "",
                "tokens": 0,
                "error": f"Modal API error: {response.status_code} - {error_text}",
            }
        
        data = response.json()
        logger.info(f"[Modal] Response received: {data.get('tokens', 0)} tokens")
        
        return {
            "text": data.get("text", "No response generated"),
            "tokens": data.get("tokens", 0),
        }
        
    except requests.exceptions.Timeout:
        logger.error("[Modal] Request timeout")
        return {
            "text": "",
            "tokens": 0,
            "error": "Request timeout - Modal endpoint took too long to respond",
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"[Modal] Request error: {e}")
        return {
            "text": "",
            "tokens": 0,
            "error": f"Failed to connect to Modal: {str(e)}",
        }
    except Exception as e:
        logger.error(f"[Modal] Unexpected error: {e}")
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

I'm powered by models finetuned by developers at Gigsama. Just send me a message and I'll respond using your trained model!


 Commands:
/help - Show all commands
/model <modelId> - Switch to a specific trained model
  Example: /model training-12345
/base - Use the base model (no finetuning)
/status - Show current settings
/settings - Configure generation parameters

💡 Tip: You can use any trained model ID from your Modal training jobs.
    """
    
    await update.message.reply_text(welcome_message.strip())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_message = """
📚 Available Commands:

/model <modelId> - Switch to a trained model
  Example: /model training-12345
  Example: /model qwen-finetuned-67890

/base - Switch back to the base model

/status - Show current model and settings

/settings - Configure temperature, max tokens, etc.

/help - Show this help message

💬 Just send a regular message to chat with your model!
    """
    await update.message.reply_text(help_message.strip())


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model <modelId> command."""
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "❌ Please provide a model ID\n\n"
            "Example: /model training-12345\n"
            "Example: /model qwen-finetuned-67890"
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
    
    await update.message.reply_text(
        "✅ Switched to base model\n\n" "Now send me a message to test it!"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    user_id = update.effective_user.id
    prefs = get_user_preferences(user_id)
    current_model = format_model_id(prefs["model_id"])
    
    status_message = f"""
📊 Current Settings:

🤖 Model: {current_model}
🌡️ Temperature: {prefs['temperature']}
📝 Max Tokens: {prefs['max_tokens']}
🎯 Top P: {DEFAULT_TOP_P}

🔗 Modal Endpoint: {MODAL_INFERENCE_URL}

💡 Use /model <modelId> to switch models
💡 Use /settings to change parameters
    """
    
    await update.message.reply_text(status_message.strip())


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settings command."""
    settings_message = f"""
⚙️ Settings Configuration:

To change settings, use environment variables when starting the bot:

• DEFAULT_TEMPERATURE - Creativity (0.0-2.0, default: 0.7)
• DEFAULT_MAX_TOKENS - Response length (default: 250)
• DEFAULT_TOP_P - Sampling parameter (0.0-1.0, default: 0.9)
• DEFAULT_MODEL_ID - Default model to use

Example:
export DEFAULT_TEMPERATURE=0.8
export DEFAULT_MAX_TOKENS=500
export DEFAULT_MODEL_ID=training-12345
python telegram_bot.py

Current defaults:
• Temperature: {DEFAULT_TEMPERATURE}
• Max Tokens: {DEFAULT_MAX_TOKENS}
• Top P: {DEFAULT_TOP_P}
• Default Model: {DEFAULT_MODEL_ID or 'base'}
    """
    await update.message.reply_text(settings_message.strip())


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
        
        logger.info(f"[Telegram] Message from user {user_id}")
        logger.info(f"[Telegram] Using model: {model_id or 'base'}")
        logger.info(f"[Telegram] Prompt: {text[:100]}...")
        
        result = call_modal_inference(
            text,
            model_id,
            prefs["temperature"],
            prefs["max_tokens"],
            DEFAULT_TOP_P,
        )
        
        if result.get("error"):
            logger.error(f"[Telegram] Error: {result['error']}")
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
        
        logger.info(f"[Telegram] Response: {result['text'][:100]}...")
        logger.info(f"[Telegram] Tokens: {result.get('tokens', 0)}")
        
        await update.message.reply_text(result["text"])
        
        if result.get("tokens", 0) > 0:
            logger.info(f"[Telegram] ✅ Sent response ({result['tokens']} tokens)")
            
    except Exception as e:
        logger.error(f"[Telegram] Unexpected error: {e}", exc_info=True)
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
    if DEFAULT_MODEL_ID:
        print(f"🎯 Default Model: {DEFAULT_MODEL_ID}")
    else:
        print(f"🎯 Default Model: base (no finetuning)")
    print(f"🌡️  Temperature: {DEFAULT_TEMPERATURE}")
    print(f"📝 Max Tokens: {DEFAULT_MAX_TOKENS}")
    print(f"🎯 Top P: {DEFAULT_TOP_P}")
    print("═══════════════════════════════════════")
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("base", base_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("settings", settings_command))
    
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    print("✅ Bot is ready to receive messages!")
    print("💡 Send /start to your bot to begin")
    print()
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

