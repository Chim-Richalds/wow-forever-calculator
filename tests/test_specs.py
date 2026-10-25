"""Offline tests for wowcalc.specs. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wowcalc.specs import HEADER, SpecError, load_spec_names, name_trees  # noqa: E402


class Entry:
    def __init__(self, spell_id):
        self.spell_id = spell_id


class Node:
    def __init__(self, id, spell, sub):
        self.id, self.entries, self.sub_tree = id, (Entry(spell),), sub


class Tree:
    class_name = "Mage"

    def __init__(self, layout):
        """layout: {sub index: [spell ids]}, -1 for no sub-tree"""
        self.nodes = {}
        for sub, spells in layout.items():
            for spell in spells:
                self.nodes[f"n{spell}"] = Node(f"n{spell}", spell, sub)
        self.sub_trees = tuple(frozenset() for i in range(max(layout) + 1))


NAMES = {"1": "A1", "2": "A2", "3": "A3", "4": "B1", "5": "B2", "6": "B3", "7": "C1", "8": "C2", "9": "C3"}
LAYOUT = {0: ["1", "2", "3", "10"], 1: ["4", "5", "6", "11"], 2: ["7", "8", "9"]}
NAMES |= {"10": "X", "11": "Y"}
SPECS = [("Alpha", [("1", "A1"), ("2", "A2"), ("3", "A3")]), ("Beta", [("4", "B1"), ("5", "B2"), ("6", "B3")]),
         ("Gamma", [("7", "C1"), ("8", "C2"), ("9", "C3")])]


class Naming(unittest.TestCase):
    def test_each_tree_gets_its_name_by_spell_not_position(self):
        names, problems = name_trees(Tree(LAYOUT), SPECS, NAMES)
        self.assertEqual((names, problems), (["Alpha", "Beta", "Gamma"], []))
        shuffled = {0: LAYOUT[2], 1: LAYOUT[0], 2: LAYOUT[1]}   # the same talents in different positions
        names, problems = name_trees(Tree(shuffled), SPECS, NAMES)
        self.assertEqual((names, problems), (["Gamma", "Alpha", "Beta"], []))

    def test_a_talent_that_is_not_in_the_tree_blocks_that_name(self):
        specs = [SPECS[0], SPECS[1], ("Gamma", [("7", "C1"), ("99", "Nope"), ("9", "C3")])]
        names, problems = name_trees(Tree(LAYOUT), specs, NAMES)
        self.assertEqual(names, ["Alpha", "Beta", None])
        self.assertTrue(any("99" in p and "not in this tree" in p for p in problems))

    def test_a_renamed_spell_is_reported(self):
        names, problems = name_trees(Tree(LAYOUT), SPECS, {**NAMES, "5": "Renamed"})
        self.assertEqual(names, ["Alpha", None, "Gamma"])
        self.assertTrue(any("'Renamed'" in p for p in problems))

    def test_talents_split_across_trees_block_the_name(self):
        specs = [("Alpha", [("1", "A1"), ("4", "B1"), ("3", "A3")])] + SPECS[1:]
        names, problems = name_trees(Tree(LAYOUT), specs, NAMES)
        self.assertEqual(names[0], None)
        self.assertTrue(any("not all in one" in p for p in problems))

    def test_two_specs_claiming_one_tree_cancel_out(self):
        specs = SPECS + [("Delta", [("1", "A1"), ("2", "A2"), ("3", "A3")])]
        names, problems = name_trees(Tree(LAYOUT), specs, NAMES)
        self.assertEqual(names, [None, "Beta", "Gamma"])
        self.assertTrue(any("both match" in p for p in problems))

    def test_a_tree_missing_from_the_file_says_so(self):
        names, problems = name_trees(Tree(LAYOUT), SPECS[:2], NAMES)
        self.assertEqual(names, ["Alpha", "Beta", None])
        self.assertEqual(problems, ["Mage: tree 3 has no name in the file"])

    def test_talents_outside_any_specialization_tree_cannot_name_one(self):
        layout = {-1: ["20"], 0: ["1", "2", "3"], 1: ["4", "5", "6"], 2: ["7", "8", "9"]}
        tree = Tree({k: v for k, v in layout.items() if k >= 0})
        tree.nodes["n20"] = Node("n20", "20", -1)
        specs = [("Zeta", [("20", "Z"), ("20", "Z"), ("20", "Z")])] + SPECS
        names, problems = name_trees(tree, specs, {**NAMES, "20": "Z"})
        self.assertEqual(names, ["Alpha", "Beta", "Gamma"])
        self.assertTrue(any("Zeta" in p for p in problems))


class Loading(unittest.TestCase):
    def write(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spec_names.tsv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_in_file_order(self):
        path = self.write("\t".join(HEADER) + "\nMage\tFire\t11119\tIgnite\nMage\tFire\t1\tX\nMage\tFrost\t2\tY\n\nRogue\tCombat\t3\tZ\n")
        self.assertEqual(load_spec_names(path), {"Mage": [("Fire", [("11119", "Ignite"), ("1", "X")]), ("Frost", [("2", "Y")])],
                                                 "Rogue": [("Combat", [("3", "Z")])]})

    def test_bad_files_are_refused_with_a_line_number(self):
        for text, needle in (("class\tspec\n", "first line"), ("\t".join(HEADER) + "\nMage\tFire\tx\tIgnite\n", "line 2"),
                             ("\t".join(HEADER) + "\nMage\tFire\t1\n", "line 2"), ("\t".join(HEADER) + "\nMage\t\t1\tI\n", "line 2")):
            with self.assertRaisesRegex(SpecError, needle):
                load_spec_names(self.write(text))

    def test_a_missing_file(self):
        with self.assertRaises(SpecError):
            load_spec_names(Path("/nonexistent/spec_names.tsv"))


class Shipped(unittest.TestCase):
    def test_the_shipped_file_names_three_specs_per_class_with_three_talents_each(self):
        path = REPO / "data" / "external" / "spec_names.tsv"
        if not path.is_file():
            self.skipTest("no data/external/spec_names.tsv here")
        specs = load_spec_names(path)
        self.assertEqual(sorted(specs), ["Druid", "Hunter", "Mage", "Paladin", "Priest", "Rogue", "Shaman", "Warlock", "Warrior"])
        for cls, items in specs.items():
            self.assertEqual(len(items), 3, cls)
            self.assertEqual(len({s for s, _ in items}), 3, cls)
            self.assertTrue(all(len(a) == 3 for _, a in items), cls)
            ids = [i for _, a in items for i, _ in a]
            self.assertEqual(len(ids), len(set(ids)), cls)


if __name__ == "__main__":
    unittest.main()
