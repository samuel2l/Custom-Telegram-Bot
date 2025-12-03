# Telegram Bot Setup Guide

Complete guide to run your VibeTune Telegram bot with Modal finetuned models.

## Quick Start

### 1. Get a Telegram Bot Token

1. Open Telegram and search for `@BotFather`
2. Start a conversation and send `/newbot`
3. Follow the instructions:
   - Choose a name for your bot (e.g., "My VibeTune Bot")
   - Choose a username (e.g., "my_vibetune_bot")
4. Copy the bot token (looks like: `8332478522:AAEpwTXfBFlirazWtz93pPAqiHYqb8pf4eo)

### 2. Install Dependencies

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install python-telegram-bot requests
```

**Requirements:**
- Python 3.8 or higher
- `python-telegram-bot` library
- `requests` library

### 3. Set Environment Variables

Export these variables:

```bash
# Required - Get from @BotFather
export TELEGRAM_BOT_TOKEN=8332478522:AAEpwTXfBFlirazWtz93pPAqiHYqb8pf4eo

# Optional - Defaults to your Modal URL
export MODAL_INFERENCE_URL=https://affum3331--vibetune-inference-inferenceservice-serve.modal.run

# Optional - Set a default finetuned model to use
export DEFAULT_MODEL_ID=training-12345

# Optional - Generation parameters
export DEFAULT_TEMPERATURE=0.7
export DEFAULT_MAX_TOKENS=250
export DEFAULT_TOP_P=0.9
```

### 4. Run the Bot

```bash
python telegram_bot.py
```

Or make it executable and run directly:

```bash
chmod +x telegram_bot.py
./telegram_bot.py
```

You should see:
```
🤖 VibeTune Telegram Bot
═══════════════════════════════════════
📡 Modal Endpoint: https://affum3331--vibetune-inference-inferenceservice-serve.modal.run
🎯 Default Model: base (no finetuning)
🌡️  Temperature: 0.7
📝 Max Tokens: 250
🎯 Top P: 0.9
═══════════════════════════════════════
✅ Bot is ready to receive messages!
💡 Send /start to your bot to begin
```

### 5. Test the Bot

1. Open Telegram on your phone
2. Search for your bot (the username you gave it)
3. Send `/start` to see the welcome message
4. Send any message to chat with your model!

## Using Finetuned Models

### How to Use a Specific Model

The bot supports using specific finetuned models via the `modelId` parameter. Here's how:

#### Option 1: Set Default Model (Environment Variable)

```bash
export DEFAULT_MODEL_ID=training-12345
python telegram_bot.py
```

Now all messages will use that model by default.

#### Option 2: Switch Models via Command

Users can switch models on the fly:

```
/model training-12345
```

This switches to that specific model. The bot will remember the preference for that user.

#### Option 3: Use Base Model

```
/base
```

Switches back to the base model (no finetuning).

### Finding Your Model IDs

Your trained model IDs are typically in one of these formats:
- `training-12345` - From Modal training jobs
- `qwen-finetuned-12345` - From specific training runs

You can find them:
1. In your Modal dashboard after training completes
2. In your database (Project config → `trainedModelId`)
3. From the training callback/status endpoint

### Model ID Format

Model IDs must match this pattern: `^[a-zA-Z0-9\-_]+$`
- Letters, numbers, dashes, and underscores only
- Examples: `training-12345`, `qwen-finetuned-67890`, `my_model_v1`

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and instructions |
| `/help` | Show all available commands |
| `/model <modelId>` | Switch to a specific trained model |
| `/base` | Switch back to the base model |
| `/status` | Show current model and settings |
| `/settings` | Show configuration options |

## How It Works

### Architecture

```
Your Phone (Telegram App)
    ↓ sends message
Telegram Servers
    ↓ forwards to
Your Bot Server (telegram-bot.ts)
    ↓ HTTP POST to
Modal Inference Endpoint (/inference)
    ↓ with modelId parameter
Your Finetuned Model (or base model)
    ↓ returns response
Your Bot Server
    ↓ sends back
Telegram → Your Phone
```

### Request Flow

1. **User sends message** → Telegram Bot receives it
2. **Bot calls Modal** → POST to `${MODAL_INFERENCE_URL}/inference` with:
   ```json
   {
     "prompt": "user's message",
     "modelId": "training-12345",  // or null for base
     "temperature": 0.7,
     "max_tokens": 250,
     "top_p": 0.9
   }
   ```
3. **Modal processes** → Uses your finetuned model (or base) to generate response
4. **Bot sends response** → Returns the generated text to the user

### Model Selection

The bot uses this priority:
1. User's current preference (set via `/model` command)
2. `DEFAULT_MODEL_ID` environment variable
3. Base model (null)

Each user can have their own model preference, stored in memory (use a database for production).

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ Yes | - | Bot token from @BotFather |
| `MODAL_INFERENCE_URL` | ❌ No | Your URL | Modal inference endpoint |
| `DEFAULT_MODEL_ID` | ❌ No | `null` | Default model to use |
| `DEFAULT_TEMPERATURE` | ❌ No | `0.7` | Generation temperature (0-2) |
| `DEFAULT_MAX_TOKENS` | ❌ No | `250` | Maximum tokens to generate |
| `DEFAULT_TOP_P` | ❌ No | `0.9` | Nucleus sampling parameter |

### Generation Parameters

- **Temperature** (0.0-2.0): Controls randomness
  - Lower (0.1-0.3): More deterministic, focused
  - Higher (0.7-1.0): More creative, varied
- **Max Tokens** (1-4096): Maximum response length
- **Top P** (0.0-1.0): Nucleus sampling threshold

## Deployment

### Local Development

```bash
export TELEGRAM_BOT_TOKEN=your_token
python telegram_bot.py
```

### Production Deployment

You can deploy to:

#### Heroku
```bash
heroku create your-bot-name
heroku config:set TELEGRAM_BOT_TOKEN=your_token
heroku config:set DEFAULT_MODEL_ID=training-12345
# Create Procfile with: worker: python telegram_bot.py
git push heroku main
```

#### Railway
1. Connect your GitHub repo
2. Set environment variables in Railway dashboard
3. Set start command: `python telegram_bot.py`
4. Deploy automatically

#### Fly.io
```bash
fly launch
fly secrets set TELEGRAM_BOT_TOKEN=your_token
# Update fly.toml with start command
fly deploy
```

#### Your Own Server
```bash
# Using systemd
sudo nano /etc/systemd/system/vibetune-bot.service
# Add service file, then:
sudo systemctl enable vibetune-bot
sudo systemctl start vibetune-bot

# Or using screen/tmux
screen -S telegram-bot
python telegram_bot.py
# Press Ctrl+A then D to detach
```

### Production Considerations

1. **Use Webhooks** (instead of polling) for better performance:
   ```python
   application.run_webhook(
       listen="0.0.0.0",
       port=8443,
       webhook_url="https://your-domain.com/webhook",
   )
   ```

2. **Store user preferences in database** instead of memory (use SQLite, PostgreSQL, etc.)

3. **Add rate limiting** to prevent abuse

4. **Add error monitoring** (Sentry, etc.)

5. **Add logging** to a service (Datadog, etc.)

6. **Use environment variable files** (`.env`) with `python-dotenv`

## Troubleshooting

### Bot doesn't respond

1. ✅ Check `TELEGRAM_BOT_TOKEN` is set correctly
2. ✅ Verify the bot is running (check console logs)
3. ✅ Make sure you've started a conversation with `/start`
4. ✅ Check Telegram for bot status

### Modal endpoint errors

1. ✅ Verify `MODAL_INFERENCE_URL` is correct
2. ✅ Check Modal dashboard to ensure endpoint is deployed
3. ✅ Test the endpoint directly:
   ```bash
   curl -X POST https://affum3331--vibetune-inference-inferenceservice-serve.modal.run/inference \
     -H "Content-Type: application/json" \
     -d '{
       "prompt": "Hello",
       "temperature": 0.7,
       "max_tokens": 100
     }'
   ```

### Model not found

1. ✅ Verify the model ID format (alphanumeric, dashes, underscores)
2. ✅ Check that the model exists in your Modal volume
3. ✅ Try using `/base` to test with the base model first
4. ✅ Check Modal logs for model loading errors

### Connection errors

1. ✅ Check your internet connection
2. ✅ Verify Modal endpoint is accessible
3. ✅ Check for firewall/proxy issues
4. ✅ Review bot console logs for detailed errors

## Example Usage

```
User: /start
Bot: 🤖 Welcome to VibeTune Bot!
     ...
     📊 Current Settings:
     • Model: base model
     ...

User: /model training-12345
Bot: ✅ Switched to model: `training-12345`
     Now send me a message to test it!

User: What's the weather like?
Bot: [Response from your finetuned model]

User: /status
Bot: 📊 Current Settings:
     🤖 Model: training-12345
     🌡️ Temperature: 0.7
     ...
```

## Next Steps

- Add conversation history/memory
- Support for multiple users with different models
- Add admin commands to manage models
- Integrate with your tool calling system
- Add support for voice messages
- Add support for images/documents
- Deploy to production with webhooks

## Support

If you encounter issues:
1. Check the console logs for detailed error messages
2. Verify all environment variables are set correctly
3. Test the Modal endpoint directly
4. Check Modal dashboard for deployment status

