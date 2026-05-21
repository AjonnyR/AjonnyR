import pdfplumber
import openpyxl
import re
import logging

logger = logging.getLogger(__name__)


def process_pdf(file_path: str) -> list[dict]:
    """
    קורא את קובץ ה-PDF מפועלים ומחלץ עסקאות.
    מחזיר רשימה של דיקשנרים עם: date, description, amount, source
    """
    transactions = []

    try:
        with pdfplumber.open(file_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"

            transactions = _parse_poalim_text(full_text)

    except Exception as e:
        logger.error(f"שגיאה בקריאת PDF: {e}")
        return []

    logger.info(f"פועלים: נמצאו {len(transactions)} עסקאות")
    return transactions


def _parse_poalim_text(text: str) -> list[dict]:
    """
    מנתח את הטקסט שחולץ מ-PDF של פועלים.
    פועלים מציג שורות בפורמט: תאריך | תיאור | סכום
    
    הערה: הפורמט המדויק משתנה לפי סוג הדוח.
    אם הבוט לא מזהה עסקאות, שלח את הפורמט האמיתי ונתאים.
    """
    transactions = []

    # דפוס 1: שורות בפורמט  DD/MM/YY  תיאור  סכום
    pattern1 = re.compile(
        r'(\d{2}/\d{2}/\d{2,4})\s+(.+?)\s+([\d,]+\.?\d*)\s*(?:₪|-)?',
        re.MULTILINE
    )

    # דפוס 2: שורות שמתחילות בתאריך DD.MM.YY
    pattern2 = re.compile(
        r'(\d{2}\.\d{2}\.\d{2,4})\s+(.+?)\s+([\d,]+\.?\d*)',
        re.MULTILINE
    )

    for pattern in [pattern1, pattern2]:
        matches = pattern.findall(text)
        if matches:
            for match in matches:
                date, description, amount_str = match
                description = description.strip()
                # מסנן שורות כותרת ושורות ריקות
                if len(description) < 2 or description.isdigit():
                    continue
                try:
                    amount = float(amount_str.replace(",", ""))
                    # מסנן סכומים לא הגיוניים (אפס, שליליים מאוד, וכו')
                    if amount <= 0 or amount > 100000:
                        continue
                    transactions.append({
                        "date": date,
                        "description": description,
                        "amount": amount,
                        "source": "פועלים"
                    })
                except ValueError:
                    continue
            if transactions:
                break  # מספיק עם הדפוס הראשון שעבד

    # אם לא מצאנו כלום — מחזירים דוגמה כדי שהמשתמש יבין מה קורה
    if not transactions:
        logger.warning("לא נמצאו עסקאות ב-PDF. ייתכן שפורמט הדוח שונה.")

    return transactions


def process_excel(file_path: str) -> list[dict]:
    """
    קורא את קובץ ה-Excel ממקס ומחלץ עסקאות.
    מקס מייצא בפורמט xlsx עם עמודות ספציפיות.
    """
    transactions = []

    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        # מחפשים את שורת הכותרת — מכילה "תאריך עסקה" או "שם בית עסק"
        header_row = None
        col_map = {}

        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            row_str = [str(c).strip() if c else "" for c in row]
            # מחפשים עמודות מפתח של מקס
            if any("תאריך" in c for c in row_str) and any("עסק" in c or "תיאור" in c for c in row_str):
                header_row = i
                for j, cell in enumerate(row_str):
                    if "תאריך" in cell and "עסקה" in cell:
                        col_map["date"] = j
                    elif "שם בית עסק" in cell or "תיאור" in cell:
                        col_map["description"] = j
                    elif "סכום" in cell and "חיוב" in cell:
                        col_map["amount"] = j
                    elif "סכום עסקה" in cell:
                        col_map["amount"] = j
                break

        if not header_row or "date" not in col_map:
            logger.warning("לא נמצאה שורת כותרת תקינה ב-Excel של מקס")
            # ננסה גישה גנרית
            transactions = _parse_excel_generic(ws)
            return transactions

        # קוראים את שורות הנתונים
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
                logger.debug(f"שורה דולגה: {e}")
                continue

    except Exception as e:
        logger.error(f"שגיאה בקריאת Excel: {e}")
        return []

    logger.info(f"מקס: נמצאו {len(transactions)} עסקאות")
    return transactions


def _parse_excel_generic(ws) -> list[dict]:
    """
    גישה גנרית אם לא נמצאה כותרת תקינה —
    מחפש שורות שמכילות תאריך וסכום.
    """
    transactions = []
    date_pattern = re.compile(r'\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}')

    for row in ws.iter_rows(values_only=True):
        row_vals = [str(c).strip() if c is not None else "" for c in row]
        if not any(row_vals):
            continue

        date_found = None
        amount_found = None
        desc_found = None

        for i, val in enumerate(row_vals):
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
