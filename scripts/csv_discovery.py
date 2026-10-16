
import csv
from pathlib import Path

root = Path("data/raw/1.60.1.70338")

keywords = (
    "spellclass",
    "spellfamily",
    "spellname",
    "spellmisc",
    "spellcategory",
    "spelllabel",
    "spell",
    "aura",
)

for path in sorted(root.glob("*.csv")):
    if not any(k in path.stem.lower() for k in keywords):
        continue

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        columns = next(reader, [])

    print(f"\n{path.name}")
    print(", ".join(columns))
