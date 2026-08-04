# Beycord — Tournament update

Is zip ko bot ke **root folder** mein extract karo (jahan `app.py` hai).
Paths already sahi hain, bas overwrite ho jayega:

```
app.py                      <- slash tree sync added
cogs/admin/admin.py         <- ;sync command added
cogs/economy/profile.py     <- ;profile now sends a card + hybrid
cogs/extras/quests.py       <- ;claim collision fix + hybrid
cogs/extras/tournament.py   <- rewritten
utils/profile_card.py       <- NEW file
utils/tournament_card.py    <- NEW file
```

## Restart ke baad

1. Startup log check karo — `❌ Failed to load` koi nahi hona chahiye.
2. `;sync` maaro (master only) — current server mein slash commands turant aa jayenge.
   Global sync apne aap startup pe hota hai par Discord ko 1 ghanta lag sakta hai.

## Kya badla

**Quests fix** — `cogs/extras/quests.py` ka `;claim` spawn cog ke `;claim` se
takra raha tha, isliye pura QuestsCog load hi nahi hota tha (`;quests` bhi dead).
Ab wo `;questclaim` / `;qclaim` hai.

**Slash** — sab hybrid hai, prefix aur slash dono chalte hain.

**Prize pool — kuch bhi daal sakte ho**

```
/tournament create size:8 entry_fee:500 prize:"MLBB Weekly Diamond Pass" split:50-30-20
```

- `prize` free text hai — MLBB weekly, diamonds, UC, nitro, custom role, kuch bhi.
  Bot sirf announce karta hai aur card pe dikhata hai; actual reward host deta hai.
- `split` se coin pot divide hota hai: `winner` (default) / `70-30` / `50-30-20`.
  2nd = final haarne wala, 3rd = dono semi-final haarne wale (aapas mein baantte hain).
  Agar bracket chhota hai aur 3rd place exist hi nahi karta, wo hissa champion ko
  chala jaata hai — pot kabhi gayab nahi hota.

**Random bey draft** — default on. `begin` pe har player ko rarity-weighted random
bey milta hai, duplicates avoid karke. Join karne ke liye equipped bey ki zaroorat
nahi. `mode:equipped` se purana behaviour.

**Card** — pot, entry fee, player count, prize ribbon + payout breakdown, aur har
match ka row (dono players, unka bey, rarity chip, winner highlight, BYE handling).

**Profile card** — `;profile` / `/profile [@user]` ab ek rendered card bhejta hai:
naam + rank tier + level, XP bar, rank-score bar (next tier tak), wins / losses /
win rate / streak / best / coins, collection bar, aur active bey ka art + rarity +
type + ATK/DEF/STA/HP bars.

Theme **demo 4 pe locked** hai — har card ko wahi amber frame / glow / avatar
accent milta hai, chahe player ka rank kuch bhi ho. Rank chhupta nahi: tier chip
aur rank-score bar apne tier ke colour mein hi rehte hain. Wapas per-tier accent
chahiye to `utils/profile_card.py` mein `THEME_LOCKED = False` kar do.

Pehle `;profile` bina equipped bey ke poora refuse kar deta tha — ab card phir bhi
banta hai, bey wale panel mein "NOTHING EQUIPPED" dikhta hai. Render fail ho to
purana embed fallback hai.

## Prefix note

Slash use karna aasan hai. Prefix se multi-word prize dena ho to quotes lagao:

```
;tournament create 8 500 random "MLBB Weekly Diamond Pass" 50-30-20
```

## Baaki

`Procfile.txt` mein abhi bhi `worker: python main.py` likha hai par file `app.py`
hai — agar panel Procfile use karta hai to usko `python app.py` kar do.

`app.py` line ~36 pe bot token hardcoded fallback hai. Use Discord dev portal se
regenerate karke sirf `.env` mein rakho.


## Setting your bot token

Pick **one**. All three work on any host — the last two are just files sitting
next to `app.py`, so your panel does not need "environment variable" support.

**Option 1 — `.env` file (recommended)**

In the panel file manager, create a file called `.env` next to `app.py`:

```
BOT_TOKEN=your_token_here
GEMINI_API_KEY=your_gemini_key_here
```

`app.py` already loads this (`python-dotenv` is in requirements.txt). Because
`.env` is never included in a zip, re-uploading the bot won't overwrite it and
your token can't leak when you share a build.

**Option 2 — panel Startup variables**

Pterodactyl: Startup tab → add `BOT_TOKEN`. Read automatically.

