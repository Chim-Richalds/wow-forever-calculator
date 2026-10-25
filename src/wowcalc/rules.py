"""Effect rules: data fingerprints and verification-status transitions (plan 5.5).

A rule describes one spell effect of one talent. Rules are identified by
(talent_spell_id, effect_index), never by node or definition: talent slots are
not stable across builds (spells move between them).

Each rule carries a *fingerprint*: short hashes of everything the rule depends on,
in four components:

    effect    the SpellEffect row(s): type, aura, base points, class masks, ...
    ranks     MaxRanks of every talent entry that uses this spell
    scaling   the effect-point operation and the curve's point values for this effect
    text      spell name and descriptions (advisory only)

Row IDs, curve IDs, node positions and number formatting are deliberately left
out, so renumbering or reformatting does not count as a change.

After an update, a rule's fingerprint is compared with the new data:
  * effect/ranks/scaling differ  -> a VERIFIED rule becomes needs_reverification
  * only text differs            -> status kept; flagged as worth a look
  * nothing differs              -> status kept; last_confirmed_build advances
Verified status is never carried across builds silently.

CLI (PYTHONPATH=src):
    python -m wowcalc.rules adopt     add fingerprints to rules that lack them
    python -m wowcalc.rules check     compare rules with a build's data
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .dataset import Dataset, DatasetError, list_builds, open_dataset

SCHEMA_VERSION = 2
INVALIDATING = ("effect", "ranks", "scaling")
ADVISORY = ("text",)

VERIFIED, PENDING, REVERIFY = "verified", "pending", "needs_reverification"

TEXT_FIELDS = ("Description_lang", "AuraDescription_lang", "NameSubtext_lang")
SNAPSHOT_FIELDS = {
    "effect_type": "Effect",
    "aura_id": "EffectAura",
    "base_points": "EffectBasePointsF",
    "misc_value_0": "EffectMiscValue_0",
}
MASK_COLUMNS = ("EffectSpellClassMask_0", "EffectSpellClassMask_1",
                "EffectSpellClassMask_2", "EffectSpellClassMask_3")

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------

def _norm(value) -> str:
    """Numbers compare as numbers (2 == 2.0); anything else compares as text."""
    v = str(value).strip()
    if _INT.match(v):
        return v
    if _FLOAT.match(v):
        return format(float(v), ".10g")
    return v


def _hash(payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _canon(item) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprints_for(ds: Dataset, keys: Iterable[tuple]) -> dict[tuple[str, str], dict[str, str]]:
    """Fingerprint every (spell_id, effect_index) in `keys` with one pass per big table."""
    wanted = {(str(s), str(i)) for s, i in keys}
    spells = {s for s, _ in wanted}
    if not wanted:
        return {}

    # effect rows
    cols = ds.columns("SpellEffect")
    i_sp, i_ix = cols.index("SpellID"), cols.index("EffectIndex")
    effects: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for vals in ds.iter_values("SpellEffect"):
        if len(vals) > max(i_sp, i_ix) and vals[i_sp] in spells:
            effects[(vals[i_sp], _norm(vals[i_ix]))].append(
                {c: _norm(v) for c, v in zip(cols, vals) if c != "ID"})

    # talent slots that currently use these spells
    defs_by_spell: dict[str, list[str]] = defaultdict(list)
    for d in ds.rows("TraitDefinition"):
        if d["SpellID"] in spells:
            defs_by_spell[d["SpellID"]].append(d["ID"])
    all_defs = {d for ds_ in defs_by_spell.values() for d in ds_}

    ranks_by_def: dict[str, list[str]] = defaultdict(list)
    for e in ds.rows("TraitNodeEntry"):
        if e["TraitDefinitionID"] in all_defs:
            ranks_by_def[e["TraitDefinitionID"]].append(_norm(e["MaxRanks"]))

    points_cols = ds.columns("TraitDefinitionEffectPoints")
    skip = {"ID", "TraitDefinitionID", "CurveID", "EffectIndex"}
    ep_by_def: dict[str, list[dict]] = defaultdict(list)
    for ep in ds.rows("TraitDefinitionEffectPoints"):
        if ep["TraitDefinitionID"] in all_defs:
            ep_by_def[ep["TraitDefinitionID"]].append(ep)
    curve_ids = {ep["CurveID"] for eps in ep_by_def.values() for ep in eps
                 if ep.get("CurveID") not in ("", "0")}

    curves: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    if curve_ids:
        cc = ds.columns("CurvePoint")
        i_c, i_o, i_0, i_1 = (cc.index(n) for n in ("CurveID", "OrderIndex", "Pos_0", "Pos_1"))
        for vals in ds.iter_values("CurvePoint"):
            if len(vals) > max(i_c, i_o, i_0, i_1) and vals[i_c] in curve_ids:
                try:
                    order = int(vals[i_o])
                except ValueError:
                    order = 0
                curves[vals[i_c]].append((order, _norm(vals[i_0]), _norm(vals[i_1])))

    # text
    text: dict[str, dict[str, str]] = {s: {} for s in spells}
    if ds.has_table("Spell"):
        sc = ds.columns("Spell")
        i_id = sc.index("ID")
        fields = [(f, sc.index(f)) for f in TEXT_FIELDS if f in sc]
        for vals in ds.iter_values("Spell"):
            if len(vals) > i_id and vals[i_id] in spells:
                text[vals[i_id]] = {f: vals[i] for f, i in fields if i < len(vals)}
    if ds.has_table("SpellName"):
        nc = ds.columns("SpellName")
        i_id, i_nm = nc.index("ID"), nc.index("Name_lang")
        for vals in ds.iter_values("SpellName"):
            if len(vals) > max(i_id, i_nm) and vals[i_id] in spells:
                text[vals[i_id]]["Name_lang"] = vals[i_nm]

    out: dict[tuple[str, str], dict[str, str]] = {}
    for spell, index in wanted:
        rows = sorted(effects.get((spell, index), []), key=_canon)
        defs = defs_by_spell.get(spell, [])
        ranks = sorted(r for d in defs for r in ranks_by_def.get(d, []))
        scaling = []
        for d in defs:
            for ep in ep_by_def.get(d, []):
                if _norm(ep.get("EffectIndex", "")) == index:
                    scaling.append({
                        "operation": {c: _norm(ep[c]) for c in points_cols if c not in skip and c in ep},
                        "points": sorted(curves.get(ep.get("CurveID", ""), [])),
                    })
        scaling.sort(key=_canon)
        out[(spell, index)] = {
            "effect": _hash(rows) if rows else "missing",
            "ranks": _hash(ranks),
            "scaling": _hash(scaling),
            "text": _hash(text.get(spell, {})),
        }
    return out


# --------------------------------------------------------------------------
# Rule documents
# --------------------------------------------------------------------------

def load_rules(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_rules(path: Path, doc: dict) -> None:
    """Write atomically: a crash leaves either the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _key(rule: dict) -> tuple[str, str]:
    return (str(rule["talent_spell_id"]), str(rule["effect_index"]))


