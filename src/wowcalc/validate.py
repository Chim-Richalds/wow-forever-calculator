"""Validation and safety gates for a build's data.

validate_dataset()  checks one dataset on its own: tables, columns, keys,
                    references, and talent-tree integrity.
evaluate_gates()    compares a candidate's report with the installed baseline
                    and decides: blocked, needs acknowledgement, or clear.

Principles (plan section 5.4):
  * Blockers stop an update. Warnings are shown and need acknowledgement.
  * Only what is NEW relative to the baseline matters. Problems already present
    in the installed build are carried, not re-litigated on every update.
  * Missing tables and missing required columns always block.

CLI (PYTHONPATH=src):
    python -m wowcalc.validate               newest staged build vs newest installed
    python -m wowcalc.validate 1.60.1.70425 --baseline 1.60.1.70338
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .dataset import Dataset, DatasetError, list_builds, open_dataset
from .fetch import TABLES

CORE_TABLES = frozenset({
    "TraitTree", "TraitNode", "TraitNodeEntry",
    "TraitNodeXTraitNodeEntry", "TraitDefinition", "TraitEdge",
})

REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "TraitTree": frozenset({"ID"}),
    "TraitNode": frozenset({"ID", "TraitTreeID", "PosX", "PosY"}),
    "TraitNodeEntry": frozenset({"ID", "TraitDefinitionID", "MaxRanks"}),
    "TraitNodeXTraitNodeEntry": frozenset({"TraitNodeID", "TraitNodeEntryID"}),
    "TraitDefinition": frozenset({"ID", "SpellID"}),
    "TraitDefinitionEffectPoints": frozenset({"ID", "TraitDefinitionID", "EffectIndex", "CurveID"}),
    "TraitEdge": frozenset({"ID", "LeftTraitNodeID", "RightTraitNodeID"}),
    "CurvePoint": frozenset({"CurveID", "OrderIndex", "Pos_0", "Pos_1"}),
    "SpellName": frozenset({"ID", "Name_lang"}),
    "Spell": frozenset({"ID", "Description_lang"}),
    "SpellEffect": frozenset({"ID", "SpellID", "EffectIndex", "EffectAura", "Effect", "EffectBasePointsF"}),
    "ChrClasses": frozenset({"ID", "Name_lang"}),
    "ChrRaces": frozenset({"ID", "Name_lang"}),
    "SkillLineXTraitTree": frozenset({"ID", "SkillLineID", "TraitTreeID"}),
    "SkillRaceClassInfo": frozenset({"ID", "SkillID", "ClassMask", "RaceMasks_0", "RaceMasks_1"}),
    "TraitCond": frozenset({"ID", "CondType", "TraitTreeID", "TraitNodeGroupID", "TraitNodeID",
                            "TraitCurrencyID", "SpentAmountRequired"}),
    "TraitNodeGroup": frozenset({"ID", "TraitTreeID"}),
    "TraitNodeGroupXTraitNode": frozenset({"TraitNodeGroupID", "TraitNodeID"}),
    "TraitNodeGroupXTraitCond": frozenset({"TraitCondID", "TraitNodeGroupID"}),
    "TraitNodeXTraitCond": frozenset({"TraitCondID", "TraitNodeID"}),
    "TraitCurrency": frozenset({"ID", "SourcedMax"}),
    "TraitCurrencySource": frozenset({"ID", "TraitCurrencyID", "Amount", "PlayerLevel"}),
    "TraitTreeXTraitCurrency": frozenset({"TraitTreeID", "TraitCurrencyID"}),
    "TraitCost": frozenset({"ID", "Amount", "TraitCurrencyID"}),
    "TraitTreeXTraitCost": frozenset({"TraitTreeID", "TraitCostID"}),
    "CharBaseInfo": frozenset({"RaceID", "ClassID"}),
}

# Issue codes that block regardless of what the baseline looked like.
ALWAYS_BLOCK = frozenset({"missing_table", "unreadable_table", "missing_columns"})


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

def _id_key(value: str):
    return (0, int(value)) if value.isdigit() else (1, value)


def _sorted_ids(values) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=_id_key))


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    subject: str  # stable identity used to match issues across builds
    message: str
    tree_id: str = ""
    values: tuple[str, ...] = ()  # offending ids, for value-level comparison

    @property
    def key(self) -> tuple[str, str]:
        return (self.code, self.subject)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "code": self.code, "subject": self.subject,
            "message": self.message, "treeId": self.tree_id, "values": list(self.values),
        }


@dataclass
class ValidationReport:
    version: str | None
    row_counts: dict[str, int] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "version": self.version, "rowCounts": self.row_counts, "columns": self.columns,
            "missingTables": self.missing_tables, "stats": self.stats,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass(frozen=True)
class Thresholds:
    max_row_drop: float = 0.20  # block if a table loses more than this fraction of rows


@dataclass(frozen=True)
class GateResult:
    blockers: tuple[str, ...] = ()
    needs_ack: tuple[str, ...] = ()
    carried: int = 0  # known issues already present in the baseline

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked, "blockers": list(self.blockers),
            "needsAcknowledgement": list(self.needs_ack), "carriedFromBaseline": self.carried,
        }


# --------------------------------------------------------------------------
# Single-dataset validation
# --------------------------------------------------------------------------

def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _primary_keys(table: str, rows: list[dict], issues: list[Issue]) -> set[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    empty = 0
    for row in rows:
        value = row.get("ID", "")
        if value == "":
            empty += 1
        elif value in seen:
            dupes.append(value)
        else:
            seen.add(value)
    if empty:
        issues.append(Issue("error", "empty_key", f"{table}.ID", f"{table} has {empty} row(s) with an empty ID"))
    if dupes:
        issues.append(Issue(
            "error", "duplicate_primary_key", f"{table}.ID",
            f"{table} has {len(dupes)} duplicate ID occurrence(s)", values=_sorted_ids(dupes),
        ))
    return seen


def _check_fk(issues, rows, src_table, src_col, target_ids, target_table, severity):
    missing = Counter()
    for row in rows:
        value = row.get(src_col, "")
        if value not in ("", "0") and value not in target_ids:
            missing[value] += 1
    if missing:
        issues.append(Issue(
            severity, "orphan_foreign_key", f"{src_table}.{src_col}",
            f"{src_table}.{src_col}: {sum(missing.values())} reference(s) to "
            f"{len(missing)} id(s) not in {target_table}",
            values=_sorted_ids(missing),
        ))


def validate_dataset(ds: Dataset) -> ValidationReport:
    report = ValidationReport(version=ds.version)
    issues = report.issues

    for table in TABLES:
        if not ds.has_table(table):
            report.missing_tables.append(table)
            issues.append(Issue("error", "missing_table", table, f"Table {table} is missing"))
            continue
        try:
            report.columns[table] = ds.columns(table)
            report.row_counts[table] = ds.row_count(table)
        except DatasetError as exc:
            report.missing_tables.append(table)
            issues.append(Issue("error", "unreadable_table", table, str(exc)))

    usable: set[str] = set()
    for table, cols in report.columns.items():
        gone = sorted(REQUIRED_COLUMNS.get(table, frozenset()) - set(cols))
        if gone:
            issues.append(Issue(
                "error", "missing_columns", table,
                f"{table} is missing required column(s): {', '.join(gone)}", values=tuple(gone),
            ))
        else:
            usable.add(table)

    if CORE_TABLES <= usable:
        try:
            _check_structure(ds, usable, report)
        except DatasetError as exc:
            issues.append(Issue("error", "unreadable_table", "structure", str(exc)))

    order = {"error": 0, "warning": 1}
    issues.sort(key=lambda i: (order[i.severity], i.code, i.subject))
    return report


def _check_structure(ds: Dataset, usable: set[str], report: ValidationReport) -> None:
    issues = report.issues
    trees, nodes = ds.rows("TraitTree"), ds.rows("TraitNode")
    entries, mappings = ds.rows("TraitNodeEntry"), ds.rows("TraitNodeXTraitNodeEntry")
    definitions, edges = ds.rows("TraitDefinition"), ds.rows("TraitEdge")

    tree_ids = _primary_keys("TraitTree", trees, issues)
    node_ids = _primary_keys("TraitNode", nodes, issues)
    entry_ids = _primary_keys("TraitNodeEntry", entries, issues)
    def_ids = _primary_keys("TraitDefinition", definitions, issues)
    _primary_keys("TraitEdge", edges, issues)

    spell_ids: set[str] | None = None
    if "Spell" in usable:
        spell_ids = _primary_keys("Spell", ds.rows("Spell"), issues)
    if "SpellName" in usable:
        _primary_keys("SpellName", ds.rows("SpellName"), issues)

    # --- references that must hold for the calculator to work: errors ---
    _check_fk(issues, nodes, "TraitNode", "TraitTreeID", tree_ids, "TraitTree", "error")
    _check_fk(issues, mappings, "TraitNodeXTraitNodeEntry", "TraitNodeID", node_ids, "TraitNode", "error")
    _check_fk(issues, mappings, "TraitNodeXTraitNodeEntry", "TraitNodeEntryID", entry_ids, "TraitNodeEntry", "error")
    _check_fk(issues, entries, "TraitNodeEntry", "TraitDefinitionID", def_ids, "TraitDefinition", "error")
    _check_fk(issues, edges, "TraitEdge", "LeftTraitNodeID", node_ids, "TraitNode", "error")
    _check_fk(issues, edges, "TraitEdge", "RightTraitNodeID", node_ids, "TraitNode", "error")

    # --- spell references: warnings (plan 5.4); see placed-talent check below ---
    if spell_ids is not None:
        _check_fk(issues, definitions, "TraitDefinition", "SpellID", spell_ids, "Spell", "warning")
        if "SpellEffect" in usable:
            effects = ds.rows("SpellEffect")
            _primary_keys("SpellEffect", effects, issues)
            _check_fk(issues, effects, "SpellEffect", "SpellID", spell_ids, "Spell", "warning")

    # --- tree integrity ---
    node_by_id = {n["ID"]: n for n in nodes if n["ID"]}
    entry_by_id = {e["ID"]: e for e in entries if e["ID"]}
    def_by_id = {d["ID"]: d for d in definitions if d["ID"]}

    nodes_per_tree: Counter = Counter(n["TraitTreeID"] for n in nodes)
    for tree in trees:
        if tree["ID"] and nodes_per_tree[tree["ID"]] == 0:
            issues.append(Issue("warning", "empty_tree", f"TraitTree:{tree['ID']}",
                                f"Tree {tree['ID']} has no nodes", tree_id=tree["ID"]))

    positions: Counter = Counter((n["TraitTreeID"], n["PosX"], n["PosY"]) for n in nodes)
    for (tree_id, x, y), count in sorted(positions.items()):
        if count > 1:
            issues.append(Issue("warning", "duplicate_position", f"TraitTree:{tree_id}@{x},{y}",
                                f"Tree {tree_id}: {count} nodes share position ({x}, {y})", tree_id=tree_id))

    for edge in edges:
        left, right = node_by_id.get(edge["LeftTraitNodeID"]), node_by_id.get(edge["RightTraitNodeID"])
        if left and right and left["TraitTreeID"] != right["TraitTreeID"]:
            issues.append(Issue("warning", "cross_tree_edge", f"TraitEdge:{edge['ID']}",
                                f"Edge {edge['ID']} connects different trees "
                                f"({left['TraitTreeID']} and {right['TraitTreeID']})",
                                tree_id=left["TraitTreeID"]))

    entries_on_node: dict[str, list[str]] = defaultdict(list)  # node id -> entry ids
    for m in mappings:
        entries_on_node[m["TraitNodeID"]].append(m["TraitNodeEntryID"])

    placed_defs: dict[str, set[str]] = defaultdict(set)  # definition id -> tree ids
    placed_entries: set[str] = set()
    bad_ranks_seen: set[str] = set()
    for node in nodes:
        if not node["ID"]:
            continue
        node_entries = entries_on_node.get(node["ID"], [])
        if not node_entries:
            issues.append(Issue("warning", "node_without_entry", f"TraitNode:{node['ID']}",
                                f"Node {node['ID']} (tree {node['TraitTreeID']}) has no talent entry",
                                tree_id=node["TraitTreeID"]))
        for entry_id in node_entries:
            entry = entry_by_id.get(entry_id)
            if not entry:
                continue
            placed_entries.add(entry_id)
            ranks = _as_int(entry.get("MaxRanks"))
            if (ranks is None or ranks < 1) and entry_id not in bad_ranks_seen:
                bad_ranks_seen.add(entry_id)
                issues.append(Issue("error", "invalid_max_ranks", f"TraitNodeEntry:{entry_id}",
                                    f"Entry {entry_id} has invalid MaxRanks {entry.get('MaxRanks')!r}",
                                    tree_id=node["TraitTreeID"]))
            if entry["TraitDefinitionID"] in def_by_id:
                placed_defs[entry["TraitDefinitionID"]].add(node["TraitTreeID"])

    # Talents a player can actually take but whose spell data is absent.
    if spell_ids is not None:
        for def_id, tree_set in sorted(placed_defs.items(), key=lambda kv: _id_key(kv[0])):
            spell_id = def_by_id[def_id]["SpellID"]
            if spell_id not in ("", "0") and spell_id not in spell_ids:
                tree_id = sorted(tree_set, key=_id_key)[0]
                issues.append(Issue(
                    "warning", "talent_missing_spell_data", f"TraitDefinition:{def_id}",
                    f"Talent definition {def_id} (spell {spell_id}, tree {tree_id}) is on a node "
                    f"but its spell is not in the Spell table", tree_id=tree_id, values=(spell_id,),
                ))

    used_defs = {e["TraitDefinitionID"] for e in entries}
    report.stats.update({
        "trees": len(tree_ids), "nodes": len(node_ids), "entries": len(entry_ids),
        "definitions": len(def_ids),
        "placedTalents": len(placed_defs),
        "entriesNotOnAnyNode": len(entry_ids - placed_entries),
        "definitionsUnused": len(def_ids - used_defs),
    })


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------

def _preview(values, limit: int = 6) -> str:
    shown = ", ".join(values[:limit])
    return shown + (f", +{len(values) - limit} more" if len(values) > limit else "")


def _novel(candidate: ValidationReport, baseline: ValidationReport | None):
    """Candidate issues (or issue values) not already present in the baseline."""
    prior = {i.key: i for i in baseline.issues} if baseline else {}
    for issue in candidate.issues:
        old = prior.get(issue.key)
        if old is None:
            yield issue, issue.values, False
        elif issue.values:
            known = set(old.values)
            fresh = tuple(v for v in issue.values if v not in known)
            if fresh:
                yield issue, fresh, True


def evaluate_gates(
    candidate: ValidationReport,
    baseline: ValidationReport | None,
    thresholds: Thresholds = Thresholds(),
) -> GateResult:
    blockers: list[str] = []
    needs_ack: list[str] = []

    for issue in candidate.issues:
        if issue.code in ALWAYS_BLOCK:
            blockers.append(issue.message)

    if baseline is not None:
        for table, was in sorted(baseline.row_counts.items()):
            now = candidate.row_counts.get(table)
            if now is not None and was > 0 and now < was * (1 - thresholds.max_row_drop):
                blockers.append(
                    f"{table} dropped from {was:,} to {now:,} rows "
                    f"(more than {thresholds.max_row_drop:.0%} loss)"
                )
        for table, old_cols in sorted(baseline.columns.items()):
            new_cols = candidate.columns.get(table)
            if new_cols is None:
                continue
            removed = [c for c in old_cols if c not in new_cols
                       and c not in REQUIRED_COLUMNS.get(table, frozenset())]
            if removed:
                needs_ack.append(f"{table}: column(s) removed or renamed: {', '.join(removed)}")

    carried = 0
    for issue, fresh, partial in _novel(candidate, baseline):
        if issue.code in ALWAYS_BLOCK:
            continue
        text = issue.message
        if partial or (issue.values and baseline is not None):
            text += f" [new: {_preview(fresh)}]"
        if issue.severity == "error":
            blockers.append(("New error: " if baseline else "Error: ") + text)
        else:
            needs_ack.append(("New warning: " if baseline else "Warning: ") + text)
    if baseline is not None:
        carried = sum(1 for i in candidate.issues if i.code not in ALWAYS_BLOCK) - sum(
            1 for issue, _, _ in _novel(candidate, baseline) if issue.code not in ALWAYS_BLOCK
        )

    return GateResult(tuple(blockers), tuple(needs_ack), max(carried, 0))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_report(report: ValidationReport, gates: GateResult, baseline_version: str | None) -> None:
    print(f"\nBuild:    {report.version}")
    print(f"Baseline: {baseline_version or '(none: every issue counts as new)'}")
    if report.stats:
        s = report.stats
        print(f"Trees {s['trees']}, nodes {s['nodes']}, entries {s['entries']}, "
              f"definitions {s['definitions']}, placed talents {s['placedTalents']}")
        print(f"Not on any node: {s['entriesNotOnAnyNode']} entries, "
              f"{s['definitionsUnused']} unused definition(s)")
    print(f"Issues:   {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
          f"  ({gates.carried} already present in baseline)")
    if gates.blockers:
        print("\nBLOCKED. This build must not be installed:")
        for line in gates.blockers:
            print(f"  - {line}")
    if gates.needs_ack:
        print("\nNeeds acknowledgement:")
        for line in gates.needs_ack:
            print(f"  - {line}")
    if not gates.blockers and not gates.needs_ack:
        print("\nCLEAR. Nothing new to review.")
    elif not gates.blockers:
        print("\nNot blocked.")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Validate a build and apply the safety gates.")
    ap.add_argument("version", nargs="?", help="default: newest staged build")
    ap.add_argument("--baseline", help="default: newest installed build other than VERSION")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--max-row-drop", type=float, default=Thresholds.max_row_drop)
    ap.add_argument("--no-write", action="store_true", help="do not write data/reports/<build>.validation.json")
    args = ap.parse_args(argv)
    data_dir: Path = args.data_dir

    version = args.version
    if version is None:
        staged = list_builds(data_dir / "staging")
        if not staged:
            print("Nothing is staged. Run: python -m wowcalc.fetch")
            return 2
        version = staged[-1].version

    baseline_version = args.baseline
    if baseline_version is None:
        installed = [b for b in list_builds(data_dir / "raw") if b.version != version]
        baseline_version = installed[-1].version if installed else None

    try:
        candidate = validate_dataset(open_dataset(data_dir, version))
        baseline = validate_dataset(open_dataset(data_dir, baseline_version)) if baseline_version else None
    except DatasetError as exc:
        print(f"ERROR: {exc}")
        return 2

    gates = evaluate_gates(candidate, baseline, Thresholds(max_row_drop=args.max_row_drop))
    _print_report(candidate, gates, baseline_version)

    if not args.no_write:
        out = data_dir / "reports" / f"{version}.validation.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "baseline": baseline_version,
            "validation": candidate.to_dict(),
            "gates": gates.to_dict(),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"\nReport: {out}")
    return 1 if gates.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
