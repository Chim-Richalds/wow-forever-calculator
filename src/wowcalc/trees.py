"""The class talent trees of one build, as a structured model.

Built only from the build's data tables. Everything the game data says about a
tree is here: which class owns it, its nodes and ranks, which nodes unlock when,
how many points a character has, and what each rank costs. Nothing here decides
whether a particular allocation is legal; that is allocation.py.

How the data fits together (established from the real tables, see the notes in
each section):

  tree -> class        SkillLineXTraitTree gives a skill line; SkillRaceClassInfo
                       gives that skill line's class mask; the mask bit is the
                       class ID minus one. Exactly one class must result.
  sub-trees            Each class tree is three side-by-side sub-trees (like the
                       old specs). The three top-tier conditions each count one
                       whole sub-tree, so those groups partition the nodes.
  unlock gates         A TraitCond says: points spent in group A must reach N
                       before the nodes of the gated group(s) become available.
                       Gated groups come from TraitNodeGroupXTraitCond, single
                       gated nodes from TraitNodeXTraitCond. A node may have
                       several gates; all must hold.
  prerequisite links   TraitEdge (left -> right) with a Type. What each Type means
                       in play is NOT verifiable from the data; see allocation.py.
  point budget         TraitCurrencySource rows: one point per level, optionally
                       tied to a node entry (the legacy "Talented" perk).
  cost                 TraitTreeXTraitCost -> TraitCost.Amount per rank.

Data problems found in the beta are reported in `anomalies`, not hidden: nodes off
the position grid, groups naming unknown nodes, sub-trees that overlap, and so on.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .dataset import Dataset, DatasetError

GRID = 600  # nodes sit on a 600-unit grid in both directions


@dataclass(frozen=True)
class Entry:
    id: str
    definition_id: str
    spell_id: str
    max_ranks: int


@dataclass(frozen=True)
class Node:
    id: str
    tree_id: str
    x: int
    y: int
    entries: tuple[Entry, ...]
    sub_tree: int  # index into ClassTree.sub_trees, or -1 if not in any

    @property
    def max_ranks(self) -> int:
        """Ranks that can be bought. 0 for a node with no entry."""
        return self.entries[0].max_ranks if self.entries else 0

    @property
    def spell_id(self) -> str:
        return self.entries[0].spell_id if self.entries else ""


@dataclass(frozen=True)
class Gate:
    """Spend `required` in `counted` to unlock `gated`."""
    cond_id: str
    required: int
    counted: frozenset[str]
    gated: frozenset[str]


@dataclass(frozen=True)
class Edge:
    left: str
    right: str
    type: int


@dataclass(frozen=True)
class PointSource:
    level: int
    amount: int
    entry_id: str  # "" for a plain level source; otherwise needs that entry owned


@dataclass(frozen=True)
class Budget:
    currency_id: str
    cap: int  # 0 means uncapped
    sources: tuple[PointSource, ...]

    def points(self, level: int, owned_entries: Iterable[str] = ()) -> int:
        """Points available to a character of `level`.

        `owned_entries` are node entries the character holds elsewhere (the legacy
        "Talented" perk grants extra points at levels 9 down to 5). The currency's
        own cap still applies, so such points arrive sooner but never exceed it.
        """
        owned = set(owned_entries)
        total = sum(
            s.amount for s in self.sources
            if s.level <= level and (not s.entry_id or s.entry_id in owned)
        )
        return min(total, self.cap) if self.cap else total


@dataclass
class ClassTree:
    class_id: int
    class_name: str
    tree_id: str
    skill_line_id: str
    nodes: dict[str, Node]
    edges: tuple[Edge, ...]
    gates: tuple[Gate, ...]
    sub_trees: tuple[frozenset[str], ...]
    budget: Budget
    cost_per_rank: int
    anomalies: list[str] = field(default_factory=list)
    gates_by_node: dict[str, list[Gate]] = field(init=False)
    edges_into: dict[str, list[Edge]] = field(init=False)

    def __post_init__(self) -> None:
        self.gates_by_node = defaultdict(list)
        for gate in self.gates:
            for node_id in gate.gated:
                self.gates_by_node[node_id].append(gate)
        self.edges_into = defaultdict(list)
        for edge in self.edges:
            self.edges_into[edge.right].append(edge)


@dataclass
class TreeModel:
    build: str | None
    trees: dict[int, ClassTree]  # class ID -> tree
    anomalies: list[str]

    def by_name(self, name: str) -> ClassTree:
        for tree in self.trees.values():
            if tree.class_name.lower() == name.lower():
                return tree
        raise KeyError(name)

    def all_anomalies(self) -> list[str]:
        out = list(self.anomalies)
        for tree in self.trees.values():
            out.extend(f"{tree.class_name} (tree {tree.tree_id}): {a}" for a in tree.anomalies)
        return out


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _grid_anomalies(nodes: dict[str, Node]) -> list[str]:
    """Nodes whose position does not fit the grid their neighbours use.

    Rows (y) share one offset across the whole tree. Columns (x) share one offset
    per sub-tree, because the three sub-trees are laid out side by side at
    different horizontal offsets.
    """
    notes: list[str] = []

    def check(members: list[Node], axis: str, label: str) -> None:
        values = Counter((n.x if axis == "x" else n.y) % GRID for n in members)
        if len(members) < 3 or not values:
            return
        common = values.most_common(1)[0][0]
        for n in members:
            v = n.x if axis == "x" else n.y
            if v % GRID != common:
                notes.append(f"node {n.id} is off the position grid ({axis}={v}; {label} uses ...{common} mod {GRID})")

    check(list(nodes.values()), "y", "this tree")
    by_sub: dict[int, list[Node]] = defaultdict(list)
    for n in nodes.values():
        by_sub[n.sub_tree].append(n)
    for index, members in by_sub.items():
        check(members, "x", f"sub-tree {index}" if index >= 0 else "unassigned nodes")
    return notes


def load_model(ds: Dataset) -> TreeModel:
    """Build the model from a complete dataset. Raises DatasetError if a needed
    table is missing; data oddities are collected as anomalies instead."""
    anomalies: list[str] = []
    classes = {_int(r["ID"]): r.get("Name_lang", "") for r in ds.rows("ChrClasses") if r.get("ID")}

    # --- tree -> class ---
    skill_classes: dict[str, set[int]] = defaultdict(set)
    skill_has_wildcard: set[str] = set()
    for r in ds.rows("SkillRaceClassInfo"):
        mask = _int(r["ClassMask"])
        if mask == -1:
            skill_has_wildcard.add(r["SkillID"])
            continue
        for class_id in classes:
            if mask & (1 << (class_id - 1)):
                skill_classes[r["SkillID"]].add(class_id)

    tree_class: dict[str, tuple[int, str]] = {}  # tree id -> (class id, skill id)
    claimed: dict[int, str] = {}
    for r in ds.rows("SkillLineXTraitTree"):
        tree_id, skill = r["TraitTreeID"], r["SkillLineID"]
        found = skill_classes.get(skill, set())
        if skill in skill_has_wildcard or len(found) != 1:
            names = ", ".join(sorted(classes[c] for c in found)) or "none"
            anomalies.append(f"tree {tree_id}: skill line {skill} maps to {len(found)} class(es) ({names}); expected exactly one")
            continue
        class_id = next(iter(found))
        if class_id in claimed:
            anomalies.append(f"class {classes[class_id]} is claimed by both tree {claimed[class_id]} and tree {tree_id}; ignoring {tree_id}")
            continue
        claimed[class_id] = tree_id
        tree_class[tree_id] = (class_id, skill)

    # --- shared tables, read once ---
    tree_ids = set(tree_class)
    node_rows = {r["ID"]: r for r in ds.rows("TraitNode") if r["TraitTreeID"] in tree_ids}
    entry_rows = {r["ID"]: r for r in ds.rows("TraitNodeEntry")}
    def_rows = {r["ID"]: r for r in ds.rows("TraitDefinition")}
    per_node: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in ds.rows("TraitNodeXTraitNodeEntry"):
        if m["TraitNodeID"] in node_rows:
            per_node[m["TraitNodeID"]].append((_int(m.get("_Index", "0")), m["TraitNodeEntryID"]))

    group_nodes: dict[str, set[str]] = defaultdict(set)
    for r in ds.rows("TraitNodeGroupXTraitNode"):
        group_nodes[r["TraitNodeGroupID"]].add(r["TraitNodeID"])
    cond_groups: dict[str, list[str]] = defaultdict(list)
    for r in ds.rows("TraitNodeGroupXTraitCond"):
        cond_groups[r["TraitCondID"]].append(r["TraitNodeGroupID"])
    cond_nodes: dict[str, list[str]] = defaultdict(list)
    for r in ds.rows("TraitNodeXTraitCond"):
        cond_nodes[r["TraitCondID"]].append(r["TraitNodeID"])
    conds_by_tree: dict[str, list[dict]] = defaultdict(list)
    for r in ds.rows("TraitCond"):
        if r["TraitTreeID"] in tree_ids:
            conds_by_tree[r["TraitTreeID"]].append(r)

    currency_of: dict[str, str] = {}
    for r in sorted(ds.rows("TraitTreeXTraitCurrency"), key=lambda r: _int(r.get("_Index", "0"))):
        currency_of.setdefault(r["TraitTreeID"], r["TraitCurrencyID"])
    cap_of = {r["ID"]: _int(r["SourcedMax"]) for r in ds.rows("TraitCurrency")}
    sources_of: dict[str, list[PointSource]] = defaultdict(list)
    for r in ds.rows("TraitCurrencySource"):
        if r.get("QuestID", "0") not in ("", "0") or r.get("AchievementID", "0") not in ("", "0"):
            continue  # needs outside state (achievements, quests): not level-based
        entry = r.get("TraitNodeEntryID", "0")
        sources_of[r["TraitCurrencyID"]].append(
            PointSource(_int(r["PlayerLevel"]), _int(r["Amount"]), "" if entry in ("", "0") else entry))
    cost_rows = {r["ID"]: r for r in ds.rows("TraitCost")}
    cost_id_of = {r["TraitTreeID"]: r["TraitCostID"] for r in ds.rows("TraitTreeXTraitCost")}

    trees: dict[int, ClassTree] = {}
    for tree_id, (class_id, skill) in tree_class.items():
        notes: list[str] = []
        currency = currency_of.get(tree_id)
        if currency is None:
            anomalies.append(f"tree {tree_id}: no point currency; skipped")
            continue

        # nodes (sub-tree index filled in below)
        raw_nodes = {n: r for n, r in node_rows.items() if r["TraitTreeID"] == tree_id}
        nodes: dict[str, Node] = {}
        for node_id, r in raw_nodes.items():
            entries: list[Entry] = []
            for _, entry_id in sorted(per_node.get(node_id, [])):
                e = entry_rows.get(entry_id)
                d = def_rows.get(e["TraitDefinitionID"]) if e else None
                if e is None or d is None:
                    notes.append(f"node {node_id}: entry {entry_id} or its definition is missing")
                    continue
                entries.append(Entry(entry_id, e["TraitDefinitionID"], d["SpellID"], _int(e["MaxRanks"])))
            if not entries:
                notes.append(f"node {node_id} has no usable entry and cannot be purchased")
            elif len(entries) > 1:
                notes.append(f"node {node_id} has {len(entries)} entries (choice nodes are not supported; using the first)")
            nodes[node_id] = Node(node_id, tree_id, _int(r["PosX"]), _int(r["PosY"]), tuple(entries), -1)

        # gates
        gates: list[Gate] = []
        for c in conds_by_tree.get(tree_id, []):
            if _int(c["SpentAmountRequired"]) == 0:
                continue  # nothing required: always satisfied, so it constrains nothing
            if c["CondType"] != "0" or c["TraitCurrencyID"] != currency:
                notes.append(f"condition {c['ID']} (type {c['CondType']}, currency {c['TraitCurrencyID']}) is not a spend condition and was ignored")
                continue
            counted = set(group_nodes.get(c["TraitNodeGroupID"], set()))
            if c["TraitNodeID"] not in ("", "0"):
                counted.add(c["TraitNodeID"])
            gated = set(cond_nodes.get(c["ID"], []))
            for g in cond_groups.get(c["ID"], []):
                gated |= group_nodes.get(g, set())
            unknown = {n for n in counted | gated if n not in nodes}
            if unknown:
                notes.append(f"condition {c['ID']} names {len(unknown)} node(s) not in the tree's node table: {', '.join(sorted(unknown)[:4])}")
            gated &= set(nodes)
            counted &= set(nodes)
            if not gated:
                notes.append(f"condition {c['ID']} gates no known node")
                continue
            gates.append(Gate(c["ID"], _int(c["SpentAmountRequired"]), frozenset(counted), frozenset(gated)))

        # sub-trees: the top-tier gates each count one whole sub-tree
        sub_trees: list[frozenset[str]] = []
        if gates:
            top = max(g.required for g in gates)
            groups = [g.counted for g in gates if g.required == top]
            groups.sort(key=lambda s: min((nodes[n].x for n in s), default=0))
            seen: set[str] = set()
            for s in groups:
                if seen & s:
                    notes.append(f"top-tier sub-tree groups overlap on {len(seen & s)} node(s)")
                seen |= s
                sub_trees.append(frozenset(s))
            missing = set(nodes) - seen
            if missing:
                notes.append(f"{len(missing)} node(s) belong to no sub-tree: {', '.join(sorted(missing)[:4])}")
            for index, members in enumerate(sub_trees):
                for n in members:
                    nodes[n] = Node(n, tree_id, nodes[n].x, nodes[n].y, nodes[n].entries, index)

        notes.extend(_grid_anomalies(nodes))

        edges = tuple(
            Edge(r["LeftTraitNodeID"], r["RightTraitNodeID"], _int(r["Type"]))
            for r in ds.rows("TraitEdge")
            if r["LeftTraitNodeID"] in nodes and r["RightTraitNodeID"] in nodes
        )

        cost_row = cost_rows.get(cost_id_of.get(tree_id, ""))
        if cost_row is None:
            notes.append("no cost per rank recorded; assuming 1")
        budget = Budget(currency, cap_of.get(currency, 0), tuple(sorted(sources_of.get(currency, []), key=lambda s: s.level)))
        trees[class_id] = ClassTree(
            class_id, classes[class_id], tree_id, skill, nodes, edges, tuple(gates),
            tuple(sub_trees), budget, _int(cost_row["Amount"], 1) if cost_row else 1, notes)

    return TreeModel(ds.version, trees, anomalies)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    from .fetch import TABLES
    from .update import UpdateError, get_active

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Summarise a build's class talent trees and the data problems in them.")
    ap.add_argument("--build", help="default: the active build")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--all", action="store_true", help="list every anomaly, not just the first 25")
    args = ap.parse_args(argv)

    try:
        build = args.build or get_active(args.data_dir)
    except UpdateError as exc:
        print(f"ERROR: {exc}")
        return 2
    if build is None:
        print("No build is installed.")
        return 2
    raw = args.data_dir / "raw" / build
    missing = [t for t in TABLES if not (raw / f"{t}.csv").is_file()]
    if missing:
        print(f"Build {build} lacks {len(missing)} table(s) ({', '.join(missing[:3])}...). "
              "Run: python -m wowcalc.update complete")
        return 2
    try:
        model = load_model(Dataset(raw, build))
    except DatasetError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(f"Build {build}: {len(model.trees)} class trees\n")
    for tree in sorted(model.trees.values(), key=lambda t: t.class_name):
        sizes = "/".join(str(len(s)) for s in tree.sub_trees) or "none"
        print(f"  {tree.class_name:8} tree {tree.tree_id:>4}  {len(tree.nodes):>3} nodes  "
              f"sub-trees {sizes:9} {len(tree.gates):>2} unlock gates  {len(tree.edges):>2} links  "
              f"{tree.budget.points(60)} points at level 60")
    notes = model.all_anomalies()
    print(f"\n{len(notes)} data anomalie(s)" + (":" if notes else "."))
    for line in (notes if args.all else notes[:25]):
        print(f"  - {line}")
    if len(notes) > 25 and not args.all:
        print(f"  ... {len(notes) - 25} more (use --all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
