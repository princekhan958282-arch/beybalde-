# 🌀 Beyblade Discord Bot

A modular, Cog-based Discord bot for Beyblade collection and battles.

---

## Project Structure

```
beyblade_bot/
├── main.py                  ← Bot entry point
├── requirements.txt
├── .env.example             ← Copy to .env and fill in your token
│
├── data/
│   ├── beyblades.json       ← Master Beyblade registry (edit this to add real Beyblades)
│   └── users.json           ← Player data (auto-managed at runtime)
│
├── cogs/
│   ├── spawn.py             ← Wild spawn & !claim system
│   ├── profile.py           ← !profile, !info, !equip, !inventory, !list
│   └── battle.py            ← !battle — button-based PvP combat
│
└── utils/
    ├── database.py          ← Shared JSON read/write helpers
    └── embeds.py            ← Shared embed builders (stat bars, colours, etc.)
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your .env file
cp .env.example .env
# Then edit .env and paste your Discord Bot Token

# 3. Run
python main.py
```

---

## Commands

| Command | Description |
|---|---|
| `!claim <name>` | Claim a wild Beyblade that has spawned |
| `!profile [@user]` | View your (or another user's) profile card |
| `!info <name>` | Look up a Beyblade's stats |
| `!equip <name>` | Switch your active Beyblade |
| `!inventory [@user]` | See someone's full collection |
| `!list` | See all Beyblades grouped by rarity |
| `!battle @user` | Challenge a player to a battle |
| `!forcespawn` | *(Admin)* Force a spawn immediately |

---

## Adding Real Beyblades

Edit `data/beyblades.json`. Each entry follows this schema:

```json
"Dragoon Storm": {
  "id": "BB005",
  "name": "Dragoon Storm",
  "rarity": "Legendary",
  "image_url": "https://your-cdn.com/dragoon.png",
  "description": "The legendary wind Beyblade of Tyson Granger.",
  "stats": {
    "attack":  90,
    "defense": 60,
    "stamina": 80,
    "special": 100
  }
}
```

**Rarity tiers and spawn weights:**

| Rarity    | Spawn Chance | Colour  |
|-----------|-------------|---------|
| Common    | 60%         | ⚪ Grey  |
| Rare      | 25%         | 🔵 Blue  |
| Epic      | 14%         | 🟣 Purple |
| Legendary | 1%          | 🟡 Gold  |

---

## Battle System

- Battles are **turn-based** — players alternate selecting moves via Discord Buttons
- **Rock-Paper-Scissors** move logic:
  - ⚔️ Attack beats 🌀 Stamina
  - 🛡️ Defense beats ⚔️ Attack  
  - 🌀 Stamina beats 🛡️ Defense
- **⚡ Special Charge** — press twice to unlock 🌟 SPECIAL for a big hit
- Damage is calculated from real stats in `beyblades.json`
- Win/Loss record is saved to `users.json`

---

## Discord Developer Portal Setup

1. Go to [discord.com/developers](https://discord.com/developers/applications)
2. Create a new Application → Bot
3. Enable: **Message Content Intent** and **Server Members Intent**
4. Copy the token → paste into `.env`
5. Invite the bot with scopes: `bot` + permissions: `Send Messages`, `Embed Links`, `Read Message History`
