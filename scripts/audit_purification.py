
import csv
import json
from pathlib import Path

root = Path("data/raw/1.60.1.70338")
out = Path("data/processed/purification-audit.json")

def read_csv(name):
    with (root / f"{name}.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))

definitions = [
    row for row in read_csv("TraitDefinition")
    if row["SpellID"] == "16178"
]

spells = [
    row for row in read_csv("Spell")
    if row["ID"] == "16178"
]

effects = [
    row for row in read_csv("SpellEffect")
    if row["SpellID"] == "16178"
]

entries = read_csv("TraitNodeEntry")
mappings = read_csv("TraitNodeXTraitNodeEntry")
nodes = read_csv("TraitNode")

definition_ids = {row["ID"] for row in definitions}
entry_rows = [
    row for row in entries
    if row["TraitDefinitionID"] in definition_ids
]
entry_ids = {row["ID"] for row in entry_rows}

mapping_rows = [
    row for row in mappings
    if row["TraitNodeEntryID"] in entry_ids
]

node_ids = {row["TraitNodeID"] for row in mapping_rows}
node_rows = [row for row in nodes if row["ID"] in node_ids]

report = {
    "talent": "Purification",
    "spell_id": 16178,
    "definitions": definitions,
    "spell_records": spells,
    "effects": effects,
    "entries": entry_rows,
    "mappings": mapping_rows,
    "nodes": node_rows,
    "effect_count": len(effects),
    "verification": {
        "description_found": bool(spells),
        "definition_found": bool(definitions),
        "has_effect_rows": bool(effects),
        "mechanics_verified_for_forever": False
    }
}

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, indent=2), encoding="utf-8")

print(f"Report: {out}")
print(f"Definitions: {len(definitions)}")
print(f"Entries: {len(entry_rows)}")
print(f"Nodes: {len(node_rows)}")
print(f"Effects: {len(effects)}")
print("Description:", spells[0]["Description_lang"] if spells else "MISSING")
