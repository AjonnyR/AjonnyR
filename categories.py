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


# In-memory cache of merchant → category. Populated from learned.json on
# startup (if GITHUB_TOKEN+GITHUB_REPO are set) and updated as AI learns
# new merchants or the user runs /correct. Without GitHub configured the
# dict is in-memory only and resets when the Render container restarts.
LEARNED: dict[str, str] = {}


def _load_from_store() -> None:
    """Best-effort load from GitHub on import. Failures don't crash the bot."""
    try:
        import learning_store
        loaded = learning_store.load()
        if loaded:
            LEARNED.update(loaded)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Couldn't load LEARNED: {e}")


_load_from_store()


def learn(description: str, category: str) -> bool:
    """Record a categorisation for future reuse. Returns True if it's new
    (i.e. wasn't already cached and isn't covered by a static rule)."""
    if category not in CATEGORY_RULES:
        return False
    if LEARNED.get(description) == category:
        return False
    # Don't waste a learned slot on something the static rules already cover.
    desc_lower = description.lower()
    for cat, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw.lower() in desc_lower:
                if cat == category:
                    return False
                break
    LEARNED[description] = category
    return True


def override(description: str, category: str) -> bool:
    """User correction. Records the mapping even if a static rule would
    have matched — the user's verdict always wins. Returns True on
    success, False if the category name isn't recognised."""
    if category not in CATEGORY_RULES:
        return False
    LEARNED[description] = category
    return True


def persist(commit_message: str) -> tuple[bool, str | None]:
    """Try to commit the current LEARNED dict to GitHub.

    Returns (True, None) on success, (False, reason) otherwise where
    reason is a human-readable Hebrew string suitable for showing the
    user.
    """
    try:
        import learning_store
        return learning_store.save(LEARNED, commit_message)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"persist failed: {e}")
        return False, f"שגיאה לא צפויה: {e}"


def persistence_enabled() -> bool:
    try:
        import learning_store
        return learning_store.enabled()
    except Exception:
        return False


def get_learned() -> dict[str, str]:
    return dict(LEARNED)
