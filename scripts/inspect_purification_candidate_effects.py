
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "1.60.1.70338"
OUTPUT = ROOT / "data" / "processed" / "purification-candidate-effects.txt"

CANDIDATE_SPELL_IDS = {"408521", "1239242", "1239243"}
TALENT_SPELL_ID = "16178"


def read_csv(name):
    path = DATA / name

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    spells = read_csv("Spell.csv")
    effects = read_csv("SpellEffect.csv")
    options = read_csv("SpellClassOptions.csv")

    spell_by_id = {row.get("ID"): row for row in spells}
    options_by_spell = {}

    for row in options:
        options_by_spell.setdefault(row.get("SpellID"), []).append(row)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as out:
        out.write("PURIFICATION CANDIDATE EFFECT INSPECTION\n")
        out.write(f"Talent spell ID: {TALENT_SPELL_ID}\n")

        for spell_id in sorted(CANDIDATE_SPELL_IDS, key=int):
            spell = spell_by_id.get(spell_id)

            out.write("\n" + "=" * 80 + "\n")
            out.write(f"CANDIDATE SPELL ID: {spell_id}\n")
            out.write("=" * 80 + "\n")

            if not spell:
                out.write("Spell record not found.\n")
                continue

            out.write(f"Name: {spell.get('Name_lang', '')}\n")
            out.write(
                f"Description: {spell.get('Description_lang', '')}\n"
            )

            out.write("\nSPELL CLASS OPTIONS\n")
            for option in options_by_spell.get(spell_id, []):
                out.write(repr(option) + "\n")

            out.write("\nSPELL EFFECTS\n")
            matches = [
                effect for effect in effects
                if effect.get("SpellID") == spell_id
            ]

            out.write(f"Effect count: {len(matches)}\n")

            for effect in matches:
                out.write(repr(effect) + "\n")

    print(f"Report: {OUTPUT}")
    print(f"Candidate spells inspected: {len(CANDIDATE_SPELL_IDS)}")


if __name__ == "__main__":
    main()
