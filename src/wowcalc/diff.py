"""Change report between two builds (plan section 5.3).

Two layers:

  tables   a footprint of every table: rows added, removed, and changed.
  detail   what changed for talents a player can actually place: nodes, ranks,
           prerequisites (edges), spell text, effect values, scaling curves.
           Changes to things no node uses are counted in `tables` but kept out
           of the detail, so the report answers "does this build change the
           calculator's inputs?".

Talents are matched across builds by node ID. If many nodes disappear and
reappear with the same spells, the report says the IDs may have been renumbered
instead of presenting it as hundreds of unrelated changes.

CLI (PYTHONPATH=src):
    python -m wowcalc.diff                    newest staged vs newest installed
    python -m wowcalc.diff NEW --old OLD      explicit builds
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .dataset import Dataset, DatasetError, list_builds, open_dataset
from .fetch import TABLES
from .validate import ValidationReport, _id_key, validate_dataset

SPELL_TEXT_FIELDS = ("Description_lang", "AuraDescription_lang", "NameSubtext_lang")
TRAIT_TABLES = ("TraitTree", "TraitNode", "TraitNodeEntry", "TraitNodeXTraitNodeEntry",
                "TraitDefinition", "TraitEdge")


def _same(a: str, b: str) -> bool:
    """Equal as text, or as numbers (so 2 and 2.0 do not count as a change)."""
    if a == b:
        return True
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _excerpt(a: str, b: str, before: int = 25, after: int = 45) -> tuple[str, str]:
    """Both texts, trimmed to the window around where they first differ."""
    a, b = " ".join(a.split()), " ".join(b.split())
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    start = max(0, p - before)

    def cut(text: str) -> str:
        return ("…" if start > 0 else "") + text[start:p + after] + ("…" if p + after < len(text) else "")

    return cut(a), cut(b)


# --------------------------------------------------------------------------
# Report type
# --------------------------------------------------------------------------

DETAIL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("trees_added", "Trees added"),
    ("trees_removed", "Trees removed"),
    ("trees_renamed", "Trees renamed"),
    ("nodes_added", "Talent nodes added"),
    ("nodes_removed", "Talent nodes removed"),
    ("nodes_moved", "Talent nodes moved"),
    ("entry_changes", "Rank / entry / spell changes on nodes"),
    ("edges_added", "Prerequisite links added"),
    ("edges_removed", "Prerequisite links removed"),
    ("definitions_added", "Talent definitions added"),
    ("definitions_removed", "Talent definitions removed"),
    ("definitions_changed", "Talent definitions changed"),
    ("spell_text_changes", "Spell name / text changes (placed talents)"),
    ("spell_presence_changes", "Spell data appeared or disappeared (placed talents)"),
    ("effects_added", "Spell effects added (placed talents)"),
    ("effects_removed", "Spell effects removed (placed talents)"),
    ("effects_changed", "Spell effect values changed (placed talents)"),
    ("effect_points_changed", "Talent scaling links changed"),
    ("curve_changes", "Scaling curves changed"),
)


@dataclass
class ChangeReport:
    old_version: str | None
    new_version: str | None
    schema: dict = field(default_factory=dict)
    tables: dict = field(default_factory=dict)
    trees_added: list = field(default_factory=list)
    trees_removed: list = field(default_factory=list)
    trees_renamed: list = field(default_factory=list)
    nodes_added: list = field(default_factory=list)
    nodes_removed: list = field(default_factory=list)
    nodes_moved: list = field(default_factory=list)
    entry_changes: list = field(default_factory=list)
    edges_added: list = field(default_factory=list)
    edges_removed: list = field(default_factory=list)
    definitions_added: list = field(default_factory=list)
    definitions_removed: list = field(default_factory=list)
    definitions_changed: list = field(default_factory=list)
    spell_text_changes: list = field(default_factory=list)
    spell_presence_changes: list = field(default_factory=list)
    effects_added: list = field(default_factory=list)
    effects_removed: list = field(default_factory=list)
    effects_changed: list = field(default_factory=list)
    effect_points_changed: list = field(default_factory=list)
    curve_changes: list = field(default_factory=list)
    issue_value_changes: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name, _ in DETAIL_SECTIONS}

    @property
    def talent_change_count(self) -> int:
        return sum(self.counts().values())

    @property
    def has_talent_changes(self) -> bool:
        return self.talent_change_count > 0

    @property
    def has_any_changes(self) -> bool:
        return (
            self.has_talent_changes
            or bool(self.schema)
            or any(t["added"] or t["removed"] or t["changed"] for t in self.tables.values())
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["counts"] = self.counts()
        return data


# --------------------------------------------------------------------------
# Talent model
# --------------------------------------------------------------------------

class _Model:
    """One build's talent structure, resolved to names."""

    def __init__(self, ds: Dataset):
        self.trees = {r["ID"]: r.get("TitleText_lang", "") for r in ds.rows("TraitTree")}
        self.nodes = {
            r["ID"]: {"tree": r["TraitTreeID"], "x": r["PosX"], "y": r["PosY"]}
            for r in ds.rows("TraitNode")
        }
        self.entries = {
            r["ID"]: {"definition": r["TraitDefinitionID"], "ranks": r["MaxRanks"]}
            for r in ds.rows("TraitNodeEntry")
        }
        self.definition_rows = {r["ID"]: r for r in ds.rows("TraitDefinition")}
        self.node_entries: dict[str, list[str]] = defaultdict(list)
        for m in ds.rows("TraitNodeXTraitNodeEntry"):
            if m["TraitNodeID"] in self.nodes and m["TraitNodeEntryID"] in self.entries:
                self.node_entries[m["TraitNodeID"]].append(m["TraitNodeEntryID"])
        for entry_ids in self.node_entries.values():
            entry_ids.sort(key=_id_key)
        self.edges = {(r["LeftTraitNodeID"], r["RightTraitNodeID"]) for r in ds.rows("TraitEdge")}
        self.spell_names: dict[str, str] = {}
        if ds.has_table("SpellName"):
            cols = ds.columns("SpellName")
            i_id, i_name = cols.index("ID"), cols.index("Name_lang")
            for vals in ds.iter_values("SpellName"):
                if len(vals) > max(i_id, i_name):
                    self.spell_names[vals[i_id]] = vals[i_name]

    def spell_of_entry(self, entry_id: str) -> str:
        d = self.definition_rows.get(self.entries[entry_id]["definition"])
        return d["SpellID"] if d else ""

    def name_of_definition(self, def_id: str) -> str:
        d = self.definition_rows.get(def_id)
        if d is None:
            return f"definition {def_id}"
        if d.get("OverrideName_lang"):
            return d["OverrideName_lang"]
        spell = d["SpellID"]
        if spell in ("", "0"):
            return f"definition {def_id}"
        return self.spell_names.get(spell) or f"spell {spell}"

    def node_label(self, node_id: str) -> str:
        counts: dict[str, int] = {}
        for e in self.node_entries.get(node_id, []):
            name = self.name_of_definition(self.entries[e]["definition"])
            counts[name] = counts.get(name, 0) + 1
        names = " / ".join(f"{n} ×{k}" if k > 1 else n for n, k in counts.items())
        return f"{names or '(no entry)'} [node {node_id}]"

    def spell_ref(self, spell_id: str) -> str:
        """'16173 (Totemic Focus)', or just the id when the name is unknown."""
        if spell_id in ("", "0"):
            return "-"
        name = self.spell_names.get(spell_id)
        return f"{spell_id} ({name})" if name else spell_id

    def tree_label(self, tree_id: str) -> str:
        name = self.trees.get(tree_id, "")
        return f"{name} (tree {tree_id})" if name else f"tree {tree_id}"

    @property
    def placed_definitions(self) -> set[str]:
        return {self.entries[e]["definition"] for es in self.node_entries.values() for e in es}

    @property
    def placed_spells(self) -> set[str]:
        out = set()
        for def_id in self.placed_definitions:
            d = self.definition_rows.get(def_id)
            if d and d["SpellID"] not in ("", "0"):
                out.add(d["SpellID"])
        return out

    def spells_on_node(self, node_id: str) -> set[str]:
        return {s for e in self.node_entries.get(node_id, []) if (s := self.spell_of_entry(e)) not in ("", "0")}


