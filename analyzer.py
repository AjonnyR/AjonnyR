import os
import asyncio
import json
import logging
import urllib.request
import urllib.error
import time
from datetime import datetime

from categories import (
    categorize, learn, persist, schedule_persist, get_learned,
    update_merchant_snapshot, update_latest_analysis, latest_saved_month,
    CATEGORY_RULES, LEARNED,
)

logger = logging.getLogger(__name__)

CATEGORIES = list(CATEGORY_RULES.keys())

async def analyze_full(
    transactions: list[dict], closing_balance: float | None = None
) -> tuple[str, list[tuple[str, float]]]:
    """Produce the full report.

    Categorisation comes from AI (primary) with a fallback to the
    categorize() chain (LEARNED → static rules → "אחר"). Everything else
    — balance, totals, net flow, merchant aggregation — is computed
    locally.

    Returns (report_text, uncategorized_descriptions) where
    uncategorized_descriptions is a list of (description, total) tuples
    for merchants that ended up in "אחר", sorted by total descending.
    The caller can use this list to offer the user inline buttons to fix
    them.
    """
    # Poalim is the main account. Monthly "Max" / "Cal" debit lines in
    # Poalim are just summaries of the per-merchant rows in those cards'
    # own files — if we also got the breakdown (Max Excel or Cal images),
    # exclude the summary lines to avoid double-counting.
    has_max_file = any(t.get("source") == "מקס" for t in transactions)
    has_cal_data = any(t.get("source") == "כאל פועלים" for t in transactions)

    expenses: list[dict] = []
    incomes: list[dict] = []
    max_summary_skipped: list[dict] = []
    cal_summary_skipped: list[dict] = []
    for t in transactions:
        ttype = t.get("type", "expense")
        if ttype == "income":
            incomes.append(t)
        elif ttype == "max_summary":
            if has_max_file:
                max_summary_skipped.append(t)
            else:
                expenses.append(t)
        elif ttype == "cal_summary":
            if has_cal_data:
                cal_summary_skipped.append(t)
            else:
                expenses.append(t)
        else:
            expenses.append(t)

    total_expense = sum(t["amount"] for t in expenses)
    total_income = sum(t["amount"] for t in incomes)
    net_flow = total_income - total_expense

    # Refresh the per-merchant snapshot so /categories and the button
    # confirmations can show how many transactions / how much per
    # merchant. Replaces any previous snapshot (matches the user's
    # monthly review pattern).
    update_merchant_snapshot(expenses)

    # Latest-analysis aggregates for /savemonth and comparison.
    all_dates = [t["date"] for t in transactions if t.get("date")]
    sorted_dates = sorted(set(all_dates), key=_parse_dmy)
    update_latest_analysis({
        "total_expense": total_expense,
        "total_income": total_income,
        "net_flow": net_flow,
        "closing_balance": closing_balance,
        "earliest_date": sorted_dates[0] if sorted_dates else None,
        "latest_date": sorted_dates[-1] if sorted_dates else None,
    })

    poalim_count = sum(1 for t in expenses if "פועלים" in t["source"] and t["source"] != "כאל פועלים")
    max_count = sum(1 for t in expenses if t["source"] == "מקס")
    cal_count = sum(1 for t in expenses if t["source"] == "כאל פועלים")

    # Merchant aggregation (always local — pure math)
    by_desc: dict[str, dict] = {}
    for t in expenses:
        key = t["description"]
        if key not in by_desc:
            by_desc[key] = {"total": 0.0, "count": 0}
        by_desc[key]["total"] += t["amount"]
        by_desc[key]["count"] += 1
    top_merchants = sorted(by_desc.items(), key=lambda x: x[1]["total"], reverse=True)[:8]

    # Ask AI to categorise unique descriptions and produce tips in one call.
    # If AI fails, categorisation falls back to categorize() (which already
    # consults LEARNED from any prior successful AI call this container).
    # IMPORTANT: only send merchants that don't already have a category
    # (not in LEARNED and no static rule matches). This:
    #   1. Protects prior decisions — user /correct + button taps + earlier
    #      AI labels are never overwritten by a fresh AI call.
    #   2. Saves Gemini tokens — already-known merchants don't go on the wire.
    unique_descs = list(by_desc.keys())
    descs_for_ai = [
        d for d in unique_descs
        if d not in LEARNED and categorize(d) == "אחר"
    ]

    api_key = os.environ.get("GEMINI_API_KEY")
    ai_categories: dict[str, str] = {}
    ai_tips: list[str] = []
    ai_error: Exception | None = None
    newly_learned: dict[str, str] = {}

    if api_key:
        try:
            ai_categories, ai_tips = await _get_ai_analysis(
                api_key, descs_for_ai, total_income, total_expense, net_flow,
            )
            for desc, cat in ai_categories.items():
                # Defence in depth — never overwrite an existing category
                # even if the AI returned one for a merchant we didn't ask about.
                if desc in LEARNED:
                    continue
                if categorize(desc) != "אחר":
                    continue
                if learn(desc, cat):
                    newly_learned[desc] = cat
        except Exception as e:
            ai_error = e
            logger.error(f"Gemini analysis failed: {e}")

    # Queue persist for the merchant snapshot (always changes on analyze)
    # and any newly-learned categories. The debounce coalesces multiple
    # analyses in a session into a single GitHub commit / Render redeploy.
    if newly_learned:
        msg = (
            f"Learn {len(newly_learned)} new "
            f"{'category' if len(newly_learned) == 1 else 'categories'} "
            f"from AI + refresh merchant snapshot"
        )
    else:
        msg = "Refresh merchant snapshot"
    try:
        await schedule_persist(msg)
    except Exception as e:
        logger.warning(f"schedule_persist after analyze failed: {e}")

    # Build per-transaction category and collect descriptions that landed
    # in "אחר" (the user might want to fix them inline via buttons).
    cat_totals: dict[str, float] = {}
    uncategorized_totals: dict[str, float] = {}
    for t in expenses:
        cat = ai_categories.get(t["description"]) or categorize(t["description"])
        if cat not in CATEGORY_RULES:
            cat = "אחר"
        cat_totals[cat] = cat_totals.get(cat, 0.0) + t["amount"]
        if cat == "אחר":
            uncategorized_totals[t["description"]] = (
                uncategorized_totals.get(t["description"], 0.0) + t["amount"]
            )

    # Tips: use AI's if delivered, otherwise local rule-based.
    tips = ai_tips if ai_tips else _local_tips(
        expenses, total_expense, total_income, net_flow, cat_totals
    )

    # Augment LATEST_ANALYSIS with per-category breakdown — needed by
    # /savemonth so future comparisons can show category-level deltas.
    cats_for_history: dict[str, dict] = {}
    for cat, total in cat_totals.items():
        count = sum(1 for t in expenses if (
            ai_categories.get(t["description"]) or categorize(t["description"])
        ) == cat)
        cats_for_history[cat] = {"total": total, "count": count}
    update_latest_analysis({
        "total_expense": total_expense,
        "total_income": total_income,
        "net_flow": net_flow,
        "closing_balance": closing_balance,
        "earliest_date": sorted_dates[0] if sorted_dates else None,
        "latest_date": sorted_dates[-1] if sorted_dates else None,
        "categories": cats_for_history,
    })

    report = _format_report(
        cat_totals, top_merchants, tips, newly_learned,
        expenses, incomes, max_summary_skipped, cal_summary_skipped,
        total_expense, total_income, net_flow,
        poalim_count, max_count, cal_count, closing_balance,
        ai_error if not ai_categories else None,
    )

    # Sort uncategorized by total descending — biggest first matters most.
    uncategorized_sorted = sorted(
        uncategorized_totals.items(), key=lambda x: x[1], reverse=True
    )
    return report, uncategorized_sorted


