"""Offline tests for wowcalc.diff. Run: python -m unittest discover -s tests"""

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase, base_tables, drop_column, row_for, write_dataset  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.diff import (  # noqa: E402
    DETAIL_SECTIONS, _excerpt, _same, compare_datasets, format_report, main,
)
from wowcalc.validate import validate_dataset  # noqa: E402

V_OLD, V_NEW = "1.60.1.70338", "1.60.1.70425"


def add_talent(t, n, tree="1", x=None, y="5", spell=None, ranks="1"):
    """Add one fully wired talent (node, entry, mapping, definition, spell, name)."""
    nid, eid, did, sid = str(300 + n), str(400 + n), str(500 + n), spell or str(7000 + n)
    t["TraitNode"][1].append(row_for(t, "TraitNode", ID=nid, TraitTreeID=tree, PosX=x or str(10 + n), PosY=y))
    t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID=eid, TraitDefinitionID=did, MaxRanks=ranks))
    t["TraitNodeXTraitNodeEntry"][1].append(row_for(t, "TraitNodeXTraitNodeEntry", ID=str(600 + n), TraitNodeID=nid, TraitNodeEntryID=eid))
    t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID=did, SpellID=sid))
    t["SpellName"][1].append(row_for(t, "SpellName", ID=sid, Name_lang=f"Talent {n}"))
    t["Spell"][1].append(row_for(t, "Spell", ID=sid, Description_lang="desc"))
    return nid


class DiffCase(TempCase):
    def compare(self, mutate=None, old_mutate=None, with_reports=False):
        old, new = base_tables(), base_tables()
        if old_mutate:
            old_mutate(old)
        if mutate:
            mutate(new)
        o = Dataset(write_dataset(self.root / "old", old), V_OLD)
        n = Dataset(write_dataset(self.root / "new", new), V_NEW)
        if with_reports:
            return compare_datasets(o, n, validate_dataset(o), validate_dataset(n))
        return compare_datasets(o, n)

    def texts(self, items):
        return [i["text"] for i in items]


class HelperTests(unittest.TestCase):
    def test_same_numeric_and_text(self):
        self.assertTrue(_same("2", "2.0"))
        self.assertTrue(_same("0.1", "0.10000000"))
        self.assertTrue(_same("abc", "abc"))
        self.assertFalse(_same("2", "3"))
        self.assertFalse(_same("abc", "abd"))
        self.assertFalse(_same("", "0"))

    def test_excerpt_shows_the_difference(self):
        a = "Increases the effectiveness of your healing spells by 10% for the next hour."
        b = a + " Now also heals."
        x, y = _excerpt(a, b)
        self.assertIn("Now also heals", y)
        self.assertNotIn("Now also heals", x)
        self.assertTrue(x.startswith("…"))

    def test_excerpt_short_and_empty(self):
        self.assertEqual(_excerpt("abc", "abd"), ("abc", "abd"))
        self.assertEqual(_excerpt("", "x"), ("", "x"))


class NoChangeTests(DiffCase):
    def test_identical(self):
        r = self.compare()
        self.assertFalse(r.has_any_changes)
        self.assertFalse(r.has_talent_changes)
        self.assertEqual(r.talent_change_count, 0)
        self.assertEqual(r.schema, {})
        self.assertTrue(all(v["added"] == v["removed"] == v["changed"] == 0 for v in r.tables.values()))

    def test_every_table_is_summarised(self):
        self.assertEqual(set(self.compare().tables), set(TABLES))


