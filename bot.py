import os
import tempfile
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from file_processor import process_pdf, process_excel
from analyzer import analyze_expenses

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
        "📋 *עזרה*\n\n"
        "שלח לי קובץ PDF מפועלים וקובץ Excel ממקס.\n"
        "אחרי ששני הקבצים יתקבלו, אני אנתח את ההוצאות שלך ואחזיר:\n\n"
        "✅ פילוח הוצאות לפי קטגוריות\n"
        "✅ גרף עוגה טקסטואלי של ההוצאות\n"
        "✅ השוואה בין כרטיסי האשראי\n"
        "✅ 3-5 טיפים חכמים לחסכון\n\n"
        "לאיפוס ושליחה מחדש: /reset",
        parse_mode="Markdown"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_files:
        del user_files[user_id]
    await update.message.reply_text("✅ איפסתי את הנתונים שלך. תוכל לשלוח קבצים חדשים.")

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

        transactions = process_pdf(file_path)
        if not transactions:
            await update.message.reply_text("⚠️ לא הצלחתי לקרוא עסקאות מה-PDF. ודא שזה קובץ פועלים תקין.")
            return

        user_files[user_id]["poalim"] = transactions
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

    # אם יש לנו את שני הקבצים — מנתחים
    if "poalim" in user_files[user_id] and "max" in user_files[user_id]:
        await update.message.reply_text("🔍 יש לי את שני הקבצים! מנתח את ההוצאות שלך עם AI... זה יקח כ-15 שניות.")
        all_transactions = user_files[user_id]["poalim"] + user_files[user_id]["max"]

        try:
            report = await analyze_expenses(all_transactions)
            await update.message.reply_text(report, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error analyzing: {e}")
            await update.message.reply_text("❌ אירעה שגיאה בניתוח. נסה שנית או פנה לתמיכה.")

        # מנקים אחרי שליחה
        del user_files[user_id]

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN לא מוגדר בסביבה!")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
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
