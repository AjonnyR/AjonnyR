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
        if e.code == 404:
            return None
        body_text = e.read().decode("utf-8", errors="replace")[:300]
        logger.error(f"GitHub API {method} {url} -> {e.code}: {body_text}")
        raise


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


def save(learned: dict[str, str], commit_message: str) -> bool:
    """Commit the full LEARNED dict to learned.json. Triggers a Render
    auto-redeploy. Returns True on success, False if disabled or failed."""
    cfg = _config()
    if not cfg:
        return False
    token, repo, path, branch = cfg

    # Get the current SHA so GitHub accepts the update.
    sha = None
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
        existing = _api("GET", url, token)
        if existing:
            sha = existing.get("sha")
    except Exception as e:
        logger.warning(f"learning_store.save: couldn't fetch existing SHA: {e}")
        # Continue — PUT without SHA will work if the file doesn't exist.

    encoded = base64.b64encode(
        json.dumps(learned, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    body: dict = {
        "message": commit_message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    try:
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        _api("PUT", url, token, body=body)
        logger.info(f"learning_store: committed {len(learned)} entries to {repo}:{path}")
        return True
    except Exception as e:
        logger.error(f"learning_store.save failed: {e}")
        return False


def enabled() -> bool:
    return _config() is not None
