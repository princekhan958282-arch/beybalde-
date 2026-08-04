"""
updater.py — pull the latest code from GitHub at boot.

What it does
------------
Runs once, early in app.py, before any cog is imported:

  1. asks GitHub for the head commit of the configured branch
  2. compares it against the commit recorded from the last successful update
  3. if they differ, downloads that commit's zipball and unpacks it into a
     temporary directory
  4. sanity-checks the unpacked tree, backs up every file it is about to
     replace, then copies the new files over the install
  5. logs what it did and RETURNS — it does not restart the bot

The bot therefore keeps running the code it booted with. The update goes live
on the NEXT restart, which is the whole point: an updater that relaunches the
process mid-boot can put a host into a restart loop if the new code is broken,
and there is no safe way to recover that remotely.

Why not `git pull`
------------------
This bot is deployed by extracting a zip into a panel's file manager (see
INSTALL.md), so the install directory is usually not a git checkout at all —
there is no `.git` to pull into and often no `git` binary. Fetching the zipball
over the API works on any host, which is the same reason bootstrap.py shells
out to pip rather than assuming a build toolchain.

What it will never overwrite
----------------------------
`data/` is committed to the repository, and several of those files are LIVE
stores that the bot reads and writes at runtime — `avatar_inventory.json` holds
who owns which avatar, `casino_wallets.json` holds balances, `config.json` holds
per-guild spawn channels, `spawn_state.json` holds active spawns. Copying the
repository's snapshot over them would silently roll every player back to
whenever that snapshot was committed.

So PROTECTED below is not a tidiness list, it is the difference between an
update and data loss. Secrets are in it for the same reason: `.env` and
`config_local.py` hold the bot token.

Configuration (utils/secrets.py — env var, .env, or config_local.py)
-------------------------------------------------------------------
    GITHUB_TOKEN    required for a private repo; fine-grained PAT, read-only
                    Contents. Without it the updater logs and does nothing.
    GITHUB_REPO     defaults to the repo this code ships from
    GITHUB_BRANCH   defaults to "main"

Turn the whole thing off with BEYCORD_AUTO_UPDATE=0.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Optional

log = logging.getLogger("beyblade_bot.update")


def _ensure_console() -> None:
    """Make our messages visible on the console.

    This runs BEFORE app.py's logging.basicConfig, so without a handler of our
    own every line here would go to the root logger's default and, on some
    hosts, vanish entirely — which for an updater is the worst possible failure
    mode: it did something and you can't see what. Same approach bootstrap.py
    uses, including turning propagation off so these lines don't double up once
    the real logging config lands.
    """
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  [%(levelname)s]  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_PATH = os.path.join(_ROOT, ".update_state.json")
_BACKUP_DIR = os.path.join(_ROOT, ".update_backup")

DEFAULT_REPO = "princekhan958282-arch/beybalde-"
DEFAULT_BRANCH = "main"

_OPT_OUT = "BEYCORD_AUTO_UPDATE"
HTTP_TIMEOUT = 30
MAX_ZIP_BYTES = 80 * 1024 * 1024        # a runaway download is a failure, not an update

# Never replaced by an update, at any path depth. See the module docstring —
# `data/` entries are live stores, the rest are secrets and local junk.
PROTECTED = (
    ".env",
    "config_local.py",
    "data/",                 # live player stores — overwriting these loses data
    "__pycache__/",
    ".update_state.json",
    ".update_backup/",
    ".git/",
)

# Only these extensions are copied. A code update has no business writing
# anything else into the install, and this keeps a compromised or malformed
# archive from dropping executables next to app.py.
ALLOWED_SUFFIXES = (".py", ".txt", ".md", ".json", ".ttf", ".png", ".example")


# ── state ────────────────────────────────────────────────────────────────────

def _read_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                # noqa: BLE001
        return {}


def _write_state(state: dict) -> None:
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:                         # noqa: BLE001
        log.warning("[update] couldn't record state: %s", exc)


def _record_attempt(outcome: str, detail: str = "") -> None:
    """Remember how the last check went, so ;version can report it.

    The console is the natural place for this, but on a hosting panel it is
    often the one thing you cannot get at — and "the updater isn't working"
    with no visible reason is the worst possible failure. Merging the outcome
    into the state file puts it somewhere a Discord command can read it.
    """
    state = _read_state()
    state["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_outcome"] = outcome
    state["last_detail"] = detail[:300]
    _write_state(state)


def _cfg(name: str, default: str = "") -> str:
    """Config via utils.secrets, so GITHUB_* resolve exactly like BOT_TOKEN."""
    try:
        from . import secrets as _secrets
        return (_secrets.get(name) or default).strip()
    except Exception:                                # noqa: BLE001
        return (os.environ.get(name) or default).strip()


def normalise_repo(value: str) -> str:
    """Accept whatever form of 'the repo' someone pastes, return `owner/repo`.

    The API wants `owner/repo`, but nobody has that on their clipboard — what
    you copy off GitHub is a URL, and what a panel asks for is usually the
    clone link. Pasting either used to produce a 404 that read like a
    permissions problem, so all the usual shapes are accepted:

        princekhan958282-arch/beybalde-
        https://github.com/princekhan958282-arch/beybalde-
        https://github.com/princekhan958282-arch/beybalde-.git
        git@github.com:princekhan958282-arch/beybalde-.git
        github.com/princekhan958282-arch/beybalde-/tree/main
    """
    v = (value or "").strip().strip("<>").rstrip("/")
    if not v:
        return ""
    if v.startswith("git@"):                       # scp-style clone URL
        v = v.split(":", 1)[-1]
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
            break
    if v.startswith("www."):
        v = v[4:]
    if v.startswith("github.com/"):
        v = v[len("github.com/"):]
    if v.endswith(".git"):
        v = v[:-4]
    parts = [p for p in v.split("/") if p]
    if len(parts) < 2:
        return v                                   # let the caller 404 on it
    # Trim anything after owner/repo — /tree/main, /blob/..., ?query, #frag
    owner, repo = parts[0], parts[1]
    repo = repo.split("?")[0].split("#")[0]
    return f"{owner}/{repo}"


# ── github ───────────────────────────────────────────────────────────────────

class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Strip Authorization when a redirect crosses to another host.

    The zipball endpoint answers with a 302 to codeload.github.com carrying a
    pre-signed URL. urllib copies every header onto the redirected request, so
    the Bearer token follows it to a host that never asked for one — and
    codeload can answer 400 for a credential it did not expect. The download
    then fails for a reason that looks nothing like its cause.

    Sending a token to a host you were merely redirected to is also just wrong,
    independently of whether that particular host tolerates it.
    """

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