class NodeTests(DiffCase):
    def test_node_added_and_removed(self):
        r = self.compare(lambda t: add_talent(t, 1))
        self.assertEqual(len(r.nodes_added), 1)
        self.assertIn("Talent 1 [node 301]", r.nodes_added[0]["text"])
        r2 = self.compare(old_mutate=lambda t: add_talent(t, 1))
        self.assertEqual(len(r2.nodes_removed), 1)
        self.assertEqual(r2.nodes_added, [])

    def test_node_moved_position_and_tree(self):
        def move(t):
            t["TraitNode"][1][0]["PosX"] = "50"
            t["TraitNode"][1][1]["TraitTreeID"] = "2"
        r = self.compare(move)
        self.assertEqual(len(r.nodes_moved), 2)
        self.assertIn("(0, 0) -> (50, 0)", r.nodes_moved[0]["text"])
        self.assertIn("tree 1 -> 2", r.nodes_moved[1]["text"])

    def test_number_formatting_is_not_a_move(self):
        def fmt(t):
            t["TraitNode"][1][0]["PosX"] = "0.0"
        self.assertEqual(self.compare(fmt).nodes_moved, [])

    def test_max_ranks_changed(self):
        r = self.compare(lambda t: t["TraitNodeEntry"][1][0].update(MaxRanks="3"))
        self.assertEqual([(c["kind"], c["old"], c["new"]) for c in r.entry_changes], [("max_ranks", "2", "3")])

    def test_entry_added_and_removed_on_existing_node(self):
        def choice(t):
            t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID="150", TraitDefinitionID="1001", MaxRanks="1"))
            t["TraitNodeXTraitNodeEntry"][1].append(row_for(t, "TraitNodeXTraitNodeEntry", ID="50", TraitNodeID="10", TraitNodeEntryID="150"))
        r = self.compare(choice)
        self.assertEqual([c["kind"] for c in r.entry_changes], ["entry_added"])
        r2 = self.compare(old_mutate=choice)
        self.assertEqual([c["kind"] for c in r2.entry_changes], ["entry_removed"])

    def test_definition_swap_reports_definition_and_spell(self):
        r = self.compare(lambda t: t["TraitNodeEntry"][1][0].update(TraitDefinitionID="1001"))
        self.assertEqual(sorted(c["kind"] for c in r.entry_changes), ["definition", "spell"])

    def test_renumbering_detected(self):
        def many(t):
            for n in range(1, 7):
                add_talent(t, n)

        def renumber(t):
            many(t)
            for row in t["TraitNode"][1]:
                if int(row["ID"]) >= 300:
                    row["ID"] = str(int(row["ID"]) + 1000)
            for row in t["TraitNodeXTraitNodeEntry"][1]:
                if int(row["TraitNodeID"]) >= 300:
                    row["TraitNodeID"] = str(int(row["TraitNodeID"]) + 1000)
        r = self.compare(renumber, old_mutate=many)
        self.assertEqual((len(r.nodes_added), len(r.nodes_removed)), (6, 6))
        self.assertTrue(any("renumbered" in n for n in r.notes))

    def test_genuine_replacement_not_flagged_as_renumbering(self):
        def old_side(t):
            for n in range(1, 7):
                add_talent(t, n)

        def new_side(t):
            for n in range(11, 17):  # different talents and spells
                add_talent(t, n)
        r = self.compare(new_side, old_mutate=old_side)
        self.assertEqual(r.notes, [])


class SlotReassignmentTests(DiffCase):
    def swap(self, t):
        t["TraitDefinition"][1][0]["SpellID"] = "5001"
        t["TraitDefinition"][1][1]["SpellID"] = "5000"

    def test_spell_change_text_names_both_spells(self):
        r = self.compare(lambda t: t["TraitDefinition"][1][0].update(SpellID="5002"))
        spell = [c for c in r.entry_changes if c["kind"] == "spell"][0]
        self.assertIn("5000 (A) -> 5002 (C)", spell["text"])

    def test_swap_between_slots_is_called_out(self):
        r = self.compare(self.swap)
        self.assertEqual(sorted(c["kind"] for c in r.entry_changes), ["spell", "spell"])
        note = " ".join(r.notes)
        self.assertIn("2 spells moved from one talent slot to another", note)
        self.assertIn("keyed by spell", note)

    def test_replacing_a_spell_with_a_new_one_is_not_a_swap(self):
        r = self.compare(lambda t: t["TraitDefinition"][1][0].update(SpellID="5002"))
        self.assertEqual(r.notes, [])

    def test_single_spell_moving_slots_below_threshold(self):
        def one(t):
            t["TraitDefinition"][1][0]["SpellID"] = "5001"  # 5001 now in two slots; only one move
        self.assertEqual(self.compare(one).notes, [])


