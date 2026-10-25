"""Offline tests for wowcalc.rules. Run: python -m unittest discover -s tests"""

import contextlib
import copy
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase, base_tables, row_for, write_dataset  # noqa: E402
from wowcalc import rules as R  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402

BUILD = "1.60.1.70425"
NOW = datetime(2026, 10, 17, 9, 0, 0, tzinfo=timezone.utc)


def rule(rid="r1", spell=5000, index=0, effect_id=1, status="verified", **extra):
    return {"rule_id": rid, "talent_spell_id": spell, "effect_id": effect_id, "effect_index": index,
            "effect_type": 6, "aura_id": 108, "base_points": 2, "verification_status": status, **extra}


class RulesCase(TempCase):
    def ds(self, tables, name="ds"):
        return Dataset(write_dataset(self.root / name, tables), BUILD)

    def fp(self, tables, spell=5000, index=0, name="ds"):
        return R.fingerprints_for(self.ds(tables, name), [(spell, index)])[(str(spell), str(index))]

    def adopted(self, tables=None, rules=None):
        doc = {"schema_version": 1, "client_build": BUILD, "rules": rules or [rule()]}
        new, results = R.adopt_fingerprints(doc, self.ds(tables or base_tables(), "adopt"), BUILD)
        return new, results


class FingerprintTests(RulesCase):
    def changed(self, mutate, **kw):
        base = self.fp(base_tables(), name="a", **kw)
        t = base_tables()
        mutate(t)
        other = self.fp(t, name="b", **kw)
        return {k for k in base if base[k] != other[k]}

    def test_identical_data_same_fingerprint(self):
        self.assertEqual(self.fp(base_tables(), name="a"), self.fp(base_tables(), name="b"))

    def test_keys_normalised_and_empty(self):
        ds = self.ds(base_tables())
        self.assertIn(("5000", "0"), R.fingerprints_for(ds, [(5000, 0)]))
        self.assertEqual(R.fingerprints_for(ds, []), {})

    def test_effect_value_change(self):
        self.assertEqual(self.changed(lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="3")), {"effect"})

    def test_number_formatting_is_not_a_change(self):
        self.assertEqual(self.changed(lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="2.0")), set())

    def test_effect_row_id_is_not_part_of_the_fingerprint(self):
        self.assertEqual(self.changed(lambda t: t["SpellEffect"][1][0].update(ID="999")), set())

    def test_max_ranks_change(self):
        self.assertEqual(self.changed(lambda t: t["TraitNodeEntry"][1][0].update(MaxRanks="3")), {"ranks"})

    def test_curve_point_change(self):
        self.assertEqual(self.changed(lambda t: t["CurvePoint"][1][3].update(Pos_1="999")), {"scaling"})

    def test_curve_renumbering_is_not_a_change(self):
        def renumber(t):
            for r in t["CurvePoint"][1]:
                r["CurveID"] = "70"
            t["TraitDefinitionEffectPoints"][1][0]["CurveID"] = "70"
        self.assertEqual(self.changed(renumber), set())

    def test_effect_point_operation_change(self):
        self.assertEqual(self.changed(lambda t: t["TraitDefinitionEffectPoints"][1][0].update(OperationType="1")), {"scaling"})

    def test_only_this_effect_index_scaling_counts(self):
        def other_index(t):
            t["TraitDefinitionEffectPoints"][1].append(row_for(t, "TraitDefinitionEffectPoints", ID="9", TraitDefinitionID="1000", EffectIndex="1", CurveID="7", OperationType="5"))
        self.assertEqual(self.changed(other_index, index=0), set())

    def test_text_changes_only_touch_text(self):
        self.assertEqual(self.changed(lambda t: t["Spell"][1][0].update(Description_lang="new words")), {"text"})
        self.assertEqual(self.changed(lambda t: t["SpellName"][1][0].update(Name_lang="Renamed")), {"text"})

    def test_spell_following_its_data_to_another_slot_is_unchanged(self):
        # Spells 5000 and 5001 trade definitions, and ranks and curve go with them.
        def swap(t):
            t["TraitDefinition"][1][0]["SpellID"] = "5001"
            t["TraitDefinition"][1][1]["SpellID"] = "5000"
            t["TraitNodeEntry"][1][0]["TraitDefinitionID"] = "1001"
            t["TraitNodeEntry"][1][1]["TraitDefinitionID"] = "1000"
            t["TraitDefinitionEffectPoints"][1][0]["TraitDefinitionID"] = "1001"
        self.assertEqual(self.changed(swap), set())

    def test_spell_moving_to_a_slot_with_different_ranks_is_changed(self):
        def swap(t):
            t["TraitDefinition"][1][0]["SpellID"] = "5001"
            t["TraitDefinition"][1][1]["SpellID"] = "5000"
        self.assertEqual(self.changed(swap), {"ranks", "scaling"})

    def test_effect_removed_is_missing(self):
        fp = self.fp((lambda t: (t["SpellEffect"][1].pop(0), t)[1])(base_tables()), name="m")
        self.assertEqual(fp["effect"], "missing")

    def test_multiple_rows_for_one_effect_are_order_independent(self):
        def two(t, reverse):
            t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="9", SpellID="5000", EffectIndex="0", EffectAura="4"))
            if reverse:
                t["SpellEffect"][1].reverse()
        a, b = base_tables(), base_tables()
        two(a, False); two(b, True)
        self.assertEqual(self.fp(a, name="a"), self.fp(b, name="b"))


