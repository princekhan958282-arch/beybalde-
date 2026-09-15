# Test cases
1. First selected player can accept UNKNOWN and starts the server's daily battle.
2. Any later spawn/battle in that same server is blocked until next IST date.
3. Another server has its own independent daily allowance.
4. At 00:00 IST the date key changes and the server can battle UNKNOWN again.
5. Two simultaneous accepts cannot both reserve the same server/day.
6. Bot restart does not clear the persisted daily lock.