# --------------------------------------------------------------------------
# Comparison helpers
# --------------------------------------------------------------------------

def _schema_changes(old: Dataset, new: Dataset) -> dict:
    out = {}
    for table in TABLES:
        o, n = old.has_table(table), new.has_table(table)
        if o != n:
            out[table] = {"tableAdded": n, "tableRemoved": o, "columnsAdded": [], "columnsRemoved": []}
        elif o:
            oc, nc = old.columns(table), new.columns(table)
            added = [c for c in nc if c not in oc]
            removed = [c for c in oc if c not in nc]
            if added or removed:
                out[table] = {"tableAdded": False, "tableRemoved": False,
                              "columnsAdded": added, "columnsRemoved": removed}
    return out


def _table_changes(old: Dataset, new: Dataset, table: str) -> dict:
    """Rows added, removed, and differing, comparing only columns both builds have."""
    oc, nc = old.columns(table), new.columns(table)
    common = [c for c in oc if c in nc]
    oi, ni = [oc.index(c) for c in common], [nc.index(c) for c in common]
    key = common.index("ID") if "ID" in common else None

    def digest(ds: Dataset, idx: list[int]):
        out = {} if key is not None else Counter()
        for row in ds.iter_values(table):
            vals = tuple(row[i] if i < len(row) else "" for i in idx)
            if key is not None:
                out[vals[key]] = hash(vals)
            else:
                out[hash(vals)] += 1
        return out

    o, n = digest(old, oi), digest(new, ni)
    if key is not None:
        added = len(n.keys() - o.keys())
        removed = len(o.keys() - n.keys())
        changed = sum(1 for k in o.keys() & n.keys() if o[k] != n[k])
    else:
        added = sum((n - o).values())
        removed = sum((o - n).values())
        changed = 0
    return {"old": old.row_count(table), "new": new.row_count(table),
            "added": added, "removed": removed, "changed": changed, "keyed": key is not None}


