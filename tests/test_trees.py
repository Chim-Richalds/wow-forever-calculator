"""Offline tests for wowcalc.trees. Run: python -m unittest discover -s tests"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase  # noqa: E402
from tree_fixtures import add_row, write_mini  # noqa: E402
from wowcalc.dataset import Dataset, DatasetError  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.trees import Budget, PointSource, load_model, main  # noqa: E402


class TreeCase(TempCase):
    def model(self, mutate=None):
        return load_model(Dataset(write_mini(self.root / "ds", mutate), "test"))

    def tree(self, mutate=None, name="Mage"):
        return self.model(mutate).by_name(name)


class ClassMappingTests(TreeCase):
    def test_trees_map_to_their_classes(self):
        m = self.model()
        self.assertEqual({t.class_name: t.tree_id for t in m.trees.values()}, {"Mage": "100", "Warrior": "200"})
        self.assertEqual(m.anomalies, [])
        self.assertEqual(m.by_name("mage").class_id, 8)
        self.assertEqual(m.by_name("Mage").skill_line_id, "237")

    def test_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            self.model().by_name("Necromancer")

    def test_a_tree_without_a_skill_line_is_ignored(self):
        def extra(t):
            add_row(t, "TraitNode", ID="99", TraitTreeID="999", PosX="1", PosY="1")
        self.assertEqual(len(self.model(extra).trees), 2)

    def test_skill_line_shared_by_two_classes_is_an_anomaly_and_skips_the_tree(self):
        def both(t):
            t["SkillRaceClassInfo"][1][0]["ClassMask"] = "129"  # Warrior + Mage
        m = self.model(both)
        self.assertNotIn("Mage", [t.class_name for t in m.trees.values()])
        self.assertTrue(any("tree 100" in a and "expected exactly one" in a for a in m.anomalies))

    def test_wildcard_class_mask_is_an_anomaly(self):
        m = self.model(lambda t: add_row(t, "SkillRaceClassInfo", ID="9", SkillID="237", ClassMask="-1"))
        self.assertTrue(any("tree 100" in a for a in m.anomalies))
        self.assertNotIn("Mage", [t.class_name for t in m.trees.values()])

    def test_skill_line_with_no_class_row_is_an_anomaly(self):
        def none(t):
            t["SkillRaceClassInfo"] = (t["SkillRaceClassInfo"][0], t["SkillRaceClassInfo"][1][1:])
        m = self.model(none)
        self.assertTrue(any("maps to 0 class(es) (none)" in a for a in m.anomalies))

    def test_two_trees_for_one_class_keeps_the_first(self):
        def dup(t):
            add_row(t, "SkillLineXTraitTree", ID="3", SkillLineID="237", TraitTreeID="300")
        m = self.model(dup)
        self.assertEqual(m.by_name("Mage").tree_id, "100")
        self.assertTrue(any("claimed by both" in a for a in m.anomalies))

    def test_tree_without_a_currency_is_skipped(self):
        def nocur(t):
            t["TraitTreeXTraitCurrency"] = (t["TraitTreeXTraitCurrency"][0], t["TraitTreeXTraitCurrency"][1][1:])
        m = self.model(nocur)
        self.assertNotIn("Mage", [t.class_name for t in m.trees.values()])
        self.assertTrue(any("no point currency" in a for a in m.anomalies))

    def test_a_missing_table_raises(self):
        def drop(t):
            del t["TraitCond"]
        with self.assertRaises(DatasetError):
            self.model(drop)


class NodeTests(TreeCase):
    def test_nodes_positions_entries_and_ranks(self):
        t = self.tree()
        self.assertEqual(sorted(t.nodes, key=int), ["1", "2", "3", "4", "5", "6"])
        n = t.nodes["2"]
        self.assertEqual((n.x, n.y, n.max_ranks, n.spell_id), (1020, 2730, 2, "3002"))
        self.assertEqual(n.entries[0].definition_id, "2002")

    def test_a_node_without_an_entry_cannot_be_bought(self):
        def noentry(t):
            add_row(t, "TraitNode", ID="8", TraitTreeID="100", PosX="1020", PosY="3330")
        t = self.tree(noentry)
        self.assertEqual(t.nodes["8"].max_ranks, 0)
        self.assertTrue(any("node 8 has no usable entry" in a for a in t.anomalies))

    def test_choice_nodes_are_flagged_not_supported(self):
        def choice(t):
            add_row(t, "TraitNodeEntry", ID="1101", TraitDefinitionID="2001", MaxRanks="1")
            add_row(t, "TraitNodeXTraitNodeEntry", ID="50", TraitNodeID="1", TraitNodeEntryID="1101", _Index="1")
        t = self.tree(choice)
        self.assertEqual(len(t.nodes["1"].entries), 2)
        self.assertEqual(t.nodes["1"].max_ranks, 2)  # first entry
        self.assertTrue(any("choice nodes are not supported" in a for a in t.anomalies))

    def test_entry_with_missing_definition_is_flagged(self):
        def broken(t):
            t["TraitDefinition"] = (t["TraitDefinition"][0], [r for r in t["TraitDefinition"][1] if r["ID"] != "2001"])
        t = self.tree(broken)
        self.assertTrue(any("missing" in a for a in t.anomalies))
        self.assertEqual(t.nodes["1"].max_ranks, 0)


class GateTests(TreeCase):
    def test_gates_carry_requirement_counted_and_gated_nodes(self):
        t = self.tree()
        by_id = {g.cond_id: g for g in t.gates}
        self.assertEqual(set(by_id), {"1", "2", "3", "4"})
        self.assertEqual((by_id["1"].required, by_id["1"].counted, by_id["1"].gated), (2, frozenset({"1"}), frozenset({"2"})))
        self.assertEqual((by_id["2"].required, by_id["2"].counted, by_id["2"].gated), (3, frozenset({"1", "2", "3"}), frozenset({"3"})))

    def test_gates_by_node_and_edges_into(self):
        t = self.tree()
        self.assertEqual([g.cond_id for g in t.gates_by_node["2"]], ["1"])
        self.assertNotIn("1", t.gates_by_node)  # root nodes are ungated
        self.assertEqual([(e.left, e.type) for e in t.edges_into["2"]], [("1", 2)])

    def test_a_node_can_have_several_gates(self):
        def second(t):
            add_row(t, "TraitCond", ID="5", CondType="0", TraitTreeID="100", TraitNodeGroupID="13", TraitCurrencyID="3820", SpentAmountRequired="1")
            add_row(t, "TraitNodeGroupXTraitNode", ID="20", TraitNodeGroupID="25", TraitNodeID="2")
            add_row(t, "TraitNodeGroupXTraitCond", ID="9", TraitCondID="5", TraitNodeGroupID="25")
        self.assertEqual(sorted(g.cond_id for g in self.tree(second).gates_by_node["2"]), ["1", "5"])

    def test_a_node_level_condition_gates_that_node(self):
        def single(t):
            add_row(t, "TraitCond", ID="6", CondType="0", TraitTreeID="100", TraitNodeGroupID="13", TraitNodeID="4",
                    TraitCurrencyID="3820", SpentAmountRequired="1")
            add_row(t, "TraitNodeXTraitCond", ID="1", TraitCondID="6", TraitNodeID="6")
        g = [g for g in self.tree(single).gates if g.cond_id == "6"][0]
        self.assertEqual((g.gated, g.counted), (frozenset({"6"}), frozenset({"4"})))

    def test_a_condition_requiring_nothing_is_skipped_quietly(self):
        def trivial(t):
            add_row(t, "TraitCond", ID="7", CondType="0", TraitTreeID="100", TraitCurrencyID="0", SpentAmountRequired="0")
        t = self.tree(trivial)
        self.assertEqual(len(t.gates), 4)
        self.assertEqual(t.anomalies, [])

    def test_a_real_condition_of_another_kind_is_flagged(self):
        def odd(t):
            add_row(t, "TraitCond", ID="8", CondType="2", TraitTreeID="100", TraitNodeGroupID="13", TraitCurrencyID="3820", SpentAmountRequired="5")
        t = self.tree(odd)
        self.assertEqual(len(t.gates), 4)
        self.assertTrue(any("condition 8" in a and "not a spend condition" in a for a in t.anomalies))

    def test_unknown_nodes_in_groups_are_flagged_and_dropped(self):
        def ghost(t):
            add_row(t, "TraitNodeGroupXTraitNode", ID="30", TraitNodeGroupID="21", TraitNodeID="777")
        t = self.tree(ghost)
        self.assertTrue(any("condition 1 names 1 node(s)" in a and "777" in a for a in t.anomalies))
        self.assertEqual([g for g in t.gates if g.cond_id == "1"][0].gated, frozenset({"2"}))

    def test_a_gate_that_gates_nothing_known_is_dropped(self):
        def orphan(t):
            add_row(t, "TraitCond", ID="9", CondType="0", TraitTreeID="100", TraitNodeGroupID="13", TraitCurrencyID="3820", SpentAmountRequired="4")
        t = self.tree(orphan)
        self.assertEqual(len(t.gates), 4)
        self.assertTrue(any("condition 9 gates no known node" in a for a in t.anomalies))

    def test_edges_to_other_trees_are_ignored(self):
        def cross(t):
            add_row(t, "TraitEdge", ID="9", LeftTraitNodeID="1", RightTraitNodeID="7", Type="2")
        self.assertEqual(len(self.tree(cross).edges), 2)


class SubTreeTests(TreeCase):
    def test_top_tier_groups_become_ordered_sub_trees(self):
        t = self.tree()
        self.assertEqual(t.sub_trees, (frozenset({"1", "2", "3"}), frozenset({"4", "5", "6"})))
        self.assertEqual([t.nodes[n].sub_tree for n in "123456"], [0, 0, 0, 1, 1, 1])

    def test_sub_trees_are_ordered_left_to_right_regardless_of_data_order(self):
        def swap(t):  # move the left sub-tree to the right
            for r in t["TraitNode"][1]:
                if r["ID"] in ("1", "2", "3"):
                    r["PosX"] = str(int(r["PosX"]) + 8000)
        t = self.tree(swap)
        self.assertEqual(t.sub_trees[0], frozenset({"4", "5", "6"}))

    def test_overlapping_sub_trees_are_flagged(self):
        def overlap(t):
            add_row(t, "TraitNodeGroupXTraitNode", ID="40", TraitNodeGroupID="14", TraitNodeID="3")
        self.assertTrue(any("overlap" in a for a in self.tree(overlap).anomalies))

    def test_a_node_in_no_sub_tree_is_flagged(self):
        def loose(t):
            add_row(t, "TraitNode", ID="8", TraitTreeID="100", PosX="1020", PosY="3330")
            add_row(t, "TraitNodeEntry", ID="1008", TraitDefinitionID="2008", MaxRanks="1")
            add_row(t, "TraitNodeXTraitNodeEntry", ID="8", TraitNodeID="8", TraitNodeEntryID="1008")
            add_row(t, "TraitDefinition", ID="2008", SpellID="3008")
        t = self.tree(loose)
        self.assertEqual(t.nodes["8"].sub_tree, -1)
        self.assertTrue(any("belong to no sub-tree: 8" in a for a in t.anomalies))

    def test_a_tree_with_no_gates_has_no_sub_trees(self):
        t = self.model().by_name("Warrior")
        self.assertEqual((t.sub_trees, t.gates), ((), ()))


class GridTests(TreeCase):
    """Rows share one offset tree-wide; columns share one per sub-tree."""

    def grid_tree(self, mutate=None):
        def many(t):  # a few more nodes per sub-tree so the grid has a clear majority
            for i, (x, y) in enumerate([(1620, 2130), (2220, 2130), (1620, 2730)]):
                nid = str(20 + i)
                add_row(t, "TraitNode", ID=nid, TraitTreeID="100", PosX=str(x), PosY=str(y))
                add_row(t, "TraitNodeGroupXTraitNode", ID=str(60 + i), TraitNodeGroupID="12", TraitNodeID=nid)
            for i, (x, y) in enumerate([(5620, 2130), (6220, 2130), (5620, 2730)]):
                nid = str(30 + i)
                add_row(t, "TraitNode", ID=nid, TraitTreeID="100", PosX=str(x), PosY=str(y))
                add_row(t, "TraitNodeGroupXTraitNode", ID=str(70 + i), TraitNodeGroupID="14", TraitNodeID=nid)
            if mutate:
                mutate(t)
        return self.tree(many)

    def test_different_offsets_per_sub_tree_are_not_flagged(self):
        self.assertEqual([a for a in self.grid_tree().anomalies if "grid" in a], [])

    def test_a_row_typo_is_flagged(self):
        t = self.grid_tree(lambda t: [r.update(PosY="39300") for r in t["TraitNode"][1] if r["ID"] == "22"])
        self.assertTrue(any("node 22 is off the position grid (y=39300" in a for a in t.anomalies))

    def test_a_column_typo_is_flagged_against_its_own_sub_tree(self):
        t = self.grid_tree(lambda t: [r.update(PosX="102800") for r in t["TraitNode"][1] if r["ID"] == "31"])
        self.assertTrue(any("node 31 is off the position grid (x=102800; sub-tree 1" in a for a in t.anomalies))

    def test_tiny_groups_are_not_judged(self):
        self.assertEqual([a for a in self.tree().anomalies if "grid" in a], [])  # 3 nodes per sub-tree, all consistent


class BudgetTests(unittest.TestCase):
    B = Budget("3820", 6, tuple(PointSource(lv, 1, "") for lv in range(10, 17)) + (PointSource(9, 1, "9000"),))

    def test_points_by_level(self):
        self.assertEqual([self.B.points(lv) for lv in (0, 9, 10, 11, 15)], [0, 0, 1, 2, 6])

    def test_cap_applies(self):
        self.assertEqual((self.B.points(16), self.B.points(99)), (6, 6))  # 7 sources, cap 6

    def test_entry_tied_source_needs_the_entry(self):
        self.assertEqual((self.B.points(9), self.B.points(9, {"9000"}), self.B.points(9, ["other"])), (0, 1, 0))

    def test_owned_entry_brings_points_forward_but_the_cap_still_holds(self):
        self.assertEqual((self.B.points(10), self.B.points(10, {"9000"})), (1, 2))
        self.assertEqual(self.B.points(16, {"9000"}), 6)

    def test_uncapped_budget(self):
        self.assertEqual(Budget("x", 0, (PointSource(1, 5, ""), PointSource(2, 5, ""))).points(5), 10)


class BudgetLoadingTests(TreeCase):
    def test_loaded_from_the_tables(self):
        b = self.tree().budget
        self.assertEqual((b.currency_id, b.cap, len(b.sources)), ("3820", 6, 8))
        self.assertEqual((b.points(9), b.points(10), b.points(16), b.points(9, {"9000"})), (0, 1, 6, 1))

    def test_sources_needing_outside_state_are_ignored(self):
        def outside(t):
            add_row(t, "TraitCurrencySource", ID="60", TraitCurrencyID="3820", Amount="5", QuestID="123", PlayerLevel="1")
            add_row(t, "TraitCurrencySource", ID="61", TraitCurrencyID="3820", Amount="5", AchievementID="55", PlayerLevel="1")
        self.assertEqual(self.tree(outside).budget.points(1), 0)

    def test_cost_per_rank(self):
        self.assertEqual(self.tree().cost_per_rank, 1)
        self.assertEqual(self.tree(lambda t: t["TraitCost"][1][0].update(Amount="2")).cost_per_rank, 2)

    def test_missing_cost_assumes_one_and_says_so(self):
        def nocost(t):
            t["TraitTreeXTraitCost"] = (t["TraitTreeXTraitCost"][0], [r for r in t["TraitTreeXTraitCost"][1] if r["TraitTreeID"] != "100"])
        t = self.tree(nocost)
        self.assertEqual(t.cost_per_rank, 1)
        self.assertTrue(any("assuming 1" in a for a in t.anomalies))

    def test_all_anomalies_are_prefixed_with_the_class(self):
        def bad(t):
            add_row(t, "TraitCond", ID="9", CondType="0", TraitTreeID="100", TraitNodeGroupID="13", TraitCurrencyID="3820", SpentAmountRequired="4")
        self.assertTrue(any(a.startswith("Mage (tree 100): condition 9") for a in self.model(bad).all_anomalies()))


class CliTests(TempCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.root / "data"), *args])
        return code, out.getvalue()

    def install(self, build="1.60.1.70450", complete=True, mutate=None):
        directory = write_mini(self.root / "data" / "raw" / build, mutate)
        if complete:  # the real command needs every table; add empty stand-ins for those the fixture lacks
            for table in TABLES:
                path = directory / f"{table}.csv"
                if not path.exists():
                    path.write_text("ID\n1\n", encoding="utf-8")

    def test_summary_lists_each_class_tree(self):
        self.install()
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Build 1.60.1.70450: 2 class trees", out)
        self.assertIn("Mage", out)
        self.assertIn("sub-trees 3/3", out)
        self.assertIn("4 unlock gates", out)
        self.assertIn("6 points at level 60", out)
        self.assertIn("0 data anomalie(s).", out)

    def test_anomalies_are_listed_and_truncated(self):
        def many(t):
            for i in range(30):
                add_row(t, "TraitCond", ID=str(100 + i), CondType="2", TraitTreeID="100", TraitNodeGroupID="13",
                        TraitCurrencyID="3820", SpentAmountRequired="5")
        self.install(mutate=many)
        _, short = self.run_cli()
        self.assertIn("30 data anomalie(s):", short)
        self.assertIn("... 5 more (use --all)", short)
        _, full = self.run_cli("--all")
        self.assertNotIn("more (use --all)", full)

    def test_incomplete_build_points_to_complete(self):
        self.install(complete=False)
        code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("python -m wowcalc.update complete", out)

    def test_nothing_installed(self):
        self.assertEqual(self.run_cli()[0], 2)

    def test_explicit_build(self):
        self.install("1.60.1.70338")
        self.assertEqual(self.run_cli("--build", "1.60.1.70338")[0], 0)


# --------------------------------------------------------------------------
# Real data (skipped unless a complete build is present)
# --------------------------------------------------------------------------

PINNED = "1.60.1.70450"
REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL = REAL_ROOT / "raw" / PINNED
COMPLETE = REAL.is_dir() and all((REAL / f"{t}.csv").is_file() for t in TABLES)

CLASS_TREES = {"Warrior": "1117", "Paladin": "1100", "Hunter": "1091", "Rogue": "1111", "Priest": "1114",
               "Shaman": "1082", "Mage": "1112", "Warlock": "1116", "Druid": "1089"}


@unittest.skipUnless(COMPLETE, f"a complete {PINNED} is not at {REAL}")
class RealDataTests(unittest.TestCase):
    """Facts about build 1.60.1.70450. If a later build changes them, these fail on
    purpose: that is the moment to look at what changed."""

    @classmethod
    def setUpClass(cls):
        cls.model = load_model(Dataset(REAL, PINNED))

    def test_every_class_has_its_tree(self):
        self.assertEqual({t.class_name: t.tree_id for t in self.model.trees.values()}, CLASS_TREES)
        self.assertEqual(self.model.anomalies, [])

    def test_each_tree_is_three_non_overlapping_sub_trees(self):
        for tree in self.model.trees.values():
            self.assertEqual(len(tree.sub_trees), 3, tree.class_name)
            self.assertFalse(any("overlap" in a for a in tree.anomalies), tree.class_name)

    def test_budget_is_51_points_from_level_10(self):
        for tree in self.model.trees.values():
            b = tree.budget
            self.assertEqual([b.points(lv) for lv in (9, 10, 11, 60, 70)], [0, 1, 2, 51, 51], tree.class_name)
            self.assertEqual(tree.cost_per_rank, 1)

    def test_the_legacy_perk_brings_points_forward_to_level_5(self):
        b = self.model.by_name("Mage").budget
        perk = {"136852", "137374", "137373", "137372", "137371"}
        self.assertEqual((b.points(5), b.points(5, perk), b.points(60, perk)), (0, 1, 51))

    def test_no_unexpected_kinds_of_anomaly(self):
        for tree in self.model.trees.values():
            for a in tree.anomalies:
                self.assertTrue("off the position grid" in a or "belong to no sub-tree" in a or "names" in a, a)


if __name__ == "__main__":
    unittest.main()
