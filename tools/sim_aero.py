"""Headless sim: verify Aero Pegasus (and the generic legacy fixes) in a battle
loop, without needing discord. Run: python3 tools/sim_aero.py
"""
import json, sys, os, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- minimal discord stub (sim only; no bot needed) -------------------------
if "discord" not in sys.modules:
    _d = types.ModuleType("discord")
    for _n in ("Member", "TextChannel", "Embed", "Interaction", "File",
               "ButtonStyle", "SelectOption", "Color", "Colour", "User"):
        setattr(_d, _n, type(_n, (), {}))
    _ui = types.ModuleType("discord.ui")
    for _n in ("View", "Button", "Select", "Modal", "TextInput", "Item"):
        setattr(_ui, _n, type(_n, (), {}))
    _ui.button = _ui.select = lambda *a, **k: (lambda f: f)
    _ext = types.ModuleType("discord.ext")
    _cmds = types.ModuleType("discord.ext.commands")
    for _n in ("Bot", "Cog", "Context", "GroupCog"):
        setattr(_cmds, _n, type(_n, (), {}))
    _cmds.command = _cmds.hybrid_command = lambda *a, **k: (lambda f: f)
    _app = types.ModuleType("discord.app_commands")
    _app.command = _app.describe = lambda *a, **k: (lambda f: f)
    _d.ui, _d.ext, _d.app_commands = _ui, _ext, _app
    _ext.commands = _cmds
    sys.modules.update({"discord": _d, "discord.ui": _ui, "discord.ext": _ext,
                        "discord.ext.commands": _cmds,
                        "discord.app_commands": _app})

# --- bypass the cog package __init__ chains (they import the whole bot) -----
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg, _rel in (("cogs", "cogs"), ("cogs.battle", "cogs/battle"),
                   ("cogs.abilities", "cogs/abilities"), ("cogs.core", "cogs/core")):
    if _pkg not in sys.modules:
        _m = types.ModuleType(_pkg)
        _m.__path__ = [os.path.join(_ROOT, _rel)]
        sys.modules[_pkg] = _m

from cogs.battle.status_manager import StatusManager
from cogs.battle.stamina_manager import StaminaManager
from cogs.abilities.ability_engine import AbilityEngine

DB = json.load(open(os.path.join(_ROOT, "data", "beyblades.json"), encoding="utf-8"))


class FakeSession:
    def __init__(self, b1, b2):
        self.blades = {"1": DB[b1], "2": DB[b2]}
        self.hp = {"1": 700, "2": 700}
        self.max_hp = 700
        self.max_hp_per_player = {"1": 700, "2": 700}
        self.last_moves = {}
        self.status = StatusManager(self)
        self.stamina_manager = StaminaManager(self.blades)
        self.chain_handler = types.SimpleNamespace(resolve=lambda *a, **k: [])


def run(b1="Aero Pegasus", b2="Dranzer", rounds=10):
    s = FakeSession(b1, b2)
    eng = AbilityEngine(s)
    s.ability = eng
    print("=== SETUP ===")
    for k in ("1", "2"):
        for line in eng.setup(k, s.blades[k]):
            print(f"  P{k}: {line}")
    print("  drain_reduction:", s.stamina_manager.drain_reduction)

    moves = ["attack", "defense", "attack", "stamina", "attack",
             "special", "attack", "defense", "attack", "attack"]
    for r in range(rounds):
        mv = moves[r % len(moves)]
        s.last_moves["1"] = mv
        print(f"\n--- round {r+1}  (P1 uses {mv}) ---")
        for line in s.stamina_manager.deduct_cost("1", mv):
            print("  " + line)
        dd, dt, logs = eng.apply("1", "2", s.blades["1"], s.blades["2"],
                                 mv, "win", 100, 0)
        for line in logs:
            print("  " + line)
        print(f"  => dmg_dealt {dd} (base 100) | atk_buff "
              f"{s.status.get_buff_bonus('1','attack')} | amp "
              f"{s.status.get_dmg_amp('1'):.2f} | sta "
              f"{s.stamina_manager.stamina['1']:g}")
        s.status.tick_buffs("1", [])

    print("\n=== drain vs resistance ===")
    s2 = FakeSession("Aero Pegasus", "Dranzer")
    e2 = AbilityEngine(s2)
    s2.ability = e2
    for k in ("1", "2"):
        e2.setup(k, s2.blades[k])
    before = s2.stamina_manager.stamina["1"]
    lg = []
    e2._run_ops({"do": [{"op": "drain_stamina", "value": 2}]}, "TestDrain",
                "2", "1", "attack", 0, 0, lg)
    print(" ", lg)
    print(f"  P1 stamina {before:g} -> {s2.stamina_manager.stamina['1']:g} "
          f"(2 drain, 10% resist => expect -1.8)")


if __name__ == "__main__":
    run()
