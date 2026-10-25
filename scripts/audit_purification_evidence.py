
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = "1.60.1.70338"
DATA = ROOT / "data" / "raw" / BUILD
OUTPUT = ROOT / "data" / "processed" / "purification-evidence.json"

SPELL_ID = "16178"
DEFINITION_ID = "134186"
EXPECTED_EFFECT_IDS = {"693066", "1197174"}


def read_csv(name, required=True):
    path = DATA / name

    # Add .csv only if the filename doesn't already have it.
    if path.suffix.lower() != ".csv":
        path = path.with_suffix(".csv")

    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing source table: {path}")
        return []

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError(f"Missing CSV header: {path}")

        return list(reader)

def index_rows(rows, key):
    result = defaultdict(list)

    for row in rows:
        result[row.get(key, "")].append(row)

    return result


def compare_spell_masks(effect_records, spell_options):
    results = []

    for effect in effect_records:
        effect_masks = [
            int(value or 0)
            for value in effect["spell_class_mask"]
        ]

        matches = []

        for option in spell_options:
            option_masks = [
                int(option.get(f"SpellClassMask_{i}") or 0)
                for i in range(4)
            ]

            overlaps = [
                effect_masks[i] & option_masks[i]
                for i in range(4)
            ]

            matches.append({
                "spell_class_set": option.get("SpellClassSet"),
                "option_spell_id": option.get("SpellID"),
                "option_masks": option_masks,
                "bitwise_overlaps": overlaps,
                "any_overlap": any(overlaps),
            })

        results.append({
            "effect_id": effect["effect_id"],
            "effect_masks": effect_masks,
            "option_comparisons": matches,
        })

    return results


