"""Offline tests for wowcalc.allocation. Run: python -m unittest discover -s tests"""

import itertools
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase  # noqa: E402
from tree_fixtures import write_mini  # noqa: E402
from wowcalc import allocation as A  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.trees import Budget, ClassTree, Edge, Entry, Gate, Node, PointSource, load_model  # noqa: E402


def make_tree(ranks, gates=(), edges=(), cost=1):
    """A hand-built tree. ranks: {node: max ranks}. gates: (required, counted, gated).
    edges: (left, right, type). A character gets one point per level."""
    nodes = {
        n: Node(n, "T", 0, i * 600, (Entry(f"e{n}", f"d{n}", f"s{n}", r),) if r else (), -1)
        for i, (n, r) in enumerate(ranks.items())
    }
    return ClassTree(
        1, "Test", "T", "S", nodes,
        tuple(Edge(l, r, t) for l, r, t in edges),
        tuple(Gate(f"c{i}", req, frozenset(c), frozenset(g)) for i, (req, c, g) in enumerate(gates)),
        (), Budget("cur", 0, tuple(PointSource(lv, 1, "") for lv in range(1, 61))), cost)


class AvailabilityTests(unittest.TestCase):
    def test_an_ungated_unlinked_node_is_always_available(self):
        t = make_tree({"a": 2})
        self.assertTrue(A.is_available(t, {}, "a"))
        self.assertEqual(A.why_unavailable(t, {}, "a"), [])

    def test_gate_needs_enough_points_in_the_counted_nodes(self):
        t = make_tree({"a": 3, "b": 1}, gates=[(2, {"a"}, {"b"})])
        self.assertFalse(A.is_available(t, {"a": 1}, "b"))
        self.assertEqual(A.why_unavailable(t, {"a": 1}, "b"), ["needs 2 points spent in its group (has 1)"])
        self.assertTrue(A.is_available(t, {"a": 2}, "b"))

    def test_gate_counts_points_not_ranks_when_a_rank_costs_more(self):
        t = make_tree({"a": 3, "b": 1}, gates=[(2, {"a"}, {"b"})], cost=2)
        self.assertTrue(A.is_available(t, {"a": 1}, "b"))  # one rank costs 2 points

    def test_every_gate_on_a_node_must_hold(self):
        t = make_tree({"a": 3, "x": 3, "b": 1}, gates=[(2, {"a"}, {"b"}), (2, {"x"}, {"b"})])
        self.assertFalse(A.is_available(t, {"a": 2}, "b"))
        self.assertEqual(len(A.why_unavailable(t, {"a": 2}, "b")), 1)
        self.assertTrue(A.is_available(t, {"a": 2, "x": 2}, "b"))

    def test_sufficient_links_need_one_fully_ranked_source(self):
        t = make_tree({"a": 2, "b": 1, "c": 1}, edges=[("a", "c", 2), ("b", "c", 2)])
        self.assertFalse(A.is_available(t, {}, "c"))
        self.assertFalse(A.is_available(t, {"a": 1}, "c"))  # partial rank is not enough
        self.assertTrue(A.is_available(t, {"a": 2}, "c"))
        self.assertTrue(A.is_available(t, {"b": 1}, "c"))
        self.assertIn("requires one of nodes a, b at full rank", A.why_unavailable(t, {}, "c")[0])

    def test_required_links_need_every_source(self):
        t = make_tree({"a": 1, "b": 1, "c": 1}, edges=[("a", "c", 3), ("b", "c", 3)])
        self.assertFalse(A.is_available(t, {"a": 1}, "c"))
        self.assertEqual(A.why_unavailable(t, {"a": 1}, "c"), ["requires node b at full rank"])
        self.assertTrue(A.is_available(t, {"a": 1, "b": 1}, "c"))

    def test_required_and_sufficient_links_combine(self):
        t = make_tree({"a": 1, "b": 1, "d": 1, "c": 1}, edges=[("a", "c", 3), ("b", "c", 2), ("d", "c", 2)])
        self.assertFalse(A.is_available(t, {"a": 1}, "c"))
        self.assertFalse(A.is_available(t, {"b": 1}, "c"))
        self.assertTrue(A.is_available(t, {"a": 1, "d": 1}, "c"))

    def test_visual_links_have_no_effect(self):
        t = make_tree({"a": 1, "b": 1}, edges=[("a", "b", 0)])
        self.assertTrue(A.is_available(t, {}, "b"))

    def test_a_source_with_no_ranks_to_buy_never_satisfies_a_link(self):
        t = make_tree({"a": 0, "b": 1}, edges=[("a", "b", 2)])
        self.assertFalse(A.is_available(t, {"a": 0}, "b"))

    def test_the_full_rank_assumption_can_be_switched_off(self):
        t = make_tree({"a": 2, "c": 1}, edges=[("a", "c", 2)])
        self.assertFalse(A.is_available(t, {"a": 1}, "c"))
        with mock.patch.object(A, "EDGE_NEEDS_FULL_RANK", False):
            self.assertTrue(A.is_available(t, {"a": 1}, "c"))
            self.assertFalse(A.is_available(t, {}, "c"))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.t = make_tree({"a": 2, "b": 2, "cap": 1}, gates=[(2, {"a"}, {"b"}), (3, {"a", "b", "cap"}, {"cap"})])

    def test_empty_is_valid_and_verdicts_are_truthy(self):
        v = A.check(self.t, {})
        self.assertTrue(v and v.valid)
        self.assertEqual(v.problems, ())

    def test_a_legal_build(self):
        self.assertTrue(A.check(self.t, {"a": 2, "b": 1, "cap": 1}))

    def test_a_capstone_never_counts_toward_its_own_requirement(self):
        # 3 points in the counted group in the final state, but only 2 before the capstone.
        alloc = {"a": 2, "cap": 1}
        self.assertEqual(sum(alloc.values()), 3)
        v = A.check(self.t, alloc)
        self.assertFalse(v)
        self.assertIn("node cap: can only reach 0 of 1 ranks (needs 3 points spent in its group (has 2))", v.problems[0])

    def test_unknown_node_negative_and_over_max(self):
        v = A.check(self.t, {"zzz": 1, "a": -1, "b": 3})
        self.assertEqual(len(v.problems), 3)
        self.assertTrue(any("not in this tree" in p for p in v.problems))
        self.assertTrue(any("negative" in p for p in v.problems))
        self.assertTrue(any("maximum is 2" in p for p in v.problems))

    def test_zero_ranks_are_ignored(self):
        self.assertTrue(A.check(self.t, {"cap": 0, "a": 1}))

    def test_an_unreachable_node_reports_how_far_it_gets(self):
        v = A.check(self.t, {"a": 1, "b": 2})
        self.assertFalse(v)
        self.assertIn("node b: can only reach 0 of 2 ranks", v.problems[0])

    def test_budget_applies_only_when_a_level_is_given(self):
        alloc = {"a": 2, "b": 2}
        self.assertTrue(A.check(self.t, alloc))
        self.assertTrue(A.check(self.t, alloc, level=4))
        v = A.check(self.t, alloc, level=3)
        self.assertEqual(v.problems, ("uses 4 points but a level 3 character has 3",))

    def test_budget_counts_points_not_ranks(self):
        t = make_tree({"a": 2}, cost=2)
        self.assertEqual(A.spent(t, {"a": 2}), 4)
        self.assertFalse(A.check(t, {"a": 2}, level=3))
        self.assertTrue(A.check(t, {"a": 2}, level=4))

    def test_owned_entries_raise_the_budget(self):
        t = make_tree({"a": 2})
        t.budget = Budget("cur", 0, (PointSource(10, 1, ""), PointSource(9, 1, "perk")))
        self.assertFalse(A.check(t, {"a": 2}, level=10))
        self.assertTrue(A.check(t, {"a": 2}, level=10, owned_entries={"perk"}))


