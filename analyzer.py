import os
import asyncio
import json
import logging
import urllib.request
import time

logger = logging.getLogger(__name__)

CATEGORIES = [
    "מזון וסופרמרקט", "מסעדות ובתי קפה", "תחבורה ודלק",
    "קניות ואופנה", "בריאות ופארמה", "בילוי ופנאי",
    "חינוך", "תקשורת וסלולר", "ביטוח ופיננסים",
    "העברות ותשלומים", "אחר"
]


async def analyze_expenses(transactions: list[dict]) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set!")

    total = sum(t["amount"] for t in transactions)
    poalim_count = sum(1 for t in transactions if "פועלים" in t["source"])
    max_count = sum(1 for t in transactions if t["source"] == "מקס")

    # Send max 60 transactions to stay within Gemini free tier limits
    sample = transactions[:60]
    transactions_text = _format_transactions(sample)
    note = f"(showing {len(sample)} of {len(transactions)} total)" if len(transactions) > 60 else ""

    prompt = f"""You are a personal finance assistant. Analyze these Israeli bank/credit transactions and reply in Hebrew.

Total transactions: {len(transactions)} {note}
Total spending: {total:,.0f} ILS
From Poalim bank: {poalim_count}
From Max credit: {max_count}

Transactions:
{transactions_text}

Categories to use: {', '.join(CATEGORIES)}

Reply with ONLY a JSON object, no markdown, no backticks:
{{
  "categories": {{"category name": total_amount}},
  "top_merchants": [{{"name": "...", "total": 0.0, "count": 0}}],
  "tips": ["specific tip 1", "specific tip 2", "specific tip 3"],
  "summary": "one sentence personal summary in Hebrew"
}}

Tips must be specific to what you saw, not generic advice."""

    loop = asyncio.get_event_loop()

    # Retry up to 3 times if rate limited
    for attempt in range(3):
        try:
            raw_text = await loop.run_in_executor(None, lambda: _call_gemini(api_key, prompt))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                logger.warning(f"Rate limited, waiting 30s (attempt {attempt+1})")
                await asyncio.sleep(30)
            else:
                raise

    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    data = json.loads(clean)
    return _format_report(data, total, poalim_count, max_count, len(transactions))


def _call_gemini(api_key: str, prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000}
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=45) as response:
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

    report = "📊 *דוח הוצאות*\n\n"
    report += f"💳 סה\"כ הוצאות: *{total:,.0f} ₪*\n"
    report += f"   • פועלים עו\"ש: {poalim_count} תנועות\n"
    report += f"   • מקס: {max_count} עסקאות\n\n"

    if categories:
        report += "📂 *פילוח לפי קטגוריות:*\n"
        for cat, amount in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            if amount <= 0:
                continue
            pct = (amount / total * 100) if total > 0 else 0
            bar = "🔴" if pct >= 30 else ("🟡" if pct >= 15 else "🟢")
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

    report += "\n\n_לניתוח חודש חדש: /reset_"
    return report
