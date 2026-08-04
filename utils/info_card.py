"""
utils/info_card.py — Beycord blade info card renderer (Playwright → PNG)
========================================================================

Renders the phone-first blade info card:

    ┌────────────────────────────────────┐
    │ [RARITY]              [SPIN BADGE] │
    │            ( blade art )           │
    │             Blade Name             │
    │            [TYPE BADGE]            │
    │  EQUIPPED PARTS                    │
    │  [ BLADE ][ RATCHET ][ BIT ]       │
    │  ┌ STATS ──┐ ┌ ABILITIES (n) ────┐ │
    │  │ ATK ▬▬  │ │ Name  [CHIP]      │ │
    │  │ DEF ▬▬  │ │ desc…              │ │
    │  │ STA ▬▬  │ │ ──────────────────│ │
    │  │ BUR ▬▬  │ │ Name  [CHIP]      │ │
    │  │ HP  ▬▬  │ │ desc…              │ │
    │  └─────────┘ └───────────────────┘ │
    │  ┌ SPECIAL MOVE ──────────────────┐│
    │  └────────────────────────────────┘│
    └────────────────────────────────────┘

Auto-layout
-----------
* **1, 2 or 3 abilities** all render correctly. The card height is driven by
  content (`full_page=True` screenshot), the two middle columns stretch to the
  taller of the pair, and ability type-size steps down as the count goes up so
  three abilities never overflow or look cramped.
* Rarity drives the entire accent palette (border, glow, badges, bars).
* Blade art loads from ``image_url``; if the CDN link is dead the CSS disc
  fallback shows through instead of a broken-image box.

Failure policy
--------------
`render_info_card()` returns ``None`` on any failure (no Chromium, dead CDN,
timeout). Callers fall back to the classic embed — a card is never worth
breaking a command over.

Performance
-----------
* PNG bytes are LRU-cached per (blade doc, parts) — repeat ``;info`` calls are
  instant and cost zero Chromium. Edit blade data → the hash key changes →
  auto-invalidation. Hot-reloading data in-process? Call ``clear_cache()``.
* Up to 2 renders run concurrently; one slow CDN no longer queues the bot.
* Art waits are hard-bounded at NAV_TIMEOUT; dead or stalled CDN links can
  never stall a render for Playwright's default 30 s.

Public API
----------
    await render_info_card(blade, parts=None) -> io.BytesIO | None
    clear_cache()     — drop cached PNGs (after live data edits).
    CARD_ENABLED — flip False to disable PNG cards globally.
    await shutdown()  — close the shared browser on bot teardown.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
from collections import OrderedDict
from typing import Any, Optional

from utils.hp_system import blade_hp_stat, hp_display_pct

log = logging.getLogger(__name__)

CARD_ENABLED = True

CARD_WIDTH   = 720          # CSS px; deviceScaleFactor 2 → 1440px PNG
NAV_TIMEOUT  = 6_000        # ms budget for remote blade art
STAT_MAX     = 200          # ATK/DEF/STA/BUR bar ceiling

# ── Rarity palettes ──────────────────────────────────────────────────────────
# accent / glow / deep background tint — the whole card recolours from here.
_RARITY_THEME = {
    "Common":          {"accent": "#a8b0bd", "glow": "#5b6472", "tint": "#14161c"},
    "Uncommon":        {"accent": "#4ade80", "glow": "#166534", "tint": "#0e1a12"},
    "Rare":            {"accent": "#5aa9ff", "glow": "#1d4ed8", "tint": "#0d1420"},
    "Epic":            {"accent": "#c084fc", "glow": "#6b21a8", "tint": "#170f22"},
    "Legendary":       {"accent": "#f5c542", "glow": "#8a5a00", "tint": "#1c1409"},
    "Mythic":          {"accent": "#ff5f5f", "glow": "#8f1d1d", "tint": "#1e0d0d"},
    "Ultimate":        {"accent": "#ffe066", "glow": "#9a7a00", "tint": "#1f1a06"},
    "Exclusive":       {"accent": "#2dd4bf", "glow": "#0f766e", "tint": "#08191a"},
    "State Exclusive": {"accent": "#ff86c8", "glow": "#9d174d", "tint": "#1d0c16"},
}
_DEFAULT_THEME = _RARITY_THEME["Common"]

_TYPE_LABEL = {
    "Attack":  "ATTACK TYPE",
    "Defense": "DEFENSE TYPE",
    "Stamina": "STAMINA TYPE",
    "Balance": "BALANCE TYPE",
}

# Trigger → short uppercase chip, matching the reference card's voice.
_TRIGGER_CHIP = {
    "passive":           "PASSIVE",
    "setup":             "BATTLE START",
    "turn_start":        "TURN START",
    "turn_end":          "TURN END",
    "on_defend":         "ON DEFEND",
    "on_take_damage":    "WHEN HIT",
    "on_hit":            "ON EACH HIT",
    "on_attack_hit":     "ON ATTACK LANDS",
    "on_special":        "ON SPECIAL",
    "on_mirror":         "ON CLASH",
    "on_attack_mirror":  "ON CLASH",
    "on_defense_mirror": "ON CLASH",
    "on_stamina_mirror": "ON CLASH",
    "on_attack_win":     "ON ATTACK WIN",
    "on_attack_loss":    "ON ATTACK LOSS",
    "on_defense_win":    "ON DEFENSE WIN",
    "on_defense_loss":   "ON DEFENSE LOSS",
    "on_stamina_win":    "ON STAMINA WIN",
    "on_stamina_loss":   "ON STAMINA LOSS",
    "on_any_win":        "ON ANY WIN",
    "on_any_loss":       "ON ANY LOSS",
    "on_low_hp":         "BELOW 50% HP",
    "on_high_hp":        "ABOVE 50% HP",
    "on_low_stamina":    "LOW STAMINA",
    "on_high_stamina":   "HIGH STAMINA",
}

_SPIN_ICON = {"Right": "↻", "Left": "↺", "Dual": "⇄"}

# ── Local blade art (assets/beys) ────────────────────────────────────────────
# Same lenient matching as utils.image_generator: case / space / underscore /
# dash insensitive. Local art is embedded as a data URI, so the render makes
# ZERO network requests — instant, and immune to dead CDN links.
# Absolute, derived from this file. It used to be the relative "assets/beys",
# which only resolves when the bot is launched from the project root — panels
# frequently launch from somewhere else, and when they do os.listdir() raises,
# the index comes back empty and EVERY blade silently loses its artwork.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BEY_DIR = os.path.join(_PROJECT_ROOT, "assets", "beys")
_art_index: dict[str, str] | None = None


def _norm(s2: str) -> str:
    """Lenient art-name key — must strip every extension the index accepts,
    or optimised .webp art silently stops matching its blade."""
    s2 = str(s2).lower().strip()
    for _ext in (".png", ".webp", ".jpeg", ".jpg"):
        if s2.endswith(_ext):
            s2 = s2[: -len(_ext)]
            break
    s2 = s2.replace("_", " ").replace("-", " ")
    # Fold the characters a file manager or zip tool may mangle, so
    # "nemesis aetherion org.png" matches "NEMESIS ÆTHERION org".
    s2 = s2.replace("æ", "ae").replace("Æ", "ae")
    return " ".join(s2.split())


def _art_aliases(name: str) -> list[str]:
    """Every key one blade's art might legitimately be filed under.

    Boss blades are named with an " org" suffix — "NEMESIS ÆTHERION org" is the
    real blade, "NEMESIS ÆTHERION" is a rolled copy of it (see boss_info
    .wants_copy). Both faces render through this same art loader, so a single
    file could only ever satisfy ONE of them: art saved as
    "NEMESIS ÆTHERION.png" left the boss fight card blank, and art saved as
    "NEMESIS ÆTHERION org.png" left every copy card blank.

    Since the marker is a naming convention and not part of the artwork, both
    spellings resolve to the same image and the artist can name the file either
    way. Exact match still wins, so a deliberate separate "… org" art file is
    still used for the boss when one exists.
    """
    base = _norm(name)
    out = [base]
    words = base.split()
    if words and words[-1] in ("org", "copy"):
        stripped = " ".join(words[:-1])
        if stripped:
            out.append(stripped)
    else:
        out.append(f"{base} org")
    return out


def _local_art_path(name: str) -> Optional[str]:
    global _art_index
    if _art_index is None:
        idx = {}
        try:
            for f in os.listdir(_BEY_DIR):
                if f.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                    idx[_norm(f)] = os.path.join(_BEY_DIR, f)
        except OSError:
            pass
        _art_index = idx
    for key in _art_aliases(name):
        hit = _art_index.get(key)
        if hit:
            return hit
    return None


def refresh_art_index() -> None:
    """Re-scan assets/beys (call after dropping in new art at runtime)."""
    global _art_index
    _art_index = None
    clear_cache()


def _art_src(blade: dict) -> str:
    """Best art source: local file as data URI, else the CDN image_url."""
    path = _local_art_path(blade.get("name", ""))
    if path:
        try:
            mime = "image/webp" if path.endswith(".webp") else \
                   "image/jpeg" if path.endswith((".jpg", ".jpeg")) else "image/png"
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except OSError:
            pass
    return blade.get("image_url") or ""


# ══════════════════════════════════════════════════════════════════════════════
#  Data shaping
# ══════════════════════════════════════════════════════════════════════════════

def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _theme(rarity: str) -> dict:
    return _RARITY_THEME.get(rarity, _DEFAULT_THEME)


def _collect_abilities(blade: dict) -> list[dict]:
    """All abilities for a blade, in order, capped at 3 (the card's design max).

    Handles every historical shape: the `abilities` list, plus the legacy
    `ability` / `ability_2` singletons for entries that never got migrated.
    """
    out: list[dict] = []
    seen: set[str] = set()

    for ab in blade.get("abilities") or []:
        if isinstance(ab, dict) and ab.get("name"):
            key = str(ab["name"]).lower()
            if key not in seen:
                seen.add(key)
                out.append(ab)

    for legacy in ("ability", "ability_2"):
        ab = blade.get(legacy)
        if isinstance(ab, dict) and ab.get("name"):
            key = str(ab["name"]).lower()
            if key not in seen:
                seen.add(key)
                out.append(ab)

    return out[:3]


def _chip_for(ab: dict) -> str:
    """Chip text for an ability — prefers an authored label, else the trigger.

    Threshold triggers carry their real percentage when the rule declares one,
    so `on_low_hp` at 0.30 reads "BELOW 30% HP" rather than the 50% default.
    """
    if ab.get("chip"):
        return str(ab["chip"]).upper()

    trig = str(ab.get("trigger", "passive"))
    if trig in ("on_low_hp", "on_high_hp"):
        # Threshold lives either directly on the ability (`threshold_pct`,
        # authored data) or inside the engine's `if` list of
        # {"cond": "hp_below_pct", "value": ...} entries.
        val = ab.get("threshold_pct")
        if val is None:
            conds = ab.get("if") or ab.get("condition") or []
            if isinstance(conds, dict):
                conds = [conds]
            for c in conds:
                if isinstance(c, dict) and "hp" in str(c.get("cond", "")):
                    val = c.get("value", c.get("pct"))
                    break
        if val is not None:
            try:
                pct = float(val)
                pct = pct * 100 if pct <= 1 else pct
                word = "BELOW" if trig == "on_low_hp" else "ABOVE"
                return f"{word} {int(round(pct))}% HP"
            except (TypeError, ValueError):
                pass

    return _TRIGGER_CHIP.get(trig, trig.replace("_", " ").upper() or "PASSIVE")


def _stat_rows(blade: dict) -> list[dict]:
    """The four stat bars, in card order: HP, ATK, DEF, STA.

    BUR (special) is intentionally absent — it already headlines the SPECIAL
    MOVE panel's damage line, so repeating it here was noise.
    HP normalises over the reachable cross-type range (see hp_system
    .HP_DISPLAY_*) rather than the 0–200 scale the other three use.
    """
    st = blade.get("stats", {}) or {}
    hp = blade_hp_stat(blade)

    rows = [{"label": "HP", "value": hp,
             "pct": max(4.0, hp_display_pct(blade)), "hp": True}]
    for label, key in (("ATK", "attack"), ("DEF", "defense"), ("STA", "stamina")):
        val = int(st.get(key, 0) or 0)
        rows.append({"label": label, "value": val,
                     "pct": max(0.0, min(100.0, val / STAT_MAX * 100))})
    return rows


def _stat_total(blade: dict) -> int:
    """TOTAL shown under the bars = the four displayed stats summed
    (HP + ATK + DEF + STA). BUR is excluded, same as the bars."""
    st = blade.get("stats", {}) or {}
    return (blade_hp_stat(blade)
            + int(st.get("attack", 0) or 0)
            + int(st.get("defense", 0) or 0)
            + int(st.get("stamina", 0) or 0))


def _parts_slots(blade: dict, parts: Optional[dict]) -> list[dict]:
    """The BLADE / RATCHET / BIT strip.

    `parts` is an optional {"ratchet": name, "bit": name} from the viewer's
    equipped loadout. Empty slots show a dash — never a blank box.
    """
    parts = parts or {}
    return [
        {"slot": "BLADE",   "value": blade.get("name", "—")},
        {"slot": "RATCHET", "value": parts.get("ratchet") or "—"},
        {"slot": "BIT",     "value": parts.get("bit") or "—"},
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  HTML template
# ══════════════════════════════════════════════════════════════════════════════

def build_html(blade: dict, parts: Optional[dict] = None) -> str:
    """Render the card to a standalone HTML string (also handy for debugging —
    dump it to a file and open it in a browser)."""
    rarity = blade.get("rarity", "Common")
    th     = dict(_theme(rarity))
    # A blade may carry its own palette. Event-limited bosses get a background
    # nothing else in the game uses, so they read as one-off at a glance rather
    # than as "another teal Exclusive". Falls back to the rarity theme, so every
    # existing blade renders exactly as before.
    custom = blade.get("card_theme")
    if isinstance(custom, dict):
        th.update({k: v for k, v in custom.items()
                   if k in ("accent", "glow", "tint") and v})
    accent, glow, tint = th["accent"], th["glow"], th["tint"]

    btype  = blade.get("type", "Balance")
    spin   = blade.get("spin_direction", "Right")
    abils  = _collect_abilities(blade)
    n_ab   = len(abils)

    # Type scale steps down as the ability list grows — this is the whole
    # "auto adjust for 1 / 2 / 3" behaviour.
    ab_name_size = {0: 21, 1: 22, 2: 21, 3: 19}[n_ab]
    ab_desc_size = {0: 15, 1: 16, 2: 15, 3: 13.5}[n_ab]
    ab_gap       = {0: 0,  1: 0,  2: 14, 3: 10}[n_ab]

    # ── Fragments ────────────────────────────────────────────────────────────
    stats_html = "".join(
        f"""
        <div class="stat{' hp' if r.get('hp') else ''}">
          <div class="stat-top">
            <span class="stat-label">{_esc(r['label'])}</span>
            <span class="stat-val">{r['value']}</span>
          </div>
          <div class="track"><div class="fill" style="width:{r['pct']:.1f}%"></div></div>
        </div>"""
        for r in _stat_rows(blade)
    )

    parts_html = "".join(
        f"""
        <div class="part">
          <div class="part-slot">{_esc(p['slot'])}</div>
          <div class="part-name">{_esc(p['value'])}</div>
        </div>"""
        for p in _parts_slots(blade, parts)
    )

    if abils:
        abilities_html = "".join(
            f"""
        <div class="ability">
          <div class="ab-name">{_esc(ab.get('name', 'Ability'))}</div>
          <div class="ab-chip">{_esc(_chip_for(ab))}</div>
          <div class="ab-desc">{_esc(ab.get('description', ''))}</div>
        </div>"""
            for ab in abils
        )
    else:
        abilities_html = '<div class="ab-empty">No abilities recorded.</div>'

    sm = blade.get("special_move") or {}
    special_html = ""
    if sm.get("name"):
        hits  = sm.get("hits", 1)
        dph   = sm.get("damage_per_hit", 0)
        total = sm.get("total_damage", hits * dph)
        meta  = (f"{hits} hit{'s' if hits != 1 else ''} × {dph} dmg &nbsp;·&nbsp; "
                 f"{total} total") if dph else ""
        special_html = f"""
      <section class="special">
        <div class="sec-head"><span class="bolt">⚡</span> SPECIAL MOVE</div>
        <div class="sm-name">{_esc(sm.get('name', '???'))}</div>
        <div class="sm-desc">{_esc(sm.get('description', ''))}</div>
        {f'<div class="sm-meta">{meta}</div>' if meta else ''}
      </section>"""

    art = _art_src(blade)
    art_html = (f'<img class="art-img" src="{_esc(art)}" alt="" '
                f'onerror="this.remove()">' if art else "")

    booster = ('<div class="booster">📦 BOOSTER EXCLUSIVE</div>'
               if blade.get("booster_exclusive") else "")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root {{
    --accent: {accent};
    --glow:   {glow};
    --tint:   {tint};
    --text:   #f4f1ea;
    --muted:  #b9b3a6;
    --panel:  rgba(0,0,0,.42);
  }}
  html, body {{
    width: {CARD_WIDTH}px;
    background: #07070a;
    font-family: "DejaVu Sans", "Noto Sans", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}

  .card {{
    margin: 10px;
    padding: 22px 20px 24px;
    border: 2px solid var(--accent);
    border-radius: 26px;
    background:
      radial-gradient(120% 70% at 50% 0%, {glow}55 0%, transparent 62%),
      linear-gradient(170deg, {tint} 0%, #06060a 100%);
    box-shadow: 0 0 44px {glow}55, inset 0 0 90px {glow}22;
  }}

  /* ── Top badges ─────────────────────────────────────────────── */
  .top {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }}
  .badge {{
    display:flex; align-items:center; gap:9px;
    padding: 11px 18px;
    border: 1.5px solid var(--accent);
    border-radius: 12px;
    background: rgba(0,0,0,.5);
    font-size: 21px; font-weight: 800; letter-spacing:.5px;
    color: var(--accent); white-space: nowrap;
  }}
  .dot {{
    width:15px; height:15px; border-radius:50%;
    background: var(--accent); box-shadow: 0 0 10px var(--accent);
  }}
  .spin-icon {{ font-size:23px; line-height:1; }}
  .badge.spin {{ color: var(--text); }}

  /* ── Art ────────────────────────────────────────────────────── */
  .art {{
    position: relative;
    height: 380px;
    display:flex; align-items:center; justify-content:center;
  }}
  .ring {{
    position:absolute; border-radius:50%;
    border: 3px solid var(--accent);
    opacity:.75;
  }}
  .ring.a {{ width:340px; height:340px; border-color: transparent var(--accent) transparent transparent; transform: rotate(-28deg); }}
  .ring.b {{ width:406px; height:406px; border-color: transparent transparent transparent var(--accent); transform: rotate(-28deg); opacity:.5; }}
  .disc {{
    position: relative;
    width: 250px; height: 250px; border-radius:50%;
    background: radial-gradient(circle at 35% 30%, {glow} 0%, #1a1a24 78%);
    border: 6px solid #6c6c7e;
    box-shadow: 0 0 60px {glow}, inset 0 6px 22px rgba(255,255,255,.10);
    overflow: hidden;
    display:flex; align-items:center; justify-content:center;
  }}
  .art-img {{ width:100%; height:100%; object-fit:cover; }}
  .art-img[src^="data:"] {{ object-fit:contain; padding:6%; }}

  /* ── Identity ───────────────────────────────────────────────── */
  .name {{
    margin-top: 6px; text-align:center;
    font-size: 50px; font-weight: 800; font-style: italic;
    color: var(--text); letter-spacing:.5px;
    text-shadow: 0 3px 16px {glow}, 0 1px 0 #000;
    line-height: 1.08;
  }}
  .name.long {{ font-size: 40px; }}
  .name.xlong {{ font-size: 33px; }}
  .type-pill {{
    margin: 14px auto 0; width: fit-content;
    padding: 8px 22px; border-radius: 10px;
    background: var(--accent); color: #17130a;
    font-size: 19px; font-weight: 800; letter-spacing: 1.2px;
  }}
  .booster {{
    margin: 10px auto 0; width: fit-content;
    padding: 6px 16px; border-radius: 8px;
    border: 1px dashed var(--accent);
    color: var(--accent); font-size: 14px; font-weight: 700; letter-spacing:.8px;
  }}

  /* ── Section chrome ─────────────────────────────────────────── */
  .sec-head {{
    display:flex; align-items:center; gap:9px;
    font-size: 19px; font-weight: 800; letter-spacing: 1px;
    color: var(--accent); margin-bottom: 12px;
  }}
  .sec-head::before {{
    content:""; width:5px; height:19px; border-radius:2px;
    background: var(--accent); flex:none;
  }}
  .sec-head.icon::before {{ display:none; }}
  .bolt {{ font-size: 19px; }}

  /* ── Parts strip ────────────────────────────────────────────── */
  .parts-wrap {{ margin-top: 26px; }}
  .parts {{ display:flex; gap: 13px; }}
  .part {{
    flex:1; min-width:0;
    padding: 14px 8px;
    border: 1.5px solid rgba(255,255,255,.14);
    border-radius: 12px;
    background: var(--panel);
    text-align:center;
  }}
  .part-slot {{ font-size: 14px; font-weight:700; letter-spacing:1.2px; color: var(--muted); }}
  .part-name {{
    margin-top: 6px; font-size: 18px; font-weight: 800; color: var(--text);
    overflow-wrap: anywhere; line-height:1.2;
  }}

  /* ── Middle: stats | abilities (stretch to equal height) ────── */
  .mid {{ display:flex; gap: 14px; margin-top: 22px; align-items: stretch; }}
  .box {{
    border-radius: 16px; padding: 16px 15px;
    background: var(--panel);
    border: 1.5px solid rgba(255,255,255,.13);
  }}
  .box.stats {{ flex: 0 0 40%; display:flex; flex-direction:column; }}
  /* Bars spread to fill whatever height the abilities column dictates, so a
     3-ability card never leaves a dead gap under HP. */
  .stat-list {{
    flex: 1; display:flex; flex-direction:column;
    justify-content: space-between; gap: 12px;
  }}
  .box.abilities {{
    flex: 1 1 auto; min-width:0;
    border-color: var(--accent);
    box-shadow: inset 0 0 30px {glow}22;
    display:flex; flex-direction:column;
  }}

  .stat-top {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .stat-label {{ font-size: 20px; font-weight: 800; color: var(--text); letter-spacing:.5px; }}
  .stat-val   {{ font-size: 25px; font-weight: 800; color: var(--text); }}
  .track {{
    margin-top: 5px; height: 7px; border-radius: 4px;
    background: rgba(255,255,255,.10); overflow:hidden;
  }}
  .fill {{
    height: 100%; border-radius: 4px;
    background: linear-gradient(90deg, {glow}, var(--accent));
  }}
  .stat.hp .fill {{ background: linear-gradient(90deg, #b91c1c, #f87171); }}
  .stat.hp .stat-label, .stat.hp .stat-val {{ color: #ff9b9b; }}
  .stat-total {{
    margin-top: 14px; padding-top: 12px;
    border-top: 1.5px solid rgba(255,255,255,.16);
    display:flex; justify-content:space-between; align-items:baseline;
  }}
  .total-label {{
    font-size: 16px; font-weight: 800; letter-spacing: 1.2px;
    color: var(--accent);
  }}
  .total-val {{ font-size: 27px; font-weight: 800; color: var(--accent); }}

  .ab-list {{ display:flex; flex-direction:column; }}
  .ability {{ padding: {ab_gap}px 0; }}
  .ability:first-child {{ padding-top: 0; }}
  .ability:last-child  {{ padding-bottom: 0; }}
  .ability + .ability {{ border-top: 1px solid rgba(255,255,255,.14); }}
  .ab-name {{
    font-size: {ab_name_size}px; font-weight: 800; font-style: italic;
    color: var(--text); line-height:1.15;
  }}
  .ab-chip {{
    display:inline-block; margin: 7px 0 6px;
    padding: 3px 9px; border-radius: 6px;
    background: var(--accent); color:#17130a;
    font-size: 12.5px; font-weight: 800; letter-spacing:.6px;
  }}
  .ab-desc {{
    font-size: {ab_desc_size}px; line-height: 1.45; color: #ddd8cd;
  }}
  .ab-empty {{ font-size:15px; color: var(--muted); font-style: italic; }}

  /* ── Special move ───────────────────────────────────────────── */
  .special {{
    margin-top: 16px; padding: 16px 16px 18px;
    border: 2px solid var(--accent); border-radius: 16px;
    background: linear-gradient(180deg, {glow}22, rgba(0,0,0,.45));
  }}
  .sm-name {{
    font-size: 27px; font-weight: 800; font-style: italic;
    color: var(--text); line-height:1.15;
  }}
  .sm-desc {{ margin-top: 6px; font-size: 16px; line-height: 1.45; color: #ddd8cd; }}
  .sm-meta {{
    margin-top: 8px; font-size: 14px; font-weight: 800;
    letter-spacing:.6px; color: var(--accent);
  }}
</style></head>
<body>
  <div class="card">
    <div class="top">
      <div class="badge"><span class="dot"></span>{_esc(rarity).upper()}</div>
      <div class="badge spin">
        <span class="spin-icon">{_SPIN_ICON.get(spin, '↻')}</span>{_esc(spin).upper()} SPIN
      </div>
    </div>

    <div class="art">
      <div class="ring b"></div>
      <div class="ring a"></div>
      <div class="disc">{art_html}</div>
    </div>

    <div class="name" id="bname">{_esc(blade.get('name', 'Unknown'))}</div>
    <div class="type-pill">{_esc(_TYPE_LABEL.get(btype, str(btype).upper() + ' TYPE'))}</div>
    {booster}

    <section class="parts-wrap">
      <div class="sec-head">EQUIPPED PARTS</div>
      <div class="parts">{parts_html}</div>
    </section>

    <div class="mid">
      <section class="box stats">
        <div class="sec-head">STATS</div>
        <div class="stat-list">{stats_html}</div>
        <div class="stat-total">
          <span class="total-label">TOTAL</span>
          <span class="total-val">{_stat_total(blade)}</span>
        </div>
      </section>
      <section class="box abilities">
        <div class="sec-head icon">◆ ABILITIES ({n_ab})</div>
        <div class="ab-list">{abilities_html}</div>
      </section>
    </div>

    {special_html}
  </div>

<script>
  // Long blade names step down instead of wrapping into the art.
  (function () {{
    var el = document.getElementById("bname");
    var n  = el.textContent.trim().length;
    if (n > 26)      el.classList.add("xlong");
    else if (n > 18) el.classList.add("long");
  }})();
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  Rendering
# ══════════════════════════════════════════════════════════════════════════════

_playwright   = None
_browser      = None
_context      = None
_launch_lock  = asyncio.Lock()          # guards launch only — never a render
_render_sem   = asyncio.Semaphore(2)    # ≤2 concurrent pages: VPS-friendly,
                                        # but battle spam no longer queues
                                        # behind one slow CDN

# ── PNG byte cache ────────────────────────────────────────────────────────────
# Blade data is static between deploys, so identical (blade, parts) requests
# re-serve cached bytes instead of paying ~2 s of Chromium per ;info. Keyed on
# a hash of the blade doc itself, so hot-editing beyblades.json auto-invalidates.
_CACHE_MAX = 16                          # ≈ 16 × 1.3 MB ≈ 21 MB ceiling
_cache: "OrderedDict[str, bytes]" = OrderedDict()

# Diagnostics for ;carddebug: which engine served the last card, and the last
# Playwright failure (panels hide logs; this surfaces the reason in Discord).
last_engine: str = "none"                # "playwright" | "pillow" | "cache" | "none"
last_playwright_error: str = ""


def _cache_key(blade: dict, parts: Optional[dict]) -> str:
    try:
        blob = json.dumps(blade, sort_keys=True, default=str)
        pb   = json.dumps(parts or {}, sort_keys=True, default=str)
        # fold in the local asset's identity+mtime, so replacing a PNG in
        # assets/beys invalidates that blade's cached card automatically
        ap = _local_art_path(blade.get("name", "")) or ""
        try:
            ap += f"@{os.path.getmtime(ap):.0f}" if ap else ""
        except OSError:
            pass
        return hashlib.sha1((blob + "|" + pb + "|" + ap).encode("utf-8")).hexdigest()
    except Exception:
        return ""                        # unhashable → just skip caching


def clear_cache() -> None:
    """Drop all cached card PNGs (call after live-editing blade data)."""
    _cache.clear()


async def _get_context():
    """One shared Chromium + one shared context; pages are cheap after that.

    Re-launches transparently if a previous instance died (panel restart,
    OOM-killed renderer), so one bad card doesn't poison every later one.
    """
    global _playwright, _browser, _context
    if _browser is not None and _browser.is_connected() and _context is not None:
        return _context

    async with _launch_lock:
        # Re-check under the lock — another task may have just relaunched.
        if _browser is not None and _browser.is_connected() and _context is not None:
            return _context
        from playwright.async_api import async_playwright
        if _playwright is None:
            _playwright = await async_playwright().start()
        base = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        try:
            _browser = await _playwright.chromium.launch(args=base)
        except Exception:
            # Pterodactyl-style containers often lack the process namespaces
            # Chromium's zygote needs — retry in single-process mode before
            # giving up to the Pillow fallback.
            _browser = await _playwright.chromium.launch(
                args=base + ["--no-zygote", "--single-process",
                             "--disable-extensions", "--mute-audio"]
            )
        _context = await _browser.new_context(
            viewport={"width": CARD_WIDTH, "height": 1280},
            device_scale_factor=2,
        )
        return _context


def compress(png_bytes: bytes, quality: int = 90) -> tuple[bytes, str]:
    """PNG screenshot -> WebP. Returns (bytes, file_extension).

    Measured on the NEMESIS card: 1,657 KB of PNG becomes 246 KB of WebP at
    q=90 for about 350 ms of encoding — 15% of the payload with no visible
    difference on a card that is mostly flat panels and text. PNG optimize=True
    only reached 93% for ten times the CPU, and quantizing to 256 colours
    banded the gradients.

    Falls back to the original PNG if Pillow or WebP support is missing, so a
    stripped-down environment still gets a card.
    """
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(png_bytes))
        out = _io.BytesIO()
        im.save(out, format="WEBP", quality=quality, method=4)
        data = out.getvalue()
        # Only take the win if it actually is one.
        if data and len(data) < len(png_bytes):
            return data, "webp"
    except Exception as exc:
        log.debug(f"[info_card] webp compression unavailable: {exc}")
    return png_bytes, "png"


async def _render_once(html_doc: str) -> bytes:
    """One page lifecycle: set content, bounded art wait, screenshot."""
    context = await _get_context()
    page = await context.new_page()
    try:
        page.set_default_timeout(NAV_TIMEOUT)
        # domcontentloaded does NOT wait on the <img> — a stalled (non-404)
        # CDN can no longer hang the render for Playwright's default 30 s.
        await page.set_content(html_doc, wait_until="domcontentloaded")
        # Bounded wait for the art: resolves the moment the image finishes
        # (loaded OR errored-and-removed via its onerror), gives up quietly
        # at NAV_TIMEOUT and screenshots whatever is on screen.
        try:
            await page.wait_for_function(
                """() => {
                    const img = document.querySelector('.art-img');
                    return !img || (img.complete && img.naturalWidth > 0);
                }""",
                timeout=NAV_TIMEOUT,
            )
        except Exception:
            pass
        return await page.locator(".card").screenshot(type="png")
    finally:
        await page.close()


async def render_info_card(blade: dict, parts: Optional[dict] = None) -> Optional[io.BytesIO]:
    """Render a blade's info card. Returns a PNG buffer, or None on any failure.

    Cached, bounded-concurrency, crash-tolerant: a browser that dies mid-render
    gets exactly one transparent relaunch+retry.
    """
    if not CARD_ENABLED or not blade:
        return None

    global last_engine, last_playwright_error

    key = _cache_key(blade, parts)
    if key and key in _cache:
        _cache.move_to_end(key)
        last_engine = "cache"
        return io.BytesIO(_cache[key])   # fresh buffer — discord.File consumes it

    try:
        html_doc = build_html(blade, parts)
        async with _render_sem:
            try:
                png = await _render_once(html_doc)
            except Exception:
                # Browser likely died between the connect-check and the render
                # (panel restart / OOM). Force a relaunch and retry exactly once.
                global _browser, _context
                _browser, _context = None, None
                png = await _render_once(html_doc)

        if key:
            _cache[key] = png
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
        last_engine = "playwright"
        last_playwright_error = ""
        return io.BytesIO(png)

    except Exception as exc:            # noqa: BLE001 — never break a command
        last_playwright_error = f"{type(exc).__name__}: {exc}"
        log.warning("playwright info card failed for %r: %s — falling back to Pillow",
                    blade.get("name"), exc)

    # ── Pillow fallback: no Chromium needed, deploys on any panel ────────────
    try:
        from utils.info_card_pillow import render_info_card_pillow
        buf = render_info_card_pillow(blade, parts)
        if buf is not None:
            last_engine = "pillow"
            if key:
                _cache[key] = buf.getvalue()
                while len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
                buf = io.BytesIO(_cache[key])
        return buf
    except Exception as exc:            # noqa: BLE001
        log.warning("pillow info card failed for %r: %s", blade.get("name"), exc)
        last_engine = "none"
        return None


async def shutdown() -> None:
    """Close the shared browser. Safe to call more than once."""
    global _playwright, _browser, _context
    try:
        if _context is not None:
            await _context.close()
    except Exception:
        pass
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception:
        pass
    _browser = _context = _playwright = None
