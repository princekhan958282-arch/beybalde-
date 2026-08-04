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
# The repo is private, so you need a token. Sign in as the account that OWNS
# the repo, then:
#
#   1. github.com  →  Settings  →  Developer settings
#                  →  Personal access tokens  →  Fine-grained tokens
#   2. "Generate new token"
#   3. Expiration      →  pick a long one, or "No expiration".
#                         The default is 30 days, and when it lapses the bot
#                         just stops updating — ;version will say "failed",
#                         and this is the reason nine times out of ten.
#   4. Resource owner  →  the account that owns beybalde-
#   5. Repository access → "Only select repositories" → tick beybalde-
#   6. Repository permissions → Contents: Read-only
#         GitHub adds "Metadata: Read-only" by itself and will NOT let you
#         remove it. That is expected — leave it. Contents is the only one
#         you set yourself.
#   7. "Generate token", copy the github_pat_... string, paste it below
#
# Read-only is all it needs: this token only ever downloads. Never give it
# write access — anything that can push to the repo can run code on your host.
GITHUB_TOKEN = ""

# Optional. Which repo to pull from. Leave blank and it uses the repo this
# code ships from, which is almost certainly what you want.
#
# If you do set it, paste the link in whatever form you have it — the browser
# URL, the clone link, or just owner/repo. All of these work:
#     princekhan958282-arch/beybalde-
#     https://github.com/princekhan958282-arch/beybalde-
#     https://github.com/princekhan958282-arch/beybalde-.git
#     git@github.com:princekhan958282-arch/beybalde-.git
GITHUB_REPO = ""

# Which branch to track. Defaults to "main".
#
# ⚠️  SET THIS if your code lives on a branch that hasn't been merged into main
#     yet, or the updater tracks main and installs whatever is there instead.
#     (Any download with no app.py in it is refused, so a near-empty main can't
#     wipe your install — but nothing will ever update either.)
GITHUB_BRANCH = ""
