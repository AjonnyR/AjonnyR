import pdfplumber
import openpyxl
import re
import logging

logger = logging.getLogger(__name__)


def process_pdf(file_path: str) -> list[dict]:
    transactions = []
    try:
        with pdfplumber.open(file_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
        transactions = _parse_poalim_evosh(full_text)
    except Exception as e:
        logger.error(f"PDF read error: {e}")
        return []
    logger.info(f"פועלים: נמצאו {len(transactions)} עסקאות")
    return transactions


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
    pattern = re.compile(
        r'##\n(₪[\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+(.+?)\s+(\d{2}/\d{2}/\d{4})'
    )

    # Hebrew keywords (reversed, since pdfplumber returns text reversed)
    # that indicate INCOME — credits to the account.
    INCOME_KEYWORDS = [
        "תרוכשמ",        # משכורת
        'ת"פומ',          # מופ"ת
        "יוכיז",          # זיכוי
        "קנעמ",           # מענק
        "םירמושה תצובק",  # קבוצת השומרים
        "הרבעה",          # העברה (incoming transfer)
        "רזחה",           # החזר
        "תיביר",          # ריבית
    ]

    # Keywords identifying the monthly Max credit-card debit line in Poalim.
    # When the Max file is also provided, this line is replaced by the
    # per-merchant breakdown from Max, so we mark it separately.
    MAX_SUMMARY_KEYWORDS = [
        "סקמ",            # מקס
        "יסנניפ טיא סקמ", # מקס איט פיננסי
        "XAM",            # MAX
    ]

    transactions = []
    for m in pattern.finditer(text):
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

        if any(kw in desc_raw for kw in INCOME_KEYWORDS):
            tx_type = "income"
        elif any(kw in desc_raw for kw in MAX_SUMMARY_KEYWORDS):
            tx_type = "max_summary"
        else:
            tx_type = "expense"

        transactions.append({
            "date": date,
            "description": description,
            "amount": amount,
            "source": 'פועלים עו"ש',
            "type": tx_type,
        })

    return transactions


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
                    if "תאריך" in cell and "עסקה" in cell:
                        col_map["date"] = j
                    elif "שם בית עסק" in cell or "תיאור" in cell:
                        col_map["description"] = j
                    elif "סכום חיוב" in cell or "סכום עסקה" in cell:
                        col_map["amount"] = j
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