async def analyze_expenses(transactions: list[dict], closing_balance: float | None = None) -> str:
    """Backwards-compatible wrapper that returns only the report string."""
    report, _ = await analyze_full(transactions, closing_balance)
    return report


async def _get_ai_analysis(
    api_key: str,
    descriptions: list[str],
    total_income: float,
    total_expense: float,
    net_flow: float,
) -> tuple[dict[str, str], list[str]]:
    """Ask Gemini to categorise the supplied descriptions AND produce tips
    in one call. Returns (description_to_category, tips).

    `descriptions` should already be filtered to merchants that need a
    category — known merchants are excluded by the caller so the AI
    doesn't waste tokens (or risk overwriting prior decisions).

    Prompt size scales with the number of unique uncategorised merchants
    (~30 chars each), not with the number of transactions."""
    cat_list = ", ".join(CATEGORY_RULES.keys())

    if descriptions:
        desc_section = (
            f"Categorise each of these NEW merchant/transaction descriptions "
            f"into exactly one of: {cat_list}\n\n"
            f"Descriptions:\n" + "\n".join(f"- {d}" for d in descriptions) + "\n\n"
        )
        categories_json_hint = '"categories": {"<description>": "<category>", ...}, '
    else:
        # All merchants already have categories — no labelling work, just tips.
        desc_section = "All merchants in this statement are already categorised.\n\n"
        categories_json_hint = '"categories": {}, '

    prompt = (
        f"You are a personal finance assistant for an Israeli user. Reply in Hebrew.\n\n"
        f"Monthly statement (ILS):\n"
        f"- Income: {total_income:,.0f}\n"
        f"- Expenses: {total_expense:,.0f}\n"
        f"- Net: {net_flow:+,.0f}\n\n"
        f"{desc_section}"
        f"Give 3-5 specific, actionable Hebrew tips based on the data.\n\n"
        f'Reply with ONLY this JSON, no markdown, no backticks:\n'
        f'{{{categories_json_hint}'
        f'"tips": ["tip 1", "tip 2", "tip 3"]}}'
    )

    loop = asyncio.get_event_loop()
    raw_text = None
    # 429 = quota, 503 = model overloaded — both are transient enough to retry.
    RETRYABLE = {429, 503}
    for attempt in range(3):
        try:
            raw_text = await loop.run_in_executor(None, lambda: _call_gemini(api_key, prompt))
            break
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE and attempt < 2:
                await asyncio.sleep(10 if e.code == 503 else 15)
            else:
                raise

    if raw_text is None:
        return {}, []

    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON: {raw_text[:200]}")
        return {}, []

    raw_cats = data.get("categories", {}) if isinstance(data, dict) else {}
    cats: dict[str, str] = {}
    if isinstance(raw_cats, dict):
        for desc, cat in raw_cats.items():
            cat_str = str(cat).strip()
            if cat_str in CATEGORY_RULES:
                cats[str(desc)] = cat_str

    raw_tips = data.get("tips", []) if isinstance(data, dict) else []
    tips = [str(t) for t in raw_tips][:5] if isinstance(raw_tips, list) else []

    return cats, tips


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


