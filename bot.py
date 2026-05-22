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
from analyzer import analyze_full, test_gemini, ocr_credit_card_image
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
        "📤 *איך להשתמש בי:*\n"
        "1. שלח לי את קובץ ה-PDF מ*פועלים* (עו\"ש — כולל הכנסות)\n"
        "2. שלח לי את קובץ ה-Excel מ*מקס* (פירוט חיובי אשראי)\n"
        "3. אני אנתח הכל ואחזיר לך דוח עם הכנסות, הוצאות, תזרים נטו וטיפים 💡\n\n"
        "אפשר לשלוח את הקבצים בכל סדר שתרצה!",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *פקודות זמינות*\n\n"
        "/start — הסבר התחלתי\n"
        "/reset — נקה קבצים ששלחת ותתחיל מההתחלה\n"
        "/analyze — הרץ ניתוח על מה שכבר שלחת (PDF, Excel, תמונות)\n"
        "/api — בדוק שמפתח Gemini עובד\n"
        "/github — אבחן את חיבור ה-GitHub (לשמירת קטגוריות)\n"
        "/correct `<תיאור> = <קטגוריה>` — תקן קטגוריזציה ותשמור לעולם\n\n"
        "💡 *תמיכה בתמונות:* אפשר לשלוח צילומי מסך של דוח כאל פועלים. "
        "אני אפענח אותם עם Gemini Vision ואכלול את העסקאות בדוח.\n\n"
        "*שימוש רגיל:* שלח לי PDF של פועלים ו-Excel של מקס. אני אחזיר דוח "
        "עם יתרה, הכנסות, הוצאות, פילוח קטגוריות, ובתי עסק מובילים.",
        parse_mode="Markdown"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_files:
        del user_files[user_id]
    await update.message.reply_text("✅ איפסתי את הנתונים שלך. תוכל לשלוח קבצים חדשים.")


def _dedup(transactions: list[dict]) -> list[dict]:
    """Drop transactions duplicated across multiple Cal screenshots."""
    seen: set[tuple[str, str, float]] = set()
    out: list[dict] = []
    for t in transactions:
        key = (t.get("date", ""), t.get("description", ""), float(t.get("amount", 0)))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a screenshot of a Cal (Hapoalim credit card) statement,
    OCR it via Gemini Vision, and add the rows to the user's queue."""
    user_id = update.effective_user.id
    if user_id not in user_files:
        user_files[user_id] = {}

    msg = update.message
    if msg.photo:
        tg_file = await context.bot.get_file(msg.photo[-1].file_id)
        mime = "image/jpeg"
        suffix = ".jpg"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        tg_file = await context.bot.get_file(msg.document.file_id)
        mime = msg.document.mime_type or "image/png"
        suffix = "." + mime.split("/")[-1]
    else:
        return

    file_path = os.path.join(TMP, f"{user_id}_cal_{tg_file.file_unique_id}{suffix}")
    await tg_file.download_to_drive(file_path)
    with open(file_path, "rb") as fh:
        image_bytes = fh.read()

    await msg.reply_text("📷 מפענח תמונה עם Gemini Vision...")
    try:
        rows = await ocr_credit_card_image(image_bytes, mime_type=mime)
    except Exception as e:
        logger.error(f"OCR failed: {e}")
        await msg.reply_text(f"❌ פענוח התמונה נכשל: {e}")
        return

    if not rows:
        await msg.reply_text(
            "⚠️ לא זיהיתי עסקאות בתמונה. שלח/י תמונה ברורה יותר או צילום ממסך הטלפון."
        )
        return

    bucket = user_files[user_id].setdefault("cal", [])
    bucket.extend(rows)
    user_files[user_id]["cal"] = _dedup(bucket)

    total = sum(t["amount"] for t in user_files[user_id]["cal"])
    await msg.reply_text(
        f"✅ קיבלתי {len(rows)} עסקאות חדשות מהתמונה.\n"
        f"סך הכל כרגע: *{len(user_files[user_id]['cal'])}* עסקאות כאל "
        f"({total:,.0f} ₪).\n\n"
        f"שלח/י עוד תמונות, או /analyze לדוח.",
        parse_mode="Markdown",
    )


async def analyze_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger analysis explicitly. Useful when Cal images are involved
    (we can't know how many the user will send)."""
    user_id = update.effective_user.id
    bucket = user_files.get(user_id, {})

    if "poalim" not in bucket:
        await update.message.reply_text(
            "⚠️ עדיין לא קיבלתי קובץ PDF של פועלים. שלח/י אותו לפני /analyze."
        )
        return

    all_tx = list(bucket["poalim"]) + list(bucket.get("max", [])) + list(bucket.get("cal", []))
    await update.message.reply_text("🔍 מנתח...")

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

    # זיהוי סוג הקובץ לפי סיומת
    if file_name.endswith(".pdf"):
        await update.message.reply_text("📥 קיבלתי את קובץ פועלים! מעבד...")
        file = await context.bot.get_file(doc.file_id)
        file_path = os.path.join(TMP, f"{user_id}_poalim.pdf")
        await file.download_to_drive(file_path)

        transactions, closing_balance = process_pdf(file_path)
        if not transactions:
            await update.message.reply_text("⚠️ לא הצלחתי לקרוא עסקאות מה-PDF. ודא שזה קובץ פועלים תקין.")
            return

        user_files[user_id]["poalim"] = transactions
        user_files[user_id]["closing_balance"] = closing_balance
        await update.message.reply_text(
            f"✅ עיבדתי את קובץ פועלים — מצאתי *{len(transactions)}* עסקאות.\n\n"
            f"עכשיו שלח לי את קובץ ה-Excel ממקס כדי שאוכל לנתח הכל יחד.",
            parse_mode="Markdown"
        )

    elif file_name.endswith((".xlsx", ".xls")):
        await update.message.reply_text("📥 קיבלתי את קובץ מקס! מעבד...")
        file = await context.bot.get_file(doc.file_id)
        file_path = os.path.join(TMP, f"{user_id}_max.xlsx")
        await file.download_to_drive(file_path)

        transactions = process_excel(file_path)
        if not transactions:
            await update.message.reply_text("⚠️ לא הצלחתי לקרוא עסקאות מה-Excel. ודא שזה קובץ מקס תקין.")
            return

        user_files[user_id]["max"] = transactions
        await update.message.reply_text(
            f"✅ עיבדתי את קובץ מקס — מצאתי *{len(transactions)}* עסקאות.\n\n"
            f"עכשיו שלח לי את קובץ ה-PDF מפועלים כדי שאוכל לנתח הכל יחד.",
            parse_mode="Markdown"
        )

    else:
        await update.message.reply_text("❌ סוג קובץ לא מזוהה. שלח PDF מפועלים או Excel ממקס.")
        return

    # Auto-analyze when both Poalim PDF and Max Excel have arrived AND no
    # Cal images are pending (Cal images require explicit /analyze since
    # we don't know how many the user will send).
    bucket = user_files[user_id]
    if "poalim" in bucket and "max" in bucket and "cal" not in bucket:
        await update.message.reply_text(
            "🔍 יש לי את שני הקבצים! מנתח את ההוצאות שלך עם AI... זה יקח כ-15 שניות.\n"
            "(אם יש לך גם תמונות של כאל פועלים — שלח אותן עכשיו ואז /analyze)"
        )
        all_transactions = bucket["poalim"] + bucket["max"]

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
    elif "cal" in bucket:
        # Cal flow: don't auto-analyze; user will run /analyze when ready.
        pass


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
    app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_image))
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