@dataclass(frozen=True)
class AdoptResult:
    rule_id: str
    outcome: str  # adopted | already | inconsistent
    detail: str = ""


def adopt_fingerprints(doc: dict, ds: Dataset, build: str) -> tuple[dict, list[AdoptResult]]:
    """Add fingerprints to rules that lack one, after checking each rule's stored
    snapshot against the SpellEffect row it names. Rules that disagree with the
    data are reported and left untouched."""
    new = copy.deepcopy(doc)
    rules = new.get("rules", [])
    wanted = [r for r in rules if not r.get("fingerprint")]
    prints = fingerprints_for(ds, [_key(r) for r in wanted])

    by_id: dict[str, dict] = {}
    if wanted:
        cols = ds.columns("SpellEffect")
        ids = {str(r.get("effect_id")) for r in wanted}
        i_id = cols.index("ID")
        for vals in ds.iter_values("SpellEffect"):
            if len(vals) > i_id and vals[i_id] in ids:
                by_id[vals[i_id]] = dict(zip(cols, vals))

    results: list[AdoptResult] = []
    for rule in rules:
        rid = rule.get("rule_id", "?")
        if rule.get("fingerprint"):
            results.append(AdoptResult(rid, "already"))
            continue
        row = by_id.get(str(rule.get("effect_id")))
        problem = _snapshot_problem(rule, row)
        if problem:
            results.append(AdoptResult(rid, "inconsistent", problem))
            continue
        rule["fingerprint"] = prints[_key(rule)]
        rule["fingerprint_build"] = build
        results.append(AdoptResult(rid, "adopted"))
    if any(r.outcome == "adopted" for r in results):
        new["schema_version"] = SCHEMA_VERSION
    return new, results


def _snapshot_problem(rule: dict, row: dict | None) -> str:
    if row is None:
        return f"effect row {rule.get('effect_id')} not found in this build"
    if _norm(row.get("SpellID", "")) != _norm(rule["talent_spell_id"]):
        return f"effect row {rule['effect_id']} belongs to spell {row.get('SpellID')}, not {rule['talent_spell_id']}"
    if _norm(row.get("EffectIndex", "")) != _norm(rule["effect_index"]):
        return f"effect row {rule['effect_id']} is effect index {row.get('EffectIndex')}, not {rule['effect_index']}"
    for field, column in SNAPSHOT_FIELDS.items():
        if field in rule and _norm(rule[field]) != _norm(row.get(column, "")):
            return f"{field} is {rule[field]} in the rule but {row.get(column)} in the data"
    if "spell_class_mask" in rule:
        actual = [_norm(row.get(c, "0") or "0") for c in MASK_COLUMNS]
        stored = [_norm(v) for v in rule["spell_class_mask"]]
        stored += ["0"] * (4 - len(stored))
        if stored != actual:
            return f"spell_class_mask is {rule['spell_class_mask']} in the rule but {actual} in the data"
    return ""


