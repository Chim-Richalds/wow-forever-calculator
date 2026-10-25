"""Shared helpers for tests that need a base stats file."""

HEADER = ["Class", "Race", "Health – level 1", "Mana–L1", "Strength-L1", "Agility-L1", "Stamina-L1", "Intellect-L1",
          "Spirit-L1", "Health – level 60", "Mana-L60", "Strength-L60", "Agility-L60", "Stamina-L60", "Intellect-L60",
          "Spirit-L60"]


def tsv(*rows, header=None):
    lines = ["\t".join(header or HEADER)]
    lines += ["\t".join(str(c) for c in r) for r in rows]
    return "\n".join(lines) + "\n"


def row(cls, race, l1=(50, 100, 20, 20, 20, 20, 20), l60=(1500, 2000, 60, 60, 60, 60, 60)):
    return (cls, race, *l1, *l60)


def warrior(race, **kw):  # no mana
    r = row("Warrior", race, **kw)
    return r[:3] + ("",) + r[4:10] + ("",) + r[11:]