class ExhaustiveTests(unittest.TestCase):
    """The greedy search must agree with trying every possible purchase order."""

    def brute_force_reachable(self, tree, target):
        start = tuple(0 for _ in target)
        nodes = list(target)
        seen, stack = {start}, [start]
        goal = tuple(target[n] for n in nodes)
        while stack:
            state = stack.pop()
            if state == goal:
                return True
            as_dict = dict(zip(nodes, state))
            for i, n in enumerate(nodes):
                if state[i] < goal[i] and A.is_available(tree, as_dict, n):
                    nxt = state[:i] + (state[i] + 1,) + state[i + 1:]
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        return False

    def compare(self, tree):
        ranks = {n: node.max_ranks for n, node in tree.nodes.items()}
        names = list(ranks)
        total = valid = 0
        for combo in itertools.product(*(range(ranks[n] + 1) for n in names)):
            alloc = {n: r for n, r in zip(names, combo) if r}
            full = {n: r for n, r in zip(names, combo)}
            expected = self.brute_force_reachable(tree, full)
            self.assertEqual(bool(A.check(tree, alloc)), expected, f"disagreement on {alloc}")
            total += 1
            valid += expected
        return total, valid

    def test_gates_links_and_a_self_counting_capstone(self):
        t = make_tree(
            {"a": 2, "b": 2, "c": 1, "d": 1},
            gates=[(2, {"a"}, {"b"}), (3, {"a", "b", "c", "d"}, {"d"})],
            edges=[("a", "b", 2), ("b", "c", 3)])
        total, valid = self.compare(t)
        self.assertEqual(total, 36)
        self.assertTrue(0 < valid < total)  # a real mix of legal and illegal builds

    def test_alternative_prerequisites_and_overlapping_gates(self):
        t = make_tree(
            {"a": 1, "b": 2, "c": 1, "d": 2},
            gates=[(1, {"a", "b"}, {"c"}), (2, {"a", "b", "c"}, {"d"})],
            edges=[("a", "d", 2), ("b", "d", 2)])
        total, valid = self.compare(t)
        self.assertTrue(0 < valid < total)

    def test_with_the_full_rank_assumption_off(self):
        t = make_tree({"a": 2, "b": 2, "c": 1}, gates=[(1, {"a"}, {"b"})], edges=[("a", "c", 2), ("b", "c", 2)])
        with mock.patch.object(A, "EDGE_NEEDS_FULL_RANK", False):
            self.compare(t)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.t = make_tree({"a": 2, "b": 2, "cap": 1}, gates=[(2, {"a"}, {"b"}), (3, {"a", "b", "cap"}, {"cap"})])

    def test_can_add(self):
        self.assertTrue(A.can_add(self.t, {}, "a"))
        v = A.can_add(self.t, {}, "b")
        self.assertFalse(v)
        self.assertIn("needs 2 points", v.problems[0])
        self.assertFalse(A.can_add(self.t, {"a": 2}, "a"))  # already at max
        self.assertFalse(A.can_add(self.t, {}, "zzz"))

    def test_can_add_respects_the_budget(self):
        self.assertTrue(A.can_add(self.t, {"a": 1}, "a", level=2))
        v = A.can_add(self.t, {"a": 2}, "b", level=2)
        self.assertIn("uses 3 points but a level 2 character has 2", v.problems[0])

    def test_can_remove_refuses_to_strand_dependents(self):
        alloc = {"a": 2, "b": 1}
        self.assertFalse(A.can_remove(self.t, alloc, "a"))  # b needs 2 in a
        self.assertTrue(A.can_remove(self.t, alloc, "b"))

    def test_can_remove_with_spare_points(self):
        self.assertTrue(A.can_remove(self.t, {"a": 2, "b": 1, "cap": 1}, "b") is not None)
        self.assertFalse(A.can_remove(self.t, {"a": 2, "b": 1, "cap": 1}, "b"))  # cap needs 3 before it

    def test_can_remove_needs_a_rank_to_remove(self):
        self.assertFalse(A.can_remove(self.t, {}, "a"))
        self.assertIn("no ranks to remove", A.can_remove(self.t, {}, "a").problems[0])

    def test_available_nodes(self):
        self.assertEqual(A.available_nodes(self.t, {}), ["a"])
        self.assertEqual(A.available_nodes(self.t, {"a": 1}), ["a"])
        self.assertEqual(A.available_nodes(self.t, {"a": 2}), ["b"])  # a is maxed
        self.assertEqual(A.available_nodes(self.t, {"a": 2, "b": 1}), ["b", "cap"])

    def test_available_nodes_empty_for_an_illegal_build_or_an_empty_budget(self):
        self.assertEqual(A.available_nodes(self.t, {"b": 1}), [])
        self.assertEqual(A.available_nodes(self.t, {"a": 1}, level=1), [])

    def test_an_illegal_build_offers_nothing_even_if_one_more_rank_would_repair_it(self):
        t = make_tree({"a": 1, "b": 1}, gates=[(1, {"a"}, {"b"})])
        self.assertFalse(A.check(t, {"b": 1}))
        self.assertTrue(A.check(t, {"a": 1, "b": 1}))          # one rank in `a` would fix it...
        self.assertEqual(A.available_nodes(t, {"b": 1}), [])    # ...but the build as given is not legal, so: nothing

    def test_available_nodes_are_ordered_numerically(self):
        t = make_tree({str(n): 1 for n in (10, 9, 100, 2)})
        self.assertEqual(A.available_nodes(t, {}), ["2", "9", "10", "100"])