def _stream_dicts(ds: Dataset, table: str, column: str, wanted: set[str]):
    cols = ds.columns(table)
    i = cols.index(column)
    for vals in ds.iter_values(table):
        if i < len(vals) and vals[i] in wanted:
            yield dict(zip(cols, vals))


def _compare_keyed(old_rows: dict, new_rows: dict):
    added = [new_rows[k] for k in sorted(new_rows.keys() - old_rows.keys(), key=_id_key)]
    removed = [old_rows[k] for k in sorted(old_rows.keys() - new_rows.keys(), key=_id_key)]
    changed = []
    for k in sorted(old_rows.keys() & new_rows.keys(), key=_id_key):
        o, n = old_rows[k], new_rows[k]
        diffs = {c: [o[c], n[c]] for c in o if c in n and c != "ID" and not _same(o[c], n[c])}
        if diffs:
            changed.append((o, n, diffs))
    return added, removed, changed


def _diff_text(diffs: dict, limit: int = 4) -> str:
    parts = [f"{c} {_short(a, 30)!r} -> {_short(b, 30)!r}" for c, (a, b) in list(diffs.items())[:limit]]
    if len(diffs) > limit:
        parts.append(f"+{len(diffs) - limit} more column(s)")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Main comparison
# --------------------------------------------------------------------------

