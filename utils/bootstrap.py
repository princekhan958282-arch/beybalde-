"""
bootstrap.py — make the bot install its own dependencies at startup.

Why this exists
---------------
The tournament card renders with Components V2, which needs discord.py 2.6+.
On a Pterodactyl panel the usual answer is "SSH in and run pip", which is
exactly the thing that doesn't happen when the bot is managed from a phone —
so the bot ends up running an old library and quietly drawing the fallback card
forever, with nothing on screen explaining why.

This module checks what's installed against requirements.txt and upgrades what
is behind, before anything imports discord.

Three rules it follows
----------------------
1. **Run before the import.** Python caches modules, so upgrading discord.py
   after `import discord` changes nothing until the next boot. `ensure()` must
   be called at the very top of app.py, and it re-execs the process after a
   successful install so the new version is actually loaded.

2. **Never loop.** A re-exec guarded only by "is it new enough yet?" spins
   forever the moment an install silently fails. `_GUARD` is set in the child's
   environment, so a re-exec can happen at most once per boot.

3. **Never block the boot.** No network, a read-only filesystem, and pip being
   absent are all normal on a locked-down panel. Every failure logs what to run
   by hand and returns — the bot then starts on whatever is installed, which
   still works because ui_v2 falls back below 2.6.

Opt out with BEYCORD_AUTO_INSTALL=0.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

log = logging.getLogger("beyblade_bot.bootstrap")


def _ensure_console_logging() -> None:
    """Give this module a handler without configuring the root logger.

    ensure() runs before app.py sets up logging, and `logging.basicConfig` is a
    no-op once any handler exists — so calling it here would silently override
    the real logging config later in app.py. Attaching one handler to this
    logger and turning off propagation keeps bootstrap's messages visible now
    and out of the way afterwards.
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
REQUIREMENTS = os.path.join(_ROOT, "requirements.txt")

_GUARD = "BEYCORD_BOOTSTRAPPED"
_OPT_OUT = "BEYCORD_AUTO_INSTALL"
PIP_TIMEOUT = 300           # a cold wheel build on a small VPS is not fast

# name, comparator, version — comments and blank lines are skipped.
_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(>=|==|~=)?\s*([0-9][0-9A-Za-z.\-]*)?")

# Import name differs from distribution name for a few of these.
_IMPORT_NAME = {
    "discord.py": "discord",
    "python-dotenv": "dotenv",
    "PyMySQL": "pymysql",
    "pillow": "PIL",
    "audioop-lts": "audioop",
}


def _parse_requirements(path: str = REQUIREMENTS) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                m = _REQ_RE.match(line)
                if not m:
                    continue
                name, op, ver = m.group(1), m.group(2) or "", m.group(3) or ""
                out.append((name, op, ver))
    except OSError as e:
        log.warning("[bootstrap] can't read requirements.txt: %s", e)
    return out


def _installed_version(dist: str) -> str:
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:                              # pragma: no cover
        return ""
    try:
        return version(dist)
    except PackageNotFoundError:
        return ""
    except Exception:                                # noqa: BLE001
        return ""


def _as_tuple(v: str) -> tuple:
    """Compare versions without depending on `packaging` being installed.

    Only the numeric head matters here — '2.6.2' beats '2.3.1'. A suffix like
    'rc1' or '.post1' sorts after the plain release, which is close enough for
    a "is this new enough" check and avoids a dependency we'd have to install
    to decide what to install.
    """
    head = re.match(r"[0-9]+(?:\.[0-9]+)*", v or "")
    if not head:
        return (0,)
    return tuple(int(p) for p in head.group(0).split("."))


def _satisfied(name: str, op: str, want: str) -> bool:
    have = _installed_version(name)
    if not have:
        mod = _IMPORT_NAME.get(name, name.replace("-", "_"))
        try:
            __import__(mod)
            # Importable but no metadata — vendored or a system package.
            # Leave it alone rather than reinstalling on every boot.
            return True
        except Exception:                            # noqa: BLE001
            return False
    if not op or not want:
        return True
    if op == "==":
        return have == want
    return _as_tuple(have) >= _as_tuple(want)


def _pip_install(specs: list[str]) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
           "--disable-pip-version-check", "--no-input", *specs]
    # Panels often run as a non-root user without a writable site-packages;
    # --user is the difference between working and a permissions wall.
    if not os.access(os.path.dirname(os.__file__) + "/site-packages", os.W_OK):
        cmd.insert(4, "--user")
    log.info("[bootstrap] installing: %s", " ".join(specs))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PIP_TIMEOUT)
    except FileNotFoundError:
        log.error("[bootstrap] pip is not available in this container.")
        return False
    except subprocess.TimeoutExpired:
        log.error("[bootstrap] pip timed out after %ss.", PIP_TIMEOUT)
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        log.error("[bootstrap] pip failed (exit %s):", proc.returncode)
        for line in tail:
            log.error("[bootstrap]   %s", line)
        return False
    log.info("[bootstrap] install finished.")
    return True


def missing() -> list[str]:
    """Requirement specs that are absent or too old."""
    out = []
    for name, op, ver in _parse_requirements():
        if not _satisfied(name, op, ver):
            have = _installed_version(name) or "not installed"
            log.warning("[bootstrap] %s %s%s needed — have %s", name, op, ver, have)
            out.append(f"{name}{op}{ver}" if op and ver else name)
    return out


def ensure() -> None:
    """Install anything missing, then re-exec so the new version is loaded.

    Call this at the TOP of app.py, before importing discord.
    """
    if os.getenv(_OPT_OUT, "1").strip().lower() in ("0", "false", "no", "off"):
        return
    _ensure_console_logging()

    try:
        needed = missing()
    except Exception as e:                           # noqa: BLE001
        log.warning("[bootstrap] dependency check failed, continuing: %s", e)
        return
    if not needed:
        return

    if os.getenv(_GUARD) == "1":
        # Already tried once this boot. Trying again would spin forever, so
        # say plainly what to run and let the bot start on what's there.
        log.error("[bootstrap] still missing after one install attempt: %s",
                  ", ".join(needed))
        log.error("[bootstrap] run this once by hand, then restart:")
        log.error("[bootstrap]   %s -m pip install -U %s",
                  sys.executable, " ".join(f'"{s}"' for s in needed))
        return

    if not _pip_install(needed):
        log.error("[bootstrap] automatic install failed. Run by hand:")
        log.error("[bootstrap]   %s -m pip install -U %s",
                  sys.executable, " ".join(f'"{s}"' for s in needed))
        return

    log.info("[bootstrap] restarting to load the new packages…")
    env = dict(os.environ, **{_GUARD: "1"})
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], env)
    except Exception as e:                           # noqa: BLE001
        # execve replaces the process and does not return; if it somehow did,
        # the packages are installed but this process still has the old ones
        # imported. A restart is the honest ask.
        log.error("[bootstrap] couldn't restart automatically (%s). "
                  "Restart the bot to finish the upgrade.", e)
