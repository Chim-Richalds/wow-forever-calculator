
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "data" / "raw"

REQUIRED = {
    "TraitTree": {"ID"},
    "TraitNode": {"ID", "TraitTreeID", "PosX", "PosY"},
    "TraitNodeEntry": {"ID", "TraitDefinitionID", "MaxRanks"},
    "TraitNodeXTraitNodeEntry": {
        "TraitNodeID", "TraitNodeEntryID"
    },
    "TraitDefinition": {"ID", "SpellID"},
    "TraitEdge": {"ID", "LeftTraitNodeID", "RightTraitNodeID"},
    "Spell": {"ID"},
    "SpellName": {"ID"},
    "SpellEffect": {"SpellID", "ID"},
}


def load_csv(directory, table):
    path = directory / f"{table}.csv"
    if not path.exists():
        return None, f"Missing file: {path}"

    try:
        with path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return None, f"No CSV header: {path}"

            rows = list(reader)
            return rows, None
    except (OSError, UnicodeError, csv.Error) as exc:
        return None, f"Cannot read {path}: {exc}"


def as_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def unique_index(rows, key, table, issues):
    index = {}
    duplicates = []

    for row in rows:
        value = row.get(key, "")
        if value in ("", None):
            issues.append({
                "severity": "error",
                "type": "empty_key",
                "table": table,
                "column": key,
            })
            continue

        if value in index:
            duplicates.append(value)
        else:
            index[value] = row

    if duplicates:
        issues.append({
            "severity": "error",
            "type": "duplicate_primary_key",
            "table": table,
            "column": key,
            "values": sorted(set(duplicates)),
            "count": len(duplicates),
        })

    return index


