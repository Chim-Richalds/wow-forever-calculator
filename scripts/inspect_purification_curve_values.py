
import csv
from pathlib import Path

ROOT = Path("data/raw/1.60.1.70338")
OUTPUT = Path("data/processed/purification-curve-values.txt")

DEFINITION_ID = "134186"


def read_csv(filename):
    with (ROOT / filename).open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))


def main():
    references = [
        row for row in read_csv("TraitDefinitionEffectPoints.csv")
        if row["TraitDefinitionID"] == DEFINITION_ID
    ]
    all_points = read_csv("CurvePoint.csv")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as out:
        out.write("PURIFICATION CURVE VALUES\n\n")

        for ref in references:
            curve_id = ref["CurveID"]
            points = [
                p for p in all_points
                if p["CurveID"] == curve_id
            ]
            points.sort(key=lambda p: int(p["OrderIndex"]))

            out.write(f"Effect index: {ref['EffectIndex']}\n")
            out.write(f"Operation type: {ref['OperationType']}\n")
            out.write(f"Curve ID: {curve_id}\n")
            out.write(f"Point count: {len(points)}\n\n")

            out.write(
                "OrderIndex | Pos_0 | Pos_1 | "
                "PosPreSquish_0 | PosPreSquish_1\n"
            )
            out.write("-" * 72 + "\n")

            for p in points:
                out.write(
                    f"{p['OrderIndex']:>10} | "
                    f"{p['Pos_0']:>5} | "
                    f"{p['Pos_1']:>5} | "
                    f"{p['PosPreSquish_0']:>15} | "
                    f"{p['PosPreSquish_1']:>15}\n"
                )

            out.write("\n" + "=" * 72 + "\n\n")

    print(f"Report written to: {OUTPUT}")


if __name__ == "__main__":
    main()
