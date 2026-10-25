"""A compact two-class talent dataset for testing wowcalc.trees.

Mage tree 100 (skill 237), two sub-trees side by side:

    left  (x = 1020, grid offset 420)      right (x = 5020, grid offset 220)
      node 1  row 0   2 ranks                node 4  row 0   1 rank
      node 2  row 1   2 ranks                node 5  row 1   2 ranks
      node 3  row 2   1 rank (capstone)      node 6  row 2   1 rank (capstone)

Gates:  cond 1: node 2 needs 2 points in {1}        cond 3: node 5 needs 1 point in {4}
        cond 2: node 3 needs 3 points in {1,2,3}   cond 4: node 6 needs 3 points in {4,5,6}
        (2 and 4 are the top tier, so their counted groups are the two sub-trees)
Links:  1 -> 2 (type 2), 4 -> 5 (type 0, visual only)

Warrior tree 200 (skill 26): one node.
Points: levels 10..16 give 1 each (cap 6); a level-9 point needs entry 9000.
Cost per rank: 1.
"""

from fixtures import write_dataset


def _rows(header, *data):
    return (header, [dict(zip(header, r)) for r in data])


def mini_tables():
    nodes = [  # id, tree, x, y
        (1, 100, 1020, 2130), (2, 100, 1020, 2730), (3, 100, 1020, 3330),
        (4, 100, 5020, 2130), (5, 100, 5020, 2730), (6, 100, 5020, 3330),
        (7, 200, 1020, 2130),
    ]
    ranks = {1: 2, 2: 2, 3: 1, 4: 1, 5: 2, 6: 1, 7: 1}
    return {
        "ChrClasses": _rows(["Name_lang", "ID"], ["Warrior", "1"], ["Priest", "5"], ["Mage", "8"]),
        "SkillRaceClassInfo": _rows(
            ["ID", "SkillID", "ClassMask", "RaceMasks_0", "RaceMasks_1"],
            ["1", "237", "128", "-1", "-1"], ["2", "26", "1", "-1", "-1"]),
        "SkillLineXTraitTree": _rows(["ID", "SkillLineID", "TraitTreeID", "Variant"],
                                     ["1", "237", "100", "0"], ["2", "26", "200", "0"]),
        "TraitNode": _rows(
            ["ID", "TraitTreeID", "PosX", "PosY", "Type"],
            *[[str(i), str(t), str(x), str(y), "0"] for i, t, x, y in nodes]),
        "TraitNodeEntry": _rows(
            ["ID", "TraitDefinitionID", "MaxRanks"],
            *[[str(1000 + i), str(2000 + i), str(ranks[i])] for i in ranks]),
        "TraitNodeXTraitNodeEntry": _rows(
            ["ID", "TraitNodeID", "TraitNodeEntryID", "_Index"],
            *[[str(i), str(i), str(1000 + i), "0"] for i in ranks]),
        "TraitDefinition": _rows(["ID", "SpellID"], *[[str(2000 + i), str(3000 + i)] for i in ranks]),
        "TraitEdge": _rows(
            ["ID", "VisualStyle", "LeftTraitNodeID", "RightTraitNodeID", "Type"],
            ["1", "1", "1", "2", "2"], ["2", "1", "4", "5", "0"]),
        "TraitCond": _rows(
            ["ID", "CondType", "TraitTreeID", "TraitNodeGroupID", "TraitNodeID", "TraitCurrencyID", "SpentAmountRequired"],
            ["1", "0", "100", "11", "0", "3820", "2"], ["2", "0", "100", "12", "0", "3820", "3"],
            ["3", "0", "100", "13", "0", "3820", "1"], ["4", "0", "100", "14", "0", "3820", "3"]),
        "TraitNodeGroupXTraitNode": _rows(
            ["ID", "TraitNodeGroupID", "TraitNodeID", "_Index"],
            ["1", "11", "1", "0"], ["2", "12", "1", "0"], ["3", "12", "2", "0"], ["4", "12", "3", "0"],
            ["5", "13", "4", "0"], ["6", "14", "4", "0"], ["7", "14", "5", "0"], ["8", "14", "6", "0"],
            # the groups that are gated:
            ["9", "21", "2", "0"], ["10", "22", "3", "0"], ["11", "23", "5", "0"], ["12", "24", "6", "0"]),
        "TraitNodeGroupXTraitCond": _rows(
            ["ID", "TraitCondID", "TraitNodeGroupID"],
            ["1", "1", "21"], ["2", "2", "22"], ["3", "3", "23"], ["4", "4", "24"]),
        "TraitNodeXTraitCond": _rows(["ID", "TraitCondID", "TraitNodeID"]),
        "TraitCurrency": _rows(["ID", "Type", "SourcedMax"], ["3820", "2", "6"]),
        "TraitCurrencySource": _rows(
            ["Requirement_lang", "ID", "TraitCurrencyID", "Amount", "QuestID", "AchievementID", "PlayerLevel", "TraitNodeEntryID"],
            *[[f"Level {lv}", str(lv), "3820", "1", "0", "0", str(lv), "0"] for lv in range(10, 17)],
            ["Perk", "50", "3820", "1", "0", "0", "9", "9000"]),
        "TraitTreeXTraitCurrency": _rows(["ID", "_Index", "TraitTreeID", "TraitCurrencyID"],
                                         ["1", "0", "100", "3820"], ["2", "0", "200", "3820"]),
        "TraitCost": _rows(["InternalName", "ID", "Amount", "TraitCurrencyID", "CurveID"], ["Tree cost", "4039", "1", "3820", "0"]),
        "TraitTreeXTraitCost": _rows(["ID", "TraitTreeID", "TraitCostID"], ["1", "100", "4039"], ["2", "200", "4039"]),
    }


def write_mini(directory, mutate=None):
    tables = mini_tables()
    if mutate:
        mutate(tables)
    return write_dataset(directory, tables)


def add_row(tables, name, **fields):
    header, data = tables[name]
    data.append({c: fields.get(c, "") for c in header})
