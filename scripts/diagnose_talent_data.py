
import csv
from collections import Counter, defaultdict
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
spells = load("Spell")
effects = load("SpellEffect")

spell_ids = {r["ID"] for r in spells}
definition_ids = {r["ID"] for r in definitions}
entry_ids = {r["ID"] for r in entries}
node_ids = {r["ID"] for r in nodes}

print("=== TREE INVENTORY ===")
nodes_by_tree = Counter(r["TraitTreeID"] for r in nodes)
maps_by_node = Counter(r["TraitNodeID"] for r in mappings)
entries_by_node = defaultdict(list)

for row in mappings:
    entries_by_node[row["TraitNodeID"]].append(
        row["TraitNodeEntryID"]
    )

for tree in trees:
    tid = tree["ID"]
    tree_nodes = [n for n in nodes if n["TraitTreeID"] == tid]
    mapped_entries = sum(
        len(entries_by_node[n["ID"]]) for n in tree_nodes
    )
    print(
        f"Tree ID={tid}, title={tree.get('TitleText_lang')!r}, "
        f"nodes={len(tree_nodes)}, mapped entries={mapped_entries}, "
        f"TraitTreeID field={tree.get('TraitTreeID')!r}, "
        f"TraitSystemID={tree.get('TraitSystemID')!r}, "
        f"first node={tree.get('FirstTraitNodeID')!r}"
    )

print("\n=== SPELL TABLE COVERAGE ===")
print("Spell rows:", len(spells))
print("Unique Spell IDs:", len(spell_ids))
print("SpellEffect rows:", len(effects))
print("Unique SpellEffect spell IDs:", len({
    r["SpellID"] for r in effects
}))

missing_effect_spells = sorted({
    r["SpellID"] for r in effects
    if r["SpellID"] not in spell_ids
}, key=lambda x: int(x) if x.isdigit() else x)

print("\nSpellEffect references absent from Spell.ID:")
for sid in missing_effect_spells:
    count = sum(r["SpellID"] == sid for r in effects)
    print(f"  SpellID={sid}, effect rows={count}")

print("\n=== TALENT DEFINITION REFERENCES ===")
for definition in definitions:
    sid = definition["SpellID"]
    if sid and sid != "0" and sid not in spell_ids:
        print(
            f"Definition ID={definition['ID']}, "
            f"SpellID={sid}, "
            f"override name={definition.get('OverrideName_lang')!r}, "
            f"visible spell={definition.get('VisibleSpellID')!r}"
        )

print("\n=== NODE AND ENTRY INTEGRITY ===")
print("Nodes with no entry mapping:", sum(
    1 for n in nodes if not maps_by_node[n["ID"]]
))
print("Mappings to missing nodes:", sum(
    1 for r in mappings if r["TraitNodeID"] not in node_ids
))
print("Mappings to missing entries:", sum(
    1 for r in mappings if r["TraitNodeEntryID"] not in entry_ids
))
print("Entries to missing definitions:", sum(
    1 for r in entries if r["TraitDefinitionID"] not in definition_ids
))

print("\n=== CSV BUILD DIRECTORY ===")
print("Data directory:", DATA.resolve())
print("Confirm every file came from the same build.")
