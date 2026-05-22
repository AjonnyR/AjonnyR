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
    # Three user-defined base categories. Keyword lists are intentionally
    # empty — every merchant is categorised by Gemini (or by user
    # /correct / button taps) on first sight, then cached in LEARNED.
    # Add more categories at runtime with /newcategory.
    "אוכל": [],
    "קטנוע": [],
    "אלי ואמז": [],
    # System fallback: where the AI lands a merchant it can't slot
    # confidently, and where /deletecategory moves orphan merchants.
    # Always present.
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

# Per-merchant totals from the most recent /analyze run. Used by
# /categories to show "how much" per merchant and by the button-tap /
# /correct confirmations to say "N transactions moved". Replaced
# wholesale on each analyze — reflects the last upload's data.
MERCHANT_SNAPSHOT: dict[str, dict] = {}

# Reserved keys inside learned.json — must not collide with merchant names.
_CATEGORIES_KEY = "__categories__"
_SNAPSHOT_KEY = "__merchant_snapshot__"


def _load_from_store() -> None:
    """Best-effort load from GitHub on import. Failures don't crash the bot."""
    try:
        import learning_store
        loaded = learning_store.load()
        if not loaded:
            return
        # Split: list under __categories__ = user-created category names;
        # dict under __merchant_snapshot__ = per-merchant {count, total};
        # string values = merchant → category mappings.
        custom = loaded.get(_CATEGORIES_KEY, [])
        if isinstance(custom, list):
            for name in custom:
                if isinstance(name, str) and name and name not in CATEGORY_RULES:
                    CATEGORY_RULES[name] = []
                    CUSTOM_CATEGORIES.append(name)
        snapshot = loaded.get(_SNAPSHOT_KEY, {})
        if isinstance(snapshot, dict):
            for k, v in snapshot.items():
                if not isinstance(v, dict):
                    continue
                try:
                    MERCHANT_SNAPSHOT[str(k)] = {
                        "count": int(v.get("count", 0)),
                        "total": float(v.get("total", 0.0)),
                    }
                except (TypeError, ValueError):
                    continue
        for k, v in loaded.items():
            if k in (_CATEGORIES_KEY, _SNAPSHOT_KEY):
                continue
            if isinstance(v, str):
                LEARNED[str(k)] = v
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Couldn't load LEARNED: {e}")


_load_from_store()


def _build_save_dict() -> dict:
    """Construct the dict that goes into learned.json: all learned
    mappings PLUS the custom-categories list and the merchant-snapshot
    under reserved keys."""
    out: dict = dict(LEARNED)
    if CUSTOM_CATEGORIES:
        out[_CATEGORIES_KEY] = sorted(set(CUSTOM_CATEGORIES))
    if MERCHANT_SNAPSHOT:
        out[_SNAPSHOT_KEY] = MERCHANT_SNAPSHOT
    return out


def update_merchant_snapshot(transactions: list[dict]) -> None:
    """Recompute per-merchant {count, total} from the latest analysis.
    Replaces any previous snapshot — represents the most recent upload
    only (matches the user's monthly review workflow)."""
    MERCHANT_SNAPSHOT.clear()
    for t in transactions:
        desc = t.get("description")
        if not desc:
            continue
        amount = float(t.get("amount", 0.0))
        info = MERCHANT_SNAPSHOT.setdefault(str(desc), {"count": 0, "total": 0.0})
        info["count"] += 1
        info["total"] += amount


def get_merchant_info(desc: str) -> tuple[int, float]:
    """Return (count, total) for a description from the latest snapshot,
    or (0, 0.0) if the merchant wasn't in the most recent analysis."""
    info = MERCHANT_SNAPSHOT.get(desc, {})
    return int(info.get("count", 0)), float(info.get("total", 0.0))


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


