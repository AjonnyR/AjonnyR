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


def _parse_poalim_evosh(text: str) -> list[dict]:
    """
    Parses Poalim bank statement (עו"ש).
    Each line looks like:
      ## 17/05/2026 כאל 622.93 ₪4,576.74
    or with a credit (זכות) amount and no debit.

    Strategy: find every line that starts with ## and a date,
    extract description + the FIRST number (debit or credit),
    ignore the last number (running balance).
    """
    transactions = []

    # Match lines like: ## DD/MM/YYYY  description  amount1  [amount2]
    # amount1 = debit or credit, amount2 = balance (we ignore it)
    pattern = re.compile(
        r'##\s+(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s+',
        re.MULTILINE
    )

    # Words that mean it's income — we skip these for expense tracking
    INCOME_KEYWORDS = [
        "משכורת", "מופ\"ת", "זיכוי", "מענק", "קבוצת השומרים", "bit העברת"
    ]

    for m in pattern.finditer(text):
        date = m.group(1)
        description = m.group(2).strip()
        amount_str = m.group(3)

        # Skip very short or numeric-only descriptions
        if len(description) < 2 or description.isdigit():
            continue

        # Skip income lines — they're credits, not expenses
        is_income = any(kw in description for kw in INCOME_KEYWORDS)
        if is_income:
            continue

        try:
            amount = float(amount_str.replace(",", ""))
            if amount <= 0 or amount > 200000:
                continue
            transactions.append({
                "date": date,
                "description": description,
                "amount": amount,
                "source": "פועלים עו\"ש"
            })
        except ValueError:
            continue

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
                    "source": "מקס"
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
                "source": "מקס"
            })

    return transactions
