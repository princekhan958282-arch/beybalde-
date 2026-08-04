"""
boss_card.py  —  🖼️ PNG battle card for boss fights

The info card in utils/info_card.py is 1400px wide at deviceScaleFactor 2 and
takes ~1.7s and 1.4 MB per render. That's right for a card you look at once.
It is completely wrong for a battle screen: an 81-turn fight would spend 141
seconds rendering and upload 114 MB, and Discord only allows about five message
edits per five seconds per channel anyway.

So this is a second, deliberately small template:

    640 CSS px at scale 1   (vs 720 at scale 2)
    no ability blocks, no parts grid, no long descriptions
    art capped at 150px

which lands around 200 ms and 60-90 KB — roughly a twentieth of the cost, and
cheap enough to redraw every turn.

It reuses info_card's browser instance rather than launching a second Chromium,
so the memory cost of having both is one process, not two.
"""

import io
import logging
from typing import Optional

log = logging.getLogger("beyblade_bot")

CARD_WIDTH = 640          # CSS px; scale 1 → 640px PNG


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bar_html(cur: float, mx: float, colour: str) -> str:
    pct = max(0.0, min(100.0, (cur / mx * 100.0) if mx else 0.0))
    return (f'<div class="bar"><div class="fill" '
            f'style="width:{pct:.1f}%;background:{colour}"></div></div>')


