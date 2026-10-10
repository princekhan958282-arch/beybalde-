# Tournament V2 — Phase 1 host panel

`;tournament` (alias `;tourney`) and `/tournament` open one public Discord
Components V2 container. Only its host can interact, and that host must retain
the configured host allowlist, Tournament Admin role, master permission, or
bot-owner permission. The panel shows all eight current values as its live
Tournament Preview, with Draft status and one Edit button beside each label and current value,
followed by a separate live preview and Confirm Setup, Reset, and Cancel Setup.
This follows the supplied form order using native Discord sections. Enum
editors have Back; reset requires Restore Defaults. Discord does not support
the reference image’s two-column dropdowns or custom field styling.

Names and fees use modals. Successful edits refresh the same original message;
errors and authorization refusals are private. Names are 1–100 single-line
characters, escaped in the preview. Fees are decimal non-negative integers,
bounded to a signed 64-bit integer. No fee is charged in Phase 1.

All requested format, slot, victory, level, combo, and start-rule options are
editable. Confirm saves a guild draft in installation-local SQLite
`data/tournament_drafts.db`, separate from legacy lobby/match state. A new panel
loads the saved guild draft. Only confirmation persists edits. Cancel and the
10-minute timeout discard unsaved changes and release the setup reservation.
A saved draft can be reopened by an authorized host in that guild; only one
editor or active legacy tournament is allowed at a time. Reload closes editors.

The legacy runner, registration components, battle engine, brackets, and match
orchestration are unchanged. The new editor exposes no Join or Start control,
and admin force-start cannot start a draft. Admin announce posts the draft
editor; its old optional message content is omitted for Components V2
compatibility. Admin cancel can also close a draft editor.

## Validation

- discord.py 2.6.4: 37 new headless interaction tests pass.
- `python tools/sim_tournament.py`: 100 pass, 1 existing failure: draft pool
  includes Boss Fighter and Limited rarities without explicit rarity weights.
  The script's view-construction harness now runs inside an event loop, as
  required by discord.py 2.6, and its command assertions check the new draft
  behavior. The runner regression assertions remain.
- Full `python -m pytest -q` before the last six added tests: 505 pass,
  5 skip, 4 fail, 1 collection error, with 566 passing subtests. All four
  failures and the error reproduce on the unchanged base commit:
  - `RealRounds::test_avatar_special_riders_receive_type_mitigation`
  - `RealRounds::test_special_reduction_and_no_type_critical`
  - `LiveBoundary::test_authored_chance_and_enemy_suppression_before_roll`
  - `InfoComponentTests::test_viewer_lookup_includes_bundled_non_shop_parts`
  - `tools/test_avatar_roster.py::test`: pytest requests a nonexistent `self` fixture.
- Compilation and `git diff --check` pass.

Tests use real discord.py UI objects and mocked Discord interactions. A live
Discord server visual and network smoke test has not been performed.

## Deferred

Registration, bracket generation from V2 configs, double elimination and round
robin execution, 32/64-player execution, multi-battle victory targets,
equalized levels, combo enforcement, ready/host approval, and fee collection
remain for later phases. Confirmed configs remain drafts regardless of their
selected values. No Phase 2 execution adapter is included.
