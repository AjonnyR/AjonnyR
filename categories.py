"""User-editable categorisation rules used when Gemini AI is unavailable.

How to customise:
    Edit the lists below. Add merchant names / keywords under the category
    you want them to land in. Matching is case-insensitive on the post-RTL
    fixed description (so use normal Hebrew reading order).
    Then push to GitHub — Render will redeploy automatically.

Order matters: the FIRST category whose keyword appears in the description
wins. Put more specific categories above more generic ones.
"""

CATEGORY_RULES: dict[str, list[str]] = {
    "דיור ושכירות": [
        "שכר דירה", "ארנונה", "ועד בית", "חברת חשמל", "מי אביבים",
        "תאגיד מים",
        # Pay rent by check? Uncomment the next line to send all checks here:
        # "שיק",
    ],
    "מזון וסופרמרקט": [
        "שופרסל", "רמי לוי", "סופר", "יוחננוף", "ויקטורי", "מגה",
        "טיב טעם", "אושר עד", "יינות ביתן", "סטופ מרקט", "מחסני השוק",
    ],
    "מסעדות ובתי קפה": [
        "מסעדה", "קפה", "פיצה", "המבורגר", "מקדונלדס", "burger",
        "ארומה", "קופי", "wolt", "10bis", "tenbis",
    ],
    "תחבורה ודלק": [
        "דלק", "פז", "סונול", "תדלוק", "דור אלון", "ten", "טן",
        "פנגו", "pango", "סלופארק", "cellopark",
        "אגד", "רכבת", "כביש 6", "כביש6", "חניון", "מונית",
    ],
    "קניות ואופנה": [
        "זארה", "zara", "H&M", "קסטרו", "פוקס", "טרמינל איקס",
        "הום סנטר", "איקאה", "ikea", "ace",
    ],
    "בריאות ופארמה": [
        "סופר פארם", "ניו פארם", "מכבי", "כללית", "מאוחדת", "לאומית",
        "בית מרקחת", "טרם", "פארמה",
    ],
    "בילוי ופנאי": [
        "סינמה", "יס פלאנט", "yes planet", "הוט סינמה", "נטפליקס",
        "netflix", "spotify", "ספוטיפיי", "חדר כושר", "gym",
    ],
    "חינוך": [
        "גן ילדים", "בית ספר", "אוניברסיטה", "מכללה", "צהרון",
    ],
    "תקשורת וסלולר": [
        "סלקום", "פרטנר", "פלאפון", "בזק", "012", "019", "hot",
    ],
    "ביטוח ופיננסים": [
        "ביטוח", "כלל", "הראל", "מנורה", "מגדל", "פניקס",
        "ביטוח לאומי", "מס הכנסה", "כאל", "ויזה",
    ],
    "העברות ותשלומים": [
        "העברה", "העב'", "שיק", "שיקים", "ביט", "paybox", "bit",
        "משיכה", "בנקט",
    ],
    "אחר": [],
}


def categorize(description: str) -> str:
    """Return the category for a description.

    Lookup order: learned cache (populated by AI on previous calls) →
    static keyword rules → "אחר". The learned cache lets AI's
    categorisations carry forward to local-only runs without code edits.
    """
    if description in LEARNED:
        return LEARNED[description]
    desc_lower = description.lower()
    for category, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw.lower() in desc_lower:
                return category
    return "אחר"


# In-memory cache of merchant → category learned from AI in past calls.
# Survives across requests within the same Render container instance;
# wiped on cold start (sleep + wake / redeploy). To persist across cold
# starts, copy entries from the bot's report into CATEGORY_RULES above.
LEARNED: dict[str, str] = {}


def learn(description: str, category: str) -> bool:
    """Record an AI categorisation for future reuse. Returns True if new."""
    if category not in CATEGORY_RULES:
        return False
    if LEARNED.get(description) == category:
        return False
    # Don't waste a learned slot on something the static rules already cover.
    desc_lower = description.lower()
    for cat, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw.lower() in desc_lower:
                # Static rule would have caught this anyway; only store if
                # the AI disagrees with the rule.
                if cat == category:
                    return False
                break
    LEARNED[description] = category
    return True


def get_learned() -> dict[str, str]:
    return dict(LEARNED)
