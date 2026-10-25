
#!/usr/bin/env python3
"""Validate talent-effect rules against the source DB2 CSV files.

This checks data integrity only. It never upgrades a rule to verified.
"""

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data/raw/1.60.1.70338"
DEFAULT_RULES = ROOT / "data/processed/effect-rules.json"
DEFAULT_OUTPUT = ROOT / "data/processed/talent-verification.json"

EFFECT_FIELDS = {
    "effect_index": "EffectIndex",
    "effect_type": "Effect",
    "aura_id": "EffectAura",
    "base_points": "EffectBasePointsF",
    "misc_value_0": "EffectMiscValue_0",
    "misc_value_1": "EffectMiscValue_1",
    "spell_class_mask": "EffectSpellClassMask",
}


def read_csv(directory, name):
    path = directory / f"{name}.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rules_doc = json.loads(args.rules.read_text(encoding="utf-8"))
    rules = rules_doc["rules"]

    effects = read_csv(args.data_dir, "SpellEffect")
    spells = read_csv(args.data_dir, "Spell")
    definitions = read_csv(args.data_dir, "TraitDefinition")

    effects_by_id = {}
    duplicate_effect_ids = set()

    for row in effects:
        effect_id = row.get("ID", "")
        if effect_id in effects_by_id:
            duplicate_effect_ids.add(effect_id)
        effects_by_id[effect_id] = row

    spell_ids = {row["ID"] for row in spells}
    definitions_by_spell = {}

    for row in definitions:
        definitions_by_spell.setdefault(row["SpellID"], []).append(row)

    seen_rule_ids = set()
    seen_rule_effects = set()
    results = []

    for rule in rules:
        problems = []
        rule_id = rule.get("rule_id", "")
        spell_id = str(rule.get("talent_spell_id", ""))
        effect_id = str(rule.get("effect_id", ""))

        if not rule_id:
            problems.append("missing_rule_id")
        elif rule_id in seen_rule_ids:
            problems.append("duplicate_rule_id")
        seen_rule_ids.add(rule_id)

        if effect_id in seen_rule_effects:
            problems.append("effect_referenced_by_multiple_rules")
        seen_rule_effects.add(effect_id)

        effect = effects_by_id.get(effect_id)

        if effect_id in duplicate_effect_ids:
            problems.append("duplicate_source_effect_id")
        if effect is None:
            problems.append("missing_source_effect")
        else:
            if effect.get("SpellID") != spell_id:
                problems.append("effect_spell_id_mismatch")

            checks = {
                "effect_index": "EffectIndex",
                "effect_type": "Effect",
                "aura_id": "EffectAura",
                "misc_value_0": "EffectMiscValue_0",
                "misc_value_1": "EffectMiscValue_1",
            }

            for rule_field, source_field in checks.items():
                if rule_field not in rule:
                    continue
                expected = integer(rule[rule_field])
                actual = integer(effect.get(source_field))
                if expected != actual:
                    problems.append(f"{rule_field}_mismatch")

            if "base_points" in rule:
                expected = float(rule["base_points"])
                actual = float(effect["EffectBasePointsF"])
                if abs(expected - actual) > 1e-6:
                    problems.append("base_points_mismatch")

            if "spell_class_mask" in rule:
                expected = rule["spell_class_mask"]
                actual = [
                    integer(effect.get(f"EffectSpellClassMask_{i}"))
                    for i in range(4)
                ]
                if expected != actual:
                    problems.append("spell_class_mask_mismatch")

        if spell_id not in spell_ids:
            problems.append("missing_source_spell")

        if not definitions_by_spell.get(spell_id):
            problems.append("missing_trait_definition")

        results.append({
            "rule_id": rule_id,
            "talent_spell_id": spell_id,
            "effect_id": effect_id,
            "source_integrity": (
                "pass" if not problems else "fail"
            ),
            "problems": problems,
            "mechanical_verification": rule.get(
                "verification_status", "pending"
            ),
        })

    report = {
        "schema_version": 1,
        "client_build": rules_doc.get("client_build"),
        "data_directory": str(args.data_dir),
        "rule_count": len(results),
        "integrity_failures": sum(
            item["source_integrity"] == "fail"
            for item in results
        ),
        "results": results,
        "important_note": (
            "Source integrity does not establish mechanical correctness. "
            "Verification status is preserved, never automatically promoted."
        ),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Rules checked: {len(results)}")
    print(f"Integrity failures: {report['integrity_failures']}")
    print(f"Report: {args.output}")

    return int(report["integrity_failures"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