class GreedyBuildTests(unittest.TestCase):
    def test_spends_what_it_can_and_stays_legal(self):
        t = make_tree({"a": 2, "b": 2, "cap": 1}, gates=[(2, {"a"}, {"b"}), (3, {"a", "b", "cap"}, {"cap"})])
        for points in range(0, 8):
            b = A.greedy_build(t, points)
            self.assertTrue(A.check(t, b, level=max(points, 1)), (points, b))
            self.assertEqual(A.spent(t, b), min(points, 5))

    def test_is_deterministic(self):
        t = make_tree({"a": 2, "b": 2, "c": 2})
        self.assertEqual(A.greedy_build(t, 4), A.greedy_build(t, 4))

    def test_only_restricts_the_nodes(self):
        t = make_tree({"a": 2, "b": 2, "c": 2})
        self.assertEqual(A.greedy_build(t, 6, only={"b"}), {"b": 2})

    def test_cost_per_rank_is_respected(self):
        t = make_tree({"a": 5}, cost=2)
        self.assertEqual(A.greedy_build(t, 6), {"a": 3})

    def test_a_node_that_cannot_unlock_is_left_alone(self):
        t = make_tree({"a": 1, "b": 1}, gates=[(5, {"a"}, {"b"})])
        self.assertEqual(A.greedy_build(t, 10), {"a": 1})


