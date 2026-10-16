
import csv
from collections import defaultdict
from pathlib import Path

DATA = Path("data/raw/1.60.1.70338")


def load(name):
    with (DATA / f"{name}.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))


trees = load("TraitTree")
nodes = load("TraitNode")
mappings = load("TraitNodeXTraitNodeEntry")
entries = load("TraitNodeEntry")
definitions = load("TraitDefinition")
spell_names = load("SpellName")

# Discover the spell-name column instead of assuming its name.
name_columns = [
    c for c in (spell_names[0].keys() if spell_names else [])
    if "name" in c.lower()
]

print("SpellName columns:", list(spell_names[0].keys())
      if spell_names else [])
print("Candidate name columns:", name_columns)

if not name_columns:
    raise SystemExit(
        "No name-like column found in SpellName. "
        "Inspect its header before continuing."
    )

name_column = next(
    (c for c in name_columns if c.lower() == "name_lang"),
    name_columns[0],
)

spell_name_by_id = {
    row["ID"]: row.get(name_column, "")
    for row in spell_names
}

definition_by_id = {row["ID"]: row for row in definitions}
entry_by_id = {row["ID"]: row for row in entries}

mappings_by_node = defaultdict(list)
for mapping in mappings:
    mappings_by_node[mapping["TraitNodeID"]].append(mapping)

nodes_by_tree = defaultdict(list)
for node in nodes:
    nodes_by_tree[node["TraitTreeID"]].append(node)

print("\n=== TREE TALENT INVENTORY ===")

for tree in sorted(trees, key=lambda row: int(row["ID"])):
    tree_id = tree["ID"]
    tree_nodes = nodes_by_tree[tree_id]

    print(
        f"\n--- Tree {tree_id}: "
        f"{len(tree_nodes)} nodes; "
        f"TraitSystemID={tree['TraitSystemID']} ---"
    )

    for node in sorted(
        tree_nodes,
        key=lambda row: (
            int(row["PosY"]), int(row["PosX"]), int(row["ID"])
        ),
    ):
        node_entries = mappings_by_node[node["ID"]]
        talents = []

        for mapping in node_entries:
            entry = entry_by_id.get(mapping["TraitNodeEntryID"])
            if not entry:
                talents.append("[MISSING ENTRY]")
                continue

            definition = definition_by_id.get(
                entry["TraitDefinitionID"]
            )

            if not definition:
                talents.append(
                    f"[NO DEFINITION: entry {entry['ID']}, "
                    f"definition {entry['TraitDefinitionID']}]"
                )
                continue

            spell_id = definition["SpellID"]
            name = (
                definition.get("OverrideName_lang", "").strip()
                or spell_name_by_id.get(spell_id, "").strip()
                or f"[Unknown spell {spell_id}]"
            )

            talents.append(
                f"{name} (rank {entry['MaxRanks']}, "
                f"entry {entry['ID']})"
            )

        print(
            f"  node={node['ID']} "
            f"pos=({node['PosX']},{node['PosY']}) "
            f"type={node['Type']} flags={node['Flags']}: "
            + " | ".join(talents or ["[no entry]"])
        )
