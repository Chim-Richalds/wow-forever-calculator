
import csv
from pathlib import Path

ROOT = Path("data/raw/1.60.1.70338")
OUTPUT = Path("data/processed/purification-curve-audit.txt")


def read_csv(name):
    path = ROOT / name
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_section(file, title, rows):
    file.write(f"\n{'=' * 80}\n")
    file.write(f"{title}\n")
    file.write(f"{'=' * 80}\n")

    if not rows:
        file.write("(No rows found)\n")
        return

    for row in rows:
        file.write(repr(row) + "\n")


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    curve_points = read_csv("CurvePoint.csv")
    effect_points = read_csv("TraitDefinitionEffectPoints.csv")

    purification_refs = [
        row for row in effect_points
        if row.get("TraitDefinitionID") == "134186"
    ]

    curve_ids = {
        row["CurveID"]
        for row in purification_refs
        if row.get("CurveID") and row["CurveID"] != "0"
    }

    curve_point_rows = [
        row for row in curve_points
        if row.get("CurveID") in curve_ids
    ]

    with OUTPUT.open("w", encoding="utf-8") as f:
        f.write("PURIFICATION CURVE AUDIT\n")
        f.write("Talent: Purification\n")
        f.write("TraitDefinitionID: 134186\n")
        f.write(f"Output file: {OUTPUT}\n")

        f.write("\n=== SUMMARY ===\n")
        f.write(f"CurvePoint rows in dataset: {len(curve_points)}\n")
        f.write(f"Talent effect-point rows: {len(effect_points)}\n")
        f.write(f"Purification scaling references: {len(purification_refs)}\n")
        f.write(f"Unique nonzero referenced curves: {len(curve_ids)}\n")
        f.write(f"Matching curve-point rows: {len(curve_point_rows)}\n")
        f.write(
            f"Referenced curves with no points: "
            f"{sum(not any(p.get('CurveID') == cid for p in curve_points) for cid in curve_ids)}\n"
        )

        write_section(
            f,
            "CURVEPOINT SCHEMA AND SAMPLE ROWS",
            [
                {"columns": list(curve_points[0].keys())}
                if curve_points else {"error": "CurvePoint.csv is empty"},
                *curve_points[:5],
            ],
        )

        write_section(
            f,
            "PURIFICATION SCALING REFERENCES",
            purification_refs,
        )

        for curve_id in sorted(curve_ids, key=int):
            matches = [
                row for row in curve_points
                if row.get("CurveID") == curve_id
            ]
            write_section(
                f,
                f"CURVE ID {curve_id} — {len(matches)} POINTS",
                matches,
            )

    print(f"Audit written to: {OUTPUT}")
    print(f"Purification references: {len(purification_refs)}")
    print(f"Referenced curves: {len(curve_ids)}")
    print(f"Matching curve points: {len(curve_point_rows)}")


if __name__ == "__main__":
    main()