def _request(url: str, token: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "beycord-updater",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with _opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read(MAX_ZIP_BYTES + 1)


def head_commit(repo: str, branch: str, token: str) -> Optional[dict]:
    """(sha, message, date) for the branch head, or None if it can't be read."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        raw = _request(url, token, "application/vnd.github+json")
        data = json.loads(raw.decode("utf-8"))
        return {
            "sha": data.get("sha", ""),
            "message": (data.get("commit", {}).get("message", "") or "").split("\n")[0],
            "date": data.get("commit", {}).get("committer", {}).get("date", ""),
        }
    except urllib.error.HTTPError as exc:
        # The reason is recorded as well as logged, so ;version can name it on
        # a host whose console you can't read.
        if exc.code == 401:
            detail = ("token rejected (401) — most likely EXPIRED. "
                      "Fine-grained tokens default to 30 days; generate a new "
                      "one and update GITHUB_TOKEN.")
        elif exc.code == 403:
            detail = ("token refused (403) — it can reach GitHub but not this "
                      "repo. Check it has Contents: Read-only and lists "
                      f"{repo} under 'Only select repositories'.")
        elif exc.code == 404:
            detail = (f"{repo}@{branch} not found — check GITHUB_REPO and "
                      f"GITHUB_BRANCH, or the token can't see this repo.")
        else:
            detail = f"GitHub returned HTTP {exc.code}"
        log.error("[update] %s", detail)
        _record_attempt("failed", detail)
    except Exception as exc:                         # noqa: BLE001
        detail = f"couldn't reach GitHub: {type(exc).__name__}: {exc}"
        log.error("[update] %s", detail)
        _record_attempt("failed", detail)
    return None


def _download_zip(repo: str, sha: str, token: str) -> Optional[zipfile.ZipFile]:
    url = f"https://api.github.com/repos/{repo}/zipball/{sha}"
    try:
        raw = _request(url, token, "application/vnd.github+json")
    except Exception as exc:                         # noqa: BLE001
        log.error("[update] download failed: %s", exc)
        return None
    if len(raw) > MAX_ZIP_BYTES:
        log.error("[update] archive larger than %d MB — refusing it.",
                  MAX_ZIP_BYTES // (1024 * 1024))
        return None
    try:
        return zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:                         # noqa: BLE001
        log.error("[update] archive is not a readable zip: %s", exc)
        return None


# ── applying ─────────────────────────────────────────────────────────────────

def _is_protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    for p in PROTECTED:
        if p.endswith("/"):
            if rel == p.rstrip("/") or rel.startswith(p):
                return True
        elif rel == p:
            return True
    return False


def _members(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(archive_name, install_relative_path)] for everything safe to copy.

    A GitHub zipball nests everything under one `owner-repo-sha/` directory,
    which is stripped here. Absolute paths and `..` are dropped outright: a
    zip is attacker-controllable input, and path traversal is how an archive
    writes outside the directory you unpacked it into.
    """
    out: list[tuple[str, str]] = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        parts = name.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        rel = parts[1]
        if os.path.isabs(rel) or ".." in rel.split("/"):
            log.warning("[update] skipping suspicious path in archive: %s", name)
            continue
        if _is_protected(rel):
            continue
        if not rel.endswith(ALLOWED_SUFFIXES):
            continue
        out.append((name, rel))
    return out


def _apply(zf: zipfile.ZipFile, sha: str) -> tuple[int, int]:
    """Copy changed files in. Returns (written, skipped_identical)."""
    members = _members(zf)
    if not any(rel == "app.py" for _n, rel in members):
        # The archive should always contain the entry point. If it doesn't,
        # something is wrong with the download and copying it over a working
        # install would be worse than doing nothing.
        raise RuntimeError("archive has no app.py — refusing to apply it")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(_BACKUP_DIR, stamp)
    written = skipped = 0

    for name, rel in members:
        new = zf.read(name)
        dest = os.path.join(_ROOT, rel)

        if os.path.exists(dest):
            try:
                with open(dest, "rb") as f:
                    if f.read() == new:
                        skipped += 1
                        continue
            except Exception:                        # noqa: BLE001
                pass
            # Back up before clobbering, so a bad update can be undone by
            # copying .update_backup/<stamp>/ back over the install.
            bpath = os.path.join(backup_root, rel)
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            try:
                shutil.copy2(dest, bpath)
            except Exception as exc:                 # noqa: BLE001
                log.debug("[update] couldn't back up %s: %s", rel, exc)

        os.makedirs(os.path.dirname(dest) or _ROOT, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(new)
        written += 1

    return written, skipped


def _prune_backups(keep: int = 5) -> None:
    try:
        stamps = sorted(os.listdir(_BACKUP_DIR))
        for old in stamps[:-keep]:
            shutil.rmtree(os.path.join(_BACKUP_DIR, old), ignore_errors=True)
    except Exception:                                # noqa: BLE001
        pass


# ── entry point ──────────────────────────────────────────────────────────────

def check_and_apply() -> None:
    """Update the install from GitHub. Never raises, never restarts.

    Called from app.py before cogs load. Every failure path logs and returns so
    the bot always boots — an updater that can take the bot down is worse than
    no updater.
    """
    _ensure_console()

    if os.environ.get(_OPT_OUT, "").strip() in ("0", "false", "no"):
        log.info("[update] disabled (%s=0)", _OPT_OUT)
        _record_attempt("disabled", f"{_OPT_OUT}=0")
        return

    repo = normalise_repo(_cfg("GITHUB_REPO", DEFAULT_REPO)) or DEFAULT_REPO
    branch = _cfg("GITHUB_BRANCH", DEFAULT_BRANCH)
    token = _cfg("GITHUB_TOKEN")

    if not token:
        log.info("[update] no GITHUB_TOKEN set — skipping. Add one to "
                 "config_local.py or .env to auto-update from %s.", repo)
        _record_attempt("no token", "set GITHUB_TOKEN in config_local.py or .env")
        return

    log.info("[update] checking %s@%s …", repo, branch)
    head = head_commit(repo, branch, token)
    if not head or not head.get("sha"):
        log.warning("[update] could not read the branch head — keeping current files.")
        # head_commit() already recorded WHY on every failure path it has, and
        # those reasons are far more useful than a generic one — don't overwrite
        # them. Only the "answered, but with no sha" case reaches here unrecorded.
        if head is not None:
            _record_attempt("failed",
                            f"{repo}@{branch} returned no commit sha")
        return

    state = _read_state()
    current = state.get("sha", "")
    if current == head["sha"]:
        log.info("[update] already up to date (%s).", head["sha"][:7])
        _record_attempt("up to date", f"{head['sha'][:7]} on {branch}")
        return

    log.info("[update] new commit %s — %s", head["sha"][:7], head["message"][:72])

    zf = _download_zip(repo, head["sha"], token)
    if zf is None:
        log.warning("[update] download failed — keeping current files.")
        _record_attempt("failed", "download failed — see console")
        return

    try:
        written, skipped = _apply(zf, head["sha"])
    except Exception as exc:                         # noqa: BLE001
        log.error("[update] apply failed, install left untouched: %s", exc)
        _record_attempt("failed", f"apply failed: {exc}")
        return
    finally:
        try:
            zf.close()
        except Exception:                            # noqa: BLE001
            pass

    _write_state({
        "sha": head["sha"],
        "message": head["message"],
        "date": head["date"],
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo,
        "branch": branch,
        "last_check": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_outcome": "updated — restart pending",
        "last_detail": f"{written} file(s) written",
    })
    _prune_backups()

    if written == 0:
        log.info("[update] nothing to write — files already matched %s.",
                 head["sha"][:7])
        return

    # Loud on purpose: the files on disk no longer match the code in memory,
    # and nothing will reconcile that until someone restarts the bot.
    log.warning("=" * 66)
    log.warning("[update] %d file(s) updated to %s (%d unchanged).",
                written, head["sha"][:7], skipped)
    log.warning("[update] RESTART THE BOT to run the new code — the process is")
    log.warning("[update] still running the version it booted with.")
    log.warning("[update] backup of replaced files: .update_backup/")
    log.warning("=" * 66)


def status() -> dict:
    """What the updater last did — for ;version and friends."""
    return _read_state()