class AdoptTests(RulesCase):
    def test_adopts_a_consistent_rule(self):
        doc = {"schema_version": 1, "client_build": BUILD, "rules": [rule()]}
        new, results = R.adopt_fingerprints(doc, self.ds(base_tables()), BUILD)
        self.assertEqual([r.outcome for r in results], ["adopted"])
        self.assertEqual(new["schema_version"], 2)
        self.assertEqual(new["rules"][0]["fingerprint_build"], BUILD)
        self.assertEqual(set(new["rules"][0]["fingerprint"]), {"effect", "ranks", "scaling", "text"})
        self.assertNotIn("fingerprint", doc["rules"][0])  # input untouched
        self.assertEqual(doc["schema_version"], 1)

    def test_already_fingerprinted_rule_is_left_alone(self):
        new, _ = self.adopted()
        again, results = R.adopt_fingerprints(new, self.ds(base_tables(), "x"), "other-build")
        self.assertEqual([r.outcome for r in results], ["already"])
        self.assertEqual(again["rules"][0]["fingerprint_build"], BUILD)

    def test_snapshot_disagreeing_with_data_is_not_adopted(self):
        for bad, expect in ((rule(aura_id=4), "aura_id"), (rule(base_points=9), "base_points"),
                            (rule(effect_type=1), "effect_type")):
            new, results = self.adopted(rules=[bad])
            self.assertEqual(results[0].outcome, "inconsistent")
            self.assertIn(expect, results[0].detail)
            self.assertNotIn("fingerprint", new["rules"][0])
            self.assertEqual(new["schema_version"], 1)  # nothing adopted, version not bumped

    def test_wrong_effect_id_spell_or_index(self):
        for bad in (rule(effect_id=999), rule(effect_id=2), rule(index=1)):
            self.assertEqual(self.adopted(rules=[bad])[1][0].outcome, "inconsistent")

    def test_class_mask_and_misc_value_checked(self):
        t = base_tables()
        header, data = t["SpellEffect"]
        extra = ["EffectMiscValue_0", "EffectSpellClassMask_0", "EffectSpellClassMask_1", "EffectSpellClassMask_2", "EffectSpellClassMask_3"]
        data[0].update({"EffectMiscValue_0": "22", "EffectSpellClassMask_0": "448", "EffectSpellClassMask_1": "0",
                        "EffectSpellClassMask_2": "16", "EffectSpellClassMask_3": "0"})
        t["SpellEffect"] = (header + extra, data)
        ok = rule(misc_value_0=22, spell_class_mask=[448, 0, 16, 0])
        self.assertEqual(self.adopted(t, [ok])[1][0].outcome, "adopted")
        bad = rule(misc_value_0=22, spell_class_mask=[448, 0, 17, 0])
        r = self.adopted(t, [bad])[1][0]
        self.assertEqual(r.outcome, "inconsistent")
        self.assertIn("spell_class_mask", r.detail)


