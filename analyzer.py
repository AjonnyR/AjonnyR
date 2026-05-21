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

async def analyze_expenses(transactions: list[dict], closing_balance: float | None = None) -> str:
    """Produce the full report.

    Categorisation, merchant aggregation, balance and net-flow are all done
    LOCALLY. Gemini is asked only for personalised tips (a tiny prompt) —
    if it fails, we fall back to local rule-based tips. This keeps API
    usage minimal and the report still works even when AI is unavailable.
    """
    # Poalim is the main account. The monthly "Max" debit line in Poalim
    # is just a summary of the per-merchant rows in the Max file — if we
    # also got the Max file, exclude those summary lines from the totals
    # to avoid double-counting.
    has_max_file = any(t.get("source") == "מקס" for t in transactions)

    expenses: list[dict] = []
    incomes: list[dict] = []
    max_summary_skipped: list[dict] = []
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

    # Local aggregation
    cat_totals = _local_categorise(expenses)
    by_desc: dict[str, dict] = {}
    for t in expenses:
        key = t["description"]
        if key not in by_desc:
            by_desc[key] = {"total": 0.0, "count": 0}
        by_desc[key]["total"] += t["amount"]
        by_desc[key]["count"] += 1
    top_merchants = sorted(by_desc.items(), key=lambda x: x[1]["total"], reverse=True)[:8]

    # Try Gemini ONLY for tips (small prompt)
    api_key = os.environ.get("GEMINI_API_KEY")
    ai_tips: list[str] = []
    ai_error: Exception | None = None
    if api_key:
        try:
            ai_tips = await _get_ai_tips(
                api_key, cat_totals, top_merchants,
                total_income, total_expense, net_flow,
            )
        except Exception as e:
            ai_error = e
            logger.error(f"Gemini tips call failed: {e}")

    # Fall back to local tips if AI didn't deliver
    tips = ai_tips if ai_tips else _local_tips(
        expenses, total_expense, total_income, net_flow, cat_totals
    )

    return _format_report(
        cat_totals, top_merchants, tips,
        expenses, incomes, max_summary_skipped,
        total_expense, total_income, net_flow,
        poalim_count, max_count, closing_balance,
        ai_error if not ai_tips else None,
    )


async def _get_ai_tips(
    api_key: str,
    cat_totals: dict[str, float],
    top_merchants: list[tuple[str, dict]],
    total_income: float,
    total_expense: float,
    net_flow: float,
) -> list[str]:
    """Call Gemini with an aggregated summary (no raw transactions) and
    return a list of tips. ~150 tokens of input vs ~1500 before."""
    cat_lines = "\n".join(
        f"- {c}: {a:,.0f} ₪"
        for c, a in sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        if a > 0
    )
    merchant_lines = "\n".join(
        f"- {name}: {info['total']:,.0f} ₪ ({info['count']}x)"
        for name, info in top_merchants[:6]
    )

    prompt = (
        f"Israeli user's monthly statement (ILS):\n"
        f"- Income: {total_income:,.0f}\n"
        f"- Expenses: {total_expense:,.0f}\n"
        f"- Net: {net_flow:+,.0f}\n\n"
        f"Category breakdown:\n{cat_lines}\n\n"
        f"Top merchants:\n{merchant_lines}\n\n"
        f"Give 3-5 specific, actionable savings/optimisation tips in Hebrew, "
        f"based on this data. Reply with ONLY a JSON array of Hebrew strings, "
        f"no markdown, no backticks. Example: [\"tip 1\", \"tip 2\"]"
    )

    loop = asyncio.get_event_loop()
    raw_text = None
    for attempt in range(2):
        try:
            raw_text = await loop.run_in_executor(None, lambda: _call_gemini(api_key, prompt))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 1:
                await asyncio.sleep(15)
            else:
                raise

    if raw_text is None:
        return []

    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        tips = json.loads(clean)
        if isinstance(tips, list):
            return [str(t) for t in tips][:5]
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON tips: {raw_text[:200]}")
    return []


