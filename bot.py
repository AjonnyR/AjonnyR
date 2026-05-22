import os
import asyncio
import html
import re
import tempfile
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, filters, ContextTypes,
)
from file_processor import process_pdf, process_excel
from analyzer import analyze_full, test_gemini
from categories import (
    CATEGORY_RULES,
    CUSTOM_CATEGORIES,
    create_category,
    rename_category,
    delete_category,
    list_all_with_contents,
    override as override_category,
    persist as persist_categories,
    persistence_enabled,
    schedule_persist,
    reset_flush_timer,
    flush_now,
    has_pending,
    pending_count,
    PERSIST_DEBOUNCE_SECONDS,
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

async def _touch_idle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 middleware: bumps the debounce timer on any user activity
    so 'flush 10 min after last interaction' holds. Never consumes the
    update — control falls through to the real handler."""
    try:
        await reset_flush_timer()
    except Exception as e:
        logger.debug(f"reset_flush_timer ignored: {e}")


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
        "/correct `<תיאור> = <קטגוריה>` — תקן קטגוריזציה ותשמור לעולם\n"
        "/newcategory `<שם>` — צור קטגוריה חדשה\n"
        "/renamecategory `<שם ישן> = <שם חדש>` — שנה שם של קטגוריה שיצרת\n"
        "/deletecategory `<שם>` — מחק קטגוריה שיצרת (המסחרים עוברים ל\"אחר\")\n"
        "/categories — הצג את כל הקטגוריות ומה מתויג בכל אחת\n"
        "/flush — סנכרן עכשיו ל-GitHub שינויים שמחכים בתור\n"
        "/api — בדוק שמפתח Gemini עובד\n"
        "/github — אבחן את חיבור ה-GitHub (לשמירת קטגוריות)\n\n"
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

    await schedule_persist(f"User correction: {desc} → {cat}")
    minutes = PERSIST_DEBOUNCE_SECONDS // 60
    await update.message.reply_text(
        f"✅ *{desc}* → *{cat}*\n"
        f"_נשמר בזיכרון. שמירה ל-GitHub תתבצע כעבור {minutes} דק' של חוסר "
        f"פעילות כדי לא להפיל את הבוט בכל תיקון. לשמירה מיידית: /flush._",
        parse_mode="Markdown",
    )


async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newcategory <name>  —  add a user-defined category."""
    raw = (update.message.text or "").split(None, 1)
    name = raw[1].strip() if len(raw) == 2 else ""
    if not name:
        await update.message.reply_text(
            "שימוש: /newcategory <שם הקטגוריה>\n\n"
            "לדוגמה: `/newcategory מילואים`",
            parse_mode="Markdown",
        )
        return
    ok, err = create_category(name)
    if not ok:
        await update.message.reply_text(f"❌ {err}")
        return
    await schedule_persist(f"New category: {name}")
    minutes = PERSIST_DEBOUNCE_SECONDS // 60
    await update.message.reply_text(
        f"✅ נוצרה קטגוריה: *{name}*\n"
        f"_מעכשיו תופיע בכפתורים וב-/correct. סנכרון ל-GitHub כעבור "
        f"{minutes} דק' של שקט (או /flush)._",
        parse_mode="Markdown",
    )


async def rename_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/renamecategory <old> = <new>"""
    raw = (update.message.text or "").split(None, 1)
    text = raw[1].strip() if len(raw) == 2 else ""
    if "=" not in text:
        await update.message.reply_text(
            "שימוש: /renamecategory <שם ישן> = <שם חדש>\n\n"
            "לדוגמה: `/renamecategory מילואים = שירות מילואים`",
            parse_mode="Markdown",
        )
        return
    old, _, new = text.partition("=")
    ok, err, moved = rename_category(old.strip(), new.strip())
    if not ok:
        await update.message.reply_text(f"❌ {err}")
        return
    await schedule_persist(
        f"Rename category: {old.strip()} → {new.strip()} ({moved} merchants)"
    )
    minutes = PERSIST_DEBOUNCE_SECONDS // 60
    moved_note = f" {moved} מסחרים עברו לשם החדש." if moved else ""
    await update.message.reply_text(
        f"✅ שינוי שם: *{old.strip()}* → *{new.strip()}*.{moved_note}\n"
        f"_סנכרון ל-GitHub בעוד {minutes} דק' של שקט (או /flush)._",
        parse_mode="Markdown",
    )


async def delete_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deletecategory <name>"""
    raw = (update.message.text or "").split(None, 1)
    name = raw[1].strip() if len(raw) == 2 else ""
    if not name:
        await update.message.reply_text(
            "שימוש: /deletecategory <שם הקטגוריה>\n\n"
            "מסחרים שתויגו לקטגוריה יעברו אוטומטית ל\"אחר\".",
        )
        return
    ok, err, moved = delete_category(name)
    if not ok:
        await update.message.reply_text(f"❌ {err}")
        return
    await schedule_persist(f"Delete category: {name} (moved {moved} merchants to אחר)")
    minutes = PERSIST_DEBOUNCE_SECONDS // 60
    moved_note = (
        f"{moved} מסחרים שתויגו לקטגוריה הזו עברו ל-\"אחר\"."
        if moved else "לא היו מסחרים מתויגים אליה."
    )
    await update.message.reply_text(
        f"✅ נמחקה: *{name}*.\n"
        f"_{moved_note}_\n"
        f"_סנכרון ל-GitHub בעוד {minutes} דק' של שקט (או /flush)._",
        parse_mode="Markdown",
    )


_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Plain-text fallback if HTML rendering fails (rare but possible)."""
    return _HTML_TAG.sub("", s)


async def list_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/categories  —  list every category and what's in it.

    Uses HTML rather than Markdown because merchant names commonly
    contain '*' (e.g. "AMAZON MKTPL*BS64T7N90"), which Markdown would
    interpret as an unmatched bold marker and Telegram would reject
    the whole message."""
    rows = list_all_with_contents()
    if not rows:
        await update.message.reply_text("אין קטגוריות מוגדרות.")
        return

    def esc(s: str) -> str:
        return html.escape(s, quote=False)

    MAX_LEN = 3800
    chunks: list[str] = []
    current = "📂 <b>קטגוריות</b>\n\n"
    for cat, keywords, merchants, is_custom in rows:
        header = f"<b>{esc(cat)}</b>" + (" 🆕" if is_custom else "")
        block = f"{header}\n"
        if keywords:
            block += f"   🔑 <i>{len(keywords)} מילות מפתח (בקוד)</i>\n"
        if merchants:
            for m in merchants[:15]:
                block += f"   • {esc(m)}\n"
            if len(merchants) > 15:
                block += f"   • <i>ועוד {len(merchants) - 15} עסקאות...</i>\n"
        elif not keywords:
            block += "   <i>(ריקה)</i>\n"
        block += "\n"

        if len(current) + len(block) > MAX_LEN:
            chunks.append(current)
            current = ""
        current += block

    if current:
        chunks.append(current)

    chunks[-1] += (
        "<i>🆕 = נוצרה על ידך. ליצירת קטגוריה חדשה: /newcategory &lt;שם&gt;</i>\n"
        "<i>לתיוג מסחר: /correct &lt;תיאור&gt; = &lt;קטגוריה&gt;</i>"
    )
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"/categories HTML send failed, falling back to plain: {e}")
            await update.message.reply_text(_strip_html(chunk))


