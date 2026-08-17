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

### Blade artwork — what size to make it

**512 × 512 px, square, PNG or WebP with a transparent background.**

That is not a preference, it is what the renderers paint. The largest surface
is the `;info` card: `utils/info_card.py` draws blade art into a 250 CSS-px
disc at `device_scale_factor=2`, so **500 real pixels** is the most any pixel
of source art will ever be shown at. `tools/optimize_assets.py` therefore caps
everything at `TARGET_PX = 512` — the next power of two above 500, with a
little headroom. Anything larger is disk, RAM and render time spent on pixels
nobody sees; at 101 blades that is the difference between ~7 MB of art and
~120 MB, which matters on a Pterodactyl panel.

Every surface that paints blade art:

| Surface | Painted at | Shape |
|---|---|---|
| `;info` card (`info_card.py`) | **500 × 500** | circle, `object-fit: cover` |
| Profile card (`profile_card.py`) | 208 × 208 | circle |
| Battle card (`image_generator.py`) | 360 × 360 | circle |
| Boss battle card (`boss_card.py`) | 120–150 | circle |
| Discord embed thumbnail | ~80 × 80 | square |
| Discord embed image (`set_image`) | ~400 wide | uncropped |

**Square matters more than resolution.** Every card crops to a circle with
`object-fit: cover`, so a 16:9 image loses its sides and a portrait one loses
its top and bottom — the blade ends up cropped through the middle no matter
how sharp the source was. Keep the blade centred and leave a margin: the
corners of the square are outside the circle and are always discarded, so
treat the **inscribed circle (~70% of the width)** as the safe zone for
anything that must survive.

Transparent background, because the disc paints its own coloured gradient
behind the art. A white or black rectangle behind the blade shows up as a
square patch inside the circle.

Local files go in `assets/beys/` named after the blade; run
`python tools/optimize_assets.py` afterwards and it converts to WebP q92 in
place (measured: ~3/255 mean RGB error at final render size, **zero** alpha
error, so cutout edges survive). If there is no local file the renderer falls
back to the entry's `image_url`, which is what every blade currently uses —
the same 512 px guidance applies to whatever you upload there.

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
- **Rock-Paper-Scissors** move logic — which *move* you pick each round:
  - ⚔️ Attack beats 🌀 Stamina
  - 🛡️ Defense beats ⚔️ Attack  
  - 🌀 Stamina beats 🛡️ Defense
- **Bey-type advantage** — a separate wheel, decided by your *blade's type*, not
  your move. The advantaged side keeps its passive bonus (the other side's is
  suppressed) and gets a signature effect:
  - ⚔️ Attack ▶ 🌀 Stamina — strips 6 stability every hit it lands
  - 🌀 Stamina ▶ 🛡️ Defense — pays 25% less stamina a move
  - 🛡️ Defense ▶ ⚔️ Attack — sends back 50% of what it blocks
  - ⚖️ Balance sits outside the triangle: half bonuses against everything, and
    it takes only half of any type edge aimed at it
  - `;help matchups` prints the full chart
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
