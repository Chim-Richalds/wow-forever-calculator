"""Names for the three specialization trees of each class.

The game data has no spec names (its specialization table has one placeholder row
per class), so they come from `data/external/spec_names.tsv`:

    class  spec  spell_id  talent

Each spec is tied to its tree by three of its talents, by SPELL, never by position:
a tree's index and its talent slots can change between builds, a spell does not.
A name is applied only if every one of its talents is found, all in the same tree,
the talent's name in the data matches the file, and no two specs claim one tree.
Anything else is reported and that tree keeps its number.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .trees import ClassTree

HEADER = ("class", "spec", "spell_id", "talent")


class SpecError(Exception):
    pass


def load_spec_names(path: Path) -> dict[str, list[tuple[str, list[tuple[str, str]]]]]:
    """class -> [(spec, [(spell id, talent name), ...]), ...] in file order."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            head = next(reader, None)
            if head is None or tuple(h.strip() for h in head) != HEADER:
                raise SpecError(f"{path}: the first line must be: {chr(9).join(HEADER)}")
            out: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(dict)
            for number, row in enumerate(reader, start=2):
                if not any(c.strip() for c in row):
                    continue
                if len(row) != 4 or not all(c.strip() for c in row) or not row[2].strip().isdigit():
                    raise SpecError(f"{path} line {number}: expected class, spec, numeric spell id and talent name")
                cls, spec, spell, talent = (c.strip() for c in row)
                out[cls].setdefault(spec, []).append((spell, talent))
    except OSError as exc:
        raise SpecError(f"{path}: {exc}") from exc
    return {cls: list(specs.items()) for cls, specs in out.items()}


def name_trees(tree: ClassTree, specs: list[tuple[str, list[tuple[str, str]]]], spell_names: dict[str, str]) -> tuple[list[str | None], list[str]]:
    """(one name or None per sub-tree, problems)."""
    names: list[str | None] = [None] * len(tree.sub_trees)
    problems: list[str] = []
    where: dict[str, int] = {}
    for node in tree.nodes.values():
        for entry in node.entries:
            where.setdefault(entry.spell_id, node.sub_tree)
    for spec, anchors in specs:
        found: set[int] = set()
        ok = True
        for spell, talent in anchors:
            if spell not in where:
                problems.append(f"{tree.class_name} {spec}: spell {spell} ({talent}) is not in this tree")
                ok = False
                continue
            actual = spell_names.get(spell)
            if actual != talent:
                problems.append(f"{tree.class_name} {spec}: spell {spell} is {actual!r} in the data, not {talent!r}")
                ok = False
            found.add(where[spell])
        if not ok:
            continue
        if len(found) != 1 or -1 in found:
            problems.append(f"{tree.class_name} {spec}: its talents are not all in one specialization tree")
            continue
        index = found.pop()
        if names[index] is not None:
            problems.append(f"{tree.class_name}: {spec} and {names[index]} both match tree {index + 1}")
            names[index] = None
            continue
        names[index] = spec
    unnamed = [i + 1 for i, n in enumerate(names) if n is None]
    if unnamed and not problems:
        problems.append(f"{tree.class_name}: tree {', '.join(map(str, unnamed))} has no name in the file")
    return names, problems
