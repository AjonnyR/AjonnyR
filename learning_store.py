"""Persist the LEARNED category mappings by committing a JSON file to
GitHub via the REST API.

Activated when both GITHUB_TOKEN and GITHUB_REPO env vars are set:
    GITHUB_TOKEN  - Personal Access Token with 'repo' scope
    GITHUB_REPO   - "<owner>/<repo>", e.g. "AjonnyR/AjonnyR"
    GITHUB_LEARNED_FILE - path in repo (default: "learned.json")
    GITHUB_BRANCH - branch to commit to (default: "main")

Without these, all functions silently degrade to no-ops and the caller
keeps using its in-memory dict only.
"""
import os
import json
import base64
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


def _config() -> tuple[str, str, str, str] | None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return None
    path = os.environ.get("GITHUB_LEARNED_FILE", "learned.json")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    return token, repo, path, branch


class GitHubError(Exception):
    def __init__(self, code: int, body: str):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:200]}")


def _api(method: str, url: str, token: str, body: dict | None = None) -> dict | None:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "expense-bot",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            # File simply doesn't exist yet — not an error for our purposes.
            return None
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        logger.error(f"GitHub API {method} {url} -> {e.code}: {body_text}")
        raise GitHubError(e.code, body_text)


def _friendly(code: int, body: str) -> str:
    if code == 401:
        return "Bad credentials — ה-`GITHUB_TOKEN` שגוי, מוקלד עם רווח מיותר, או פג תוקף."
    if code == 403:
        if "rate limit" in body.lower():
            return "GitHub rate limit. נסה שוב בעוד דקה."
        return "המפתח חסר הרשאת *Contents: Read and write* לריפו הזה."
    if code == 404:
        return "הריפו או הקובץ לא נמצאו. בדוק ש-`GITHUB_REPO` הוא בפורמט `owner/repo` (למשל `AjonnyR/AjonnyR`) ושהמפתח אושר לריפו הזה ספציפית."
    if code == 409 or code == 422:
        return "התנגשות עם שינוי אחר בריפו. נסה שוב."
    return body[:200]


def load() -> dict[str, str]:
    """Fetch learned.json from the repo. Returns {} if nothing's there yet."""
    cfg = _config()
    if not cfg:
        return {}
    token, repo, path, branch = cfg
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        result = _api("GET", url, token)
    except Exception as e:
        logger.warning(f"learning_store.load failed: {e}")
        return {}
    if not result:
        return {}
    try:
        content = base64.b64decode(result["content"]).decode("utf-8")
        data = json.loads(content)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"learning_store.load couldn't parse {path}: {e}")
    return {}


def save(learned: dict[str, str], commit_message: str) -> tuple[bool, str | None]:
    """Commit the full LEARNED dict to learned.json. Triggers a Render
    auto-redeploy. Returns (True, None) on success, (False, reason) on
    failure (reason is a human-readable Hebrew string)."""
    cfg = _config()
    if not cfg:
        missing = []
        if not os.environ.get("GITHUB_TOKEN"):
            missing.append("GITHUB_TOKEN")
        if not os.environ.get("GITHUB_REPO"):
            missing.append("GITHUB_REPO")
        return False, f"שמירה כבויה — חסרים משתני סביבה ב-Render: {', '.join(missing)}"
    token, repo, path, branch = cfg

    sha = None
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
        existing = _api("GET", url, token)
        if existing:
            sha = existing.get("sha")
    except GitHubError as e:
        # 404 already returns None above, so any GitHubError here is real.
        return False, f"GET נכשל ({e.code}): {_friendly(e.code, e.body)}"
    except Exception as e:
        return False, f"בעיית רשת ב-GET: {e}"

    encoded = base64.b64encode(
        json.dumps(learned, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    body: dict = {"message": commit_message, "content": encoded, "branch": branch}
    if sha:
        body["sha"] = sha

    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        _api("PUT", url, token, body=body)
        logger.info(f"learning_store: committed {len(learned)} entries to {repo}:{path}")
        return True, None
    except GitHubError as e:
        return False, f"PUT נכשל ({e.code}): {_friendly(e.code, e.body)}"
    except Exception as e:
        return False, f"בעיית רשת ב-PUT: {e}"


def diagnose() -> str:
    """Return a multi-line Hebrew diagnostic of the GitHub setup."""
    cfg = _config()
    if not cfg:
        token_set = bool(os.environ.get("GITHUB_TOKEN"))
        repo_set = bool(os.environ.get("GITHUB_REPO"))
        return (
            f"❌ שמירה ל-GitHub כבויה.\n"
            f"GITHUB_TOKEN: {'✓ מוגדר' if token_set else '✗ חסר'}\n"
            f"GITHUB_REPO: {'✓ מוגדר' if repo_set else '✗ חסר'}\n\n"
            f"הוסף את שניהם ב-Render → Environment."
        )
    token, repo, path, branch = cfg
    lines = [
        f"בודק חיבור ל-GitHub...",
        f"repo: `{repo}`  branch: `{branch}`  file: `{path}`",
        f"token: `{token[:10]}…{token[-4:]}`",
    ]
    try:
        url = f"https://api.github.com/repos/{repo}"
        result = _api("GET", url, token)
        if result is None:
            return "\n".join(lines + ["❌ הריפו לא נמצא. בדוק את `GITHUB_REPO`."])
        lines.append(f"✓ ריפו נמצא ({result.get('full_name')}, default branch `{result.get('default_branch')}`)")
    except GitHubError as e:
        return "\n".join(lines + [f"❌ GET /repos נכשל ({e.code}): {_friendly(e.code, e.body)}"])
    except Exception as e:
        return "\n".join(lines + [f"❌ בעיית רשת: {e}"])

    # Try a real test by reading the learned file (or proving access).
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
        existing = _api("GET", url, token)
        if existing is None:
            lines.append(f"ℹ️ {path} עוד לא קיים — ייווצר בפעם הראשונה שתשמור.")
        else:
            lines.append(f"✓ {path} קיים ב-`{branch}`")
    except GitHubError as e:
        return "\n".join(lines + [f"❌ GET contents נכשל ({e.code}): {_friendly(e.code, e.body)}"])

    lines.append("✅ הגדרה תקינה — /correct ישמור לריפו.")
    return "\n".join(lines)


def enabled() -> bool:
    return _config() is not None