def parse_int(value):
    """Parse decimal DB2 values, returning None when unavailable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



def mask_diagnostics(effect_records, spell_options, spells):
    """Diagnose spell-class-mask overlaps without claiming applicability."""
    spell_by_id = {
        row.get("ID", ""): row
        for row in spells
    }

    results = []

    for effect in effect_records:
        effect_mask = [
            parse_int(value)
            for value in effect["spell_class_mask"]
        ]

        effect_id = effect["effect_id"]
        effect_index = effect["effect_index"]

        # A missing mask word is unknown, not necessarily zero.
        if not any(value not in (None, 0) for value in effect_mask):
            results.append({
                "effect_id": effect_id,
                "effect_index": effect_index,
                "status": "no_nonzero_effect_mask",
                "matching_option_count": 0,
                "matching_options": [],
                "interpretation": (
                    "No nonzero effect mask was found. "
                    "This does not establish which spells are affected."
                ),
            })
            continue

        candidates = []

        for option in spell_options:
            option_mask = [
                parse_int(option.get(f"SpellClassMask_{i}"))
                for i in range(4)
            ]

            # Do not silently treat missing mask words as zero.
            unknown_words = [
                i for i, (effect_word, option_word) in enumerate(
                    zip(effect_mask, option_mask)
                )
                if effect_word is None or option_word is None
            ]

            overlapping_bits = []

            for i, (effect_word, option_word) in enumerate(
                zip(effect_mask, option_mask)
            ):
                if effect_word is None or option_word is None:
                    continue

                overlap = effect_word & option_word

                if overlap:
                    overlapping_bits.append({
                        "word": i,
                        "bits": overlap,
                    })

            if not overlapping_bits:
                continue

            spell_id = option.get("SpellID", "")
            spell = spell_by_id.get(spell_id, {})

            spell_name = next(
                (
                    value.strip()
                    for key, value in spell.items()
                    if "name" in key.lower()
                    and value and value.strip()
                ),
                "",
            )

            spell_description = next(
                (
                    value.strip()
                    for key, value in spell.items()
                    if "description" in key.lower()
                    and value and value.strip()
                ),
                "",
            )

            candidates.append({
                "spell_id": spell_id,
                "spell_class_set": option.get("SpellClassSet"),
                "overlapping_mask_words": [
                    item["word"] for item in overlapping_bits
                ],
                "overlapping_bits": overlapping_bits,
                "unknown_mask_words": unknown_words,
                "spell_name": spell_name,
                "spell_description": spell_description,
                "status": (
                    "partial_mask_data"
                    if unknown_words
                    else "bitwise_overlap_only"
                ),
            })

        results.append({
            "effect_id": effect_id,
            "effect_index": effect_index,
            "effect_mask": effect["spell_class_mask"],
            "matching_option_count": len(candidates),
            "matching_options": candidates,
            "interpretation": (
                "Candidates indicate bitwise overlap only. "
                "SpellClassSet compatibility and actual in-game "
                "effect application have not been established."
            ),
        })

    return results


def main():
    definitions = read_csv("TraitDefinition.csv")
    spells = read_csv("Spell.csv")
    effects = read_csv("SpellEffect.csv")
    points = read_csv("TraitDefinitionEffectPoints.csv")
    curves = read_csv("CurvePoint.csv")
    spell_options = read_csv("SpellClassOptions.csv", required=False)

    definitions_by_id = index_rows(definitions, "ID")
    spells_by_id = index_rows(spells, "ID")
    effects_by_spell = index_rows(effects, "SpellID")
    effects_by_id = index_rows(effects, "ID")
    curves_by_id = index_rows(curves, "CurveID")
    options_by_spell = index_rows(spell_options, "SpellID")

    purification_definitions = [
        row for row in definitions
        if row.get("ID") == DEFINITION_ID
    ]

    purification_points = [
        row for row in points
        if row.get("TraitDefinitionID") == DEFINITION_ID
    ]

    purification_effects = effects_by_spell.get(SPELL_ID, [])

    effect_records = []

    for effect in purification_effects:
        effect_id = effect.get("ID", "")

        if effect_id not in EXPECTED_EFFECT_IDS:
            continue

        mask = [
            effect.get(f"EffectSpellClassMask_{i}")
            for i in range(4)
        ]

        effect_records.append({
            "effect_id": effect_id,
            "spell_id": effect.get("SpellID"),
            "effect_index": effect.get("EffectIndex"),
            "effect_type": effect.get("Effect"),
            "aura_id": effect.get("EffectAura"),
            "base_points": effect.get("EffectBasePointsF"),
            "misc_values": [
                effect.get("EffectMiscValue_0"),
                effect.get("EffectMiscValue_1"),
            ],
            "spell_class_mask": mask,
            "source_id_match_count": len(
                effects_by_id.get(effect_id, [])
            ),
        })

    spell_mask_report = compare_spell_masks(
        effect_records,
        spell_options,
    )

    curve_records = []

    for ref in purification_points:
        curve_id = ref.get("CurveID", "")
        curve_rows = sorted(
            curves_by_id.get(curve_id, []),
            key=lambda row: int(row.get("OrderIndex") or 0),
        ) if curve_id not in ("", "0") else []

        curve_records.append({
            "effect_index": ref.get("EffectIndex"),
            "operation_type": ref.get("OperationType"),
            "curve_id": curve_id,
            "point_count": len(curve_rows),
            "points": [
                {
                    "order_index": row.get("OrderIndex"),
                    "pos_0": row.get("Pos_0"),
                    "pos_1": row.get("Pos_1"),
                    "pos_pre_squish_0": row.get("PosPreSquish_0"),
                    "pos_pre_squish_1": row.get("PosPreSquish_1"),
                }
                for row in curve_rows
            ],
        })
    
    spell_mask_report = mask_diagnostics(
        effect_records,
        spell_options,
        spells,
    )

    report = {
        "schema_version": 1,
        "build": BUILD,
        "talent": "Purification",
        "spell_id": SPELL_ID,
        "definition_id": DEFINITION_ID,
        "source_counts": {
            "definitions": len(definitions),
            "spells": len(spells),
            "spell_effects": len(effects),
            "effect_point_references": len(points),
            "curve_points": len(curves),
            "spell_class_options": len(spell_options),
        },
        "source_availability": {
            "spell_class_options": bool(spell_options),
        },
        "definition_records": purification_definitions,
        "spell_records": spells_by_id.get(SPELL_ID, []),
        "effect_records": effect_records,
        "effect_point_references": purification_points,
        "curve_records": curve_records,
        "spell_class_options_for_talent_spell": (
            options_by_spell.get(SPELL_ID, [])
        ),
        "spell_mask_diagnostics": spell_mask_report,
        "spell_class_options_for_talent_spell": (
            options_by_spell.get(SPELL_ID, [])
        ),
        "integrity_checks": {
            "definition_found": bool(purification_definitions),
            "spell_found": SPELL_ID in spells_by_id,
            "expected_effect_ids_found": (
                EXPECTED_EFFECT_IDS
                <= {
                    row.get("ID", "")
                    for row in purification_effects
                }
            ),
            "effect_point_reference_count": len(purification_points),
            "referenced_nonzero_curves_without_points": [
                record["curve_id"]
                for record in curve_records
                if record["curve_id"] not in ("", "0")
                and record["point_count"] == 0
            ],
        },
        "mechanical_verification": "pending",
        "unresolved_questions": [
            "Resolve spell-class-mask matching against spell-class options",
            "Interpret each effect-point operation and its curve",
            "Determine whether both effects contribute independently",
            "Verify actual healing behavior independently",
        ],
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Report: {OUTPUT}")
    print(f"Definitions: {len(purification_definitions)}")
    print(f"Spell effects: {len(effect_records)}")
    print(f"Effect-point references: {len(purification_points)}")
    print(f"Curve records: {len(curve_records)}")
    print(
        "SpellClassOptions available:",
        report["source_availability"]["spell_class_options"],
    )
    print("Mechanical verification remains pending.")


if __name__ == "__main__":
    main()
