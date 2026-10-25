
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "1.60.1.70338"
OUTPUT = ROOT / "data" / "processed" / "chain-heal-search.txt"


def read_csv(name):
    with (DATA / name).open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))


def main():
    spells = read_csv("Spell.csv")
    effects = read_csv("SpellEffect.csv")
    options = read_csv("SpellClassOptions.csv")

    # Find records mentioning Chain Heal in any text field.
    matches = [
        spell for spell in spells
        if any(
            "chain heal" in str(value).lower()
            for value in spell.values()
        )
    ]

    matched_ids = {spell.get("ID") for spell in matches}

    options_by_spell = {}
    for option in options:
        options_by_spell.setdefault(
            option.get("SpellID"), []
        ).append(option)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as out:
        out.write(f"Chain Heal text matches: {len(matches)}\n")

        for spell in matches:
            spell_id = spell.get("ID", "")
            out.write("\n" + "=" * 72 + "\n")
            out.write(f"SPELL ID: {spell_id}\n")

            for key, value in spell.items():
                if value and (
                    "name" in key.lower()
                    or "description" in key.lower()
                ):
                    out.write(f"{key}: {value}\n")

            out.write("\nSPELL CLASS OPTIONS\n")
            for option in options_by_spell.get(spell_id, []):
                out.write(repr(option) + "\n")

        out.write("\n" + "=" * 72 + "\n")
        out.write("EFFECTS FOR MATCHING SPELLS\n")

        for effect in effects:
            if effect.get("SpellID") in matched_ids:
                out.write(repr(effect) + "\n")

    print(f"Report: {OUTPUT}")
    print(f"Chain Heal text matches: {len(matches)}")


if __name__ == "__main__":
    main()
