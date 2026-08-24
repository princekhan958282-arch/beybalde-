"""
utils/github_backup.py — push a snapshot to a second, off-host repo.

`backups/` (utils/snapshot.py) is the primary backup and works with zero
configuration. This module is a SECOND, best-effort copy of the same bytes,
pushed to a private GitHub repo, for the one failure `backups/` alone cannot
survive: the whole container — install, disk, and `backups/` with it — being
lost at once.

Deliberately separate from `utils/updater.py`
-----------------------------------------------
The updater's `GITHUB_TOKEN` is documented read-only and points at the BOT'S
OWN repo. This module pushes real player data to a DIFFERENT repo, so it
reads its own pair of secrets:

    GITHUB_BACKUP_TOKEN   fine-grained PAT, Contents: Read and write,
                           scoped to ONLY the backup repo
    GITHUB_BACKUP_REPO    "owner/repo" — e.g. princekhan958282-arch/Beycord-Backup-

A leak of one token never grants the other. Both are read through
`utils.secrets.get()` — env -> .env -> config_local.py, same as every other
secret in this codebase — and NEVER from `data/config.json` or the database:
a credential that could enter a *snapshot* is the one loop this cannot allow.
The token is never logged, never returned in a result dict, never put in an
exception string — only whether it is set and how long it is, exactly like
`updater.py`'s own convention.

HTTP client mirrors utils/updater.py on purpose rather than adding a
dependency: raw `urllib.request`, the same opener that drops `Authorization`
on a cross-host redirect, the same header set.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("beyblade_bot.github_backup")

HTTP_TIMEOUT = 30
_API = "https://api.github.com"


def _cfg(name: str) -> str:
    try:
        from . import secrets as _secrets
        return (_secrets.get(name) or "").strip()
    except Exception:                                # noqa: BLE001
        return ""


def configured() -> tuple[str, str]:
    """(token, repo) if both secrets are set, else ("", "")."""
    token = _cfg("GITHUB_BACKUP_TOKEN")
    repo = _cfg("GITHUB_BACKUP_REPO")
    if not token or not repo:
        return "", ""
    return token, repo


# ── github ───────────────────────────────────────────────────────────────────

class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Same reasoning as utils/updater.py's handler of the same name: strip
    the bearer token before it can follow a redirect to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for key in list(new.headers):
                if key.lower() == "authorization":
                    del new.headers[key]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _request(url: str, token: str, method: str = "GET",
             payload: Optional[dict] = None) -> bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "beycord-backup",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    with _opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def _classify(exc: urllib.error.HTTPError, repo: str) -> str:
    code = exc.code
    if code == 401:
        return ("token rejected (401) — most likely EXPIRED. Generate a new "
                 "fine-grained PAT and update GITHUB_BACKUP_TOKEN.")
    if code == 403:
        return (f"token refused (403) — it can reach GitHub but not write to "
                f"{repo}. Check it has Contents: Read and write, and lists "
                f"{repo} under 'Only select repositories'.")
    if code == 404:
        return (f"{repo} not found. Either GITHUB_BACKUP_REPO is wrong, or "
                f"the token cannot see it — a private repo 404s for a token "
                f"that lacks access, it does not 403.")
    return f"GitHub returned HTTP {code}"


def _sha_of(repo: str, path: str, token: str) -> Optional[str]:
    """The current file's blob sha, or None if it doesn't exist yet."""
    url = f"{_API}/repos/{repo}/contents/{path}"
    try:
        raw = _request(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return json.loads(raw.decode("utf-8")).get("sha")


def _put_file(repo: str, path: str, content: bytes, token: str, message: str) -> dict:
    """Upsert one file via the Contents API. Retries once on a stale sha."""
    url = f"{_API}/repos/{repo}/contents/{path}"
    b64 = base64.b64encode(content).decode("ascii")

    for attempt in range(2):
        sha = _sha_of(repo, path, token)
        payload = {"message": message, "content": b64}
        if sha:
            payload["sha"] = sha
        try:
            raw = _request(url, token, method="PUT", payload=payload)
            data = json.loads(raw.decode("utf-8"))
            return {"ok": True,
                    "url": data.get("content", {}).get("html_url", "")}
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and attempt == 0:
                # Someone else (or our own retry after a dropped response)
                # moved the file between our GET and our PUT — refetch and
                # try exactly once more rather than looping forever.
                continue
            return {"ok": False, "error": _classify(exc, repo)}
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False,
                    "error": f"couldn't reach GitHub: {type(exc).__name__}"}
    return {"ok": False, "error": "stale sha persisted after a retry"}


def push_snapshot(gz_bytes: bytes, day_stamp: str) -> dict:
    """Push one snapshot to `daily/<day_stamp>.json.gz` and `latest.json.gz`.

    Never raises — every failure comes back as `{"ok": False, "error": ...}`
    so a GitHub outage can never take the local backup path down with it.
    """
    token, repo = configured()
    if not token or not repo:
        return {"ok": False, "error": "not configured"}

    # Defense in depth: `SN.collect()` already guarantees no token-shaped
    # string rides along, but this is the one path that actually leaves the
    # host, so it re-checks immediately before the network call rather than
    # trusting a guarantee made earlier and elsewhere.
    try:
        from utils import snapshot as SN
        hits = SN.audit_secrets(json.loads(gzip.decompress(gz_bytes)))
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": f"pre-push secrets audit failed: {exc}"}
    if hits:
        log.error("[github_backup] refusing to push — audit found %d hit(s)", len(hits))
        return {"ok": False, "error": "secrets audit found a possible token — refused to push"}

    daily = _put_file(repo, f"daily/{day_stamp}.json.gz", gz_bytes, token,
                       f"backup {day_stamp}")
    if not daily["ok"]:
        return daily

    latest = _put_file(repo, "latest.json.gz", gz_bytes, token,
                        f"latest -> {day_stamp}")
    if not latest["ok"]:
        return {"ok": False,
                 "error": f"daily archive pushed, but latest.json.gz failed: {latest['error']}"}

    return {"ok": True, "repo": repo, "day": day_stamp, "url": daily.get("url", "")}
