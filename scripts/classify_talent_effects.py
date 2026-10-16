
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("data/raw/1.60.1.70338")
OUT = Path("data/processed")

TREE_IDS = {"1082"}

# These are discovery hints, not proof of mechanical behaviour.
KEYWORDS = {
    "direct_stat_candidate": re.compile(
        r"\b(strength|agility|stamina|intellect|spirit|"
        r"armor|armour|critical strike|crit chance|"
        r"haste|mastery|versatility|dodge|parry|"
        r"hit rating|expertise|attack power|spell power)\b",
        re.I,
    ),
    "damage_or_healing_candidate": re.compile(
        r"\b(damage|healing|heal for|increases .* damage|"
        r"critical damage)\b",
        re.I,
    ),
    "resource_candidate": re.compile(
        r"\b(mana|rage|energy|focus|runic power|resource)\b",
        re.I,
    ),
    "ability_or_rotation_candidate": re.compile(
        r"\b(cooldown|cast time|casting time|proc|"
        r"chance to|trigger|additional target|"
        r"duration|range)\b",
        re.I,
    ),
}


def load(name, required=True):
    path = DATA / f"{name}.csv"

    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing CSV: {path}")
        return []

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def first_value(row, candidates):
    for col in candidates:
        value = row.get(col, "")
        if value not in ("", None, "0"):
            return value
    return ""


def find_text(row, patterns):
    """Find non-empty text fields matching column-name patterns."""
    result = []

    for col, value in row.items():
        if not value or not value.strip():
            continue

        if any(re.search(pattern, col, re.I) for pattern in patterns):
            result.append(value.strip())

    return " | ".join(dict.fromkeys(result))


def classify_text(text):
    matches = [
        category
        for category, pattern in KEYWORDS.items()
        if pattern.search(text or "")
    ]
    return matches or ["no_textual_hint"]


def integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    trees = load("TraitTree")
    nodes = load("TraitNode")
    mappings = load("TraitNodeXTraitNodeEntry")
    entries = load("TraitNodeEntry")
    definitions = load("TraitDefinition")
    spells = load("Spell")
    effects = load("SpellEffect")

    # SpellName may be absent or have a different schema in this export.
    spell_names = load("SpellName", required=False)

    tree_ids = {t["ID"] for t in trees}
    selected_tree_ids = TREE_IDS & tree_ids

    if not selected_tree_ids:
        raise ValueError(
            f"None of TREE_IDS={TREE_IDS} found in TraitTree"
        )

    node_by_id = {r["ID"]: r for r in nodes}
    entry_by_id = {r["ID"]: r for r in entries}
    definition_by_id = {r["ID"]: r for r in definitions}
    spell_by_id = {r["ID"]: r for r in spells}

    names_by_spell = defaultdict(list)
    for row in spell_names:
        sid = first_value(row, ["ID", "SpellID"])
        name = find_text(row, [r"name.*lang$", r"^name$"])
        if sid and name:
            names_by_spell[sid].append(name)

    effects_by_spell = defaultdict(list)
    for effect in effects:
        effects_by_spell[effect["SpellID"]].append(effect)

    mappings_by_node = defaultdict(list)
    for mapping in mappings:
        mappings_by_node[mapping["TraitNodeID"]].append(mapping)

    rows = []
    signature_counts = Counter()
    category_counts = Counter()
    unresolved = Counter()

    for node_id, node in node_by_id.items():
        if node["TraitTreeID"] not in selected_tree_ids:
            continue

        for mapping in mappings_by_node[node_id]:
            entry_id = mapping["TraitNodeEntryID"]
            entry = entry_by_id.get(entry_id)

            if not entry:
                unresolved["missing_entry"] += 1
                continue

            definition_id = entry["TraitDefinitionID"]
            definition = definition_by_id.get(definition_id)

            if not definition or definition_id == "0":
                unresolved["missing_definition"] += 1
                rows.append({
                    "tree_id": node["TraitTreeID"],
                    "node_id": node_id,
                    "entry_id": entry_id,
                    "definition_id": definition_id,
                    "spell_id": "",
                    "talent_name": "",
                    "max_ranks": entry["MaxRanks"],
                    "effect_index": "",
                    "effect_type": "",
                    "effect_aura": "",
                    "effect_misc_0": "",
                    "effect_misc_1": "",
                    "text_hints": "missing_definition",
                    "preliminary_category": "unresolved",
                    "confidence": "none",
                    "review_status": "investigate",
                })
                continue

            spell_id = definition["SpellID"]
            spell = spell_by_id.get(spell_id)

            talent_name = (
                definition.get("OverrideName_lang", "").strip()
                or " | ".join(names_by_spell.get(spell_id, []))
                or find_text(spell or {}, [r"name.*lang$", r"^name$"])
                or f"Unknown spell {spell_id}"
            )

            description = find_text(
                spell or {},
                [r"description", r"tooltip", r"effect.*lang"],
            )

            spell_effects = effects_by_spell.get(spell_id, [])

            if not spell:
                unresolved["spell_missing_from_Spell"] += 1

            if not spell_effects:
                unresolved["spell_has_no_effect_rows"] += 1

            hints = classify_text(
                f"{talent_name} {description}"
            )

            if not spell_effects:
                spell_effects = [None]

            for effect in spell_effects:
                if effect is None:
                    effect_type = ""
                    aura = ""
                    effect_index = ""
                    misc0 = ""
                    misc1 = ""
                    preliminary = "no_effect_rows"
                    review = "investigate"
                else:
                    effect_type = effect.get("Effect", "")
                    aura = effect.get("EffectAura", "")
                    effect_index = effect.get("EffectIndex", "")
                    misc0 = effect.get("EffectMiscValue_0", "")
                    misc1 = effect.get("EffectMiscValue_1", "")

                    # Numeric values are retained without guessing
                    # their enum meaning.
                    signature = (
                        effect_type, aura, misc0, misc1
                    )
                    signature_counts[signature] += 1

                    preliminary = (
                        "text_hint_only"
                        if hints != ["no_textual_hint"]
                        else "unclassified"
                    )
                    review = "map_enum_and_review"

                category_counts[preliminary] += 1

                rows.append({
                    "tree_id": node["TraitTreeID"],
                    "node_id": node_id,
                    "entry_id": entry_id,
                    "definition_id": definition_id,
                    "spell_id": spell_id,
                    "talent_name": talent_name,
                    "max_ranks": entry["MaxRanks"],
                    "effect_index": effect_index,
                    "effect_type": effect_type,
                    "effect_aura": aura,
                    "effect_misc_0": misc0,
                    "effect_misc_1": misc1,
                    "text_hints": ";".join(hints),
                    "preliminary_category": preliminary,
                    "confidence": (
                        "low" if hints != ["no_textual_hint"]
                        else "none"
                    ),
                    "review_status": review,
                })

    csv_path = OUT / "talent-effect-inventory.csv"
    fieldnames = list(rows[0]) if rows else []

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "data_directory": str(DATA),
        "selected_tree_ids": sorted(selected_tree_ids),
        "effect_row_count": len(rows),
        "category_counts": dict(category_counts),
        "unresolved_counts": dict(unresolved),
        "most_common_effect_signatures": [
            {
                "effect_type": signature[0],
                "effect_aura": signature[1],
                "misc_0": signature[2],
                "misc_1": signature[3],
                "count": count,
            }
            for signature, count in signature_counts.most_common(50)
        ],
        "classification_policy": (
            "Text matching supplies discovery hints only. "
            "Numeric effect enums must be mapped to the exact client "
            "version before assigning mechanical meaning."
        ),
    }

    json_path = OUT / "talent-effect-summary.json"
    json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps({
        "csv_output": str(csv_path),
        "json_output": str(json_path),
        "effect_row_count": len(rows),
        "category_counts": dict(category_counts),
        "unresolved_counts": dict(unresolved),
    }, indent=2))


if __name__ == "__main__":
    main()
