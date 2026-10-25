"""Is a talent allocation legal, and what can be bought next?

An allocation maps node IDs to ranks bought. Legality is judged the way the game
would: by whether the points could have been bought one at a time, in some order,
each purchase allowed at the moment it is made. Because every rule only ever gets
easier to satisfy as more points are spent, a simple greedy search decides this
exactly: keep buying any rank that is currently available; if everything wanted
can be reached, some order exists. (Tested against exhaustive search.)

Two kinds of rule come from the data:

  gates   Spend N points in a set of nodes before the gated nodes unlock. The
          count is taken BEFORE the purchase, so a capstone never counts toward
          its own requirement. This is why a "final state" check would be wrong.

  links   Prerequisite arrows between nodes (trees.Edge). The data gives each a
          Type but not its in-game meaning. Meaning used here, following the retail
          talent system's edge types:
              0  visual only; no effect on availability                 (not verified)
              2  "sufficient": if a node has any of these arrows pointing at it,
                 at least one source node must be fully ranked           (VERIFIED)
              3  "required": every such source node must be fully ranked (not verified)
          "Fully ranked" means all of the source node's ranks are bought. Verified
          in game, October 2026: Anger Management unlocks only when Improved
          Tactical Mastery is 5/5. Types 0 and 3 are rare (about 14 of 98 links in
          build 70338) and have not been checked. EDGE_NEEDS_FULL_RANK stays True.

Nothing here knows about stats; it only knows what is buyable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .trees import ClassTree

Allocation = dict[str, int]  # node ID -> ranks bought

EDGE_VISUAL, EDGE_SUFFICIENT, EDGE_REQUIRED = 0, 2, 3
EDGE_NEEDS_FULL_RANK = True  # verified in game for type-2 links; see module docstring


@dataclass(frozen=True)
class Verdict:
    valid: bool
    problems: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.valid


def spent(tree: ClassTree, alloc: Allocation) -> int:
    return sum(r for r in alloc.values() if r > 0) * tree.cost_per_rank


def _points_in(tree: ClassTree, state: dict[str, int], nodes: Iterable[str]) -> int:
    return sum(state.get(n, 0) for n in nodes) * tree.cost_per_rank


def _source_ok(tree: ClassTree, state: dict[str, int], node_id: str) -> bool:
    node = tree.nodes.get(node_id)
    if node is None or node.max_ranks <= 0:
        return False
    needed = node.max_ranks if EDGE_NEEDS_FULL_RANK else 1
    return state.get(node_id, 0) >= needed


def why_unavailable(tree: ClassTree, state: dict[str, int], node_id: str) -> list[str]:
    """Reasons `node_id` cannot receive a rank right now. Empty means it can."""
    reasons: list[str] = []
    for gate in tree.gates_by_node.get(node_id, []):
        have = _points_in(tree, state, gate.counted)
        if have < gate.required:
            reasons.append(f"needs {gate.required} points spent in its group (has {have})")
    sufficient: list[str] = []
    for edge in tree.edges_into.get(node_id, []):
        if edge.type == EDGE_REQUIRED and not _source_ok(tree, state, edge.left):
            reasons.append(f"requires node {edge.left} at full rank")
        elif edge.type == EDGE_SUFFICIENT:
            sufficient.append(edge.left)
    if sufficient and not any(_source_ok(tree, state, s) for s in sufficient):
        which = ", ".join(sufficient)
        reasons.append(f"requires one of nodes {which} at full rank")
    return reasons


def is_available(tree: ClassTree, state: dict[str, int], node_id: str) -> bool:
    return not why_unavailable(tree, state, node_id)


def reach(tree: ClassTree, target: Allocation) -> Allocation:
    """Buy as much of `target` as the rules allow, one rank at a time, in any
    order that works. The result equals `target` exactly when `target` is legal
    (ignoring budget)."""
    state = {n: 0 for n in target}
    progress = True
    while progress:
        progress = False
        for node_id, want in target.items():
            while state[node_id] < want and is_available(tree, state, node_id):
                state[node_id] += 1
                progress = True
    return state


def check(
    tree: ClassTree,
    alloc: Allocation,
    level: int | None = None,
    owned_entries: Iterable[str] = (),
) -> Verdict:
    """Is `alloc` a legal build? If `level` is given, the point budget applies."""
    problems: list[str] = []
    clean: Allocation = {}
    for node_id, ranks in alloc.items():
        node = tree.nodes.get(node_id)
        if node is None:
            problems.append(f"node {node_id} is not in this tree")
        elif ranks < 0:
            problems.append(f"node {node_id}: negative ranks ({ranks})")
        elif ranks > node.max_ranks:
            problems.append(f"node {node_id}: {ranks} ranks but the maximum is {node.max_ranks}")
        elif ranks > 0:
            clean[node_id] = ranks

    if level is not None:
        have = tree.budget.points(level, owned_entries)
        used = spent(tree, clean)
        if used > have:
            problems.append(f"uses {used} points but a level {level} character has {have}")

    if not problems:
        reached = reach(tree, clean)
        for node_id, want in clean.items():
            if reached[node_id] < want:
                why = why_unavailable(tree, reached, node_id)
                problems.append(
                    f"node {node_id}: can only reach {reached[node_id]} of {want} ranks"
                    + (f" ({'; '.join(why)})" if why else ""))
    return Verdict(not problems, tuple(problems))


def can_add(
    tree: ClassTree,
    alloc: Allocation,
    node_id: str,
    level: int | None = None,
    owned_entries: Iterable[str] = (),
) -> Verdict:
    """Would the build still be legal with one more rank in `node_id`?"""
    if node_id not in tree.nodes:
        return Verdict(False, (f"node {node_id} is not in this tree",))
    new = dict(alloc)
    new[node_id] = new.get(node_id, 0) + 1
    return check(tree, new, level, owned_entries)


def can_remove(tree: ClassTree, alloc: Allocation, node_id: str) -> Verdict:
    """Would the build still be legal with one rank taken out of `node_id`?
    Removing a rank can strand points that depended on it."""
    if alloc.get(node_id, 0) <= 0:
        return Verdict(False, (f"node {node_id} has no ranks to remove",))
    new = dict(alloc)
    new[node_id] -= 1
    return check(tree, new)


def available_nodes(
    tree: ClassTree,
    alloc: Allocation,
    level: int | None = None,
    owned_entries: Iterable[str] = (),
) -> list[str]:
    """Nodes that can legally take one more rank right now, in ID order."""
    base = check(tree, alloc, level, owned_entries)
    if not base:
        return []
    return sorted(
        (n for n in tree.nodes if can_add(tree, alloc, n, level, owned_entries)),
        key=lambda n: (len(n), n),
    )


def greedy_build(tree: ClassTree, points: int, only: Iterable[str] | None = None) -> Allocation:
    """A deterministic legal build that spends `points` (or as many as it can),
    filling nodes top row first, then left to right. `only` restricts it to some
    nodes (for example one sub-tree). For examples and tests; it makes no claim
    to be a good build."""
    allowed = set(tree.nodes) if only is None else set(only) & set(tree.nodes)
    order = sorted((tree.nodes[n] for n in allowed), key=lambda n: (n.y, n.x, n.id))
    alloc: Allocation = {}
    remaining = points // tree.cost_per_rank
    progress = True
    while remaining > 0 and progress:
        progress = False
        for node in order:
            if remaining <= 0:
                break
            if alloc.get(node.id, 0) < node.max_ranks and is_available(tree, alloc, node.id):
                alloc[node.id] = alloc.get(node.id, 0) + 1
                remaining -= 1
                progress = True
    return alloc
