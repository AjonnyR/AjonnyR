import os
import asyncio
import json
import logging
import urllib.request
import urllib.error
import time

from categories import categorize, CATEGORY_RULES

logger = logging.getLogger(__name__)

CATEGORIES = list(CATEGORY_RULES.keys())

# Max transactions sent to Gemini (the rest are summarised locally to stay
# inside the free-tier quota).
MAX_SAMPLE = 50


async def analyze_expenses(transactions: list[dict], closing_balance: float | None = None) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set!")

    # Poalim is the main account. The monthly "Max" debit line in Poalim
    # is just a summary of the per-merchant rows in the Max file — if we
    # also got the Max file, exclude those summary lines from the totals
    # to avoid double-counting.
    has_max_file = any(t.get("source") == "מקס" for t in transactions)

    expenses = []
    incomes = []
    max_summary_skipped = []
    for t in transactions:
        ttype = t.get("type", "expense")
        if ttype == "income":
            incomes.append(t)
        elif ttype == "max_summary":
            if has_max_file:
                max_summary_skipped.append(t)
            else:
                expenses.append(t)
        else:
            expenses.append(t)

    total_expense = sum(t["amount"] for t in expenses)
    total_income = sum(t["amount"] for t in incomes)
    net_flow = total_income - total_expense

    poalim_count = sum(1 for t in expenses if "פועלים" in t["source"])
    max_count = sum(1 for t in expenses if t["source"] == "מקס")

    sample = expenses[:MAX_SAMPLE]
    transactions_text = _format_transactions(sample)
    note = f"(showing {len(sample)} of {len(expenses)} expenses)" if len(expenses) > MAX_SAMPLE else ""

    prompt = f"""You are a personal finance assistant. Analyze these Israeli expense transactions and reply in Hebrew.

Total expenses: {len(expenses)} {note}
Total spending: {total_expense:,.0f} ILS

Expenses:
{transactions_text}

Categories: {', '.join(CATEGORIES)}

Reply with ONLY a JSON object, no markdown, no backticks:
{{
  "categories": {{"category name": total_amount}},
  "top_merchants": [{{"name": "...", "total": 0.0, "count": 0}}],
  "tips": ["specific tip 1", "specific tip 2", "specific tip 3"],
  "summary": "one sentence personal summary in Hebrew"
}}

Tips must be specific to what you saw, not generic."""

    loop = asyncio.get_event_loop()

    raw_text = None
    last_error = None
    for attempt in range(3):
        try:
            raw_text = await loop.run_in_executor(None, lambda: _call_gemini(api_key, prompt))
            break
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429 and attempt < 2:
                logger.warning(f"Rate limited, waiting 30s (attempt {attempt + 1})")
                await asyncio.sleep(30)
            else:
                break
        except Exception as e:
            last_error = e
            break

    if raw_text is None:
        # Gemini unavailable — return a local report instead of crashing.
        logger.error(f"Gemini failed: {last_error}")
        return _format_report_no_ai(
            expenses, incomes, max_summary_skipped,
            total_expense, total_income, net_flow,
            poalim_count, max_count, closing_balance, last_error,
        )

    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        logger.error(f"Gemini returned non-JSON: {e} | raw={raw_text[:200]}")
        return _format_report_no_ai(
            expenses, incomes, max_summary_skipped,
            total_expense, total_income, net_flow,
            poalim_count, max_count, closing_balance, e,
        )

    return _format_report(
        data, expenses, incomes, max_summary_skipped,
        total_expense, total_income, net_flow,
        poalim_count, max_count, closing_balance,
    )


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


def _income_section(incomes: list[dict], total_income: float) -> str:
    if not incomes:
        return ""
    section = "💰 *הכנסות:*\n"
    section += f"   סה\"כ: *{total_income:,.0f} ₪* ({len(incomes)} תנועות)\n"
    for t in sorted(incomes, key=lambda x: x["amount"], reverse=True)[:5]:
        section += f"   • {t['date']} — {t['description']}: {t['amount']:,.0f} ₪\n"
    return section + "\n"


def _max_summary_note(max_summary_skipped: list[dict]) -> str:
    if not max_summary_skipped:
        return ""
    total = sum(t["amount"] for t in max_summary_skipped)
    note = "ℹ️ _זיהיתי "
    note += f"{len(max_summary_skipped)} חיוב{'י' if len(max_summary_skipped) > 1 else ''} מקס בפועלים "
    note += f"(סה\"כ {total:,.0f} ₪) — לא נספרו פעמיים, "
    note += "הפירוט נלקח מקובץ מקס._\n\n"
    return note