class CheckTests(RulesCase):
    def check(self, mutate=None, status="verified", **rule_kw):
        doc, _ = self.adopted(rules=[rule(status=status, **rule_kw)])
        t = base_tables()
        if mutate:
            mutate(t)
        return R.check_rules(doc, self.ds(t, "now"))[0]

    def test_unchanged(self):
        c = self.check()
        self.assertEqual((c.state, c.changed, c.advisory, c.invalidates), ("unchanged", (), (), False))

    def test_changed_lists_components(self):
        def both(t):
            t["SpellEffect"][1][0]["EffectBasePointsF"] = "5"
            t["TraitNodeEntry"][1][0]["MaxRanks"] = "3"
        c = self.check(both)
        self.assertEqual((c.state, c.changed, c.invalidates), ("changed", ("effect", "ranks"), True))

    def test_missing_effect(self):
        c = self.check(lambda t: t["SpellEffect"][1].pop(0))
        self.assertEqual(c.state, "missing")
        self.assertTrue(c.invalidates)

    def test_text_only_is_advisory_and_does_not_invalidate(self):
        c = self.check(lambda t: t["Spell"][1][0].update(Description_lang="different"))
        self.assertEqual((c.state, c.advisory, c.invalidates), ("unchanged", ("text",), False))

    def test_no_fingerprint(self):
        c = R.check_rules({"rules": [rule()]}, self.ds(base_tables()))[0]
        self.assertEqual((c.state, c.invalidates), ("no_fingerprint", True))

    def test_only_verified_rules_invalidate(self):
        change = lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="5")
        self.assertFalse(self.check(change, status="pending").invalidates)
        self.assertFalse(self.check(change, status="needs_reverification").invalidates)
        self.assertTrue(self.check(change, status="verified").invalidates)


class TransitionTests(RulesCase):
    def run_transitions(self, rules, mutate=None):
        doc, _ = self.adopted(rules=rules)
        t = base_tables()
        if mutate:
            mutate(t)
        checks = R.check_rules(doc, self.ds(t, "now"))
        return doc, R.apply_transitions(doc, checks, BUILD, NOW)

    CHANGE = staticmethod(lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="5"))

    def test_verified_and_changed_needs_reverification(self):
        doc, (new, transitions) = self.run_transitions([rule()], self.CHANGE)
        r = new["rules"][0]
        self.assertEqual(r["verification_status"], "needs_reverification")
        self.assertEqual(r["history"][0]["changed"], ["effect"])
        self.assertEqual((r["history"][0]["from"], r["history"][0]["build"]), ("verified", BUILD))
        self.assertEqual(len(transitions), 1)
        self.assertEqual(doc["rules"][0]["verification_status"], "verified")  # input not mutated

    def test_fingerprint_is_not_refreshed_for_changed_rule(self):
        doc, (new, _) = self.run_transitions([rule()], self.CHANGE)
        self.assertEqual(new["rules"][0]["fingerprint"], doc["rules"][0]["fingerprint"])

    def test_verified_and_unchanged_keeps_status_and_confirms_build(self):
        _, (new, transitions) = self.run_transitions([rule()])
        r = new["rules"][0]
        self.assertEqual((r["verification_status"], r["last_confirmed_build"], transitions), ("verified", BUILD, []))
        self.assertNotIn("history", r)

    def test_pending_is_never_changed(self):
        _, (new, transitions) = self.run_transitions([rule(status="pending")], self.CHANGE)
        self.assertEqual((new["rules"][0]["verification_status"], transitions), ("pending", []))

    def test_text_change_is_noted_but_status_kept(self):
        _, (new, _) = self.run_transitions([rule()], lambda t: t["Spell"][1][0].update(Description_lang="x"))
        r = new["rules"][0]
        self.assertEqual((r["verification_status"], r["text_changed_in_build"]), ("verified", BUILD))

    def test_verified_without_fingerprint_is_flagged_conservatively(self):
        doc = {"rules": [rule()]}
        checks = R.check_rules(doc, self.ds(base_tables()))
        new, transitions = R.apply_transitions(doc, checks, BUILD, NOW)
        self.assertEqual(new["rules"][0]["verification_status"], "needs_reverification")
        self.assertEqual(transitions[0]["changed"], ["no_fingerprint"])

    def test_mixed_rules(self):
        rules = [rule("a", status="verified"), rule("b", status="pending"), rule("c", spell=5001, effect_id=2, status="verified")]
        doc, _ = self.adopted(rules=[rule("a"), rule("b", status="pending"), rule("c", spell=5001, effect_id=2, aura_id=0, effect_type=0, base_points=0)])
        t = base_tables()
        self.CHANGE(t)
        new, transitions = R.apply_transitions(doc, R.check_rules(doc, self.ds(t, "now")), BUILD, NOW)
        self.assertEqual({r["rule_id"]: r["verification_status"] for r in new["rules"]},
                         {"a": "needs_reverification", "b": "pending", "c": "verified"})


