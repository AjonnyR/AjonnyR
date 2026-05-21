# Telegram Expense Bot

Personal-finance Telegram bot for Israeli accounts.

Send it your **Bank Hapoalim** PDF (checking account / עו"ש) and your **Max** Excel
(credit-card statement) — it returns a monthly report with:

- Income (recognised automatically from the Hapoalim statement)
- Expenses, categorised by Gemini AI
- Net cash flow (income − expenses)
- Top merchants and savings tips

Hapoalim is treated as the main account where the money sits. The monthly
Max debit line in Hapoalim is detected and **not counted twice** — its breakdown
comes from the Max file instead.

If the Gemini quota is exhausted, the bot still returns a local report with
totals, income, net flow, and top merchants (no AI categorisation).

---

## Deploying for free on Render

Render's free Web Service tier sleeps after ~15 min of inactivity and takes
~30–60 seconds to wake. That matches what we want.

### 1. Get a Telegram bot token

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, choose a name and a username ending in `bot`.
3. Copy the token it gives you. Looks like `1234567890:ABCdef...`.

### 2. Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey>.
2. Click **Create API key** (sign in with a Google account if needed).
3. Copy the key.

> Note: the free tier of `gemini-2.0-flash` has a daily quota that resets every
> 24 h. If you hit it, the bot falls back to a local report.

### 3. Push this repo to GitHub

If you haven't already:

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

### 4. Create the Render Web Service

1. Sign up at <https://render.com> (free, no card required for the free tier).
2. Click **New +** → **Web Service**.
3. Connect your GitHub account and pick this repo.
4. Fill in:
   - **Name**: `expense-bot` (or anything — this becomes part of the URL)
   - **Region**: Frankfurt (closest to Israel) or any
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Instance Type**: **Free**

5. Scroll to **Environment Variables** and add three:

   | Key                  | Value                                                              |
   |----------------------|--------------------------------------------------------------------|
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather                                           |
   | `GEMINI_API_KEY`     | the key from Google AI Studio                                      |
   | `WEBHOOK_URL`        | `https://<your-service-name>.onrender.com` (whatever you named it) |

   The `WEBHOOK_URL` must match the name you chose in step 4. E.g. if you named
   the service `expense-bot`, set it to `https://expense-bot.onrender.com`
   (no trailing slash).

6. Click **Create Web Service**. First deploy takes 2–5 minutes.

### 5. Test it

1. Open Telegram, find your bot by its username, send `/start`.
2. Send a Hapoalim PDF, then a Max Excel.
3. You should get a report back.

If the bot has been idle 15+ minutes, the first message takes ~30 seconds to get
a response (Render is waking the service). Subsequent messages are instant
until it sleeps again.

---

## Running locally (development)

```bash
python -m venv .venv
.venv\Scripts\activate         # Windows
# or: source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt

set TELEGRAM_BOT_TOKEN=...      # Windows cmd
set GEMINI_API_KEY=...
# or: $env:TELEGRAM_BOT_TOKEN="..."   # Windows PowerShell

python bot.py
```

Without `WEBHOOK_URL` set, the bot runs in polling mode — fine for local
testing, no public URL needed.

---

## Commands

- `/start` — welcome message
- `/help` — usage instructions
- `/reset` — clear cached files for the current user

---

## File layout

| File               | Purpose                                              |
|--------------------|------------------------------------------------------|
| `bot.py`           | Telegram handlers and entry point                    |
| `file_processor.py`| Parses Hapoalim PDF and Max Excel into transactions  |
| `analyzer.py`      | Calls Gemini, formats the report, local fallback     |
| `requirements.txt` | Python dependencies                                  |
| `Procfile`         | Process command (`web: python bot.py`)               |

---

## Updating after a code change

```bash
git add .
git commit -m "<what changed>"
git push
```

Render auto-deploys on push to `main`.