def _net_flow_section(total_income: float, total_expense: float, net_flow: float) -> str:
    if total_income == 0:
        return ""
    section = "📈 *תזרים נטו (לתקופת הדוח):*\n"
    section += f"   הכנסות: +{total_income:,.0f} ₪\n"
    section += f"   הוצאות: −{total_expense:,.0f} ₪\n"
    sign = "+" if net_flow >= 0 else "−"
    emoji = "✅" if net_flow >= 0 else "⚠️"
    section += f"   {emoji} נטו: *{sign}{abs(net_flow):,.0f} ₪*\n\n"
    return section


def _balance_section(closing_balance: float | None) -> str:
    if closing_balance is None:
        return ""
    return f"🏦 *יתרה נוכחית בפועלים:* {closing_balance:,.2f} ₪\n\n"


def _local_categorise(expenses: list[dict]) -> dict[str, float]:
    """Group expenses by category using categories.py rules."""
    totals: dict[str, float] = {}
    for t in expenses:
        cat = categorize(t["description"])
        totals[cat] = totals.get(cat, 0.0) + t["amount"]
    return totals


def _format_report(
    data: dict,
    expenses: list[dict], incomes: list[dict], max_summary_skipped: list[dict],
    total_expense: float, total_income: float, net_flow: float,
    poalim_count: int, max_count: int,
    closing_balance: float | None,
) -> str:
    categories = data.get("categories", {})
    top_merchants = data.get("top_merchants", [])
    tips = data.get("tips", [])
    summary = data.get("summary", "")

    report = "📊 *דוח חודשי*\n\n"
    report += _balance_section(closing_balance)
    report += f"💳 סה\"כ הוצאות: *{total_expense:,.0f} ₪*\n"
    report += f"   • פועלים עו\"ש: {poalim_count} תנועות\n"
    report += f"   • מקס: {max_count} עסקאות\n\n"

    report += _max_summary_note(max_summary_skipped)
    report += _income_section(incomes, total_income)
    report += _net_flow_section(total_income, total_expense, net_flow)

    if categories:
        report += "📂 *פילוח הוצאות:*\n"
        for cat, amount in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            if amount <= 0:
                continue
            pct = (amount / total_expense * 100) if total_expense > 0 else 0
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


def _format_report_no_ai(
    expenses: list[dict], incomes: list[dict], max_summary_skipped: list[dict],
    total_expense: float, total_income: float, net_flow: float,
    poalim_count: int, max_count: int,
    closing_balance: float | None,
    error: Exception | None,
) -> str:
    """Fallback report when Gemini is unavailable (quota / network).
    Shows totals, income, net flow, locally-categorised expenses, and top
    merchants computed locally."""
    report = "📊 *דוח חודשי* _(ללא AI — פולבק מקומי)_\n\n"

    if isinstance(error, urllib.error.HTTPError) and error.code == 429:
        report += "⚠️ _חרגתי ממכסת ה-API של Gemini. הניתוח מתבסס על חישוב מקומי בלבד._\n\n"
    elif error is not None:
        report += "⚠️ _שירות ה-AI לא היה זמין. הניתוח מתבסס על חישוב מקומי בלבד._\n\n"

    report += _balance_section(closing_balance)
    report += f"💳 סה\"כ הוצאות: *{total_expense:,.0f} ₪*\n"
    report += f"   • פועלים עו\"ש: {poalim_count} תנועות\n"
    report += f"   • מקס: {max_count} עסקאות\n\n"

    report += _max_summary_note(max_summary_skipped)
    report += _income_section(incomes, total_income)
    report += _net_flow_section(total_income, total_expense, net_flow)

    # Local categorisation
    cat_totals = _local_categorise(expenses)
    if cat_totals and total_expense > 0:
        report += "📂 *פילוח הוצאות (לפי כללים מקומיים):*\n"
        for cat, amount in sorted(cat_totals.items(), key=lambda x: x[1], reverse=True):
            if amount <= 0:
                continue
            pct = amount / total_expense * 100
            bar = "🔴" if pct >= 30 else ("🟡" if pct >= 15 else "🟢")
            report += f"{bar} {cat}: *{amount:,.0f} ₪* ({pct:.0f}%)\n"
        report += "\n"

    # Local top-merchants: aggregate by description
    by_desc: dict[str, dict] = {}
    for t in expenses:
        key = t["description"]
        if key not in by_desc:
            by_desc[key] = {"total": 0.0, "count": 0}
        by_desc[key]["total"] += t["amount"]
        by_desc[key]["count"] += 1

    top = sorted(by_desc.items(), key=lambda x: x[1]["total"], reverse=True)[:5]
    if top:
        report += "🏪 *בתי עסק מובילים:*\n"
        for name, info in top:
            report += f"   • {name}: {info['total']:,.0f} ₪ ({info['count']} פעמים)\n"
        report += "\n"

    report += "_לעדכון קטגוריות (למשל \"שיק\" → שכר דירה): ערוך את categories.py בריפו._\n"
    report += "_לניתוח חודש חדש: /reset_"
    return report
