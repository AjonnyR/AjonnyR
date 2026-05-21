import os
import asyncio
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

CATEGORIES = [
    "מזון וסופרמרקט",
    "מסעדות ובתי קפה",
    "תחבורה ודלק",
    "קניות ואופנה",
    "בריאות ופארמה",
    "בילוי ופנאי",
    "חינוך",
    "תקשורת וסלולר",
    "ביטוח ופיננסים",
    "אחר"
]


async def analyze_expenses(transactions: list[dict]) -> str:
    """
    שולח את העסקאות ל-Gemini API (חינמי לגמרי) לניתוח וקטגוריזציה,
    ומחזיר דוח מפורט בעברית.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY לא מוגדר בסביבה!")

    transactions_text = _format_transactions(transactions)
    total = sum(t["amount"] for t in transactions)
    poalim_count = sum(1 for t in transactions if t["source"] == "פועלים")
    max_count = sum(1 for t in transactions if t["source"] == "מקס")

    prompt = f"""אתה עוזר פיננסי אישי. קיבלת את העסקאות הבאות מכרטיסי האשראי של המשתמש לחודש האחרון.

נתונים כלליים:
- סה"כ עסקאות: {len(transactions)}
- מפועלים: {poalim_count} עסקאות
- ממקס: {max_count} עסקאות
- סה"כ הוצאות: {total:,.0f} ₪

העסקאות:
{transactions_text}

קטגוריות אפשריות: {', '.join(CATEGORIES)}

החזר תשובה בפורמט JSON בלבד (בלי טקסט נוסף, בלי backticks), עם המבנה הבא:
{{
  "categories": {{
    "מזון וסופרמרקט": 1500.00,
    "מסעדות ובתי קפה": 800.00
  }},
  "top_merchants": [
    {{"name": "שם בית עסק", "total": 500.00, "count": 3}}
  ],
  "tips": [
    "טיפ 1 ספציפי לנתונים",
    "טיפ 2 ספציפי לנתונים",
    "טיפ 3 ספציפי לנתונים"
  ],
  "summary": "משפט סיכום קצר ואישי על ההוצאות"
}}

חשוב: הטיפים חייבים להיות ספציפיים לנתונים שראית, לא כלליים."""

    loop = asyncio.get_event_loop()
    raw_text = await loop.run_in_executor(None, lambda: _call_gemini(api_key, prompt))

    # ניקוי אם יש backticks
    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    data = json.loads(clean)
    return _format_report(data, total, poalim_count, max_count, len(transactions))


def _call_gemini(api_key: str, prompt: str) -> str:
    """
    קורא ל-Gemini API דרך urllib (בלי ספריות חיצוניות).
    משתמש במודל gemini-2.0-flash שהוא חינמי לגמרי.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2000
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    return result["candidates"][0]["content"]["parts"][0]["text"]


def _format_transactions(transactions: list[dict]) -> str:
    lines = []
    for t in transactions:
        lines.append(f"{t['date']} | {t['description']} | {t['amount']:.0f}₪ | {t['source']}")
    return "\n".join(lines)


def _format_report(data: dict, total: float, poalim_count: int, max_count: int, total_count: int) -> str:
    categories = data.get("categories", {})
    top_merchants = data.get("top_merchants", [])
    tips = data.get("tips", [])
    summary = data.get("summary", "")

    report = "📊 *דוח הוצאות חודשי*\n\n"
    report += f"💳 סה\"כ הוצאות: *{total:,.0f} ₪*\n"
    report += f"   • פועלים: {poalim_count} עסקאות\n"
    report += f"   • מקס: {max_count} עסקאות\n\n"

    if categories:
        report += "📂 *פילוח לפי קטגוריות:*\n"
        for cat, amount in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            if amount <= 0:
                continue
            pct = (amount / total * 100) if total > 0 else 0
            bar = _progress_bar(pct)
            report += f"{bar} {cat}: *{amount:,.0f} ₪* ({pct:.0f}%)\n"
        report += "\n"

    if top_merchants:
        report += "🏪 *בתי עסק מובילים:*\n"
        for m in top_merchants[:5]:
            report += f"   • {m['name']}: {m['total']:,.0f} ₪ ({m['count']} פעמים)\n"
        report += "\n"

    if summary:
        report += f"💬 _{summary}_\n\n"

    if tips:
        report += "💡 *טיפים לחסכון:*\n"
        for i, tip in enumerate(tips[:5], 1):
            report += f"{i}. {tip}\n"

    report += "\n\n_לניתוח חודש חדש, שלח /reset ואז קבצים חדשים_"
    return report


def _progress_bar(pct: float) -> str:
    if pct >= 30:
        return "🔴"
    elif pct >= 15:
        return "🟡"
    else:
        return "🟢"
