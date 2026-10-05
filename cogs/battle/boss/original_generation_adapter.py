"""Project boss fighters into the shared Original Generation skill runtime.

The projection owns no new effect definitions: activation, timers, energy,
resource riders, and terminal damage are the same class used by PvP/Story.
Snapshots are deep-copied by Fighter.clone for counter-AI search isolation.
"""
from types import SimpleNamespace
from ..original_generation import OriginalGeneration, SkillState
from ..status_manager import StatusManager


def project(a, b, engine, moves):
    if not b.avatar_card.get('active_battle_skills'):
        return None
    fighters = {'a': a, 'b': b}
    snapshot = b.avatar_combat_state
    context = SimpleNamespace(
        round=b.combat_round + 1, finished=not a.alive() or not b.alive(),
        hp={k:f.hp for k,f in fighters.items()},
        max_hp_per_player={k:f.max_hp for k,f in fighters.items()},
        moves=moves, avatar_cards={'a':{}, 'b':b.avatar_card},
        blades={k:{'name':f.name, 'type':f.bey_type, 'stats':{
            'attack':f.attack, 'defense':f.defense, 'stamina':f.stamina_stat}}
            for k,f in fighters.items()},
        avatar_bonuses={}, type_gimmicks=engine,
        og_previous_moves=dict(snapshot.get('previous', {})))
    context.status = StatusManager(context)
    context.status.active_buffs = snapshot.setdefault('buffs', {})
    context.status.shield_hp = snapshot.setdefault('shields', {})
    context.stamina_manager = SimpleNamespace(
        stamina={k:f.sp for k,f in fighters.items()},
        cap_for=lambda k: fighters[k].sp_max)
    stability = context.stability_manager = SimpleNamespace(
        max={k:f.max_stability for k,f in fighters.items()},
        stability={k:f.stability for k,f in fighters.items()})
    states = snapshot.setdefault('states', {'b':SkillState()})
    og = context.original_generation = OriginalGeneration(context, states=states)
    og.debuffs = snapshot.setdefault('debuffs', {})
    def apply_stability(key, delta):
        logs=[]
        old=stability.stability[key]
        delta=og.stability_delta(key, old, delta, logs)
        stability.stability[key]=max(0,min(stability.max[key],old+delta))
        return logs
    stability._apply=apply_stability
    context.effective_stats_for=lambda k:{
        'attack':fighters[k].eff_attack,
        'defense':fighters[k].eff_defense,
        'stamina':fighters[k].eff_stamina,
        'level':fighters[k].level}
    context.attack_manager=SimpleNamespace(_apply_passive_reduction=lambda key, blade, damage, logs:(
        max(0,damage*(1-min(.95,max(0,fighters[key].incoming_reduction)))-max(0,fighters[key].incoming_flat_reduction)),logs))
    def rider_absorb(key, damage):
        state=fighters[key].state
        if state and hasattr(state, 'absorb') and damage > 0:
            damage,_=state.absorb(damage)
        return damage
    context.rider_absorb=rider_absorb
    context.ability=SimpleNamespace(tactical=None)
    return Bridge(a,b,og)


class Bridge:
    def __init__(self, a,b,og):
        self.fighters={'a':a,'b':b}
        self.og=og
        self.logs=[]
        self.split_keys=set()

    def begin(self):
        for f in self.fighters.values(): f.avatar_stat_multipliers={}
        self.og.begin_round(self.logs)
        for key,f in self.fighters.items():
            f.avatar_stat_multipliers={stat:self.og.stat_multiplier(key,stat) for stat in ('attack','defense')}
        self.sync_to_fighters()

    def cost(self,key,move,base):
        return self.og.cost(key,move,base)

    def offensive(self,key,other,move,damage):
        self.refresh_resources()
        damage=self.og.before_damage(key,other,move,damage,self.logs)
        self.sync_to_fighters()
        return damage

    def refresh_resources(self):
        for key,f in self.fighters.items():
            self.og.s.hp[key]=f.hp
            self.og.s.stamina_manager.stamina[key]=f.sp
            self.og.s.stability_manager.stability[key]=f.stability

    def defensive(self,key,other,move,damage):
        self.refresh_resources()
        damage=self.og.mitigate(key,other,move,damage,self.logs)
        remaining,bypass=self.og.shield(key,other,damage,self.logs)
        remaining-=self.og.s.status.absorb_shield(other,int(remaining))
        if move=='attack' and self.og.effect(key,'tiger_claw'):
            self.og.states[key].effects.pop('tiger_claw',None)
            self.split_keys.add(key)
        self.sync_to_fighters()
        return remaining+bypass

    def sync_to_fighters(self):
        context=self.og.s
        for key,f in self.fighters.items():
            f.hp=context.hp[key]
            f.sp=context.stamina_manager.stamina[key]
            f.stability=context.stability_manager.stability[key]

    def end(self, moves, actual_a, actual_b):
        context=self.og.s
        for key,f in self.fighters.items():
            context.hp[key]=f.hp
            context.stamina_manager.stamina[key]=f.sp
            context.stability_manager.stability[key]=f.stability
        self.og.committed('a','b',moves['a'],actual_b,self.logs)
        self.og.committed('b','a',moves['b'],actual_a,self.logs)
        self.og.end_round(self.logs)
        self.sync_to_fighters()
        for f in self.fighters.values():
            guard = getattr(f.state, 'guard_hp', None) if f.state else None
            if guard:
                f.hp=float(guard(f.hp))
        b=self.fighters['b']
        b.avatar_combat_state['previous']=dict(context.og_previous_moves)
        for f in self.fighters.values(): f.avatar_stat_multipliers={}
