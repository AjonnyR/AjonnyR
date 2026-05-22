"""User-editable categorisation rules used when Gemini AI is unavailable.

How to customise:
    Edit the lists below. Add merchant names / keywords under the category
    you want them to land in. Matching is case-insensitive on the post-RTL
    fixed description (so use normal Hebrew reading order).
    Then push to GitHub — Render will redeploy automatically.

Order matters: the FIRST category whose keyword appears in the description
wins. Put more specific categories above more generic ones.
"""

import os

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

# User-created categories that aren't in the static CATEGORY_RULES above.
# Persisted in the same learned.json file under the reserved key
# `__categories__` so creating a category doesn't cost a second commit.
CUSTOM_CATEGORIES: list[str] = []

# Reserved key inside learned.json — must not collide with merchant names.
_CATEGORIES_KEY = "__categories__"


def _load_from_store() -> None:
    """Best-effort load from GitHub on import. Failures don't crash the bot."""
    try:
        import learning_store
        loaded = learning_store.load()
        if not loaded:
            return
        # Split: list under __categories__ = user-created category names;
        # string values = merchant → category mappings.
        custom = loaded.get(_CATEGORIES_KEY, [])
        if isinstance(custom, list):
            for name in custom:
                if isinstance(name, str) and name and name not in CATEGORY_RULES:
                    CATEGORY_RULES[name] = []
                    CUSTOM_CATEGORIES.append(name)
        for k, v in loaded.items():
            if k == _CATEGORIES_KEY:
                continue
            if isinstance(v, str):
                LEARNED[str(k)] = v
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Couldn't load LEARNED: {e}")


_load_from_store()


def _build_save_dict() -> dict:
    """Construct the dict that goes into learned.json: all learned
    mappings PLUS the custom-categories list under the reserved key."""
    out: dict = dict(LEARNED)
    if CUSTOM_CATEGORIES:
        out[_CATEGORIES_KEY] = sorted(set(CUSTOM_CATEGORIES))
    return out


def create_category(name: str) -> tuple[bool, str | None]:
    """Add a new user-defined category. Returns (True, None) on success,
    (False, reason) if the name is invalid or already exists. The change
    must still be persisted via schedule_persist()."""
    name = name.strip()
    if not name:
        return False, "שם הקטגוריה ריק"
    if len(name) > 30:
        return False, "שם ארוך מדי (מקסימום 30 תווים)"
    if name == _CATEGORIES_KEY or name.startswith("__"):
        return False, "השם הזה שמור — בחר אחר"
    if name in CATEGORY_RULES:
        return False, "הקטגוריה כבר קיימת"
    CATEGORY_RULES[name] = []
    if name not in CUSTOM_CATEGORIES:
        CUSTOM_CATEGORIES.append(name)
    return True, None


def list_all_with_contents() -> list[tuple[str, list[str], list[str], bool]]:
    """Return [(category, static_keywords, learned_merchants, is_custom)]
    for every known category, sorted with custom categories last so they
    stand out."""
    rows: list[tuple[str, list[str], list[str], bool]] = []
    for cat in CATEGORY_RULES:
        keywords = list(CATEGORY_RULES.get(cat, []))
        merchants = sorted([d for d, c in LEARNED.items() if c == cat])
        is_custom = cat in CUSTOM_CATEGORIES
        rows.append((cat, keywords, merchants, is_custom))
    # Static categories first (in declaration order), then custom ones.
    rows.sort(key=lambda r: (r[3], 0))
    return rows


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
    """Immediate (non-debounced) commit. Reserved for /flush and shutdown
    — application code should call schedule_persist() instead so commits
    are batched and the user isn't subjected to a Render redeploy on
    every single category change."""
    try:
        import learning_store
        return learning_store.save(_build_save_dict(), commit_message)
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


# ---------------------------------------------------------------------------
# Debounced GitHub commit
# ---------------------------------------------------------------------------
# Committing to GitHub triggers a Render redeploy (~2-3 min of downtime).
# Batch changes so a burst of /correct + button taps produces ONE commit
# instead of one per change. The flush fires PERSIST_DEBOUNCE_SECONDS
# after the last activity that touched the timer.

import asyncio as _asyncio

PERSIST_DEBOUNCE_SECONDS = int(os.environ.get("PERSIST_DEBOUNCE_SECONDS", "600"))

_pending_messages: list[str] = []
_pending_task: "_asyncio.Task | None" = None


def has_pending() -> bool:
    return len(_pending_messages) > 0


def pending_count() -> int:
    return len(_pending_messages)


async def schedule_persist(commit_message: str) -> None:
    """Queue a change for later commit. The actual GitHub PUT happens
    PERSIST_DEBOUNCE_SECONDS after the last call to schedule_persist or
    reset_flush_timer — so several rapid changes coalesce into one
    commit and one Render redeploy."""
    _pending_messages.append(commit_message)
    _reschedule()


async def reset_flush_timer() -> None:
    """Push the flush further out without adding a new change. Called
    from every user-facing handler so the rule becomes 'flush 10 min
    after the last user interaction' rather than 'after the last
    change'."""
    if _pending_messages:
        _reschedule()


def _reschedule() -> None:
    global _pending_task
    if _pending_task and not _pending_task.done():
        _pending_task.cancel()
    try:
        _pending_task = _asyncio.create_task(_flush_after_delay())
    except RuntimeError:
        # No running event loop (e.g. called from a sync test) — give up
        # silently; the next async call site will reschedule.
        _pending_task = None


async def _flush_after_delay() -> None:
    try:
        await _asyncio.sleep(PERSIST_DEBOUNCE_SECONDS)
    except _asyncio.CancelledError:
        return
    await _do_flush()


async def _do_flush() -> tuple[bool, str | None]:
    """Drain pending_messages and commit. Safe to call from outside the
    debounce path (e.g. /flush command)."""
    global _pending_messages, _pending_task
    if not _pending_messages:
        return True, None
    n = len(_pending_messages)
    first = _pending_messages[0]
    if n == 1:
        msg = first
    else:
        msg = f"Batch: {n} changes ({first}, +{n - 1} more)"
    msgs_being_flushed = list(_pending_messages)
    _pending_messages = []
    _pending_task = None
    loop = _asyncio.get_event_loop()
    try:
        ok, err = await loop.run_in_executor(None, persist, msg)
    except Exception as e:
        ok, err = False, str(e)
    if not ok:
        # Restore so a later flush can retry.
        _pending_messages[:0] = msgs_being_flushed
        import logging
        logging.getLogger(__name__).warning(f"Batched persist failed: {err}")
    return ok, err


async def flush_now() -> tuple[bool, str | None]:
    """User-triggered immediate flush (/flush command)."""
    global _pending_task
    if _pending_task and not _pending_task.done():
        _pending_task.cancel()
        _pending_task = None
    return await _do_flush()