def rename_category(old: str, new: str) -> tuple[bool, str | None, int]:
    """Rename a USER-CREATED category. Built-in categories defined in code
    can't be renamed at runtime — they'd come back on the next deploy.

    Side effects: every merchant in LEARNED that pointed to the old name
    is updated to the new name. Position in CATEGORY_RULES is preserved so
    callback indices in already-sent buttons stay valid.

    Returns (ok, error, num_merchants_updated)."""
    old = old.strip()
    new = new.strip()
    if not old or not new:
        return False, "שם ריק", 0
    if old == new:
        return False, "השם החדש זהה לישן", 0
    if len(new) > 30:
        return False, "השם החדש ארוך מדי (מקסימום 30 תווים)", 0
    if new.startswith("__") or new == _CATEGORIES_KEY:
        return False, "השם הזה שמור — בחר אחר", 0
    if old not in CATEGORY_RULES:
        return False, f'הקטגוריה "{old}" לא קיימת', 0
    if old not in CUSTOM_CATEGORIES:
        return False, (
            f'הקטגוריה "{old}" מוגדרת בקוד ולא ניתנת לשינוי בזמן ריצה. '
            "ערוך את categories.py ו-push."
        ), 0
    if new in CATEGORY_RULES:
        return False, f'כבר קיימת קטגוריה בשם "{new}"', 0

    # Preserve dict order by rebuilding — that keeps callback-data indices
    # stable for any pending inline-button messages.
    keywords = CATEGORY_RULES[old]
    rebuilt: dict[str, list[str]] = {}
    for k, v in CATEGORY_RULES.items():
        if k == old:
            rebuilt[new] = keywords
        else:
            rebuilt[k] = v
    CATEGORY_RULES.clear()
    CATEGORY_RULES.update(rebuilt)

    CUSTOM_CATEGORIES.remove(old)
    if new not in CUSTOM_CATEGORIES:
        CUSTOM_CATEGORIES.append(new)

    moved = 0
    for desc, cat in list(LEARNED.items()):
        if cat == old:
            LEARNED[desc] = new
            moved += 1
    return True, None, moved


def delete_category(name: str, move_to: str = "אחר") -> tuple[bool, str | None, int]:
    """Delete a USER-CREATED category. Merchants previously tagged to it
    are moved to `move_to` (default: 'אחר').

    Note: deleting shifts the index of every category that follows it,
    so callback-data on already-sent inline buttons may point to the
    wrong category. The user can fix with /correct.

    Returns (ok, error, num_merchants_moved)."""
    name = name.strip()
    if not name:
        return False, "שם ריק", 0
    if name not in CATEGORY_RULES:
        return False, f'הקטגוריה "{name}" לא קיימת', 0
    if name not in CUSTOM_CATEGORIES:
        return False, (
            f'הקטגוריה "{name}" מוגדרת בקוד ולא ניתנת למחיקה בזמן ריצה. '
            "ערוך את categories.py ו-push."
        ), 0
    if move_to not in CATEGORY_RULES:
        return False, f'קטגוריית היעד "{move_to}" לא קיימת', 0
    if move_to == name:
        return False, "אי אפשר להעביר את המסחרים לקטגוריה שנמחקת", 0

    moved = 0
    for desc, cat in list(LEARNED.items()):
        if cat == name:
            LEARNED[desc] = move_to
            moved += 1
    del CATEGORY_RULES[name]
    CUSTOM_CATEGORIES.remove(name)
    return True, None, moved


def list_all_with_contents() -> list[tuple[str, list[str], list[tuple[str, int, float]], bool]]:
    """Return [(category, static_keywords, merchants, is_custom)] for every
    known category. Each `merchants` entry is (name, count, total) where
    count/total come from the latest analysis snapshot (0/0.0 for merchants
    learned previously but not in the most recent upload). Merchants are
    sorted by total descending. Custom categories appear last."""
    rows: list[tuple[str, list[str], list[tuple[str, int, float]], bool]] = []
    for cat in CATEGORY_RULES:
        keywords = list(CATEGORY_RULES.get(cat, []))
        names = [d for d, c in LEARNED.items() if c == cat]
        merchants: list[tuple[str, int, float]] = []
        for name in names:
            count, total = get_merchant_info(name)
            merchants.append((name, count, total))
        # Biggest spend first; merchants without snapshot data (count == 0)
        # fall to the bottom, alphabetised among themselves.
        merchants.sort(key=lambda m: (-m[2], m[0]))
        is_custom = cat in CUSTOM_CATEGORIES
        rows.append((cat, keywords, merchants, is_custom))
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