async def test_gemini(api_key: str) -> tuple[bool, str]:
    """Diagnostic helper. Returns (ok, message)."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: _call_gemini(api_key, "Reply with exactly: ok")
        )
        return True, result.strip()[:200]
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "<no body>"
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _call_gemini(api_key: str, prompt: str) -> str:
    # Default model has the most generous free tier as of late 2025;
    # gemini-2.0-flash is paid-only on most free accounts. Override via
    # the GEMINI_MODEL env var if needed.
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
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


def _local_tips(
    expenses: list[dict],
    total_expense: float,
    total_income: float,
    net_flow: float,
    cat_totals: dict[str, float],
) -> list[str]:
    """Generate rule-based savings tips without AI.

    Tips are derived from the data: dominant category/merchant, deficit,
    cash withdrawals, possible subscriptions. Returns 0-5 tips.
    """
    tips: list[str] = []

    # 1. Deficit warning
    if total_income > 0 and net_flow < 0:
        deficit_pct = abs(net_flow) / total_income * 100
        tips.append(
            f"החודש הוצאת {abs(net_flow):,.0f} ₪ יותר ממה שהכנסת "
            f"({deficit_pct:.0f}% גרעון). זהה 1-2 קטגוריות לקיצוץ."
        )

    # 2. Dominant category (>= 35% of expenses, excluding "אחר")
    if total_expense > 0 and cat_totals:
        ranked = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        for cat, amt in ranked:
            if cat == "אחר":
                continue
            pct = amt / total_expense * 100
            if pct >= 35:
                tips.append(
                    f"הקטגוריה הגדולה ביותר היא {cat} עם {pct:.0f}% מההוצאות "
                    f"({amt:,.0f} ₪). שווה לבדוק לעומק מה יושב שם."
                )
            break

    # 3. Dominant single merchant (>= 15%), skipping generic "transfer"-like entries
    SKIP = ("העברה", "העב'", "שיק", "ביט", "bit", "משיכה", "בנקט", "לא ידוע")
    by_desc: dict[str, float] = {}
    for t in expenses:
        if any(s in t["description"] for s in SKIP):
            continue
        by_desc[t["description"]] = by_desc.get(t["description"], 0.0) + t["amount"]
    if by_desc and total_expense > 0:
        top_merchant, top_amt = max(by_desc.items(), key=lambda x: x[1])
        top_pct = top_amt / total_expense * 100
        if top_pct >= 15:
            tips.append(
                f"בית עסק יחיד ({top_merchant}) לקח {top_pct:.0f}% מההוצאות החודש "
                f"({top_amt:,.0f} ₪). אם זה צפוי — מצוין. אם לא — שווה בדיקה."
            )

    # 4. Cash withdrawals: hard to track
    withdrawals = [t for t in expenses if "משיכה" in t["description"] or "בנקט" in t["description"]]
    total_withdrawn = sum(t["amount"] for t in withdrawals)
    if total_withdrawn >= 1000:
        tips.append(
            f"בוצעו {len(withdrawals)} משיכות מזומן בסך {total_withdrawn:,.0f} ₪. "
            f"מזומן קשה לעקוב — תשלום בכרטיס ייתן לך דוחות מדויקים יותר."
        )

    # 5. Recurring charges (3+ times with similar amounts) — likely subscriptions
    groups: dict[str, list[float]] = {}
    for t in expenses:
        if any(s in t["description"] for s in SKIP):
            continue
        groups.setdefault(t["description"], []).append(t["amount"])
    for desc, amts in groups.items():
        if len(amts) < 3:
            continue
        avg = sum(amts) / len(amts)
        if avg > 0 and all(abs(a - avg) / avg < 0.2 for a in amts):
            tips.append(
                f"חיובים חוזרים מ-{desc}: {len(amts)} פעמים, ~{avg:,.0f} ₪ כל פעם. "
                f"אם זה מנוי שכבר לא בשימוש — בטל אותו."
            )
            break  # one subscription tip is enough

    # 6. Eating-out heavy (>= 15% of expenses)
    eating_out = cat_totals.get("מסעדות ובתי קפה", 0.0)
    if total_expense > 0 and eating_out / total_expense >= 0.15:
        tips.append(
            f"הוצאת {eating_out:,.0f} ₪ במסעדות ובתי קפה "
            f"({eating_out / total_expense * 100:.0f}% מהחודש). בישול בבית 2-3 פעמים בשבוע "
            f"יכול לחסוך מאות שקלים."
        )

    return tips[:5]


def _format_report(
    cat_totals: dict[str, float],
    top_merchants: list[tuple[str, dict]],
    tips: list[str],
    expenses: list[dict], incomes: list[dict], max_summary_skipped: list[dict],
    total_expense: float, total_income: float, net_flow: float,
    poalim_count: int, max_count: int,
    closing_balance: float | None,
    ai_error: Exception | None,
) -> str:
    report = "📊 *דוח חודשי*\n\n"

    if isinstance(ai_error, urllib.error.HTTPError) and ai_error.code == 429:
        report += "ℹ️ _AI הגיע למכסה — הקטגוריות והטיפים חושבו מקומית._\n\n"
    elif ai_error is not None:
        report += "ℹ️ _שירות ה-AI לא היה זמין — הקטגוריות והטיפים חושבו מקומית._\n\n"

    report += _balance_section(closing_balance)
    report += f"💳 סה\"כ הוצאות: *{total_expense:,.0f} ₪*\n"
    report += f"   • פועלים עו\"ש: {poalim_count} תנועות\n"
    report += f"   • מקס: {max_count} עסקאות\n\n"

    report += _max_summary_note(max_summary_skipped)
    report += _income_section(incomes, total_income)
    report += _net_flow_section(total_income, total_expense, net_flow)

    if cat_totals and total_expense > 0:
        report += "📂 *פילוח הוצאות:*\n"
        for cat, amount in sorted(cat_totals.items(), key=lambda x: x[1], reverse=True):
            if amount <= 0:
                continue
            pct = amount / total_expense * 100
            bar = "🔴" if pct >= 30 else ("🟡" if pct >= 15 else "🟢")
            report += f"{bar} {cat}: *{amount:,.0f} ₪* ({pct:.0f}%)\n"
        report += "\n"

    if top_merchants:
        report += "🏪 *בתי עסק מובילים:*\n"
        for name, info in top_merchants[:5]:
            report += f"   • {name}: {info['total']:,.0f} ₪ ({info['count']} פעמים)\n"
        report += "\n"

    if tips:
        report += "💡 *טיפים לייעול:*\n"
        for i, tip in enumerate(tips[:5], 1):
            report += f"{i}. {tip}\n"
        report += "\n"

    report += "_לעדכון קטגוריות (למשל \"שיק\" → שכר דירה): ערוך את categories.py בריפו._\n"
    report += "_לניתוח חודש חדש: /reset_"
    return report


