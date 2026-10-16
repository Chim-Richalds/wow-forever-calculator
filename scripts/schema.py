import json
from pathlib import Path

reports = sorted(Path("data/manifests").glob("*-inspection.json"))
if not reports:
    raise SystemExit("No inspection report found.")

report = json.loads(reports[-1].read_text(encoding="utf-8"))

wanted = {
    "TraitTree",
    "TraitNode",
    "TraitNodeEntry",
    "TraitNodeXTraitNodeEntry",
    "TraitDefinition",
    "TraitEdge",
    "SpellEffect",
}

for table in report["tables"]:
    if table["table"] in wanted:
        print(f"\n## {table['table']}")
        print("Status:", table["status"])
        print("Rows:", table.get("row_count"))
        print("Columns:", json.dumps(table.get("columns", [])))
        print("Sample:", json.dumps(
            (table.get("sample_rows") or [])[:1],
            ensure_ascii=False
        ))
