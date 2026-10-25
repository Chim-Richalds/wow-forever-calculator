
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "1.60.1.70338"
OUTPUT = ROOT / "data" / "processed" / "chain-heal-summary.txt"

CANDIDATE_IDS = {"408521", "1239242", "1239243"}


def read_csv(name):
    with (DATA / name).open(
        encoding="utf-8-sig", newline=""
    ) as f:
        return list(csv.DictReader(f))


def main():
    spells = read_csv("Spell.csv")
    effects = read_csv("SpellEffect.csv")
    options = read_csv("SpellClassOptions.csv")

    matches = [
        spell for spell in spells
        if any(
            "chain heal" in str(value).lower()
            for value in spell.values()
        )
    ]

    options_by_spell = {}
    for option in options:
        options_by_spell.setdefault(
            option.get("SpellID"), []
        ).append(option)

    effects_by_spell = {}
    for effect in effects:
        effects_by_spell.setdefault(
            effect.get("SpellID"), []
        ).append(effect)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as out:
        for spell in matches:
            spell_id = spell.get("ID", "")
            descriptions = [
                str(value).replace("\r", " ").replace("\n", " ")
                for key, value in spell.items()
                if "description" in key.lower() and value
            ]
            names = [
                str(value)
                for key, value in spell.items()
                if "name" in key.lower() and value
            ]

            out.write(f"\nSPELL ID: {spell_id}\n")
            out.write(f"Name fields: {names}\n")
            out.write(f"Descriptions: {descriptions}\n")

            for option in options_by_spell.get(spell_id, []):
                out.write(
                    "CLASS OPTION: "
                    f"class_set={option.get('SpellClassSet')}, "
                    f"masks={[option.get(f'SpellClassMask_{i}') for i in range(4)]}\n"
                )

            for effect in effects_by_spell.get(spell_id, []):
                out.write(
                    "EFFECT: "
                    f"id={effect.get('ID')}, "
                    f"index={effect.get('EffectIndex')}, "
                    f"type={effect.get('Effect')}, "
                    f"aura={effect.get('EffectAura')}, "
                    f"trigger={effect.get('EffectTriggerSpell')}, "
                    f"mask={[effect.get(f'EffectSpellClassMask_{i}') for i in range(4)]}\n"
                )

            if spell_id in CANDIDATE_IDS:
                out.write("PRIORITY: Purification candidate rank\n")

    print(f"Report: {OUTPUT}")
    print(f"Matching spells summarized: {len(matches)}")


if __name__ == "__main__":
    main()