async def flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force an immediate GitHub commit of any queued category changes.
    Useful when the user knows they're done and doesn't want to wait the
    debounce window."""
    n = pending_count()
    if n == 0:
        await update.message.reply_text("ℹ️ אין שינויים בתור — אין מה לסנכרן.")
        return
    await update.message.reply_text(f"💾 מסנכרן {n} שינויים ל-GitHub...")
    ok, err = await flush_now()
    if ok:
        await update.message.reply_text(
            "✅ נשמר. Render יעלה גרסה חדשה בעוד 2-3 דקות."
        )
    else:
        await update.message.reply_text(
            f"⚠️ השמירה נכשלה: {err}\nהשינויים נשארים בזיכרון לניסיון נוסף."
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


# Telegram's callback_data limit is 64 bytes (UTF-8). After the "c:NN:"
# prefix (max 5 bytes) we have ~59 bytes for the description. Hebrew
# encodes as 2 bytes/char, so ~29 Hebrew chars fit.
CB_PREFIX = "c"
CB_MAX_DESC_BYTES = 59


def _fits_in_callback(desc: str) -> bool:
    return len(desc.encode("utf-8")) <= CB_MAX_DESC_BYTES


def _category_keyboard(desc: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard with one button per category.

    The merchant description is encoded directly into each button's
    callback_data so the button survives bot restarts (Render's free
    tier sleeps the worker after 15 min, which used to wipe the
    in-memory token→desc map and produce "button expired" errors).
    """
    categories = list(CATEGORY_RULES.keys())
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(categories), KEYBOARD_COLS):
        rows.append([
            InlineKeyboardButton(cat, callback_data=f"{CB_PREFIX}:{idx}:{desc}")
            for idx, cat in enumerate(categories[i:i + KEYBOARD_COLS], start=i)
        ])
    return InlineKeyboardMarkup(rows)


