import os
import asyncio
import tempfile
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
from file_processor import process_pdf, process_excel
from analyzer import analyze_full, test_gemini
from categories import (
    CATEGORY_RULES,
    override as override_category,
    persist as persist_categories,
    persistence_enabled,
)

# Max uncategorised merchants to expose as button messages per upload.
MAX_BUTTON_PROMPTS = 5
# How many categories per inline-keyboard row.
KEYBOARD_COLS = 2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TMP = tempfile.gettempdir()

# זוכר את הקבצים של כל משתמש עד שהוא שולח את שניהם
user_files = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "שלום! 👋 אני הבוט שיעזור לך לעקוב אחרי הכסף שלך.\n\n"
        "📤 *שלושת הקבצים שאני צריך:*\n"
        "1️⃣ PDF של *עו\"ש פועלים* (תנועות בחשבון)\n"
        "2️⃣ Excel של *כאל פועלים* (פירוט חיובי כרטיס)\n"
        "3️⃣ Excel של *מקס* (פירוט חיובי כרטיס)\n\n"
        "ברגע ששלושתם הגיעו אני אריץ ניתוח אוטומטית — בלי כפילויות בין "
        "סיכומי הכרטיסים שבעו\"ש לבין הפירוטים שבקבצים הנפרדים. "
        "אני גם אבדוק שכל סיכום בעו\"ש תואם לסך הכרטיס המתאים.\n\n"
        "אפשר לשלוח את הקבצים בכל סדר. אם יש לך רק חלק מהם — שלח /analyze "
        "כדי להריץ עם מה שיש.",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *פקודות זמינות*\n\n"
        "/start — הסבר התחלתי\n"
        "/reset — נקה קבצים ששלחת ותתחיל מההתחלה\n"
        "/analyze — הרץ ניתוח על מה שכבר שלחת\n"
        "/api — בדוק שמפתח Gemini עובד\n"
        "/github — אבחן את חיבור ה-GitHub (לשמירת קטגוריות)\n"
        "/correct `<תיאור> = <קטגוריה>` — תקן קטגוריזציה ותשמור לעולם\n\n"
        "*שימוש רגיל:* שלח לי שלושה קבצים:\n"
        "1️⃣ PDF של עו\"ש פועלים\n"
        "2️⃣ Excel של כאל פועלים\n"
        "3️⃣ Excel של מקס\n\n"
        "כשכל השלושה הגיעו אני אנתח אוטומטית. הסדר לא משנה.",
        parse_mode="Markdown"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_files:
        del user_files[user_id]
    await update.message.reply_text("✅ איפסתי את הנתונים שלך. תוכל לשלוח קבצים חדשים.")


async def analyze_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger analysis explicitly with whatever files are queued."""
    user_id = update.effective_user.id
    bucket = user_files.get(user_id, {})

    if "poalim" not in bucket:
        await update.message.reply_text(
            "⚠️ עדיין לא קיבלתי PDF של עו\"ש פועלים. שלח/י אותו לפני /analyze."
        )
        return

    all_tx = list(bucket["poalim"]) + list(bucket.get("max", [])) + list(bucket.get("cal", []))
    await update.message.reply_text("🔍 מנתח את הנתונים...")

    try:
        report, uncategorized = await analyze_full(
            all_tx, closing_balance=bucket.get("closing_balance"),
        )
    except Exception as e:
        logger.exception("analyze_full raised in /analyze")
        await update.message.reply_text(
            f"❌ שגיאה ב-analyze_full:\n{type(e).__name__}: {e}"
        )
        return

    try:
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        logger.exception("reply_text(Markdown) raised in /analyze")
        await update.message.reply_text(
            f"⚠️ שלחתי את הדוח בלי Markdown ({type(e).__name__}: {e}):"
        )
        await update.message.reply_text(report)

    if uncategorized:
        try:
            await _send_category_prompts(update, context, uncategorized)
        except Exception as e:
            logger.exception("_send_category_prompts raised in /analyze")
            await update.message.reply_text(
                f"⚠️ לא הצלחתי לשלוח כפתורי קטגוריות: {type(e).__name__}: {e}"
            )

    del user_files[user_id]


async def correct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User correction: /correct <description> = <category>

    Saves to the in-memory LEARNED dict and, if GitHub is configured,
    commits learned.json to the repo so the correction survives restarts.
    """
    raw = (update.message.text or "").split(None, 1)
    text = raw[1].strip() if len(raw) == 2 else ""
    if "=" not in text:
        cats = ", ".join(CATEGORY_RULES.keys())
        await update.message.reply_text(
            "שימוש: /correct <תיאור> = <קטגוריה>\n\n"
            f"קטגוריות אפשריות:\n{cats}",
        )
        return

    desc, _, cat = text.partition("=")
    desc, cat = desc.strip(), cat.strip()
    if not desc or not cat:
        await update.message.reply_text(
            "❌ חסר תיאור או קטגוריה. שימוש: /correct <תיאור> = <קטגוריה>"
        )
        return

    if not override_category(desc, cat):
        cats = ", ".join(CATEGORY_RULES.keys())
        await update.message.reply_text(
            f"❌ הקטגוריה \"{cat}\" לא קיימת.\n\nקטגוריות אפשריות:\n{cats}"
        )
        return

    await update.message.reply_text(f"✏️ עודכן בזיכרון: *{desc}* → *{cat}*\nשומר...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(
        None, persist_categories, f"User correction: {desc} → {cat}"
    )
    if ok:
        await update.message.reply_text(
            "✅ נשמר בריפו. Render יעלה גרסה חדשה בעוד 2-3 דקות "
            "ואז התיקון יישאר לתמיד."
        )
    else:
        await update.message.reply_text(
            f"⚠️ התיקון בזיכרון אבל לא נשמר לעולם:\n\n{err}\n\n"
            f"לאבחון מלא שלח /github",
        )


