"""Effect data for the compact two-class tree in tree_fixtures.

Mage tree 100. Each node's talent spell is 3000 + node id, its definition 2000 + node id.
Node 1 (2 ranks) is the stat talent the tests work on; helpers adjust or add effect rows.
"""

from fixtures import write_dataset
from tree_fixtures import mini_tables

EFFECT_COLUMNS = ["ID", "SpellID", "EffectIndex", "Effect", "EffectAura", "EffectBasePointsF",
                  "EffectMiscValue_0", "EffectMiscValue_1"]


def effect_tables(mutate=None):
    t = mini_tables()
    t["SpellEffect"] = (EFFECT_COLUMNS, [])
    t["TraitDefinitionEffectPoints"] = (["ID", "TraitDefinitionID", "EffectIndex", "OperationType", "CurveID"], [])
    t["CurvePoint"] = (["ID", "CurveID", "OrderIndex", "Pos_0", "Pos_1"], [])
    t["SpellName"] = (["ID", "Name_lang"], [dict(ID=str(3000 + i), Name_lang=f"Talent {i}") for i in range(1, 8)])
    t["Spell"] = (["ID", "NameSubtext_lang", "Description_lang", "AuraDescription_lang"],
                  [dict(ID=str(3000 + i), Description_lang=f"Does thing {i}.") for i in range(1, 8)])
    if mutate:
        mutate(t)
    return t


def add_effect(t, node, index=0, aura=137, base=2, misc0=0, misc1=8, row_id=None):
    t["SpellEffect"][1].append(dict(ID=str(row_id or 9000 + len(t["SpellEffect"][1])), SpellID=str(3000 + node),
                                    EffectIndex=str(index), Effect="6", EffectAura=str(aura), EffectBasePointsF=str(base),
                                    EffectMiscValue_0=str(misc0), EffectMiscValue_1=str(misc1)))


def add_curve(t, node, index, values, curve_id=None, operation=0, first_rank=1):
    cid = str(curve_id or 500 + len(t["CurvePoint"][1]) + 1)
    t["TraitDefinitionEffectPoints"][1].append(dict(ID=str(len(t["TraitDefinitionEffectPoints"][1]) + 1),
                                                    TraitDefinitionID=str(2000 + node), EffectIndex=str(index),
                                                    OperationType=str(operation), CurveID=cid))
    for i, v in enumerate(values):
        t["CurvePoint"][1].append(dict(ID=str(len(t["CurvePoint"][1]) + 1), CurveID=cid, OrderIndex=str(i),
                                       Pos_0=str(first_rank + i), Pos_1=str(v)))


def write_effects(directory, mutate=None):
    return write_dataset(directory, effect_tables(mutate))