**Option 3 — `config_local.py` (hardcoded)**

```
cp config_local.example.py config_local.py
```

Then edit it and paste your keys. Excluded from every zip and in `.gitignore`.

Order of precedence: environment variable → `.env` → `config_local.py`.

> Don't paste keys directly into `app.py`. That file ships in every zip and
> screenshot — a token that leaks that way gives someone full control of the
> bot until you reset it.

## Dependencies — ab apne aap install hote hain

Startup pe `utils/bootstrap.py` `requirements.txt` ke against check karta hai
aur jo purana ya missing hai use install kar deta hai, phir ek baar restart
karta hai taaki naya version load ho. Panel pe manually pip chalane ki zaroorat
nahi.

- Band karna ho to Startup variables mein `BEYCORD_AUTO_INSTALL=0` set kar do.
- Install fail hua (network nahi, pip nahi, permissions) to bot **band nahi
  hota** — log mein exact command likh deta hai aur jo installed hai usi pe
  chal padta hai.
- Ek boot mein zyada se zyada **ek** restart hota hai, to loop nahi banega.

Pehli baar `discord.py>=2.6` install hote waqt boot thoda lamba lagega. Log
mein `[bootstrap]` lines dikhengi.

## Purane / duplicate slash commands

Startup pe bot Discord ki command list ko apni list se milata hai aur jo
commands Discord ke paas hain par bot ke paas nahi, unhe hata deta hai.

Kyun zaroori tha: `;sync` har global command ki **guild copy** banata hai, aur
us copy se kuch kabhi delete nahi hota. Command code se hatao to guild copy
wahin reh jaati hai aur uske replacement ko shadow karti hai — isi se "same
command do baar" aur "purana command jo ab hai hi nahi" dono hote hain.

- `;sync`        — globals ki copy is server mein (instant)
- `;sync global` — sab jagah (Discord ko 1 ghanta lag sakta hai)
- `;sync clean`  — sirf stale hatao, valid rehne do
- `;sync purge`  — is server ki saari copies uda do, sirf globals bachein

Band karna ho: `BEYCORD_AUTO_PRUNE=0`. Ek boot mein max 25 guilds process hote
hain, baaki agle boot pe.

## Announcement — confirm aur RSVP

`/announcement` ab do naye cheezein deta hai:

- **Confirm step**: `🚀 Send announcement` dabate hi bhejta nahi — pehle "kisko,
  kitno ko, kya text" dikhata hai, tabhi `🚀 Confirm` se asli bhejta hai.
  `⬅️ Back` se composer pe wapas jaa sakte ho, kuch bhejega nahi.
- **DM RSVP**: DM target chuno to har player ki DM mein `✅ I'll be there` /
  `❌ Can't make it` buttons aate hain. Wahi apna jawab badal bhi sakte hain.
  Bhejne ke baad jo confirmation dikhta hai usme **`📊 Check responses`**
  button hai — kabhi bhi dabao, live count milega (Coming / Not coming / No
  answer yet). Ye button restart ke baad bhi kaam karta hai (7 din tak).

## Slash commands ab grouped hain

Top-level slash entries 9 se **5** ho gayi:

- `/casino` — balance, daily, leaderboard, exchange, give, take, games, menu, **play**
- `/player` — profile, quests, claim, inventory, balance, achievements, mastery
- `/tournament` — create, join, start, end, cancel, bracket, ... (19 subs)
- `/match` — checkin, report, dispute
- `/announcement` — send, history

**`/casino play`** ek hi subcommand hai jo saare 18 games launch karta hai,
autocomplete ke saath. Har game ka alag subcommand isliye nahi banaya kyunki
Discord ek group mein max **25 subcommands** allow karta hai aur games usse
zyada ho sakte hain — autocomplete search bhi karta hai, flat list nahi karti.

**Saare `;` prefix commands bilkul waise hi hain.** `;profile`, `;quests`,
`;blackjack`, `;casinomenu` — kuch nahi badla. Sirf slash side group hui hai.

## `/admin` — saare admin actions ek command mein

Pehle 17 admin subcommands alag-alag picker lines lete the (`/tournament create`,
`/tournament ban_player`, `/casino give`, …). Ab sab ek `/admin` ke andar hain —
picker mein **ek hi line**, action dropdown se chuno.

```
/admin action:<dropdown>  [target] [user] [user2] [amount] [text] [extra]
```

