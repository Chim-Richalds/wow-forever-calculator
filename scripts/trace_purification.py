#!/usr/bin/env python3
"""Trace Purification (spell 16178) through trait and spell-effect tables.

This is a data-discovery tool, not proof of runtime behavior.
It discovers schema relationships from column names and values, and
reports unresolved links instead of silently treating guesses as facts.
"""

import argparse
import csv
import json
import re
from pathlib import Path


TALENT_SPELL_ID = "16178"


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def normalize(value):
    return str(value or "").strip()


def numeric_id(value):
    value = normalize(value)
    return value if value.isdigit() else None


def column_names(rows):
    return list(rows[0].keys()) if rows else []


def find_spell_columns(rows, spell_id):
    """Find rows that explicitly reference the target spell ID."""
    matches = []

    for row in rows:
        for column, value in row.items():
            if column.lower() in {
                "spellid",
                "spell_id",
                "spell",
                "traitspellid",
                "spell_id_1",
            }:
                if numeric_id(value) == spell_id:
                    matches.append({
                        "matched_column": column,
                        "row": row,
                    })

    return matches


def index_by_id(rows):
    """Index records by their ID field, preserving duplicate IDs."""
    result = {}

    for row in rows:
        record_id = numeric_id(row.get("ID"))

        if record_id is not None:
            result.setdefault(record_id, []).append(row)

    return result


def discover_reference_columns(rows, target_ids, excluded=None):
    """Find columns whose values reference known IDs.

    This identifies possible relationships, not guaranteed foreign keys.
    """
    excluded = set(excluded or [])
    findings = []

    for row_number, row in enumerate(rows):
        for column, value in row.items():
            if column in excluded:
                continue

            value_id = numeric_id(value)

            if value_id in target_ids:
                findings.append({
                    "row_number": row_number,
                    "column": column,
                    "value": value_id,
                    "row": row,
                })

    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing the source CSV files",
    )
    parser.add_argument(
        "--output",
        default="data/processed/purification-trace.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--talent-spell-id",
        default=TALENT_SPELL_ID,
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    trait_path = data_dir / "TraitDefinition.csv"
    points_path = data_dir / "TraitDefinitionEffectPoints.csv"
    effects_path = data_dir / "SpellEffect.csv"

    for path in (trait_path, points_path, effects_path):
        if not path.exists():
            raise SystemExit(f"Missing input file: {path}")

    traits = read_csv(trait_path)
    points = read_csv(points_path)
    effects = read_csv(effects_path)

    # Stage 1: Locate the talent in trait definitions.
    talent_matches = find_spell_columns(
        traits, args.talent_spell_id
    )

    # Also search effect-point rows in case this build stores the
    # talent spell reference there rather than in TraitDefinition.
    point_talent_matches = find_spell_columns(
        points, args.talent_spell_id
    )

    # Stage 2: Collect IDs from matched trait records.
    trait_ids = {
        numeric_id(match["row"].get("ID"))
        for match in talent_matches
    }
    trait_ids.discard(None)

    # Stage 3: Look for effect-point rows referencing those trait IDs.
    point_links = discover_reference_columns(
        points,
        trait_ids,
        excluded={"ID"},
    )

    # Keep all candidate point rows, but label their relationship
    # as inferred until the schema confirms the foreign key.
    linked_point_rows = {
        item["row_number"]: item["row"]
        for item in point_links
    }

    # Stage 4: Collect potential effect IDs from linked point records.
    effect_index = index_by_id(effects)
    effect_id_columns = [
        column for column in column_names(points)
        if re.search(r"effect.*id|id.*effect", column, re.I)
    ]

    effect_references = []
    for row_number, row in linked_point_rows.items():
        for column in effect_id_columns:
            effect_id = numeric_id(row.get(column))
            if effect_id and effect_id in effect_index:
                effect_references.append({
                    "point_row_number": row_number,
                    "reference_column": column,
                    "effect_id": effect_id,
                    "effect_records": effect_index[effect_id],
                })

    # Stage 5: Report trigger-spell links from resolved effects.
    trigger_links = []
    for reference in effect_references:
        for effect in reference["effect_records"]:
            trigger_id = numeric_id(
                effect.get("EffectTriggerSpell")
            )

            if trigger_id and trigger_id != "0":
                trigger_links.append({
                    "source_effect_id": reference["effect_id"],
                    "trigger_spell_id": trigger_id,
                })

    report = {
        "talent_spell_id": args.talent_spell_id,
        "inputs": {
            "TraitDefinition.csv": str(trait_path),
            "TraitDefinitionEffectPoints.csv": str(points_path),
            "SpellEffect.csv": str(effects_path),
        },
        "schema": {
            "TraitDefinition": column_names(traits),
            "TraitDefinitionEffectPoints": column_names(points),
            "SpellEffect": column_names(effects),
        },
        "talent_trait_matches": talent_matches,
        "direct_effect_point_talent_matches": point_talent_matches,
        "candidate_trait_ids": sorted(trait_ids),
        "candidate_effect_point_links": [
            {
                "row_number": item["row_number"],
                "reference_column": item["column"],
                "referenced_trait_id": item["value"],
                "row": item["row"],
                "relationship_status": "candidate_reference",
            }
            for item in point_links
        ],
        "effect_id_columns_detected": effect_id_columns,
        "resolved_effect_references": effect_references,
        "trigger_spell_links": trigger_links,
        "limitations": [
            "Column-name heuristics do not prove foreign-key semantics.",
            "No matching rows does not prove that a relationship is absent.",
            "Triggered spells require additional resolution before conclusions.",
            "Mask overlap alone does not prove runtime effect application.",
        ],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Report: {output_path}")
    print(f"Talent trait matches: {len(talent_matches)}")
    print(f"Direct effect-point matches: {len(point_talent_matches)}")
    print(f"Candidate trait IDs: {len(trait_ids)}")
    print(f"Candidate point links: {len(point_links)}")
    print(f"Resolved effect references: {len(effect_references)}")
    print(f"Trigger spell links: {len(trigger_links)}")


if __name__ == "__main__":
    main()