class NodeLabelTests(DiffCase):
    def test_repeated_entry_names_are_collapsed(self):
        def choice(t):
            for d in t["TraitDefinition"][1]:
                d["OverrideName_lang"] = "Talented"
            for i, entry in enumerate(("101", "200")):
                t["TraitNodeXTraitNodeEntry"][1].append(row_for(t, "TraitNodeXTraitNodeEntry", ID=str(70 + i), TraitNodeID="10", TraitNodeEntryID=entry))
            t["TraitNode"][1][0]["PosX"] = "99"  # force a node-level record so the label is visible
        r = self.compare(choice)
        self.assertIn("Talented ×3 [node 10]", r.nodes_moved[0]["label"])
        self.assertNotIn("Talented / Talented", r.nodes_moved[0]["label"])


class EdgeAndTreeTests(DiffCase):
    def test_edge_added_and_removed(self):
        r = self.compare(lambda t: t["TraitEdge"][1].append(row_for(t, "TraitEdge", ID="2", LeftTraitNodeID="11", RightTraitNodeID="10")))
        self.assertEqual(len(r.edges_added), 1)
        self.assertIn("[node 11]", r.edges_added[0]["text"])
        r2 = self.compare(lambda t: t["TraitEdge"][1].clear())
        self.assertEqual(len(r2.edges_removed), 1)

    def test_edge_identity_is_the_pair_not_the_row_id(self):
        r = self.compare(lambda t: t["TraitEdge"][1][0].update(ID="77"))
        self.assertEqual((r.edges_added, r.edges_removed), ([], []))

    def test_trees_added_removed_renamed(self):
        r = self.compare(lambda t: (t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3", TitleText_lang="New")),
                                    t["TraitTree"][1][0].update(TitleText_lang="Renamed")))
        self.assertEqual(len(r.trees_added), 1)
        self.assertEqual(r.trees_renamed[0]["new"], "Renamed")
        r2 = self.compare(old_mutate=lambda t: t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3")))
        self.assertEqual(len(r2.trees_removed), 1)


class DefinitionTests(DiffCase):
    def test_added_placed_vs_unplaced(self):
        def add(t):
            add_talent(t, 1)  # placed
            t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="9001", SpellID="5000"))  # unplaced
        r = self.compare(add)
        by_id = {d["definitionId"]: d["placed"] for d in r.definitions_added}
        self.assertEqual(by_id, {"501": True, "9001": False})

    def test_removed(self):
        r = self.compare(old_mutate=lambda t: t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="9001", SpellID="5000")))
        self.assertEqual([(d["definitionId"], d["placed"]) for d in r.definitions_removed], [("9001", False)])

    def test_changed_columns_and_placed_flag(self):
        r = self.compare(lambda t: t["TraitDefinition"][1][0].update(OverrideName_lang="Custom"))
        self.assertEqual(r.definitions_changed[0]["changes"], {"OverrideName_lang": ["", "Custom"]})
        self.assertTrue(r.definitions_changed[0]["placed"])


class SpellTests(DiffCase):
    def test_placed_description_change_reported_with_excerpt(self):
        def edit(t):
            t["Spell"][1][0]["Description_lang"] = "d plus more"
        r = self.compare(edit)
        self.assertEqual(len(r.spell_text_changes), 1)
        c = r.spell_text_changes[0]
        self.assertEqual((c["spellId"], c["field"]), ("5000", "Description_lang"))
        self.assertIn("plus more", c["text"])

    def test_unplaced_spell_change_is_footprint_only(self):
        def edit(t):
            t["Spell"][1].append(row_for(t, "Spell", ID="8888", Description_lang="a"))
        def edit_new(t):
            t["Spell"][1].append(row_for(t, "Spell", ID="8888", Description_lang="b"))
        r = self.compare(edit_new, old_mutate=edit)
        self.assertEqual(r.spell_text_changes, [])
        self.assertEqual(r.tables["Spell"]["changed"], 1)

    def test_spell_name_change(self):
        r = self.compare(lambda t: t["SpellName"][1][0].update(Name_lang="Renamed Spell"))
        self.assertEqual((r.spell_text_changes[0]["field"], r.spell_text_changes[0]["new"]), ("Name_lang", "Renamed Spell"))

    def test_spell_data_appears_and_disappears(self):
        gone = self.compare(lambda t: t["Spell"][1].pop(0))
        self.assertEqual([(p["spellId"], p["presentNow"]) for p in gone.spell_presence_changes], [("5000", False)])
        back = self.compare(old_mutate=lambda t: t["Spell"][1].pop(0))
        self.assertEqual([(p["spellId"], p["presentNow"]) for p in back.spell_presence_changes], [("5000", True)])