async def github_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test GitHub credentials end-to-end and report what's wrong."""
    await update.message.reply_text("בודק את הגדרות GitHub...")
    loop = asyncio.get_event_loop()
    try:
        import learning_store
        report = await loop.run_in_executor(None, learning_store.diagnose)
    except Exception as e:
        report = f"❌ שגיאה: {e}"
    await update.message.reply_text(report, parse_mode="Markdown")


async def api_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostic: ping Gemini with a tiny prompt and show the real error."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        await update.message.reply_text("❌ GEMINI_API_KEY לא מוגדר בכלל ב-Render.")
        return
    masked = f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else "(too short)"
    await update.message.reply_text(f"בודק את המפתח `{masked}`...", parse_mode="Markdown")
    ok, message = await test_gemini(api_key)
    if ok:
        await update.message.reply_text(f"✅ המפתח עובד. תגובה: {message}")
    else:
        # Pre-escape underscores so Markdown doesn't choke on error bodies.
        safe = message.replace("_", "\\_")
        await update.message.reply_text(f"❌ נכשל:\n```\n{safe}\n```", parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document
    file_name = doc.file_name.lower()

    if user_id not in user_files:
        user_files[user_id] = {}

    if file_name.endswith(".pdf"):
        await update.message.reply_text("📥 קיבלתי PDF — מעבד את עו\"ש פועלים...")
        file = await context.bot.get_file(doc.file_id)
        file_path = os.path.join(TMP, f"{user_id}_{file.file_unique_id}.pdf")
        await file.download_to_drive(file_path)

        transactions, closing_balance, pdf_type = process_pdf(file_path)
        if pdf_type == "unknown" or not transactions:
            await update.message.reply_text(
                "⚠️ לא הצלחתי לקרוא עסקאות מה-PDF. ודא שזה PDF של עו\"ש פועלים."
            )
            return

        user_files[user_id]["poalim"] = transactions
        user_files[user_id]["closing_balance"] = closing_balance
        await update.message.reply_text(
            f"✅ עיבדתי PDF של עו\"ש פועלים — *{len(transactions)}* תנועות.",
            parse_mode="Markdown",
        )

    elif file_name.endswith((".xlsx", ".xls")):
        await update.message.reply_text("📥 קיבלתי Excel — מזהה אם מקס או כאל...")
        file = await context.bot.get_file(doc.file_id)
        file_path = os.path.join(TMP, f"{user_id}_{file.file_unique_id}.xlsx")
        await file.download_to_drive(file_path)

        transactions = process_excel(file_path)
        if not transactions:
            await update.message.reply_text(
                "⚠️ לא הצלחתי לקרוא עסקאות מה-Excel. ודא שזה קובץ מקס או כאל פועלים תקין."
            )
            return

        source = transactions[0].get("source", "")
        total = sum(t["amount"] for t in transactions)
        if source == "כאל פועלים":
            user_files[user_id]["cal"] = transactions
            await update.message.reply_text(
                f"✅ עיבדתי קובץ כאל פועלים — *{len(transactions)}* "
                f"עסקאות (סה\"כ {total:,.0f} ₪).",
                parse_mode="Markdown",
            )
        else:
            user_files[user_id]["max"] = transactions
            await update.message.reply_text(
                f"✅ עיבדתי קובץ מקס — *{len(transactions)}* "
                f"עסקאות (סה\"כ {total:,.0f} ₪).",
                parse_mode="Markdown",
            )

    else:
        await update.message.reply_text(
            "❌ סוג קובץ לא מזוהה. שלח PDF של עו\"ש פועלים או Excel של מקס/כאל."
        )
        return

    # Tell the user what's still missing.
    bucket = user_files[user_id]
    have = []
    missing = []
    for key, label in [("poalim", "PDF עו\"ש פועלים"),
                        ("cal", "PDF כאל פועלים"),
                        ("max", "Excel מקס")]:
        (have if key in bucket else missing).append(label)
    if missing:
        await update.message.reply_text(
            "📦 קיבלתי: " + ", ".join(have) + "\n"
            "⏳ עוד חסר: " + ", ".join(missing) + "\n"
            "אפשר גם /analyze עכשיו עם מה שיש."
        )
        return

    # All three files are here — auto-analyze.
    await update.message.reply_text(
        "🔍 יש לי את כל השלושה! מנתח... זה יקח כ-15 שניות."
    )
    all_transactions = bucket["poalim"] + bucket["max"] + bucket["cal"]

    try:
        report, uncategorized = await analyze_full(
            all_transactions,
            closing_balance=bucket.get("closing_balance"),
        )
    except Exception as e:
        logger.exception("analyze_full raised")
        await update.message.reply_text(
            f"❌ שגיאה ב-analyze_full:\n{type(e).__name__}: {e}"
        )
        del user_files[user_id]
        return

    try:
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        # Most common cause: Markdown parsing failure on user content.
        # Fall back to plain text so the user at least sees the data.
        logger.exception("reply_text(Markdown) raised")
        await update.message.reply_text(
            f"⚠️ שלחתי את הדוח בלי Markdown ({type(e).__name__}: {e}):"
        )
        await update.message.reply_text(report)

    if uncategorized:
        try:
            await _send_category_prompts(update, context, uncategorized)
        except Exception as e:
            logger.exception("_send_category_prompts raised")
            await update.message.reply_text(
                f"⚠️ לא הצלחתי לשלוח כפתורי קטגוריות: {type(e).__name__}: {e}"
            )

    del user_files[user_id]


def _category_keyboard(token: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard with one button per category.

    token: a short numeric id encoded in callback_data to identify which
    merchant the user is categorising. The full description is kept in
    context.user_data so we don't blow the 64-byte callback_data limit.
    """
    categories = list(CATEGORY_RULES.keys())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(categories), KEYBOARD_COLS):
        rows.append([
            InlineKeyboardButton(cat, callback_data=f"setcat:{token}:{idx}")
            for idx, cat in enumerate(categories[i:i + KEYBOARD_COLS], start=i)
        ])
    return InlineKeyboardMarkup(rows)


async def _send_category_prompts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    uncategorized: list[tuple[str, float]],
) -> None:
    """Send one button-message per uncategorised merchant (capped)."""
    pending = context.user_data.setdefault("pending_categories", {})
    shown = uncategorized[:MAX_BUTTON_PROMPTS]
    if len(uncategorized) > MAX_BUTTON_PROMPTS:
        await update.message.reply_text(
            f"🔍 {len(uncategorized)} בתי עסק לא קוטלגו. "
            f"מציג כפתורים ל-{MAX_BUTTON_PROMPTS} הגדולים; "
            f"לשאר שלח /correct."
        )
    for desc, amount in shown:
        # Use the next available short numeric token.
        token = str(len(pending))
        pending[token] = desc
        await update.message.reply_text(
            f"🔍 *{desc}*  ({amount:,.0f} ₪)\nבחר קטגוריה:",
            reply_markup=_category_keyboard(token),
            parse_mode="Markdown",
        )


async def category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler for the inline-keyboard taps."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "setcat":
        return

    token, cat_idx_str = parts[1], parts[2]
    try:
        cat_idx = int(cat_idx_str)
    except ValueError:
        return

    pending = context.user_data.get("pending_categories") or {}
    desc = pending.get(token)
    categories = list(CATEGORY_RULES.keys())
    if not desc or cat_idx >= len(categories):
        await query.edit_message_text("❌ הכפתור פג תוקף. שלח /correct <תיאור> = <קטגוריה>")
        return

    cat = categories[cat_idx]
    override_category(desc, cat)

    # Persist to GitHub off-thread.
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(
        None, persist_categories, f"User correction (button): {desc} → {cat}"
    )

    if ok:
        text = f"✅ *{desc}* → *{cat}* (נשמר בריפו)"
    elif err and "כבויה" in err:
        text = f"✅ *{desc}* → *{cat}* (בזיכרון בלבד — GitHub לא מוגדר)"
    else:
        text = f"⚠️ *{desc}* → *{cat}* (בזיכרון; שמירה נכשלה: {err})"
    await query.edit_message_text(text, parse_mode="Markdown")

    # Drop the token so it can't be reused.
    pending.pop(token, None)

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN לא מוגדר בסביבה!")

    # Python 3.14 removed implicit event-loop creation in
    # asyncio.get_event_loop(); python-telegram-bot 21.6 still calls it
    # synchronously from run_webhook/run_polling. Pre-create a loop so
    # the library finds one regardless of interpreter version.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("correct", correct))
    app.add_handler(CommandHandler("api", api_diagnose))
    app.add_handler(CommandHandler("github", github_diagnose))
    app.add_handler(CommandHandler("analyze", analyze_now))
    app.add_handler(CallbackQueryHandler(category_button, pattern=r"^setcat:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        # Production (Render etc.): listen on $PORT, let Telegram POST updates.
        # Telegram remembers the webhook URL, so when the dyno wakes from
        # sleep the first message arriving wakes the service and is delivered.
        port = int(os.environ.get("PORT", "8080"))
        logger.info(f"Starting in webhook mode on port {port}, url={webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url.rstrip('/')}/{token}",
        )
    else:
        logger.info("Starting in polling mode (local dev)")
        app.run_polling()

if __name__ == "__main__":
    main()