def build_html(state: dict) -> str:
    """state comes from BossFight.card_state()."""
    accent = state.get("accent", "#c77dff")
    glow   = state.get("glow", "#6a0dad")
    tint   = state.get("tint", "#140a1e")

    boss_pct = (state["boss_hp"] / state["boss_max"] * 100) if state["boss_max"] else 0
    art = state.get("art_src") or ""
    art_html = (f'<img class="art" src="{art}" alt="">' if art
                else '<div class="art noart"></div>')

    rows = []
    for m in state["party"]:
        cls = "row" + (" dead" if not m["alive"] else "") + (" active" if m["active"] else "")
        ready = '<span class="ready">✦</span>' if m["ready"] else ""
        rows.append(f'''
        <div class="{cls}">
          <div class="pname">{"▶" if m["active"] else ("✖" if not m["alive"] else "&nbsp;")}
            {_esc(m["name"])}{ready}</div>
          <div class="pbars">
            {_bar_html(m["hp"], m["max_hp"], "#4ade80")}
            <div class="sub">{m["hp"]:.0f} hp
              <span class="dot">·</span> 🌀 {m["gauge"]:.0f}/{m["gauge_max"]:.0f}
              <span class="dot">·</span> 💨 {m["sp"]:.1f}</div>
          </div>
        </div>''')

    log_html = "".join(
        f'<div class="logline">{_esc(l)}</div>' for l in state.get("log", [])[-3:])

    verdict = ""
    if state.get("verdict"):
        verdict = f'<div class="verdict">{_esc(state["verdict"])}</div>'

    line = ""
    if state.get("line"):
        line = f'<div class="quote">“{_esc(state["line"])}”</div>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:transparent;font-family:'DejaVu Sans',system-ui,sans-serif}}
  .card{{width:{CARD_WIDTH}px;background:linear-gradient(160deg,{tint},#05070c 70%);
        border:1px solid {accent}44;border-radius:18px;padding:18px 20px;
        color:#e8ecf4;box-shadow:0 0 40px {glow}33 inset}}
  .head{{display:flex;gap:16px;align-items:center}}
  .art{{width:120px;height:120px;border-radius:50%;object-fit:cover;
       border:2px solid {accent};box-shadow:0 0 22px {glow}}}
  .noart{{background:#11151f}}
  .htext{{flex:1;min-width:0}}
  .bname{{font-size:26px;font-weight:800;letter-spacing:.4px;color:#fff;
         text-shadow:0 0 16px {glow}}}
  .tag{{display:inline-block;margin-top:4px;padding:2px 10px;border-radius:999px;
       background:{accent}22;border:1px solid {accent}66;color:{accent};
       font-size:11px;letter-spacing:1.2px;text-transform:uppercase}}
  .bar{{height:12px;border-radius:99px;background:#1b2130;overflow:hidden;
       border:1px solid #262d3d}}
  .fill{{height:100%;border-radius:99px}}
  .bosshp{{margin-top:10px}}
  .bosshp .sub{{margin-top:4px;font-size:12px;color:#93a0b5}}
  .quote{{margin:12px 0 4px;font-style:italic;color:#c9d3e4;font-size:13px}}
  .sect{{margin-top:14px;font-size:11px;letter-spacing:1.6px;color:{accent};
        text-transform:uppercase}}
  .row{{display:flex;gap:12px;align-items:center;padding:7px 0;
       border-bottom:1px solid #161b26}}
  .row:last-child{{border-bottom:none}}
  .row.dead{{opacity:.42}}
  .row.active .pname{{color:{accent}}}
  .pname{{width:150px;font-size:13px;font-weight:700;white-space:nowrap;
         overflow:hidden;text-overflow:ellipsis}}
  .ready{{color:#ffd166;margin-left:4px}}
  .pbars{{flex:1}}
  .sub{{margin-top:3px;font-size:11px;color:#8d99ad}}
  .dot{{color:#3a4356;margin:0 4px}}
  .logline{{font-size:11.5px;color:#9aa6ba;padding:2px 0}}
  .verdict{{margin-top:12px;text-align:center;font-size:18px;font-weight:800;
           color:{accent};text-shadow:0 0 18px {glow}}}
  .foot{{margin-top:12px;font-size:10.5px;color:#5d6a7a;letter-spacing:.6px}}
</style></head><body>
<div class="card">
  <div class="head">
    {art_html}
    <div class="htext">
      <div class="bname">{_esc(state["boss_name"])}</div>
      <div class="tag">{_esc(state.get("tier", "boss"))}</div>
      <div class="bosshp">
        {_bar_html(state["boss_hp"], state["boss_max"], accent)}
        <div class="sub">{state["boss_hp"]:.0f} / {state["boss_max"]:.0f}
          <span class="dot">·</span> 🌀 {state["boss_gauge"]:.0f}/{state["gauge_max"]:.0f}
          <span class="dot">·</span> 💨 {state["boss_sp"]:.1f}</div>
      </div>
    </div>
  </div>
  {line}
  <div class="sect">Party ({state["alive"]}/{state["total"]})</div>
  {"".join(rows)}
  {'<div class="sect">Last exchanges</div>' + log_html if log_html else ''}
  {verdict}
  <div class="foot">TURN {state["turn"]} &nbsp;·&nbsp; {_esc(state.get("difficulty",""))}</div>
</div></body></html>"""


_context = None
_lock = None


async def _get_context():
    """Our own context at deviceScaleFactor 1.

    info_card's shared context runs at scale 2 for a crisp collectible card,
    which quadruples the pixels — using it here produced a 1280px, 447 KB
    battle card. A battle screen is glanced at once per turn and then replaced,
    so 1x is the right trade and cuts the payload to a quarter.

    The BROWSER is still shared, so this doesn't launch a second Chromium.
    """
    global _context, _lock
    import asyncio
    from utils import info_card

    if _lock is None:
        _lock = asyncio.Lock()

    if _context is not None:
        try:
            if _context.browser and _context.browser.is_connected():
                return _context
        except Exception:
            pass
        _context = None

    async with _lock:
        if _context is not None:
            return _context
        await info_card._get_context()          # ensures the browser is up
        browser = info_card._browser
        _context = await browser.new_context(
            viewport={"width": CARD_WIDTH, "height": 900},
            device_scale_factor=1,
        )
        return _context


async def render(state: dict) -> Optional[io.BytesIO]:
    """PNG bytes, or None if rendering isn't available. Never raises."""
    try:
        html = build_html(state)
        ctx = await _get_context()
        page = await ctx.new_page()
        try:
            page.set_default_timeout(8000)
            await page.set_content(html, wait_until="domcontentloaded")
            try:
                await page.wait_for_function(
                    """() => { const i = document.querySelector('.art');
                               return !i || i.tagName !== 'IMG'
                                      || (i.complete && i.naturalWidth > 0); }""",
                    timeout=3000,
                )
            except Exception:
                pass
            png = await page.locator(".card").screenshot(type="png")
        finally:
            await page.close()
        if not png:
            return None
        from utils import info_card
        # A battle frame is glanced at and replaced, so q=85 is plenty.
        data, ext = info_card.compress(png, quality=85)
        buf = io.BytesIO(data)
        buf.name = f"boss.{ext}"
        return buf
    except Exception as exc:
        log.debug(f"[boss_card] render failed: {type(exc).__name__}: {exc}")
        return None