class FileTests(RulesCase):
    def test_save_and_load_atomic(self):
        path = self.root / "rules" / "effect-rules.json"
        R.save_rules(path, {"rules": []})
        self.assertEqual(R.load_rules(path), {"rules": []})
        self.assertEqual([p.name for p in path.parent.iterdir()], ["effect-rules.json"])  # no .tmp left

    def test_load_missing(self):
        self.assertIsNone(R.load_rules(self.root / "nope.json"))


class CliTests(RulesCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        write_dataset(self.data / "raw" / BUILD, base_tables())

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["--data-dir", str(self.data), *args])
        return code, out.getvalue()

    def put_rules(self, rules, **extra):
        R.save_rules(self.data / "rules" / "effect-rules.json", {"schema_version": 1, "client_build": BUILD, "rules": rules, **extra})

    def test_no_rules_file(self):
        self.assertEqual(self.run_cli("check")[0], 2)

    def test_adopt_then_check(self):
        self.put_rules([rule()])
        code, out = self.run_cli("adopt")
        self.assertEqual(code, 0)
        self.assertIn("r1: adopted", out)
        self.assertEqual(R.load_rules(self.data / "rules" / "effect-rules.json")["schema_version"], 2)
        code, out = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertIn("0 verified rule(s) would need re-verification", out)
        self.assertIn("Nothing to adopt", self.run_cli("adopt")[1])

    def test_adopt_reports_inconsistent_rule_with_exit_one(self):
        self.put_rules([rule(aura_id=4)])
        code, out = self.run_cli("adopt")
        self.assertEqual(code, 1)
        self.assertIn("inconsistent", out)

    def test_unknown_build(self):
        self.put_rules([rule()])
        self.assertEqual(self.run_cli("check", "--build", "1.60.1.1")[0], 2)


# --------------------------------------------------------------------------
# Real data (skipped when the build is not present)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_70338 = REAL_ROOT / "raw" / "1.60.1.70338"

PURIFICATION = [
    {"rule_id": "shaman.purification.effect_0", "talent_spell_id": 16178, "effect_id": 693066, "effect_index": 0,
     "effect_type": 6, "aura_id": 108, "base_points": 10, "misc_value_0": 0, "spell_class_mask": [448, 0, 16, 0],
     "verification_status": "pending"},
    {"rule_id": "shaman.purification.effect_1", "talent_spell_id": 16178, "effect_id": 1197174, "effect_index": 1,
     "effect_type": 6, "aura_id": 108, "base_points": 10, "misc_value_0": 22, "spell_class_mask": [0, 0, 16, 0],
     "verification_status": "pending"},
]


@unittest.skipUnless(REAL_70338.is_dir(), f"real data not found at {REAL_70338}")
class RealDataTests(unittest.TestCase):
    def test_purification_rules_match_the_real_data_and_are_stable(self):
        ds = Dataset(REAL_70338, "1.60.1.70338")
        doc = {"schema_version": 1, "client_build": "1.60.1.70338", "rules": copy.deepcopy(PURIFICATION)}
        new, results = R.adopt_fingerprints(doc, ds, "1.60.1.70338")
        self.assertEqual([r.outcome for r in results], ["adopted", "adopted"])
        self.assertEqual([c.state for c in R.check_rules(new, ds)], ["unchanged", "unchanged"])


if __name__ == "__main__":
    unittest.main()