async def _send_category_prompts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    uncategorized: list[tuple[str, float]],
) -> None:
    """Send one button-message per uncategorised merchant (capped)."""
    shown = uncategorized[:MAX_BUTTON_PROMPTS]
    if len(uncategorized) > MAX_BUTTON_PROMPTS:
        await update.message.reply_text(
            f"🔍 {len(uncategorized)} בתי עסק לא קוטלגו. "
            f"מציג כפתורים ל-{MAX_BUTTON_PROMPTS} הגדולים; "
            f"לשאר שלח /correct."
        )
    for desc, amount in shown:
        if _fits_in_callback(desc):
            await update.message.reply_text(
                f"🔍 *{desc}*  ({amount:,.0f} ₪)\nבחר קטגוריה:",
                reply_markup=_category_keyboard(desc),
                parse_mode="Markdown",
            )
        else:
            # Description too long for a 64-byte callback. Skip the button
            # and give the user the exact /correct command to copy-paste.
            await update.message.reply_text(
                f"🔍 *{desc}*  ({amount:,.0f} ₪)\n"
                f"השם ארוך מדי לכפתור — העתק את הפקודה:\n"
                f"`/correct {desc} = <קטגוריה>`",
                parse_mode="Markdown",
            )


async def category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler for the inline-keyboard taps.

    Accepts two callback_data shapes:
      - New "c:<cat_idx>:<desc>": description is self-contained.
      - Old "setcat:<token>:<cat_idx>": legacy — token map is wiped on
        restart, so we just tell the user the button is stale.
    """
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    desc = None
    cat_idx: int | None = None
    if data.startswith(f"{CB_PREFIX}:"):
        parts = data.split(":", 2)  # only split twice: desc may contain ":"
        if len(parts) == 3:
            try:
                cat_idx = int(parts[1])
            except ValueError:
                return
            desc = parts[2]
    elif data.startswith("setcat:"):
        # Legacy buttons from before the self-contained callback fix.
        await query.edit_message_text(
            "❌ הכפתור הזה ישן ולא נשמר בין הפעלות של הבוט. "
            "שלח /analyze כדי לקבל כפתורים חדשים, "
            "או /correct <תיאור> = <קטגוריה>"
        )
        return

    categories = list(CATEGORY_RULES.keys())
    if desc is None or cat_idx is None or cat_idx >= len(categories):
        return

    cat = categories[cat_idx]
    override_category(desc, cat)
    await schedule_persist(f"User correction (button): {desc} → {cat}")
    minutes = PERSIST_DEBOUNCE_SECONDS // 60
    text = (
        f"✅ *{desc}* → *{cat}*\n"
        f"_נשמר. סנכרון ל-GitHub בעוד {minutes} דק' של שקט (או /flush)._"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

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
    # Group -1: runs before every other handler. Resets the debounced
    # commit timer so "10 min of inactivity" measures from the user's
    # last interaction, not just from the last category change.
    app.add_handler(TypeHandler(Update, _touch_idle_timer), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("correct", correct))
    app.add_handler(CommandHandler("newcategory", new_category))
    app.add_handler(CommandHandler("renamecategory", rename_cat))
    app.add_handler(CommandHandler("deletecategory", delete_cat))
    app.add_handler(CommandHandler("categories", list_categories))
    app.add_handler(CommandHandler("flush", flush))
    app.add_handler(CommandHandler("api", api_diagnose))
    app.add_handler(CommandHandler("github", github_diagnose))
    app.add_handler(CommandHandler("analyze", analyze_now))
    app.add_handler(CallbackQueryHandler(category_button, pattern=r"^(setcat|c):"))
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