def _summary_note(skipped: list[dict], card_name: str, source_name: str) -> str:
    if not skipped:
        return ""
    total = sum(t["amount"] for t in skipped)
    plural = "י" if len(skipped) > 1 else ""
    return (
        f"ℹ️ _זיהיתי {len(skipped)} חיוב{plural} {card_name} בפועלים "
        f"(סה\"כ {total:,.0f} ₪) — לא נספרו פעמיים, "
        f"הפירוט נלקח מ{source_name}._\n\n"
    )


def _max_summary_note(max_summary_skipped: list[dict]) -> str:
    return _summary_note(max_summary_skipped, "מקס", "קובץ מקס")


def _cal_summary_note(cal_summary_skipped: list[dict]) -> str:
    return _summary_note(cal_summary_skipped, "כאל", "קובץ כאל")


def _parse_dmy(s: str) -> datetime:
    """Parse DD/MM/YYYY → datetime; falls back to epoch on garbage."""
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except (ValueError, TypeError):
        return datetime(1970, 1, 1)


def _verify_card(
    skipped_summaries: list[dict],
    breakdown_transactions: list[dict],
    card_name: str,
) -> str:
    """Cross-check the breakdown file (Max/Cal Excel) against Hapoalim.

    Strategy: walk the Hapoalim card-debit lines most-recent-first,
    accumulating their amounts until the running sum matches the
    breakdown total within tolerance (±5 ₪ or ±0.5%, whichever larger).
    If a prefix matches → ✅ with the date range. Otherwise → ⚠️ with the
    delta so the user can see what's missing.
    """
    if not skipped_summaries or not breakdown_transactions:
        return ""

    breakdown_total = sum(t["amount"] for t in breakdown_transactions)
    tolerance = max(5.0, abs(breakdown_total) * 0.005)

    # Most-recent-first matches the user's mental model: "the latest
    # Excel covers the latest debit(s)".
    summaries_sorted = sorted(
        skipped_summaries, key=lambda t: _parse_dmy(t["date"]), reverse=True
    )

    running = 0.0
    used: list[dict] = []
    matched = False
    for t in summaries_sorted:
        running += t["amount"]
        used.append(t)
        if abs(running - breakdown_total) <= tolerance:
            matched = True
            break
        if running > breakdown_total + tolerance:
            break

    if matched:
        dates_sorted = sorted({t["date"] for t in used}, key=_parse_dmy)
        if len(dates_sorted) == 1:
            date_str = dates_sorted[0]
        else:
            date_str = f"{dates_sorted[0]}–{dates_sorted[-1]}"
        return (
            f"✅ *{card_name}:* {breakdown_total:,.2f} ₪ בקובץ — "
            f"תואם ל-{len(used)} חיוב{'י' if len(used) > 1 else ''} בפועלים "
            f"({date_str}).\n"
        )

    # No exact prefix matched. Show the closest cluster (the lines we
    # walked through before overshooting), NOT the full PDF total — the
    # user's Hapoalim usually covers several months, so reporting the
    # multi-month grand total would be misleading.
    if not used:
        return ""
    cluster_total = sum(t["amount"] for t in used)
    cluster_dates = sorted({t["date"] for t in used}, key=_parse_dmy)
    date_str = (
        cluster_dates[0] if len(cluster_dates) == 1
        else f"{cluster_dates[0]}–{cluster_dates[-1]}"
    )
    delta = breakdown_total - cluster_total
    plural = "י" if len(used) > 1 else ""
    return (
        f"⚠️ *{card_name}:* קובץ {card_name} {breakdown_total:,.2f} ₪; "
        f"{len(used)} חיוב{plural} בעו\"ש ({date_str}) = "
        f"{cluster_total:,.2f} ₪. פער {delta:+,.2f} ₪ — "
        f"ייתכן שחסרות עסקאות בקובץ (למשל כרטיסי משפחה נוספים).\n"
    )


