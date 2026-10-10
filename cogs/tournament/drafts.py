"""Phase 1 configuration and durable drafts, deliberately separate from Lobby."""
from dataclasses import asdict, dataclass, replace
from contextlib import closing
from pathlib import Path
import json
import sqlite3

OPTIONS = {
    'format': ('Single Elimination', 'Double Elimination', 'Round Robin'),
    'slots': (4, 8, 16, 32, 64),
    'victory_target': (1, 2, 3, 4, 5, 7, 10),
    'level_rule': ('Actual Levels', 'Equalized Level 100'),
    'combo_lock': ('Whole Tournament', 'Per Match', 'Per Battle'),
    'match_start': ('Both Players Ready', 'Host Approval'),
}
LABELS = {
    'name': 'Tournament Name', 'format': 'Tournament Format',
    'slots': 'Player Slots', 'victory_target': 'Victory Target',
    'level_rule': 'Beyblade Level Rule', 'combo_lock': 'Beyblade Combo Lock',
    'match_start': 'Match Start Rule', 'entry_fee': 'Entry Fee',
}

@dataclass
class DraftConfig:
    name: str = 'World Blader Championship'
    format: str = 'Single Elimination'
    slots: int = 16
    victory_target: int = 3
    level_rule: str = 'Actual Levels'
    combo_lock: str = 'Whole Tournament'
    match_start: str = 'Both Players Ready'
    entry_fee: int = 0

    def validate(self):
        if not isinstance(self.name, str) or not 1 <= len(self.name.strip()) <= 100:
            raise ValueError('Tournament name must contain 1–100 characters.')
        if any(ord(c) < 32 for c in self.name):
            raise ValueError('Tournament name must be a single line.')
        for key, choices in OPTIONS.items():
            value = getattr(self, key)
            if type(value) is not type(choices[0]) or value not in choices:
                raise ValueError(f'Invalid {LABELS[key]}.')
        if type(self.entry_fee) is not int or not 0 <= self.entry_fee <= 2**63 - 1:
            raise ValueError('Entry fee must be a non-negative integer (maximum 9223372036854775807).')

    def updated(self, key, value):
        if key not in LABELS:
            raise ValueError('Unknown setting.')
        new = replace(self, **{key: value})
        new.validate()
        return new

    def display(self, key):
        value = getattr(self, key)
        if key == 'victory_target':
            return f'First to {value}'
        if key == 'entry_fee':
            return f'{value:,} Beycoins'
        return str(value)

class DraftStore:
    """One saved draft per guild. No registration or runner reads this table."""
    def __init__(self, path=None):
        self.path = Path(path or Path(__file__).resolve().parents[2] / 'data' / 'tournament_drafts.db')

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE IF NOT EXISTS drafts (guild_id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL, config TEXT NOT NULL, status TEXT NOT NULL DEFAULT \'draft\')')
        return conn

    def save(self, guild_id, host_id, config):
        config.validate()
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT INTO drafts (guild_id, host_id, config) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET host_id=excluded.host_id, config=excluded.config, status=\'draft\'',
                         (guild_id, host_id, json.dumps(asdict(config))))

    def load(self, guild_id):
        with closing(self._connect()) as conn, conn:
            row = conn.execute('SELECT host_id, config FROM drafts WHERE guild_id=? AND status=\'draft\'', (guild_id,)).fetchone()
        if not row:
            return None
        config = DraftConfig(**json.loads(row[1]))
        config.validate()
        return row[0], config