Actions: create · start · end · cancel · pause · resume · force_win ·
reschedule · replace_player · ban_player · unban_player · set_reward ·
broadcast · announce · announce_history · casino_give · casino_take

Dropdown mein har action ke saath likha hai ki usko kaunse parameter chahiye.
Koi parameter reh gaya to command chalne se **pehle** saaf error milta hai —
jaise *"🚫 Ban from tournaments also needs `user`, `text`."*

Player-facing commands waise hi hain: `/tournament join`, `/casino play`,
`/player profile` waghairah. Aur saare `;` prefix commands bhi.

## Deploy sach mein laga ya nahi — `;version`

Panel zip ko **adhoora** extract kar sakta hai — `cogs/` update ho jaye aur
`utils/` purana reh jaye. Phir naye cogs purane utils ko call karte hain aur
ajeeb error aata hai (jaise *"MySQLStore object has no attribute
'all_user_ids'"*, jo database ki problem lagti hai par asal mein ek file
copy nahi hui thi).

Bot ab **startup pe khud check** karta hai aur log mein saaf likhta hai:

```
[build] Beycord v54 · python 3.12.3 · discord.py 2.6.x
[build] self-check passed — all modules agree.
```

Gadbad ho to:
```
[build] STORE MISMATCH — MySQLStore is missing: all_user_ids
[build] This usually means utils/ didn't update. Re-upload the whole zip.
```

`;version` kabhi bhi chala ke dekh sakte ho — build, python, discord.py,
DB backend aur self-check ka result.

**Agar mismatch dikhe:** poora zip dobara upload karo (sirf `cogs/` nahi),
saare `__pycache__` folders delete karo, phir restart.

## Parts aur Avatar ab har jagah asar karte hain

Pehle equipped parts aur avatar sirf PvP mein lagte the. Profile card, info
card aur boss fight teeno **raw blade** use karte the — isliye kuch equip
karne se koi farak nahi dikhta tha.

Ab teeno `utils/loadout.py` ke `effective_blade()` se guzarte hain. Ek hi
jagah sach hai, to dobara drift nahi hogi.

- Boss copy pe parts **nahi** lagte (farmed copy boss se strong ho jaati)
- Avatar ke timing wale effects (dodge, counter, nth-hit) battle engine ke
  paas hi rehte hain — sirf flat/percent stats blade mein fold hote hain

## Boss changes (v55)

- Defence buff: NEMESIS 135 → **165**, Drakos 128 → **155**
- Gauge full hote hi boss **hamesha** Special use karta hai
- NEMESIS 4p HP cap **4800** (pehle 6400 aur badhta hi jaata)
- **NEMESIS PROTOCOL** ab opener hai — pehli full charge pe, poori fight mein
  ek hi baar, aur sabse strong player pe

## Boss lock aur daily limit (v56)

- **NEMESIS locked hai** jab tak Drakos clear na ho. `BOSS_REQUIRES` mein hai
  (`boss_battle.py`) — badalna ho to ek line.
- **Har boss ek din mein ek baar.** Rolling 24h, `;daily` jaisa hi.

Do cheezein jaanbujh ke:

- Attempt **fight shuru hone pe** kata jaata hai, jeetne pe nahi — warna haarne
  ke baad infinite retry mil jaati.
- **Poori party** ka attempt kata hai, sirf host ka nahi — warna 4 dost baari
  baari host karke boss farm kar lete.
- Limit **profile mein save** hoti hai, memory mein nahi — bot restart hone se
  free attempt nahi milta.

`;boss` list ab har boss ke saath status dikhati hai: `✅ Ready today` /
`🌙 Again in 6h 20m` / `🔒 Beat Aetherion Drakos first`.

## Boss opening stamina (v57)

Boss ab **35 stamina** se shuru hota hai (pehle 10). `BOSS_START_SP` in
`boss_battle.py`.

Ye ceiling se upar hai jaanbujh ke — `ai.STAMINA_MAX` 15 hai, aur regen usi tak
top-up karta hai. Matlab 35 ek **opening reserve** hai: boss shuru mein bina
ruke act kar sakta hai, phir reserve khatam hone ke baad player wali hi economy
mein aa jaata hai. `STAMINA_MAX` badhaana iske bajaye boss ko hamesha 35 tak
top-up karne deta — woh bahut bada buff hai.

Saath mein ek regen bug fix hua: `min(STAMINA_MAX, sp + regen)` neeche ki taraf
bhi clamp karta tha, to ceiling se upar shuru hone wale ka poora surplus pehle
hi turn mein gayab ho jaata tha. Ab regen sirf **add** karta hai.

