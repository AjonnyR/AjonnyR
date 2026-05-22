import pdfplumber
import openpyxl
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def process_pdf(file_path: str) -> tuple[list[dict], float | None]:
    """Parse a Hapoalim PDF.

    Returns (transactions, closing_balance). closing_balance is the balance
    after the most recent dated transaction in the statement — i.e. the
    user's current account balance — or None if it couldn't be extracted.
    """
    transactions: list[dict] = []
    closing_balance: float | None = None
    try:
        with pdfplumber.open(file_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
        transactions, closing_balance = _parse_poalim_evosh(full_text)
    except Exception as e:
        logger.error(f"PDF read error: {e}")
        return [], None
    logger.info(f"פועלים: נמצאו {len(transactions)} עסקאות, יתרה={closing_balance}")
    return transactions, closing_balance


def _fix_rtl(text: str) -> str:
    """
    pdfplumber reverses Hebrew text in two ways:
    1. Word order is reversed
    2. Each word's letters are reversed
    e.g. 'יסנניפ טיא סקמ' -> 'מקס איט פיננסי'
    Fix: reverse word order AND reverse each word's characters.
    """
    return " ".join(w[::-1] for w in text.split()[::-1])


def _parse_poalim_evosh(text: str) -> list[dict]:
    """
    Poalim bank statement (עו"ש) PDF.
    pdfplumber extracts each row as TWO lines:
      Line 1: ##
      Line 2: ₪BALANCE  AMOUNT  DESCRIPTION(reversed)  DATE

    Poalim is the main account — money sits here and everything is debited
    from it. We classify each transaction:
      - income       : credits (salary, refunds, transfers in)
      - max_summary  : the monthly Max credit-card debit (do NOT sum with
                       the Max file rows — that would double-count)
      - expense      : everything else
    """
    # The "##" marker appears either on its own line ("##\n₪...") or — for
    # the very first (most recent) transaction — inline with the data
    # ("## ₪..."). Allow either with \s+ so we don't drop the latest row.
    pattern = re.compile(
        r'##\s+(₪[\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+(.+?)\s+(\d{2}/\d{2}/\d{4})'
    )

    # Hebrew keywords (reversed, since pdfplumber returns text reversed)
    # that indicate INCOME — credits to the account.
    # NOTE: "העברה" is intentionally NOT here — it matches outgoing transfers
    # too (e.g. "העברה לאחר נייד") and would mis-classify expenses as income.
    INCOME_KEYWORDS = [
        "תרוכשמ",        # משכורת
        'ת"פומ',          # מופ"ת
        "יוכיז",          # זיכוי
        "קנעמ",           # מענק
        "םירמושה תצובק",  # קבוצת השומרים
        "רזחה",           # החזר
        "תיביר",          # ריבית
    ]

    # Identifies the monthly Max credit-card debit line in the Poalim
    # statement. We require the full distinctive phrase "מקס איט" (reversed:
    # "טיא סקמ") — matching just "מקס"/"סקמ" caught dozens of unrelated rows
    # (any word containing those 3 letters: מקסים, מקסי, etc).
    MAX_SUMMARY_PATTERNS = [
        "טיא סקמ",        # מקס איט   (the Max debit line in Hapoalim)
        "יסנניפ טיא סקמ", # מקס איט פיננסי
    ]
    # Cal credit card monthly debit line. The description in the PDF after
    # RTL fix is exactly "כאל". Reversed in the raw text: "לאכ".
    CAL_SUMMARY_DESCRIPTIONS = {"כאל"}

    transactions = []
    balances_by_date: list[tuple[datetime, float]] = []

    for m in pattern.finditer(text):
        balance_str = m.group(1)
        amount_str = m.group(2)
        desc_raw = m.group(3).strip()
        date = m.group(4)
        description = _fix_rtl(desc_raw)

        try:
            amount = float(amount_str.replace(",", ""))
            if amount <= 0 or amount > 200000:
                continue
        except ValueError:
            continue

        try:
            balance = float(balance_str.replace("₪", "").replace(",", ""))
            dt = datetime.strptime(date, "%d/%m/%Y")
            balances_by_date.append((dt, balance))
        except ValueError:
            pass

        if any(kw in desc_raw for kw in INCOME_KEYWORDS):
            tx_type = "income"
        elif any(kw in desc_raw for kw in MAX_SUMMARY_PATTERNS):
            tx_type = "max_summary"
        elif description in CAL_SUMMARY_DESCRIPTIONS:
            tx_type = "cal_summary"
        else:
            tx_type = "expense"

        transactions.append({
            "date": date,
            "description": description,
            "amount": amount,
            "source": 'פועלים עו"ש',
            "type": tx_type,
        })

    closing_balance: float | None = None
    if balances_by_date:
        balances_by_date.sort(key=lambda x: x[0], reverse=True)
        closing_balance = balances_by_date[0][1]

    return transactions, closing_balance


def process_excel(file_path: str) -> list[dict]:
    transactions = []
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        header_row = None
        col_map = {}

        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            row_str = [str(c).strip() if c else "" for c in row]
            if any("תאריך" in c for c in row_str) and any("עסק" in c or "תיאור" in c for c in row_str):
                header_row = i
                for j, cell in enumerate(row_str):
                    if "תאריך עסקה" in cell:
                        col_map["date"] = j
                    elif "שם בית" in cell or "תיאור" in cell:
                        # Catches both "שם בית עסק" and "שם בית העסק".
                        col_map["description"] = j
                    elif "סכום חיוב" in cell:
                        # Prefer the actually-charged amount, not "סכום עסקה
                        # מקורי" which holds the gross/installment-total
                        # and inflates totals for split payments.
                        col_map["amount"] = j
                # If no "סכום חיוב" column exists, fall back to "סכום עסקה".
                if "amount" not in col_map:
                    for j, cell in enumerate(row_str):
                        if "סכום עסקה" in cell:
                            col_map["amount"] = j
                            break
                break

        if not header_row or "date" not in col_map:
            logger.warning("Max Excel: header not found, trying generic parse")
            transactions = _parse_excel_generic(ws)
            return transactions

        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            if not any(row):
                continue
            try:
                date = str(row[col_map["date"]]).strip() if row[col_map["date"]] else ""
                description = str(row[col_map["description"]]).strip() if "description" in col_map and row[col_map["description"]] else "לא ידוע"
                amount_raw = row[col_map["amount"]] if "amount" in col_map else None

                if not date or date == "None":
                    continue

                amount = 0.0
                if amount_raw is not None:
                    amount = float(str(amount_raw).replace(",", "").replace("₪", "").strip())
                    if amount < 0:
                        amount = abs(amount)

                if amount <= 0 or amount > 100000:
                    continue

                transactions.append({
                    "date": date,
                    "description": description,
                    "amount": amount,
                    "source": "מקס",
                    "type": "expense",
                })
            except (ValueError, TypeError, IndexError) as e:
                logger.debug(f"Skipped row: {e}")
                continue

    except Exception as e:
        logger.error(f"Excel read error: {e}")
        return []

    logger.info(f"מקס: נמצאו {len(transactions)} עסקאות")
    return transactions


def _parse_excel_generic(ws) -> list[dict]:
    transactions = []
    date_pattern = re.compile(r'\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}')

    for row in ws.iter_rows(values_only=True):
        row_vals = [str(c).strip() if c is not None else "" for c in row]
        if not any(row_vals):
            continue

        date_found = None
        amount_found = None
        desc_found = None

        for val in row_vals:
            if date_pattern.match(val):
                date_found = val
            else:
                try:
                    num = float(val.replace(",", ""))
                    if 1 < num < 50000:
                        amount_found = num
                except ValueError:
                    if len(val) > 3 and not val.isdigit():
                        desc_found = val

        if date_found and amount_found:
            transactions.append({
                "date": date_found,
                "description": desc_found or "לא ידוע",
                "amount": amount_found,
                "source": "מקס",
                "type": "expense",
            })

    return transactions
