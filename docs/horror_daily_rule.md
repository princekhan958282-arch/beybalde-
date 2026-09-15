# UNKNOWN Daily Server Rule

- Auto-spawn roll remains 30% per server per 15-minute check.
- Only one UNKNOWN encounter may be active in a server at a time.
- Only one player may actually battle UNKNOWN in each server per IST calendar day.
- The daily battle lock resets automatically at 12:00 AM India Standard Time (UTC+05:30).
- The daily lock is claimed atomically when the selected player accepts BATTLE, preventing simultaneous players from starting two battles.
- State is persisted in `data/horror_state.json`, so bot restarts do not reset the daily lock.
