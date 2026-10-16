
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "data" / "raw"

TARGET_TABLES = {
    "TraitTree",
    "TraitNode",
    "TraitNodeEntry",
    "TraitNodeXTraitNodeEntry",
    "TraitDefinition",
    "TraitDefinitionEffectPoints",
    "TraitEdge",
    "CurvePoint",
    "SpellName",
    "Spell",
    "SpellEffect",
    "ChrClasses",
    "ChrRaces",
}


def inspect_csv(path: Path) -> dict:
    result = {
        "file": str(path.relative_to(ROOT)),
        "table": path.stem,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)

            headers = reader.fieldnames or []
            counts = Counter(headers)
            duplicate_headers = sorted(
                name for name, count in counts.items() if count > 1
            )

            row_count = 0
            malformed_rows = []
            empty_rows = 0
            samples = []

            for line_number, row in enumerate(reader, start=2):
                row_count += 1

                if not any(value not in (None, "") for value in row.values()):
                    empty_rows += 1

                if None in row:
                    malformed_rows.append(line_number)

                if len(samples) < 3:
                    samples.append(row)

            result.update({
                "status": "ok",
                "columns": headers,
                "column_count": len(headers),
                "row_count": row_count,
                "duplicate_headers": duplicate_headers,
                "malformed_rows": malformed_rows[:20],
                "empty_rows": empty_rows,
                "sample_rows": samples,
            })

    except (OSError, UnicodeError, csv.Error) as exc:
        result.update({
            "status": "error",
            "error": str(exc),
        })

    return result


def main():
    snapshots = sorted(
        path for path in RAW_ROOT.iterdir() if path.is_dir()
    )

    if not snapshots:
        raise SystemExit(f"No build directories found in {RAW_ROOT}")

    # Inspect the most recently modified snapshot directory.
    snapshot = max(snapshots, key=lambda path: path.stat().st_mtime)
    files = sorted(snapshot.glob("*.csv"))

    if not files:
        raise SystemExit(f"No CSV files found in {snapshot}")

    results = [inspect_csv(path) for path in files]

    report = {
        "snapshot": snapshot.name,
        "file_count": len(files),
        "expected_tables_missing": sorted(
            TARGET_TABLES - {path.stem for path in files}
        ),
        "tables": results,
    }

    report_path = ROOT / "data" / "manifests" / (
        f"{snapshot.name}-inspection.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Snapshot: {snapshot.name}")
    print(f"CSV files: {len(files)}")
    print(f"Report: {report_path}")
    print()

    for table in results:
        print(
            f"{table['table']}: "
            f"{table.get('row_count', '?')} rows, "
            f"{table.get('column_count', '?')} columns, "
            f"status={table['status']}"
        )

        if table.get("duplicate_headers"):
            print("  DUPLICATE HEADERS:", table["duplicate_headers"])

        if table.get("malformed_rows"):
            print("  MALFORMED ROWS:", table["malformed_rows"])

    print("\nMissing expected tables:")
    for name in report["expected_tables_missing"]:
        print(f"  - {name}")

    errors = [
        t for t in results
        if t["status"] != "ok"
        or t.get("duplicate_headers")
        or t.get("malformed_rows")
    ]

    if errors:
        raise SystemExit(
            f"Inspection found issues in {len(errors)} table(s)."
        )


if __name__ == "__main__":
    main()
