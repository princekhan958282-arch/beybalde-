# Tournament V2 — channel publication, registration, and battle execution

This follow-up replaces the draft-only Confirm action with:
**Confirm Setup → Select Registration Channel → Publish Registration**.
The first two actions edit the original host message. Publish creates exactly
one public Components V2 registration panel in the selected server text channel
and replaces the host controls with a link to it. Back returns to settings;
Cancel Setup and editor timeout release temporary state. Confirmed drafts remain
saved. The host must retain host/role/owner authorization at each interaction.

## Host settings

All existing V2 settings remain:

- Avatars: Allowed (default) / Disabled.
- Avatar Level: Actual Levels (default) / Equalized Level. Selecting Equalized
  opens a level modal; valid levels follow `MAX_CARD_LEVEL`, currently 1–5.
  Disabling avatars hides level editing/preview without destroying the saved
  level preference. Stars and skill levels are not changed.
- Beyblade Selection: Equipped Beyblade (default) / Random Beyblade.

The form uses per-setting Edit buttons. To stay within Discord's
40-component budget with eleven visible rows, editing/channel/reset states
show settings as static text until returning to the main form. The equalized
level is displayed with its rule in one row. The public preview uses the same
summary formatter as the host panel. Existing saved drafts gain new defaults
when read; player/ownership data is unchanged.

## Registration & 24-Hour Duration

Publication rechecks same-server text-channel type and bot View Channel, Send
Messages, and Read Message History permissions. One active tournament,
registration, or editor per guild is allowed. Publishing reserves a durable
record before posting; failures disable any partial panel and release memory
state. Incomplete publication reservations are cleared on restart.

- **Duration Window:** Registration is open for 24 hours from publication (`created_at`).
  The panel displays a live countdown via Discord's relative timestamp (`<t:TIMESTAMP:R>`).
- **Auto-Notification on Full:** When all player slots are filled (`len(entrants) == config.slots`),
  the bot automatically sends an announcement pinging the tournament host in the channel,
  letting them know registration is full and they can start.
- **24-Hour Expiry Monitor:** If 24 hours elapse and $\ge 2$ players have registered,
  the registration status moves to `closed` and prompts the host to start. If fewer than
  2 players joined within 24 hours, the registration is automatically cancelled and any
  collected entry fees are refunded.
- **Entry Fees & Refunds:** If an entry fee is configured, coins are checked and deducted
  atomically upon joining. If a player leaves or the tournament is cancelled, fees are
  refunded automatically.

The public panel displays all visible settings, joined-player count, roster, live
deadline, and four active buttons: `Join`, `Leave`, `Start Tournament`, and `Cancel Registration`.

## Bracket Execution & Payouts

- **Start Tournament Button:** The host (or an admin) can start the tournament whenever
  at least 2 players have joined.
- **Manual Battle Execution:** Tournament matches are manual, not auto-simulated:
  - When matches are ready, the bot announces each match in the tournament channel or dedicated match thread.
  - **Manual Command:** Players run `;battle @opponent` to fight their tournament match. The tournament's `on_beycord_battle_blades` listener automatically detects the battle outcome and advances the bracket.
  - **Match Controls View:** Each match announcement includes:
    - `⚔️ Battle Now`: Allows participants or the host to launch the interactive move-selection battle directly.
    - `🏳️ Forfeit`: Allows a player to forfeit, awarding the win to their opponent.
    - `👑 Advance Player`: Host/Admin tool to force-advance a player if their opponent is absent or offline.
  - **Host Commands:** Hosts can also advance players via `;tourney_win @member` (or `/tourney_win`) and view live brackets via `;tourney_bracket`.
- **Rules Forwarded to Battle:**
  - `Avatars: Allowed / Disabled` and `Equalized Level` are passed directly to `BattleSession`.
  - `Level Rule: Equalized Level 100` normalizes combat stats.
  - `Beyblade Selection: Random Beyblade` drafts random balanced blades for all participants.
- **Thread Isolation:** Matches create dedicated Discord threads (`⚔️ R{rnd}: P1 vs P2`)
  to prevent spam in the main tournament channel, with automatic fallback to the channel.
- **Prize Distribution:**
  - Pools base bot rewards (`1,500` coins/entrant) and collected entry fees into a total prize pot.
  - Awards 1st, 2nd, and 3rd place champions atomically via `mutate_user` with celebratory announcements.

## Validation

- Full test suite passes: registration publication, component budgets, join/leave,
  Start Tournament button permissions & validation, full notification, 24h expiration,
  entry fee deduction/refunds, bracket generation, thread isolation, and champion payouts.