# --------------------------------------------------------------------------
# Checking and transitions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleCheck:
    rule_id: str
    verification_status: str
    state: str  # unchanged | changed | missing | no_fingerprint
    changed: tuple[str, ...] = ()
    advisory: tuple[str, ...] = ()

    @property
    def invalidates(self) -> bool:
        """Would this move a verified rule to needs_reverification?"""
        return self.verification_status == VERIFIED and self.state != "unchanged"


def check_rules(doc: dict, ds: Dataset) -> list[RuleCheck]:
    rules = doc.get("rules", [])
    current = fingerprints_for(ds, [_key(r) for r in rules])
    checks: list[RuleCheck] = []
    for rule in rules:
        rid, status = rule.get("rule_id", "?"), rule.get("verification_status", PENDING)
        stored, now = rule.get("fingerprint"), current[_key(rule)]
        if not stored:
            checks.append(RuleCheck(rid, status, "no_fingerprint", ("no_fingerprint",)))
            continue
        changed = tuple(c for c in INVALIDATING if stored.get(c) != now[c])
        advisory = tuple(c for c in ADVISORY if stored.get(c) != now[c])
        if now["effect"] == "missing":
            state = "missing"
        else:
            state = "changed" if changed else "unchanged"
        checks.append(RuleCheck(rid, status, state, changed, advisory))
    return checks


def apply_transitions(
    doc: dict, checks: list[RuleCheck], build: str, now: datetime | None = None
) -> tuple[dict, list[dict]]:
    """Return a copy of `doc` with statuses updated for `build`, plus the transitions made.

    Does not mutate `doc`. Fingerprints are NOT refreshed for changed rules: they
    keep describing the data that was verified until a human re-verifies.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    new = copy.deepcopy(doc)
    by_id = {c.rule_id: c for c in checks}
    transitions: list[dict] = []
    for rule in new.get("rules", []):
        chk = by_id.get(rule.get("rule_id", "?"))
        if chk is None:
            continue
        if chk.state == "unchanged":
            rule["last_confirmed_build"] = build
            if chk.advisory:
                rule["text_changed_in_build"] = build
        elif chk.invalidates:
            rule["verification_status"] = REVERIFY
            record = {"ruleId": rule["rule_id"], "from": VERIFIED, "to": REVERIFY,
                      "build": build, "changed": list(chk.changed), "at": stamp}
            rule.setdefault("history", []).append(record)
            transitions.append(record)
    return new, transitions


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _default_build(data_dir: Path) -> str | None:
    from .update import get_active  # local import: update depends on this module
    return get_active(data_dir)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Work with effect rules.")
    ap.add_argument("command", choices=["adopt", "check"])
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--rules", type=Path, help="default: <data-dir>/rules/effect-rules.json")
    ap.add_argument("--build", help="build to read data from (default: the rules' client_build for adopt, the active build for check)")
    args = ap.parse_args(argv)

    rules_path = args.rules or args.data_dir / "rules" / "effect-rules.json"
    doc = load_rules(rules_path)
    if doc is None:
        print(f"No rules file at {rules_path}")
        return 2

    build = args.build or (doc.get("client_build") if args.command == "adopt" else _default_build(args.data_dir))
    if not build:
        print("No build to read: pass --build.")
        return 2
    try:
        ds = open_dataset(args.data_dir, build)
    except DatasetError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.command == "adopt":
        new, results = adopt_fingerprints(doc, ds, build)
        for r in results:
            print(f"  {r.rule_id}: {r.outcome}" + (f" ({r.detail})" if r.detail else ""))
        if any(r.outcome == "adopted" for r in results):
            save_rules(rules_path, new)
            print(f"\nSaved {rules_path} (schema {new['schema_version']}).")
        else:
            print("\nNothing to adopt.")
        return 1 if any(r.outcome == "inconsistent" for r in results) else 0

    checks = check_rules(doc, ds)
    for c in checks:
        extra = f"  changed: {', '.join(c.changed)}" if c.changed else ""
        extra += f"  (text changed)" if c.advisory else ""
        print(f"  {c.rule_id:40} {c.verification_status:22} {c.state}{extra}")
    print(f"\n{sum(c.invalidates for c in checks)} verified rule(s) would need re-verification against {build}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