def compare_datasets(
    old: Dataset,
    new: Dataset,
    old_report: ValidationReport | None = None,
    new_report: ValidationReport | None = None,
) -> ChangeReport:
    r = ChangeReport(old.version, new.version)
    r.schema = _schema_changes(old, new)

    for table in TABLES:
        if old.has_table(table) and new.has_table(table):
            r.tables[table] = _table_changes(old, new, table)

    missing = [t for t in TRAIT_TABLES if not (old.has_table(t) and new.has_table(t))]
    if missing:
        r.notes.append("Talent detail skipped: trait table(s) missing in one build: " + ", ".join(missing))
        return r

    om, nm = _Model(old), _Model(new)
    _compare_trees(om, nm, r)
    _compare_nodes(om, nm, r)
    _compare_edges(om, nm, r)
    _compare_definitions(om, nm, r)

    relevant_spells = om.placed_spells | nm.placed_spells
    relevant_defs = om.placed_definitions | nm.placed_definitions
    if old.has_table("Spell") and new.has_table("Spell"):
        _compare_spells(old, new, om, nm, relevant_spells, r)
    if old.has_table("SpellEffect") and new.has_table("SpellEffect"):
        _compare_effects(old, new, om, nm, relevant_spells, r)
    if all(d.has_table(t) for d in (old, new) for t in ("TraitDefinitionEffectPoints", "CurvePoint")):
        _compare_scaling(old, new, om, nm, relevant_defs, r)

    if old_report is not None and new_report is not None:
        _compare_issue_values(old_report, new_report, r)
    return r


def _compare_trees(om: _Model, nm: _Model, r: ChangeReport) -> None:
    for tid in sorted(nm.trees.keys() - om.trees.keys(), key=_id_key):
        r.trees_added.append({"treeId": tid, "text": f"Added {nm.tree_label(tid)}"})
    for tid in sorted(om.trees.keys() - nm.trees.keys(), key=_id_key):
        r.trees_removed.append({"treeId": tid, "text": f"Removed {om.tree_label(tid)}"})
    for tid in sorted(om.trees.keys() & nm.trees.keys(), key=_id_key):
        if om.trees[tid] != nm.trees[tid]:
            r.trees_renamed.append({"treeId": tid, "old": om.trees[tid], "new": nm.trees[tid],
                                    "text": f"Tree {tid} renamed {om.trees[tid]!r} -> {nm.trees[tid]!r}"})


def _compare_nodes(om: _Model, nm: _Model, r: ChangeReport) -> None:
    added = sorted(nm.nodes.keys() - om.nodes.keys(), key=_id_key)
    removed = sorted(om.nodes.keys() - nm.nodes.keys(), key=_id_key)
    for nid in added:
        n = nm.nodes[nid]
        r.nodes_added.append({"nodeId": nid, "treeId": n["tree"], "label": nm.node_label(nid),
                              "text": f"{nm.node_label(nid)} in {nm.tree_label(n['tree'])} at ({n['x']}, {n['y']})"})
    for nid in removed:
        n = om.nodes[nid]
        r.nodes_removed.append({"nodeId": nid, "treeId": n["tree"], "label": om.node_label(nid),
                                "text": f"{om.node_label(nid)} from {om.tree_label(n['tree'])}"})

    for nid in sorted(om.nodes.keys() & nm.nodes.keys(), key=_id_key):
        o, n = om.nodes[nid], nm.nodes[nid]
        label = nm.node_label(nid)
        if o["tree"] != n["tree"] or not (_same(o["x"], n["x"]) and _same(o["y"], n["y"])):
            where = (f"tree {o['tree']} -> {n['tree']}, " if o["tree"] != n["tree"] else "")
            r.nodes_moved.append({
                "nodeId": nid, "treeId": n["tree"], "label": label,
                "old": {"tree": o["tree"], "x": o["x"], "y": o["y"]},
                "new": {"tree": n["tree"], "x": n["x"], "y": n["y"]},
                "text": f"{label}: {where}({o['x']}, {o['y']}) -> ({n['x']}, {n['y']})"})
        _compare_node_entries(om, nm, nid, n["tree"], label, r)

    left = {c["old"] for c in r.entry_changes if c["kind"] == "spell"} - {"", "0"}
    arrived = {c["new"] for c in r.entry_changes if c["kind"] == "spell"} - {"", "0"}
    moved_spells = sorted(left & arrived, key=_id_key)
    if len(moved_spells) >= 2:
        shown = ", ".join(nm.spell_ref(s) if nm.spell_names.get(s) else om.spell_ref(s) for s in moved_spells[:4])
        more = f" and {len(moved_spells) - 4} more" if len(moved_spells) > 4 else ""
        r.notes.append(
            f"{len(moved_spells)} spells moved from one talent slot to another ({shown}{more}). "
            "Talent slots (nodes and definitions) are not stable identities in this build pair; "
            "anything tied to a talent should be keyed by spell, not by slot.")

    removed_spells = set().union(*(om.spells_on_node(n) for n in removed)) if removed else set()
    added_spells = set().union(*(nm.spells_on_node(n) for n in added)) if added else set()
    if (len(added) >= 5 and len(removed) >= 5 and removed_spells
            and len(removed_spells & added_spells) / len(removed_spells) >= 0.8):
        r.notes.append(
            f"{len(removed)} nodes were removed and {len(added)} added with largely the same spells: "
            "node IDs may have been renumbered rather than the talents changing. "
            "Treat node-level differences below with caution.")