def _verification_section(
    max_summary_skipped: list[dict],
    cal_summary_skipped: list[dict],
    max_breakdown: list[dict],
    cal_breakdown: list[dict],
) -> str:
    """Build the self-check section comparing each card breakdown to the
    matching Hapoalim debits. Skipped when nothing to check."""
    lines = []
    line = _verify_card(max_summary_skipped, max_breakdown, "מקס")
    if line:
        lines.append(line)
    line = _verify_card(cal_summary_skipped, cal_breakdown, "כאל")
    if line:
        lines.append(line)
    if not lines:
        return ""
    return "🧮 *בדיקה עצמית של הבוט:*\n" + "".join(lines) + "\n"


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


def _comparison_section(
    cat_totals: dict[str, float], total_expense: float, total_income: float
) -> str:
    """If there's a saved month in history, show how the current report
    compares to it. Built from MONTHLY_HISTORY via latest_saved_month()."""
    pair = latest_saved_month()
    if not pair:
        return ""
    label, prev = pair
    prev_exp = float(prev.get("total_expense", 0) or 0)
    prev_inc = float(prev.get("total_income", 0) or 0)
    prev_cats = prev.get("categories", {}) or {}

    if prev_exp <= 0:
        return ""

    def _delta(cur: float, prev: float) -> str:
        if prev <= 0:
            return f"חדש (+{cur:,.0f} ₪)"
        diff = cur - prev
        pct = diff / prev * 100
        arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
        return f"{arrow} {abs(pct):.0f}% ({diff:+,.0f} ₪)"

    section = f"📊 *השוואה ל-{label}:*\n"
    section += f"   הוצאות: {_delta(total_expense, prev_exp)}\n"
    if prev_inc > 0:
        section += f"   הכנסות: {_delta(total_income, prev_inc)}\n"

    # Per-category deltas — only show categories that exist in both periods,
    # sorted by absolute change.
    cat_lines: list[tuple[float, str]] = []
    for cat, cur_total in cat_totals.items():
        if cur_total <= 0:
            continue
        prev_total = float(prev_cats.get(cat, {}).get("total", 0) or 0)
        if prev_total == 0 and cur_total == 0:
            continue
        line = f"   • {cat}: {_delta(cur_total, prev_total)}"
        cat_lines.append((abs(cur_total - prev_total), line))
    cat_lines.sort(key=lambda x: -x[0])
    for _, line in cat_lines[:5]:
        section += line + "\n"

    return section + "\n"


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
    newly_learned: dict[str, str],
    expenses: list[dict], incomes: list[dict],
    max_summary_skipped: list[dict], cal_summary_skipped: list[dict],
    total_expense: float, total_income: float, net_flow: float,
    poalim_count: int, max_count: int, cal_count: int,
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
    if max_count:
        report += f"   • מקס: {max_count} עסקאות\n"
    if cal_count:
        report += f"   • כאל פועלים: {cal_count} עסקאות\n"
    report += "\n"

    report += _max_summary_note(max_summary_skipped)
    report += _cal_summary_note(cal_summary_skipped)

    # Bot self-check: card breakdown totals vs Hapoalim debit lines.
    max_breakdown = [t for t in expenses if t.get("source") == "מקס"]
    cal_breakdown = [t for t in expenses if t.get("source") == "כאל פועלים"]
    report += _verification_section(
        max_summary_skipped, cal_summary_skipped,
        max_breakdown, cal_breakdown,
    )

    report += _income_section(incomes, total_income)
    report += _net_flow_section(total_income, total_expense, net_flow)
    report += _comparison_section(cat_totals, total_expense, total_income)

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

    if newly_learned:
        report += "🎓 *הבוט למד קטגוריות חדשות מה-AI:*\n"
        for desc, cat in newly_learned.items():
            report += f"   • {desc} → {cat}\n"
        report += (
            "_להפיכתן לקבועות (שיעבוד גם בלי AI): הוסף אותן ל-CATEGORY\\_RULES "
            "ב-categories.py ו-push לריפו._\n\n"
        )

    report += "_לעדכון קטגוריות (למשל \"שיק\" → שכר דירה): ערוך את categories.py בריפו._\n"
    report += "_לניתוח חודש חדש: /reset_"
    return report


