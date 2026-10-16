
import csv
from collections import defaultdict
from pathlib import Path

DATA = Path("data/raw/1.60.1.70338")


def load(name):
    with (DATA / f"{name}.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))


trees = {r["ID"]: r for r in load("TraitTree")}
nodes = load("TraitNode")
entries = {r["ID"]: r for r in load("TraitNodeEntry")}
definitions = {r["ID"]: r for r in load("TraitDefinition")}
mappings = load("TraitNodeXTraitNodeEntry")
edges = load("TraitEdge")

mapped_by_node = defaultdict(list)
for m in mappings:
    mapped_by_node[m["TraitNodeID"]].append(m)

nodes_by_id = {n["ID"]: n for n in nodes}

print("=== NODES WITHOUT ENTRIES ===")
for node in nodes:
    if not mapped_by_node[node["ID"]]:
        print({
            "node": node,
            "tree": trees.get(node["TraitTreeID"]),
            "edges": [
                e for e in edges
                if node["ID"] in (
                    e["LeftTraitNodeID"], e["RightTraitNodeID"]
                )
            ],
        })

print("\n=== NODES WITH MULTIPLE ENTRIES ===")
for node_id, rows in mapped_by_node.items():
    if len(rows) > 1:
        node = nodes_by_id[node_id]
        print({
            "node": node,
            "tree": trees.get(node["TraitTreeID"]),
            "mappings": rows,
            "entries": [entries.get(r["TraitNodeEntryID"]) for r in rows],
        })

print("\n=== ENTRIES WITH MISSING DEFINITIONS ===")
for entry_id, entry in entries.items():
    definition_id = entry["TraitDefinitionID"]
    if definition_id not in definitions:
        print({
            "entry": entry,
            "missingDefinitionID": definition_id,
        })

print("\n=== TREE 1083 ===")
for node in nodes:
    if node["TraitTreeID"] == "1083":
        print({
            "node": node,
            "mappings": mapped_by_node[node["ID"]],
        })
