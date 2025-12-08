# TELEGRAM BOT USING MODAL FINETUNED MODELS

A Telegram bot that integrates with your VibeTune web application, allowing users to chat with your finetuned models and report issues directly from Telegram.

## Features

- 🤖 Chat with Modal finetuned models via Telegram
- 📝 Report problems with conversation context
- 🗑️ Clear conversation history
- 💬 Conversation history tracking (last 20 messages)
- 🔗 Integration with VibeTune web application
- 📊 Admin dashboard to view and manage reports

## Prerequisites

- Python 3.8 or higher
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A deployed VibeTune web application (for report submission)
- Modal inference endpoint (optional, for model inference)

## Installation

### 1. Get a Telegram Bot Token

1. Open Telegram and search for `@BotFather`
2. Start a conversation and send `/newbot`
3. Follow the instructions:
   - Choose a name for your bot (e.g., "My VibeTune Bot")
   - Choose a username (e.g., "my_vibetune_bot")
4. Copy the bot token (looks like: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 2. Install Dependencies

```bash
pip install python-telegram-bot requests
```

**Requirements:**

- Python 3.8 or higher
- `python-telegram-bot` library (v20+)
- `requests` library

### 3. Set Environment Variables

Export these variables:

```bash
# Required - Get from @BotFather
export TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Required - Your VibeTune web application URL
export APP_URL=https://your-app-url.com
# For local development:
# export APP_URL=http://localhost:3000

# Optional - Defaults to your Modal URL
export MODAL_INFERENCE_URL=your_modal_inference_url

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

You should see:

```
🤖 VibeTune Telegram Bot
═══════════════════════════════════════
📡 Modal Endpoint: your_endpoint
🌐 Web App URL: https://your-app-url.com
🎯 Default Model: base (no finetuning)
🌡️  Temperature: 0.7
📝 Max Tokens: 250
🎯 Top P: 0.9
═══════════════════════════════════════
✅ Bot is ready to receive messages!
💡 Send /start to your bot to begin
```

## Commands

| Command                 | Description                                                              |
| ----------------------- | ------------------------------------------------------------------------ |
| `/start`                | Welcome message and instructions                                         |
| `/help`                 | Show all available commands                                              |
| `/report <description>` | Report a problem with the bot. Includes conversation history for context |
| `/clear`                | Clear your conversation history and start fresh                          |
| `/model <modelId>`      | Switch to a specific trained model                                       |
| `/base`                 | Switch back to the base model                                            |
| `/status`               | Show current model and settings                                          |
| `/settings`             | Show configuration options                                               |

## How It Works

### Architecture

```
Your Phone (Telegram App)
    ↓ sends message
Telegram Servers
    ↓ forwards to
Your Bot Server (telegram_bot.py)
    ↓ HTTP POST to
Modal Inference Endpoint (/inference)
    ↓ with modelId parameter
Your Finetuned Model (or base model)
    ↓ returns response
Your Bot Server
    ↓ sends back
Telegram → Your Phone
```

### Report Problem Flow

When a user sends `/report <description>`:

1. **Bot collects data:**

   - User's Telegram ID and username
   - Bot's username
   - Report description
   - Last 20 messages of conversation history

2. **Bot sends to web app:**

   - POST to `${APP_URL}/api/reports`
   - Includes all collected data

3. **Bot clears conversation:**

   - Clears user's conversation history
   - Creates a new conversation
   - Sends confirmation message

4. **Admin views report:**
   - Reports appear in `/admin/reports` page
   - Can filter by bot username
   - Can view full conversation history in a chat interface

### Conversation History

The bot maintains conversation history for each user:

- Stores last 20 messages (user + assistant)
- Automatically includes history when reporting problems
- Cleared when user sends `/clear` command
- Resets after submitting a report

### Request Flow

1. **User sends message** → Telegram Bot receives it
2. **Bot calls Modal** → POST to `${MODAL_INFERENCE_URL}/inference` with:
   ```json
   {
     "prompt": "user's message with conversation history",
     "modelId": "training-12345", // or null for base
     "temperature": 0.7,
     "max_tokens": 250,
     "top_p": 0.9
   }
   ```
3. **Modal processes** → Uses your finetuned model (or base) to generate response
4. **Bot sends response** → Returns the generated text to the user
5. **Bot saves to history** → Adds both user and assistant messages to conversation history

## Configuration

### Environment Variables

| Variable              | Required | Default  | Description                       |
| --------------------- | -------- | -------- | --------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | ✅ Yes   | -        | Bot token from @BotFather         |
| `APP_URL`             | ✅ Yes   | -        | Your VibeTune web application URL |
| `MODAL_INFERENCE_URL` | ❌ No    | Your URL | Modal inference endpoint          |
| `DEFAULT_MODEL_ID`    | ❌ No    | `null`   | Default model to use              |
| `DEFAULT_TEMPERATURE` | ❌ No    | `0.7`    | Generation temperature (0-2)      |
| `DEFAULT_MAX_TOKENS`  | ❌ No    | `250`    | Maximum tokens to generate        |
| `DEFAULT_TOP_P`       | ❌ No    | `0.9`    | Nucleus sampling parameter        |

### Generation Parameters

- **Temperature** (0.0-2.0): Controls randomness
  - Lower (0.1-0.3): More deterministic, focused
  - Higher (0.7-1.0): More creative, varied
- **Max Tokens** (1-4096): Maximum response length
- **Top P** (0.0-1.0): Nucleus sampling threshold

## Using Finetuned Models

### How to Use a Specific Model

The bot supports using specific finetuned models via the `modelId` parameter.

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

## Report Problem Feature

### For Users

To report a problem with the bot:

1. Send `/report <description>` where `<description>` is your issue description
2. The bot will:
   - Collect your conversation history (last 20 messages)
   - Send the report to the admin dashboard
   - Clear your conversation history
   - Start a new conversation

**Example:**

```
User: /report The bot is giving incorrect responses about weather
Bot: ✅ Report submitted! Your conversation history has been included for context.
     Starting a new conversation...
```

### For Admins

Reports are viewable in the web application:

1. Navigate to `/admin/reports` (requires authentication)
2. View all reports with:
   - Status (pending/resolved)
   - Bot username
   - User information
   - Report description
   - Timestamp
3. Filter reports by bot username
4. Click on a report to see:
   - Full report details
   - Conversation history (displayed as a chat interface)
   - User and bot information

## Deployment

### Local Development

```bash
export TELEGRAM_BOT_TOKEN=your_token
export APP_URL=http://localhost:3000
python telegram_bot.py
```

**Note:** Make sure your Next.js app is running on `localhost:3000` for reports to work.

### Production Deployment

You can deploy to:

#### Railway

1. Create a new service in Railway
2. Connect your GitHub repo (bot folder)
3. Set environment variables in Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `APP_URL` (your production web app URL)
   - `MODAL_INFERENCE_URL` (optional)
   - `DEFAULT_MODEL_ID` (optional)
4. Create `Procfile` with: `worker: python telegram_bot.py`
5. Create `requirements.txt` with:
   ```
   python-telegram-bot>=20.0
   requests>=2.31.0
   ```
6. Create `runtime.txt` with: `python-3.11` (or your Python version)
7. Deploy automatically

#### Heroku

```bash
heroku create your-bot-name
heroku config:set TELEGRAM_BOT_TOKEN=your_token
heroku config:set APP_URL=https://your-app-url.com
heroku config:set DEFAULT_MODEL_ID=training-12345
# Create Procfile with: worker: python telegram_bot.py
git push heroku main
```

#### Fly.io

```bash
fly launch
fly secrets set TELEGRAM_BOT_TOKEN=your_token
fly secrets set APP_URL=https://your-app-url.com
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

### Report submission fails

1. ✅ Verify `APP_URL` is set correctly
2. ✅ Check that your web app is running and accessible
3. ✅ Verify `/api/reports` endpoint exists and is working
4. ✅ Check bot console logs for error messages
5. ✅ Test the endpoint directly:
   ```bash
   curl -X POST https://your-app-url.com/api/reports \
     -H "Content-Type: application/json" \
     -d '{
       "telegramUserId": 123456,
       "username": "test_user",
       "botUsername": "test_bot",
       "reportText": "Test report",
       "conversationHistory": []
     }'
   ```

### Modal endpoint errors

1. ✅ Verify `MODAL_INFERENCE_URL` is correct
2. ✅ Check Modal dashboard to ensure endpoint is deployed
3. ✅ Test the endpoint directly:
   ```bash
   curl -X POST https://your-modal-endpoint/inference \
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

User: Hello, how are you?
Bot: [Response from your model]

User: /model training-12345
Bot: ✅ Switched to model: `training-12345`
     Now send me a message to test it!

User: What's the weather like?
Bot: [Response from your finetuned model]

User: /report The bot is giving incorrect weather information
Bot: ✅ Report submitted! Your conversation history has been included for context.
     Starting a new conversation...

User: /status
Bot: 📊 Current Settings:
     🤖 Model: training-12345
     🌡️ Temperature: 0.7
     ...

User: /clear
Bot: ✅ Conversation history cleared! Starting fresh...
```

## Integration with Web Application

The bot integrates with your VibeTune web application:

1. **Report Submission:**

   - Reports are sent to `${APP_URL}/api/reports`
   - Includes conversation history as JSON
   - Admin can view reports in `/admin/reports`

2. **Authentication:**

   - Reports endpoint requires authentication
   - Only authenticated users can view reports

3. **Data Format:**
   - Reports include: `telegramUserId`, `username`, `botUsername`, `reportText`, `conversationHistory`
   - Conversation history is an array of message objects with `role` and `content`

## Next Steps

- Add conversation history/memory across sessions
- Support for multiple users with different models
- Add admin commands to manage models
- Integrate with your tool calling system
- Add support for voice messages
- Add support for images/documents
- Deploy to production with webhooks
- Add database persistence for user preferences
- Add rate limiting and abuse prevention

## Support

If you encounter issues:

1. Check the console logs for detailed error messages
2. Verify all environment variables are set correctly
3. Test the Modal endpoint directly
4. Check Modal dashboard for deployment status
5. Verify your web app is running and accessible
6. Check the `/admin/reports` page to see if reports are being received

## License

This bot is part of the VibeTune project.
