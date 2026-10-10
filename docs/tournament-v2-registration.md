# Tournament V2 — channel publication and registration

This follow-up to PR #228 replaces the draft-only Confirm action with:
**Confirm Setup → Select Registration Channel → Publish Registration**.
The first two actions edit the original host message. Publish creates exactly
one public Components V2 registration panel in the selected server text channel
and replaces the host controls with a link to it. Back returns to settings;
Cancel Setup and editor timeout release temporary state. Confirmed drafts remain
saved. The host must retain host/role/owner authorization at each interaction.

## Host settings

All existing V2 settings remain. Added:

- Avatars: Allowed (default) / Disabled.
- Avatar Level: Actual Levels (default) / Equalized Level. Selecting Equalized
  opens a level modal; valid levels follow `MAX_CARD_LEVEL`, currently 1–5.
  Disabling avatars hides level editing/preview without destroying the saved
  level preference. Stars and skill levels are not changed.
- Beyblade Selection: Equipped Beyblade (default) / Random Beyblade.

The form still uses per-setting Edit buttons. To stay within Discord's
40-component budget with eleven visible rows, editing/channel/reset states
show settings as static text until returning to the main form. The equalized
level is displayed with its rule in one row. The public preview uses the same
summary formatter as the host panel. Existing saved drafts gain new defaults
when read; player/ownership data is unchanged.

## Registration

Publication rechecks same-server text-channel type and bot View Channel, Send
Messages, and Read Message History permissions. One active legacy tournament,
registration, or editor per guild is allowed. Publishing reserves a durable
record before posting; failures disable any partial panel and release memory
state. Incomplete publication reservations are cleared on restart.

The public panel displays all visible settings, joined-player count, roster,
and working Join / Leave / Cancel Registration buttons. Joins reject bots,
wrong-server users, banned/busy users, full slots, duplicates, and missing
Beyblade ownership. Equipped mode additionally uses the existing equipped-blade
resolver, including boss-copy support. Random mode does not require an equipped
blade; publication requires an eligible existing draft pool.

Join/Leave updates the same public message. Errors are ephemeral. A full panel
never starts matches. Host cancellation requires typing CANCEL into a modal,
then disables controls and releases all entrants; the existing admin cancel
hook can also close registrations. Admin ban removes registered players.

Rules, channel/message IDs, and joined players are stored in a separate
registration table in the already ignored installation-local
`data/tournament_drafts.db`. Persistent custom IDs and cog_load reattach views
and busy-player state on restart, even before the channel cache is populated.
Reload detaches old views/modals without deleting durable registrations.

## Deferred execution

This change opens registration only. No V2 config is passed to the legacy
battle runner. There is no Start button or automatic launch. Avatar disabling,
level equalization, random assignment, combo locks, formats, victory targets,
and match start policies are recorded and displayed; their battle execution
remains deferred. Joining checks equipment eligibility but does not freeze or
assign battle loadouts yet. **Fees are displayed but not charged**; that is
stated on both panels. No player coins, avatars, equipment, or levels are
modified by registration.

## Validation

- 64 targeted real-discord.py/mock-interaction tests pass: both commands,
  enum/modals, old draft migration, level bounds and hidden controls, component
  budgets, confirm/channel/publish, permission changes, send failures,
  duplicates/full slots, concurrent joins, cross-guild membership, Join/Leave,
  host cancellation, stale callbacks, restart/reload, and draft-only runner guard.
- Legacy tournament simulation: 100 passed, 1 pre-existing missing-rarity-weight
  failure for Boss Fighter and Limited; legacy runner assertions preserved.
- Full pytest: results recorded in the PR. Existing four failures and one
  collection error remain in combat special mitigation/chance suppression,
  bundled component lookup, and the avatar roster's missing `self` fixture.
- Compilation and diff whitespace checks pass.

No live Discord visual or network smoke test was performed. The UI remains
Discord-native and single column; custom two-column form styling is unsupported.