def _compare_node_entries(om: _Model, nm: _Model, nid: str, tree: str, label: str, r: ChangeReport) -> None:
    oe, ne = om.node_entries.get(nid, []), nm.node_entries.get(nid, [])

    def rec(kind: str, text: str, **extra) -> None:
        r.entry_changes.append({"nodeId": nid, "treeId": tree, "label": label, "kind": kind,
                                "text": f"{label}: {text}", **extra})

    for eid in sorted(set(ne) - set(oe), key=_id_key):
        rec("entry_added", f"entry {eid} added ({nm.name_of_definition(nm.entries[eid]['definition'])})", entryId=eid)
    for eid in sorted(set(oe) - set(ne), key=_id_key):
        rec("entry_removed", f"entry {eid} removed ({om.name_of_definition(om.entries[eid]['definition'])})", entryId=eid)
    for eid in sorted(set(oe) & set(ne), key=_id_key):
        o, n = om.entries[eid], nm.entries[eid]
        if not _same(o["ranks"], n["ranks"]):
            rec("max_ranks", f"max ranks {o['ranks']} -> {n['ranks']}", entryId=eid, old=o["ranks"], new=n["ranks"])
        if o["definition"] != n["definition"]:
            rec("definition", f"definition {o['definition']} -> {n['definition']}", entryId=eid,
                old=o["definition"], new=n["definition"])
        os_, ns = om.spell_of_entry(eid), nm.spell_of_entry(eid)
        if os_ != ns:
            rec("spell", f"spell {om.spell_ref(os_)} -> {nm.spell_ref(ns)}", entryId=eid, old=os_, new=ns)


def _compare_edges(om: _Model, nm: _Model, r: ChangeReport) -> None:
    def rec(model: _Model, pair, verb: str) -> dict:
        left, right = pair
        tree = (model.nodes.get(left) or model.nodes.get(right) or {}).get("tree", "")
        return {"left": left, "right": right, "treeId": tree,
                "text": f"{verb} link {model.node_label(left)} -> {model.node_label(right)}"}

    for pair in sorted(nm.edges - om.edges, key=lambda p: (_id_key(p[0]), _id_key(p[1]))):
        r.edges_added.append(rec(nm, pair, "Added"))
    for pair in sorted(om.edges - nm.edges, key=lambda p: (_id_key(p[0]), _id_key(p[1]))):
        r.edges_removed.append(rec(om, pair, "Removed"))