def check_fk(source_rows, source_table, source_column,
             target_index, target_table, target_column, issues,
             severity="error"):
    missing = Counter()

    for row in source_rows:
        value = row.get(source_column, "")
        if value in ("", None, "0"):
            continue
        if value not in target_index:
            missing[value] += 1

    if missing:
        issues.append({
            "severity": severity,
            "type": "orphan_foreign_key",
            "source": f"{source_table}.{source_column}",
            "target": f"{target_table}.{target_column}",
            "missing_values": dict(missing),
            "count": sum(missing.values()),
        })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory containing the extracted CSV files",
    )
    args = parser.parse_args()

    if args.data_dir:
        data_dir = args.data_dir
    else:
        builds = sorted(
            p for p in DEFAULT_RAW.iterdir() if p.is_dir()
        ) if DEFAULT_RAW.exists() else []

        if not builds:
            raise SystemExit(
                "No raw build directories found. "
                "Pass --data-dir PATH."
            )
        data_dir = max(builds, key=lambda p: p.stat().st_mtime)

    issues = []
    tables = {}
    missing_files = []

    table_names = sorted(REQUIRED)
    for name in table_names:
        rows, error = load_csv(data_dir, name)
        if error:
            missing_files.append(error)
            continue

        tables[name] = rows

        expected_columns = REQUIRED[name]
        actual_columns = set(rows[0].keys()) if rows else set()

        if not expected_columns.issubset(actual_columns):
            issues.append({
                "severity": "error",
                "type": "missing_columns",
                "table": name,
                "expected": sorted(expected_columns),
                "actual": sorted(actual_columns),
            })

    if missing_files:
        print("Missing or unreadable tables:")
        for item in missing_files:
            print(f"  - {item}")

    # Stop if the core tables are unavailable or have wrong schemas.
    core = {
        "TraitTree", "TraitNode", "TraitNodeEntry",
        "TraitNodeXTraitNodeEntry", "TraitDefinition", "TraitEdge"
    }
    if not core.issubset(tables):
        raise SystemExit("Core talent tables are missing; cannot validate.")

    indexes = {}
    for name in tables:
        if name in {"TraitTree", "TraitNode", "TraitNodeEntry",
                    "TraitDefinition", "TraitEdge", "Spell",
                    "SpellName", "SpellEffect"}:
            indexes[name] = unique_index(
                tables[name], "ID", name, issues
            )

    tree_index = indexes["TraitTree"]
    node_index = indexes["TraitNode"]
    entry_index = indexes["TraitNodeEntry"]
    definition_index = indexes["TraitDefinition"]

    check_fk(
        tables["TraitNode"], "TraitNode", "TraitTreeID",
        tree_index, "TraitTree", "ID", issues
    )
    check_fk(
        tables["TraitNodeXTraitNodeEntry"],
        "TraitNodeXTraitNodeEntry", "TraitNodeID",
        node_index, "TraitNode", "ID", issues
    )
    check_fk(
        tables["TraitNodeXTraitNodeEntry"],
        "TraitNodeXTraitNodeEntry", "TraitNodeEntryID",
        entry_index, "TraitNodeEntry", "ID", issues
    )
    check_fk(
        tables["TraitNodeEntry"], "TraitNodeEntry",
        "TraitDefinitionID", definition_index,
        "TraitDefinition", "ID", issues
    )

    if "Spell" in indexes:
        check_fk(
            tables["TraitDefinition"], "TraitDefinition", "SpellID",
            indexes["Spell"], "Spell", "ID", issues,
            severity="warning"
        )

    if "SpellEffect" in tables and "Spell" in indexes:
        check_fk(
            tables["SpellEffect"], "SpellEffect", "SpellID",
            indexes["Spell"], "Spell", "ID", issues
        )

    # Build node -> entries and tree -> nodes maps.
    mappings_by_node = defaultdict(list)
    for mapping in tables["TraitNodeXTraitNodeEntry"]:
        mappings_by_node[mapping["TraitNodeID"]].append(mapping)

    nodes_by_tree = defaultdict(list)
    for node in tables["TraitNode"]:
        nodes_by_tree[node["TraitTreeID"]].append(node)

    edges_by_node = defaultdict(list)
    for edge in tables["TraitEdge"]:
        left = edge["LeftTraitNodeID"]
        right = edge["RightTraitNodeID"]

        if left not in node_index:
            issues.append({
                "severity": "error",
                "type": "invalid_edge_endpoint",
                "edge_id": edge["ID"],
                "endpoint": "LeftTraitNodeID",
                "value": left,
            })
        if right not in node_index:
            issues.append({
                "severity": "error",
                "type": "invalid_edge_endpoint",
                "edge_id": edge["ID"],
                "endpoint": "RightTraitNodeID",
                "value": right,
            })

        edges_by_node[left].append(edge)
        edges_by_node[right].append(edge)

    # Spell names are optional; detect the name column dynamically.
    spell_names = {}
    if "SpellName" in tables:
        name_column = next(
            (c for c in tables["SpellName"][0].keys()
             if c.lower() in {"name_lang", "name", "spellname_lang"}),
            None
        ) if tables["SpellName"] else None

        if name_column:
            spell_names = {
                row["ID"]: row.get(name_column, "")
                for row in tables["SpellName"]
            }

    talent_trees = []

    for tree_id, tree in tree_index.items():
        nodes = nodes_by_tree.get(tree_id, [])
        talent_records = []
        position_counts = Counter()

        for node in nodes:
            position = (node.get("PosX"), node.get("PosY"))
            position_counts[position] += 1

            for mapping in mappings_by_node.get(node["ID"], []):
                entry = entry_index.get(mapping["TraitNodeEntryID"])
                if not entry:
                    continue

                definition = definition_index.get(
                    entry.get("TraitDefinitionID", "")
                )
                if not definition:
                    continue

                spell_id = definition.get("SpellID", "")
                name = (
                    definition.get("OverrideName_lang")
                    or spell_names.get(spell_id)
                    or f"Spell {spell_id}"
                )

                max_ranks = as_int(entry.get("MaxRanks"))
                if max_ranks is None or max_ranks < 1:
                    issues.append({
                        "severity": "error",
                        "type": "invalid_max_ranks",
                        "tree_id": tree_id,
                        "node_id": node["ID"],
                        "entry_id": entry["ID"],
                        "value": entry.get("MaxRanks"),
                    })

                talent_records.append({
                    "nodeId": node["ID"],
                    "entryId": entry["ID"],
                    "definitionId": definition["ID"],
                    "spellId": spell_id,
                    "name": name,
                    "maxRanks": max_ranks,
                    "posX": node.get("PosX"),
                    "posY": node.get("PosY"),
                    "edgeCount": len(edges_by_node[node["ID"]]),
                })

        talent_trees.append({
            "treeId": tree_id,
            "title": tree.get("TitleText_lang", ""),
            "nodeCount": len(nodes),
            "mappingCount": sum(
                len(mappings_by_node[n["ID"]]) for n in nodes
            ),
            "talentCount": len(talent_records),
            "talents": talent_records,
            "duplicatePositions": [
                {"posX": p[0], "posY": p[1], "count": count}
                for p, count in position_counts.items()
                if count > 1
            ],
        })

    report = {
        "buildDirectory": str(data_dir),
        "rowCounts": {
            name: len(rows) for name, rows in tables.items()
        },
        "treeCount": len(talent_trees),
        "trees": talent_trees,
        "issueCount": len(issues),
        "issues": issues,
    }

    output = ROOT / "data" / "processed" / "tree-validation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"\nData directory: {data_dir}")
    print(f"Trees found: {len(talent_trees)}")
    print(f"Issues: {len(issues)}")
    print(f"Report: {output}\n")

    for tree in sorted(talent_trees, key=lambda t: t["treeId"]):
        print(
            f"Tree {tree['treeId']} "
            f"({tree['title'] or 'untitled'}): "
            f"{tree['nodeCount']} nodes, "
            f"{tree['talentCount']} mapped talent records"
        )

    for issue in issues:
        print(
            f"[{issue['severity'].upper()}] "
            f"{issue['type']}: "
            f"{json.dumps(issue, ensure_ascii=False)}"
        )

    errors = sum(i["severity"] == "error" for i in issues)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
