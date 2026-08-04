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