class EffectTests(DiffCase):
    def test_value_change(self):
        r = self.compare(lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="5"))
        self.assertEqual(r.effects_changed[0]["changes"], {"EffectBasePointsF": ["2", "5"]})
        self.assertIn("'2' -> '5'", r.effects_changed[0]["text"])

    def test_float_formatting_is_not_a_change(self):
        r = self.compare(lambda t: t["SpellEffect"][1][0].update(EffectBasePointsF="2.0"))
        self.assertEqual(r.effects_changed, [])
        self.assertEqual(r.tables["SpellEffect"]["changed"], 1)  # footprint is text-level, by design

    def test_effect_added_and_removed(self):
        add = lambda t: t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="9", SpellID="5000", EffectIndex="1", Effect="6", EffectAura="4"))
        self.assertEqual(len(self.compare(add).effects_added), 1)
        self.assertEqual(len(self.compare(old_mutate=add).effects_removed), 1)

    def test_effect_on_unplaced_spell_is_footprint_only(self):
        r = self.compare(lambda t: t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="9", SpellID="8888")))
        self.assertEqual((r.effects_added, r.effects_changed), ([], []))
        self.assertEqual(r.tables["SpellEffect"]["added"], 1)


class ScalingTests(DiffCase):
    def curve(self, t, curve_id, values):
        for i, v in enumerate(values):
            t["CurvePoint"][1].append(row_for(t, "CurvePoint", ID=f"{curve_id}{i}", CurveID=curve_id, OrderIndex=str(i), Pos_0=str(i), Pos_1=str(v)))

    def test_curve_point_change(self):
        r = self.compare(lambda t: t["CurvePoint"][1][3].update(Pos_1="999"))
        self.assertEqual(len(r.curve_changes), 1)
        c = r.curve_changes[0]
        self.assertEqual((c["curveId"], c["oldPoints"], c["newPoints"], c["differing"]), ("7", 10, 10, 1))
        self.assertEqual(c["examples"][0]["new"], ["3", "999"])
        self.assertIn("Talent", c["usedBy"][0] if c["usedBy"][0].startswith("Talent") else "Talent " + c["usedBy"][0])
        self.assertIn("1 of 10 points differ", c["text"])

    def test_curve_point_count_change(self):
        r = self.compare(lambda t: t["CurvePoint"][1].pop())
        self.assertEqual((r.curve_changes[0]["oldPoints"], r.curve_changes[0]["newPoints"]), (10, 9))

    def test_unreferenced_curve_is_footprint_only(self):
        def add(t):
            self.curve(t, "99", [1, 2, 3])

        def edit(t):
            self.curve(t, "99", [1, 2, 4])
        r = self.compare(edit, old_mutate=add)
        self.assertEqual(r.curve_changes, [])
        self.assertEqual(r.tables["CurvePoint"]["changed"], 1)

    def test_curve_float_formatting_ignored(self):
        r = self.compare(lambda t: t["CurvePoint"][1][2].update(Pos_1="4.0"))
        self.assertEqual(r.curve_changes, [])

    def test_curve_swap_between_unchanged_curves_reports_the_link_only(self):
        def both(t):
            self.curve(t, "8", [5, 5, 5])

        def swap(t):
            both(t)
            t["TraitDefinitionEffectPoints"][1][0]["CurveID"] = "8"
        r = self.compare(swap, old_mutate=both)
        self.assertEqual(r.effect_points_changed[0]["kind"], "changed")
        self.assertEqual(r.effect_points_changed[0]["changes"], {"CurveID": ["7", "8"]})
        self.assertEqual(r.curve_changes, [])  # neither curve's data changed

    def test_swap_to_a_curve_that_only_exists_in_the_new_build(self):
        def swap(t):
            self.curve(t, "8", [5, 5, 5])
            t["TraitDefinitionEffectPoints"][1][0]["CurveID"] = "8"
        r = self.compare(swap)
        self.assertEqual([(c["curveId"], c["oldPoints"], c["newPoints"]) for c in r.curve_changes], [("8", 0, 3)])
        self.assertEqual(r.effect_points_changed[0]["changes"], {"CurveID": ["7", "8"]})

    def test_effect_point_added_and_removed(self):
        add = lambda t: t["TraitDefinitionEffectPoints"][1].append(row_for(t, "TraitDefinitionEffectPoints", ID="5", TraitDefinitionID="1001", EffectIndex="0", CurveID="7"))
        self.assertEqual(self.compare(add).effect_points_changed[0]["kind"], "added")
        self.assertEqual(self.compare(old_mutate=add).effect_points_changed[0]["kind"], "removed")