## NEMESIS PROTOCOL damage (v58)

`mult` ab **3.00 (300%)** hai — `boss_abilities.py` ke `SPECIALS["zerohour"]`
mein.

Dhyan rahe ye pehle **3.60 (360%)** tha, to ye halka sa **nerf** hai, buff
nahi. Aur ab ye `eclipse` (3.20) se bhi kam hai, jabki zerohour signature
ultimate hai — chaho to eclipse ko 3.00 se neeche karke order theek kar sakte
ho.

Damage 300% + `true_damage` (defence aur block dono ignore) + `execute_below`
0.35 pe +60%.

## Bey levels (v59)

Har bey ka apna level, max **100**. `utils/bey_levels.py`.

```
Stat = floor(Base + (Growth × Level) + IV + CurveBonus)
CurveBonus = floor(Level / 5) * 2
```

- **Base** = blade ka beyblades.json wala stat
- **Growth** = type archetype (Attack 3.0 atk, Defense 3.0 def, waghairah)
- **IV** = 0-10 per stat, har player ki apni copy ke liye alag, ek baar roll
- **Cap** = 300 per stat

**EXP:** chat 5-10 (**koi cooldown nahi** — har qualifying message pays) · battle win 60-100 · loss 25-40
**Notification:** har 5th level pe, stat gains ke saath

`LEVEL_OFFSET = 1` rakha hai — Lv1 = base stats, yaani abhi ka balance floor
banta hai. Spec ke literal `Growth × Level` ke liye 0 kar do (phir Lv1 bhi base
se upar hoga).

Boss copies level nahi hote — woh fixed roll hain.

## Boss timer ab 2 ghante (v59)

`DAILY_LIMIT_H = 2` in `boss_battle.py`.

## Chat EXP fix (v59)

Trainer XP chat se **kabhi milta hi nahi tha** — `grant_xp()` maujood tha aur
battles use call karte the, par chat ke liye koi listener likha hi nahi gaya
tha. Ab `cogs/economy/chat_xp.py` dono deta hai: trainer XP aur equipped bey
ka EXP.

## Chat EXP cooldown hata diya (v60)

Ab har message pay karta hai. Sirf ye filters bache hain: bots, `;` commands,
aur 3 char se chhote messages.

Wapas chahiye to `XP_CHAT_COOLDOWN_S` (`utils/bey_levels.py`) set karo aur
`chat_xp.py` mein timestamp check restore kar do — docstring mein likha hai.

## v61 changes

**EXP band 50-459** — chat 50-90, battle win 300-459, loss 120-220
(`utils/bey_levels.py`). Lv100 ab ~1,120 messages ya ~206 wins (pehle 10,400
messages).

**Base HP 4000** — `BASE_HP` (core/constants.py, PvP) aur `BASE_PLAYER_HP`
(boss_battle.py) dono. Dono saath badle kyunki kuch effects BASE_HP ka
percentage heal karte hain.

**Attack vs Attack ab flat nahi** — pehle dono ko flat 32 chip damage lagta
tha, chahe blade 47 attack ka ho ya 300 ka. Ab attacker enemy ki defence ka
**20%** apne attack mein convert karta hai, phir asli damage roll hota hai.

```
ATTACK_CLASH_DEF_CONVERSION = 0.20   # core/constants.py
ATTACK_CLASH_MULT           = 1.00
```

Doosre mirrors (Defense vs Defense, Stamina vs Stamina) waise hi hain.

## Boss system locked (v62)

`;boss`, `;bosses`, `;bossinfo` band hain — player ko milta hai:

> 🔒 **Boss Battles are closed**
> Boss Battles are being reworked and will **open in the next update**.

**Khula rakha:** `;copies`, `;copy`, `;equipcopy`, `;sellcopy`. Wajah — woh
items players ke paas pehle se hain. Unhe bhi lock karte to jiske paas copy
equipped hai woh na dekh paata, na hata paata, na bech paata.

**Owner (MASTER_ID) bypass** kar sakta hai, taaki rework live pe test ho sake.

Kholna ho: `BOSS_SYSTEM_LOCKED = False` in `boss_battle.py`. Bas. Fights niche
poori tarah salamat hain (40/40 test fights pass).

Locked cheezein: `LOCK_EXEMPT` set mein se naam nikaalo/daalo.