def _compare_definitions(om: _Model, nm: _Model, r: ChangeReport) -> None:
    oplaced, nplaced = om.placed_definitions, nm.placed_definitions
    added, removed, changed = _compare_keyed(om.definition_rows, nm.definition_rows)
    for d in added:
        placed = d["ID"] in nplaced
        r.definitions_added.append({
            "definitionId": d["ID"], "spellId": d["SpellID"], "placed": placed,
            "text": f"Definition {d['ID']} ({nm.name_of_definition(d['ID'])}, spell {d['SpellID']}), "
                    f"{'placed on a node' if placed else 'not placed on any node'}"})
    for d in removed:
        placed = d["ID"] in oplaced
        r.definitions_removed.append({
            "definitionId": d["ID"], "spellId": d["SpellID"], "placed": placed,
            "text": f"Definition {d['ID']} ({om.name_of_definition(d['ID'])}, spell {d['SpellID']}), "
                    f"{'was placed on a node' if placed else 'was not placed on any node'}"})
    for o, n, diffs in changed:
        placed = n["ID"] in nplaced or o["ID"] in oplaced
        r.definitions_changed.append({
            "definitionId": n["ID"], "placed": placed, "changes": diffs,
            "text": f"Definition {n['ID']} ({nm.name_of_definition(n['ID'])}), "
                    f"{'placed' if placed else 'unplaced'}: {_diff_text(diffs)}"})


def _compare_spells(old: Dataset, new: Dataset, om: _Model, nm: _Model, spells: set[str], r: ChangeReport) -> None:
    if not spells:
        return
    orows = {d["ID"]: d for d in _stream_dicts(old, "Spell", "ID", spells)}
    nrows = {d["ID"]: d for d in _stream_dicts(new, "Spell", "ID", spells)}
    fields = [f for f in SPELL_TEXT_FIELDS if f in old.columns("Spell") and f in new.columns("Spell")]

    def name(sid: str) -> str:
        return nm.spell_names.get(sid) or om.spell_names.get(sid) or f"spell {sid}"

    for sid in sorted(spells, key=_id_key):
        o_present, n_present = sid in orows, sid in nrows
        if o_present != n_present:
            r.spell_presence_changes.append({
                "spellId": sid, "presentNow": n_present,
                "text": f"{name(sid)} (spell {sid}): spell data {'appeared' if n_present else 'disappeared'}"})
            continue
        if sid in om.spell_names or sid in nm.spell_names:
            on, nn = om.spell_names.get(sid, ""), nm.spell_names.get(sid, "")
            if on != nn:
                r.spell_text_changes.append({
                    "spellId": sid, "field": "Name_lang", "old": on, "new": nn,
                    "text": f"spell {sid}: Name_lang {_short(on, 40)!r} -> {_short(nn, 40)!r}"})
        if o_present:
            for f in fields:
                a, b = orows[sid].get(f, ""), nrows[sid].get(f, "")
                if a != b:
                    x, y = _excerpt(a, b)
                    r.spell_text_changes.append({
                        "spellId": sid, "field": f, "old": a, "new": b,
                        "text": f"{name(sid)} (spell {sid}): {f} changed: {x!r} -> {y!r}"})


def _compare_effects(old: Dataset, new: Dataset, om: _Model, nm: _Model, spells: set[str], r: ChangeReport) -> None:
    if not spells:
        return
    orows = {d["ID"]: d for d in _stream_dicts(old, "SpellEffect", "SpellID", spells)}
    nrows = {d["ID"]: d for d in _stream_dicts(new, "SpellEffect", "SpellID", spells)}
    added, removed, changed = _compare_keyed(orows, nrows)

    def name(sid: str) -> str:
        return nm.spell_names.get(sid) or om.spell_names.get(sid) or f"spell {sid}"

    for d in added:
        r.effects_added.append({"effectId": d["ID"], "spellId": d["SpellID"], "index": d.get("EffectIndex", ""),
                                "text": f"{name(d['SpellID'])} (spell {d['SpellID']}): effect {d.get('EffectIndex', '?')} added "
                                        f"(Effect {d.get('Effect', '?')}, Aura {d.get('EffectAura', '?')}, "
                                        f"base {d.get('EffectBasePointsF', '?')})"})
    for d in removed:
        r.effects_removed.append({"effectId": d["ID"], "spellId": d["SpellID"], "index": d.get("EffectIndex", ""),
                                  "text": f"{name(d['SpellID'])} (spell {d['SpellID']}): effect {d.get('EffectIndex', '?')} removed"})
    for o, n, diffs in changed:
        r.effects_changed.append({
            "effectId": n["ID"], "spellId": n["SpellID"], "index": n.get("EffectIndex", ""), "changes": diffs,
            "text": f"{name(n['SpellID'])} (spell {n['SpellID']}): effect {n.get('EffectIndex', '?')}: {_diff_text(diffs)}"})