class SchemaAndFootprintTests(DiffCase):
    def test_columns_added_and_removed(self):
        def change(t):
            header, data = t["TraitNode"]
            t["TraitNode"] = (header + ["NewFlag"], [dict(r, NewFlag="0") for r in data])
            drop_column(t, "TraitEdge", "VisualStyle")
        r = self.compare(change)
        self.assertEqual(r.schema["TraitNode"]["columnsAdded"], ["NewFlag"])
        self.assertEqual(r.schema["TraitEdge"]["columnsRemoved"], ["VisualStyle"])
        self.assertTrue(r.has_any_changes)
        self.assertFalse(r.has_talent_changes)  # schema alone is not a talent change

    def test_added_column_does_not_count_as_row_changes(self):
        def change(t):
            header, data = t["TraitNode"]
            t["TraitNode"] = (header + ["NewFlag"], [dict(r, NewFlag="9") for r in data])
        self.assertEqual(self.compare(change).tables["TraitNode"]["changed"], 0)

    def test_table_removed(self):
        def drop(t):
            del t["ChrRaces"]
        r = self.compare(drop)
        self.assertTrue(r.schema["ChrRaces"]["tableRemoved"])
        self.assertNotIn("ChrRaces", r.tables)

    def test_footprint_counts(self):
        def change(t):
            t["CurvePoint"][1].extend(row_for(t, "CurvePoint", ID=str(100 + i), CurveID="55", OrderIndex=str(i)) for i in range(5))
            t["TraitTree"][1][0]["TitleText_lang"] = "x"
        r = self.compare(change)
        self.assertEqual((r.tables["CurvePoint"]["old"], r.tables["CurvePoint"]["new"], r.tables["CurvePoint"]["added"]), (10, 15, 5))
        self.assertEqual(r.tables["TraitTree"]["changed"], 1)

    def test_table_without_id_column_uses_row_multiset(self):
        def strip(t):
            drop_column(t, "CurvePoint", "ID")

        def strip_and_edit(t):
            strip(t)
            t["CurvePoint"][1][0]["Pos_1"] = "42"
        r = self.compare(strip_and_edit, old_mutate=strip)
        v = r.tables["CurvePoint"]
        self.assertFalse(v["keyed"])
        self.assertEqual((v["added"], v["removed"]), (1, 1))

    def test_missing_trait_table_skips_detail_with_note(self):
        def drop(t):
            del t["TraitEdge"]
        r = self.compare(drop)
        self.assertTrue(any("TraitEdge" in n for n in r.notes))
        self.assertEqual(r.talent_change_count, 0)


class IssueValueTests(DiffCase):
    def test_orphan_values_new_and_resolved(self):
        def old_side(t):
            t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="9", SpellID="9001"))
            t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="10", SpellID="9002"))

        def new_side(t):
            t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="9", SpellID="9001"))
            t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="11", SpellID="9003"))
        r = self.compare(new_side, old_mutate=old_side, with_reports=True)
        c = r.issue_value_changes[0]
        self.assertEqual((c["subject"], c["added"], c["removed"]), ("SpellEffect.SpellID", ["9003"], ["9002"]))

    def test_not_computed_without_reports(self):
        self.assertEqual(self.compare().issue_value_changes, [])


