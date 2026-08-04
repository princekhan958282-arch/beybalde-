"""
config_local.example.py
-----------------------
Copy this file to `config_local.py` and paste your keys in, if you'd rather
hardcode than use a .env file.

    cp config_local.example.py config_local.py

`config_local.py` is excluded from every zip and listed in .gitignore, so your
keys stay on your server instead of travelling with the code.

Do NOT put keys in this .example file — this one DOES ship.
"""

# Discord bot token — https://discord.com/developers/applications
BOT_TOKEN = ""

# Optional. Boss trash-talk only; boss AI works fine without it.
# https://aistudio.google.com/apikey
GEMINI_API_KEY = ""

# Optional. Leave blank for gemini-2.5-flash (free tier, plenty for
# one-line trash talk). Other options: gemini-3.6-flash, or
# gemini-flash-latest to always track the newest release.
GEMINI_MODEL = ""

# Optional. Moves the player registry to MySQL so it survives a container
# rebuild. Leave blank to keep everything in the local SQLite file.
# Accepts mysql://... or the jdbc:mysql://... string panels usually give you.
MYSQL_URL = ""

# ── Auto-update from GitHub ───────────────────────────────────────────────────
# Fill this in and the bot pulls the latest code from GitHub every time it
# starts, so you stop re-uploading zips. Leave blank to turn the whole thing
# off — with no token the updater logs one line and does nothing.
#
# The repo is private, so you need a token:
#   1. github.com  →  Settings  →  Developer settings
#                  →  Personal access tokens  →  Fine-grained tokens
#   2. "Generate new token", pick ONLY the beybalde- repository
#   3. Repository permissions  →  Contents: Read-only          ← nothing else
#   4. Generate, copy the github_pat_... string, paste it below
#
# Read-only Contents is all it needs. Do not give it write access: this token
# only ever downloads.
GITHUB_TOKEN = ""

# Optional. Which repo and branch to track. Defaults: the repo this code ships
# from, and the "main" branch.
#
# ⚠️  SET GITHUB_BRANCH if your code lives on a branch that hasn't been merged
#     into main yet, or the updater will track main and try to install whatever
#     is there instead. (The updater refuses any download with no app.py in it,
#     so a near-empty main can't wipe your install — but it also means nothing
#     will ever update until you point this at the right branch.)
GITHUB_REPO = ""
GITHUB_BRANCH = ""