def _compare_scaling(old: Dataset, new: Dataset, om: _Model, nm: _Model, defs: set[str], r: ChangeReport) -> None:
    if not defs:
        return
    orows = {d["ID"]: d for d in _stream_dicts(old, "TraitDefinitionEffectPoints", "TraitDefinitionID", defs)}
    nrows = {d["ID"]: d for d in _stream_dicts(new, "TraitDefinitionEffectPoints", "TraitDefinitionID", defs)}
    added, removed, changed = _compare_keyed(orows, nrows)

    def label(row: dict) -> str:
        return nm.name_of_definition(row["TraitDefinitionID"]) if row["TraitDefinitionID"] in nm.definition_rows \
            else om.name_of_definition(row["TraitDefinitionID"])

    for d in added:
        r.effect_points_changed.append({"id": d["ID"], "kind": "added", "definitionId": d["TraitDefinitionID"],
                                        "text": f"{label(d)}: scaling link added (effect {d['EffectIndex']}, curve {d['CurveID']})"})
    for d in removed:
        r.effect_points_changed.append({"id": d["ID"], "kind": "removed", "definitionId": d["TraitDefinitionID"],
                                        "text": f"{label(d)}: scaling link removed (effect {d['EffectIndex']}, curve {d['CurveID']})"})
    for o, n, diffs in changed:
        r.effect_points_changed.append({"id": n["ID"], "kind": "changed", "definitionId": n["TraitDefinitionID"],
                                        "changes": diffs, "text": f"{label(n)}: scaling link changed: {_diff_text(diffs)}"})

    users: dict[str, list[str]] = defaultdict(list)
    for row in list(orows.values()) + list(nrows.values()):
        if row["CurveID"] not in ("", "0"):
            who = label(row)
            if who not in users[row["CurveID"]]:
                users[row["CurveID"]].append(who)
    if not users:
        return

    def curves(ds: Dataset) -> dict[str, list[tuple[int, str, str]]]:
        out: dict[str, list] = defaultdict(list)
        for d in _stream_dicts(ds, "CurvePoint", "CurveID", set(users)):
            try:
                order = int(d["OrderIndex"])
            except ValueError:
                order = 0
            out[d["CurveID"]].append((order, d["Pos_0"], d["Pos_1"]))
        return {k: sorted(v) for k, v in out.items()}

    oc, nc = curves(old), curves(new)
    for cid in sorted(users, key=_id_key):
        a, b = oc.get(cid, []), nc.get(cid, [])
        same = len(a) == len(b) and all(_same(x[1], y[1]) and _same(x[2], y[2]) for x, y in zip(a, b))
        if same:
            continue
        differing = [(x, y) for x, y in zip(a, b) if not (_same(x[1], y[1]) and _same(x[2], y[2]))]
        examples = [{"order": x[0], "old": [x[1], x[2]], "new": [y[1], y[2]]} for x, y in differing[:3]]
        who = ", ".join(users[cid][:3]) + (f" +{len(users[cid]) - 3} more" if len(users[cid]) > 3 else "")
        detail = (f"{len(a)} -> {len(b)} points" if len(a) != len(b) else f"{len(differing)} of {len(a)} points differ")
        r.curve_changes.append({
            "curveId": cid, "oldPoints": len(a), "newPoints": len(b), "differing": len(differing),
            "examples": examples, "usedBy": users[cid],
            "text": f"Curve {cid} ({who}): {detail}"})