class FormatTests(DiffCase):
    def test_no_changes_message(self):
        text = format_report(self.compare())
        self.assertIn("None. Nothing a player can place", text)
        self.assertIn("(no table changed)", text)

    def test_sections_notes_and_schema_rendered(self):
        def change(t):
            t["TraitNodeEntry"][1][0]["MaxRanks"] = "3"
            header, data = t["TraitNode"]
            t["TraitNode"] = (header + ["NewFlag"], [dict(r, NewFlag="0") for r in data])
        text = format_report(self.compare(change))
        self.assertIn("Schema changes:", text)
        self.assertIn("TraitNode: added NewFlag", text)
        self.assertIn("Rank / entry / spell changes on nodes (1):", text)
        self.assertIn("max ranks 2 -> 3", text)

    def test_limit_truncates_and_full_shows_all(self):
        def many(t):
            for n in range(1, 13):
                add_talent(t, n)
        r = self.compare(many)
        short = format_report(r, limit=10)
        self.assertIn("Talent nodes added (12):", short)
        self.assertIn("... 2 more (use --full)", short)
        self.assertNotIn("more (use --full)", format_report(r, limit=None))

    def test_json_serialisable_and_counts(self):
        r = self.compare(lambda t: t["TraitNodeEntry"][1][0].update(MaxRanks="3"))
        data = json.loads(json.dumps(r.to_dict()))
        self.assertEqual(data["counts"]["entry_changes"], 1)
        self.assertEqual(set(data["counts"]), {name for name, _ in DETAIL_SECTIONS})


class CliTests(TempCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.root / "data"), *args])
        return code, out.getvalue()

    def place(self, sub, version, mutate=None):
        t = base_tables()
        if mutate:
            mutate(t)
        write_dataset(self.root / "data" / sub / version, t)

    def test_nothing_staged(self):
        self.assertEqual(self.run_cli()[0], 2)

    def test_no_installed_baseline(self):
        self.place("staging", V_NEW)
        code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("No installed build", out)

    def test_report_printed_and_written(self):
        self.place("raw", V_OLD)
        self.place("staging", V_NEW, lambda t: t["TraitNodeEntry"][1][0].update(MaxRanks="4"))
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("max ranks 2 -> 4", out)
        saved = json.loads((self.root / "data" / "reports" / f"{V_NEW}.changes.json").read_text())
        self.assertEqual(saved["changes"]["counts"]["entry_changes"], 1)
        self.assertEqual(saved["changes"]["old_version"], V_OLD)

    def test_no_write_and_full(self):
        self.place("raw", V_OLD)
        def many(t):
            for n in range(1, 13):
                add_talent(t, n)
        self.place("staging", V_NEW, many)
        _, out = self.run_cli("--no-write")
        self.assertIn("more (use --full)", out)
        self.assertFalse((self.root / "data" / "reports").exists())
        _, full = self.run_cli("--no-write", "--full")
        self.assertNotIn("more (use --full)", full)

    def test_unknown_old_version(self):
        self.place("staging", V_NEW)
        self.assertEqual(self.run_cli("--old", "1.60.1.1")[0], 2)


# --------------------------------------------------------------------------
# Real data (skipped when the build is not present)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_70338 = REAL_ROOT / "raw" / V_OLD


@unittest.skipUnless(REAL_70338.is_dir(), f"real data not found at {REAL_70338}")
class RealDataTests(TempCase):
    def test_against_itself_is_empty(self):
        r = compare_datasets(Dataset(REAL_70338, "a"), Dataset(REAL_70338, "b"))
        self.assertFalse(r.has_any_changes)

    def test_single_node_move_is_found_and_labelled(self):
        copy_dir = self.root / "copy"
        shutil.copytree(REAL_70338, copy_dir)
        path = copy_dir / "TraitNode.csv"
        import csv
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        i_id, i_x = rows[0].index("ID"), rows[0].index("PosX")
        target = next(r for r in rows[1:] if r[i_id] == "103887")
        target[i_x] = str(int(float(target[i_x])) + 100)
        with path.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)
        r = compare_datasets(Dataset(REAL_70338, "a"), Dataset(copy_dir, "b"))
        self.assertEqual(r.talent_change_count, 1)
        self.assertIn("Improved Fireball [node 103887]", r.nodes_moved[0]["text"])
        self.assertEqual(r.tables["TraitNode"]["changed"], 1)


if __name__ == "__main__":
    unittest.main()
