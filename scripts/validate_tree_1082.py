
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("data/raw/1.60.1.70338")
OUT = Path("data/processed/tree-1082-audit.json")
TREE_ID = "1082"


def load(name):
    path = DATA / f"{name}.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


trees = {r["ID"]: r for r in load("TraitTree")}
nodes = load("TraitNode")
entries = {r["ID"]: r for r in load("TraitNodeEntry")}
definitions = {r["ID"]: r for r in load("TraitDefinition")}
mappings = load("TraitNodeXTraitNodeEntry")
edges = load("TraitEdge")
spells = {r["ID"]: r for r in load("Spell")}
effects = load("SpellEffect")

tree = trees[TREE_ID]
tree_nodes = {
    n["ID"]: n for n in nodes if n["TraitTreeID"] == TREE_ID
}

mappings_by_node = defaultdict(list)
for m in mappings:
    mappings_by_node[m["TraitNodeID"]].append(m)

effects_by_spell = defaultdict(list)
for effect in effects:
    effects_by_spell[effect["SpellID"]].append(effect)

talents = []
used_entry_ids = set()
used_definition_ids = set()
used_spell_ids = set()
missing = Counter()

for node_id, node in tree_nodes.items():
    node_entries = []

    for mapping in mappings_by_node[node_id]:
        entry_id = mapping["TraitNodeEntryID"]
        entry = entries.get(entry_id)

        if entry is None:
            missing["entry"] += 1
            continue

        used_entry_ids.add(entry_id)
        definition_id = entry["TraitDefinitionID"]
        definition = definitions.get(definition_id)

        if definition is None:
            missing["definition"] += 1
            node_entries.append({
                "entry_id": entry_id,
                "definition_id": definition_id,
                "max_ranks": entry["MaxRanks"],
                "status": "missing_definition",
            })
            continue

        used_definition_ids.add(definition_id)
        spell_id = definition["SpellID"]
        used_spell_ids.add(spell_id)

        spell_exists = spell_id in spells
        spell_effects = effects_by_spell.get(spell_id, [])

        node_entries.append({
            "entry_id": entry_id,
            "definition_id": definition_id,
            "spell_id": spell_id,
            "max_ranks": int(entry["MaxRanks"]),
            "node_entry_type": entry["NodeEntryType"],
            "spell_exists": spell_exists,
            "effect_count": len(spell_effects),
            "effects": spell_effects,
            "status": (
                "ok" if spell_exists else "spell_missing"
            ),
        })

        if not spell_exists:
            missing["spell"] += 1
        if not spell_effects:
            missing["spell_effect"] += 1

    talents.append({
        "node_id": node_id,
        "x": int(node["PosX"]),
        "y": int(node["PosY"]),
        "type": node["Type"],
        "flags": node["Flags"],
        "entries": node_entries,
    })

# Keep all edge records, including reverse-direction records.
tree_edges = [
    edge for edge in edges
    if edge["LeftTraitNodeID"] in tree_nodes
    or edge["RightTraitNodeID"] in tree_nodes
]

invalid_edges = [
    edge for edge in tree_edges
    if edge["LeftTraitNodeID"] not in tree_nodes
    or edge["RightTraitNodeID"] not in tree_nodes
]

report = {
    "tree": {
        "id": TREE_ID,
        "trait_system_id": tree["TraitSystemID"],
        "node_count": len(tree_nodes),
        "entry_mapping_count": sum(
            len(mappings_by_node[n]) for n in tree_nodes
        ),
        "unique_entry_count": len(used_entry_ids),
        "unique_definition_count": len(used_definition_ids),
        "unique_spell_count": len(used_spell_ids),
        "edge_record_count": len(tree_edges),
        "invalid_edge_count": len(invalid_edges),
    },
    "missing_counts": dict(missing),
    "invalid_edges": invalid_edges,
    "talents": sorted(
        talents, key=lambda t: (t["y"], t["x"], t["node_id"])
    ),
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps(report, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

print(json.dumps({
    "output": str(OUT),
    "tree": report["tree"],
    "missing_counts": report["missing_counts"],
    "invalid_edge_count": len(invalid_edges),
}, indent=2))