class MiniTreeIntegrationTests(TempCase):
    """The loader and the engine together, on the fixture tree."""

    def setUp(self):
        super().setUp()
        self.tree = load_model(Dataset(write_mini(self.root / "ds"), "test")).by_name("Mage")

    def test_builds_on_the_loaded_tree(self):
        self.assertTrue(A.check(self.tree, {"1": 2, "2": 1, "3": 1}, level=16))
        self.assertFalse(A.check(self.tree, {"1": 1, "2": 1}))            # gate and link both unmet
        self.assertTrue(A.check(self.tree, {"4": 1, "5": 2, "6": 1}))     # right sub-tree, visual link ignored
        self.assertFalse(A.check(self.tree, {"1": 2, "3": 1}))            # capstone needs 3 first

    def test_budget_from_the_tables(self):
        build = {"1": 2, "2": 1, "3": 1}  # 4 points; the fixture gives one point per level from 10
        self.assertFalse(A.check(self.tree, build, level=12))   # 3 points
        self.assertTrue(A.check(self.tree, build, level=13))    # 4 points
        self.assertTrue(A.check(self.tree, {"1": 1}, level=9, owned_entries={"9000"}))
        self.assertFalse(A.check(self.tree, {"1": 1}, level=9))

    def test_the_cap_limits_the_budget_even_at_a_high_level(self):
        six = {"1": 2, "2": 2, "3": 1, "4": 1}
        seven = {**six, "5": 1}
        self.assertEqual((A.spent(self.tree, six), A.spent(self.tree, seven)), (6, 7))
        self.assertTrue(A.check(self.tree, six, level=60))
        v = A.check(self.tree, seven, level=60)
        self.assertEqual(v.problems, ("uses 7 points but a level 60 character has 6",))


# --------------------------------------------------------------------------
# Real data (skipped unless a complete build is present)
# --------------------------------------------------------------------------

PINNED = "1.60.1.70450"
REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL = REAL_ROOT / "raw" / PINNED
COMPLETE = REAL.is_dir() and all((REAL / f"{t}.csv").is_file() for t in TABLES)


@unittest.skipUnless(COMPLETE, f"a complete {PINNED} is not at {REAL}")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_model(Dataset(REAL, PINNED))

    def test_every_class_has_a_legal_full_budget_build(self):
        for tree in self.model.trees.values():
            points = tree.budget.points(60)
            build = A.greedy_build(tree, points)
            self.assertEqual(A.spent(tree, build), points, tree.class_name)
            self.assertTrue(A.check(tree, build, level=60), tree.class_name)

    def test_every_sub_tree_capstone_is_reachable_within_the_budget(self):
        for tree in self.model.trees.values():
            top = max(g.required for g in tree.gates)
            for members in tree.sub_trees:
                caps = set().union(*(g.gated for g in tree.gates if g.required == top and g.counted == members))
                build = A.greedy_build(tree, tree.budget.points(60), only=members)
                self.assertTrue(A.check(tree, build, level=60))
                self.assertTrue(any(build.get(n) for n in caps), f"{tree.class_name}: capstone not reached")

    def test_buying_everything_is_far_over_budget(self):
        for tree in self.model.trees.values():
            everything = {n: node.max_ranks for n, node in tree.nodes.items() if node.max_ranks}
            v = A.check(tree, everything, level=60)
            self.assertFalse(v, tree.class_name)
            self.assertTrue(any("uses" in p for p in v.problems), tree.class_name)

    def test_a_full_build_has_nothing_left_to_buy(self):
        tree = self.model.by_name("Shaman")
        build = A.greedy_build(tree, tree.budget.points(60))
        self.assertEqual(A.available_nodes(tree, build, level=60), [])
        self.assertTrue(A.available_nodes(tree, build))  # but is not stuck without a budget limit


if __name__ == "__main__":
    unittest.main()
