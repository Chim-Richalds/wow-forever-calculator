
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = "1.60.1.70338"
DATA = ROOT / "data" / "raw" / BUILD
OUTPUT = (
    ROOT / "data" / "processed"
    / "purification-candidate-audit.json"
)

TARGET_SPELL_IDS = {"16178", "408521", "1239242", "1239243"}
EFFECT_MASK_WORD = 2
EFFECT_MASK_BIT = 16

HEALING_HINTS = (
    "heal",
    "healing",
    "chain heal",
    "heals a friendly target",
)


def read_csv(filename, required=True):
    path = DATA / filename

    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing source table: {path}")
        return []

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def first_text(row, fragment):
    for key, value in row.items():
        if fragment in key.lower() and value and value.strip():
            return value.strip()
    return ""


def main():
    options = read_csv("SpellClassOptions.csv")
    spells = read_csv("Spell.csv")

    spells_by_id = {
        row.get("ID", ""): row
        for row in spells
    }

    candidates = []

    for option in options:
        try:
            mask = int(option.get(
                f"SpellClassMask_{EFFECT_MASK_WORD}", "0"
            ) or 0)
        except ValueError:
            continue

        if not mask & EFFECT_MASK_BIT:
            continue

        spell_id = option.get("SpellID", "")
        spell = spells_by_id.get(spell_id, {})

        name = first_text(spell, "name")
        description = first_text(spell, "description")
        text = f"{name} {description}".lower()

        candidates.append({
            "spell_id": spell_id,
            "spell_class_set": option.get("SpellClassSet", ""),
            "mask_word_2": mask,
            "overlapping_bits": mask & EFFECT_MASK_BIT,
            "name": name,
            "description": description,
            "looks_healing_related": any(
                hint in text for hint in HEALING_HINTS
            ),
            "is_priority_spell": spell_id in TARGET_SPELL_IDS,
        })

    candidates.sort(
        key=lambda row: (
            not row["is_priority_spell"],
            not row["looks_healing_related"],
            row["spell_class_set"],
            row["spell_id"],
        )
    )

    report = {
        "build": BUILD,
        "talent_spell_id": "16178",
        "effect_id": "1197174",
        "effect_mask_word": EFFECT_MASK_WORD,
        "effect_mask_value": EFFECT_MASK_BIT,
        "candidate_count": len(candidates),
        "priority_spell_records": [
            {
                "spell_id": spell_id,
                "record_found": spell_id in spells_by_id,
                "name": first_text(
                    spells_by_id.get(spell_id, {}), "name"
                ),
                "description": first_text(
                    spells_by_id.get(spell_id, {}), "description"
                ),
            }
            for spell_id in sorted(TARGET_SPELL_IDS)
        ],
        "candidates": candidates,
        "warning": (
            "Text matches and mask overlaps are discovery clues only. "
            "They do not establish actual effect application."
        ),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Report: {OUTPUT}")
    print(f"Candidates: {len(candidates)}")
    print("\nPriority spell records:")

    for spell in report["priority_spell_records"]:
        print(
            f"  {spell['spell_id']}: "
            f"{spell['name'] or '(no name)'} — "
            f"{'found' if spell['record_found'] else 'missing'}"
        )

    print("\nHealing-related text candidates:")

    for candidate in candidates:
        if candidate["looks_healing_related"]:
            print(
                f"  {candidate['spell_id']} "
                f"(class set {candidate['spell_class_set']}): "
                f"{candidate['name'] or '(no name)'}"
            )

    print("\nThis report is exploratory, not mechanical verification.")


if __name__ == "__main__":
    main()