def _compare_issue_values(old: ValidationReport, new: ValidationReport, r: ChangeReport) -> None:
    o = {i.key: set(i.values) for i in old.issues if i.values}
    n = {i.key: set(i.values) for i in new.issues if i.values}
    for key in sorted(set(o) | set(n)):
        added = sorted(n.get(key, set()) - o.get(key, set()), key=_id_key)
        removed = sorted(o.get(key, set()) - n.get(key, set()), key=_id_key)
        if added or removed:
            bits = ([f"new: {', '.join(added[:6])}" + (f" +{len(added) - 6}" if len(added) > 6 else "")] if added else []) \
                 + ([f"resolved: {', '.join(removed[:6])}" + (f" +{len(removed) - 6}" if len(removed) > 6 else "")] if removed else [])
            r.issue_value_changes.append({
                "code": key[0], "subject": key[1], "added": added, "removed": removed,
                "text": f"{key[1]}: " + "; ".join(bits)})


# --------------------------------------------------------------------------
# Text output
# --------------------------------------------------------------------------

def format_report(r: ChangeReport, limit: int | None = 10) -> str:
    lines = [f"\nChanges: {r.old_version}  ->  {r.new_version}"]
    for note in r.notes:
        lines.append(f"\nNOTE: {note}")

    if r.schema:
        lines.append("\nSchema changes:")
        for table, s in r.schema.items():
            if s["tableAdded"]:
                lines.append(f"  - {table}: table added")
            elif s["tableRemoved"]:
                lines.append(f"  - {table}: table removed")
            else:
                bits = ([f"added {', '.join(s['columnsAdded'])}"] if s["columnsAdded"] else []) + \
                       ([f"removed {', '.join(s['columnsRemoved'])}"] if s["columnsRemoved"] else [])
                lines.append(f"  - {table}: " + "; ".join(bits))

    moved = [(t, v) for t, v in r.tables.items() if v["added"] or v["removed"] or v["changed"] or v["old"] != v["new"]]
    lines.append("\nTable footprint (rows added / removed / differing):")
    if moved:
        for table, v in moved:
            suffix = "" if v["keyed"] else " (no ID column: differing rows count as added + removed)"
            lines.append(f"  {table:30} {v['old']:>7,} -> {v['new']:>7,}   +{v['added']:,} / -{v['removed']:,} / ~{v['changed']:,}{suffix}")
    else:
        lines.append("  (no table changed)")

    lines.append("\nTalent-relevant changes:")
    if not r.has_talent_changes:
        lines.append("  None. Nothing a player can place in the talent trees changed.")
    for name, title in DETAIL_SECTIONS:
        items = getattr(r, name)
        if not items:
            continue
        lines.append(f"\n  {title} ({len(items)}):")
        shown = items if limit is None else items[:limit]
        for item in shown:
            lines.append(f"    - {item['text']}")
        if len(shown) < len(items):
            lines.append(f"    ... {len(items) - len(shown)} more (use --full)")

    if r.issue_value_changes:
        lines.append("\nData-quality changes (references to missing data):")
        for item in r.issue_value_changes:
            lines.append(f"    - {item['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Change report between two builds.")
    ap.add_argument("version", nargs="?", help="default: newest staged build")
    ap.add_argument("--old", help="default: newest installed build other than VERSION")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--full", action="store_true", help="print every change, not the first 10 per section")
    ap.add_argument("--no-write", action="store_true", help="do not write data/reports/<build>.changes.json")
    args = ap.parse_args(argv)
    data_dir: Path = args.data_dir

    version = args.version
    if version is None:
        staged = list_builds(data_dir / "staging")
        if not staged:
            print("Nothing is staged. Run: python -m wowcalc.fetch")
            return 2
        version = staged[-1].version
    old_version = args.old
    if old_version is None:
        installed = [b for b in list_builds(data_dir / "raw") if b.version != version]
        if not installed:
            print("No installed build to compare against.")
            return 2
        old_version = installed[-1].version

    try:
        old_ds, new_ds = open_dataset(data_dir, old_version), open_dataset(data_dir, version)
        report = compare_datasets(old_ds, new_ds, validate_dataset(old_ds), validate_dataset(new_ds))
    except DatasetError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(format_report(report, limit=None if args.full else 10))
    if not args.no_write:
        out = data_dir / "reports" / f"{version}.changes.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"generatedAt": datetime.now(timezone.utc).isoformat(),
                                   "changes": report.to_dict()}, indent=2) + "\n", encoding="utf-8")
        print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
