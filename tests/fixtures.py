"""Shared test fixtures: a small, fully consistent dataset and helpers."""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wowcalc.dataset import Dataset
from wowcalc.validate import validate_dataset

# --------------------------------------------------------------------------
# A small, fully consistent dataset, plus helpers to mutate and write it
# --------------------------------------------------------------------------

def base_tables():
    def rows(header, *data):
        return (header, [dict(zip(header, r)) for r in data])

    return {
        "TraitTree": rows(["TitleText_lang", "ID"], ["Tree A", "1"], ["Tree B", "2"]),
        "TraitNode": rows(
            ["ID", "TraitTreeID", "PosX", "PosY", "Type", "Flags", "TraitSubTreeID"],
            ["10", "1", "0", "0", "0", "0", "0"],
            ["11", "1", "1", "0", "0", "0", "0"],
            ["20", "2", "0", "0", "0", "0", "0"]),
        "TraitNodeEntry": rows(
            ["ID", "TraitDefinitionID", "MaxRanks", "NodeEntryType", "TraitSubTreeID"],
            ["100", "1000", "2", "0", "0"], ["101", "1001", "1", "0", "0"], ["200", "2000", "5", "0", "0"]),
        "TraitNodeXTraitNodeEntry": rows(
            ["ID", "TraitNodeID", "TraitNodeEntryID", "_Index"],
            ["1", "10", "100", "0"], ["2", "11", "101", "0"], ["3", "20", "200", "0"]),
        "TraitDefinition": rows(
            ["OverrideName_lang", "ID", "SpellID"],
            ["", "1000", "5000"], ["", "1001", "5001"], ["", "2000", "5002"]),
        "TraitDefinitionEffectPoints": rows(
            ["ID", "TraitDefinitionID", "EffectIndex", "OperationType", "CurveID"],
            ["1", "1000", "0", "0", "7"]),
        "TraitEdge": rows(
            ["ID", "VisualStyle", "LeftTraitNodeID", "RightTraitNodeID", "Type"],
            ["1", "0", "10", "11", "0"]),
        "CurvePoint": rows(
            ["Pos_0", "Pos_1", "ID", "CurveID", "OrderIndex"],
            *[[str(i), str(i * 2), str(i), "7", str(i)] for i in range(10)]),
        "SpellName": rows(["ID", "Name_lang"], ["5000", "A"], ["5001", "B"], ["5002", "C"]),
        "Spell": rows(
            ["ID", "NameSubtext_lang", "Description_lang", "AuraDescription_lang"],
            ["5000", "", "d", ""], ["5001", "", "d", ""], ["5002", "", "d", ""]),
        "SpellEffect": rows(
            ["ID", "SpellID", "EffectIndex", "EffectAura", "Effect", "EffectBasePointsF"],
            ["1", "5000", "0", "108", "6", "2"], ["2", "5001", "0", "0", "0", "0"], ["3", "5002", "0", "0", "0", "0"]),
        "ChrClasses": rows(["Name_lang", "ID"], ["Warrior", "1"], ["Mage", "2"]),
        "ChrRaces": rows(["ID", "Name_lang"], ["1", "Human"], ["2", "Orc"]),
        # --- extended tables ---
        "SkillLineXTraitTree": rows(["ID", "SkillLineID", "TraitTreeID", "Variant"],
                                    ["1", "237", "1", "0"], ["2", "373", "2", "0"]),
        "SkillRaceClassInfo": rows(
            ["ID", "SkillID", "ClassMask", "Flags", "Availability", "MinLevel", "SkillTierID", "RaceMasks_0", "RaceMasks_1"],
            ["1", "237", "128", "1040", "0", "0", "0", "-1", "-1"],
            ["2", "373", "64", "1040", "0", "0", "0", "-1", "-1"]),
        "TraitCond": rows(
            ["ID", "CondType", "TraitTreeID", "GrantedRanks", "TraitNodeGroupID", "TraitNodeID",
             "TraitNodeEntryID", "TraitCurrencyID", "SpentAmountRequired", "RequiredLevel"],
            ["1", "0", "1", "0", "11", "0", "0", "3820", "5", "0"]),
        "TraitNodeGroup": rows(["ID", "TraitTreeID", "Flags"], ["11", "1", "0"], ["12", "1", "0"]),
        "TraitNodeGroupXTraitNode": rows(
            ["ID", "TraitNodeGroupID", "TraitNodeID", "_Index"],
            ["1", "11", "10", "0"], ["2", "12", "11", "0"]),
        "TraitNodeGroupXTraitCond": rows(["ID", "TraitCondID", "TraitNodeGroupID"], ["1", "1", "12"]),
        "TraitNodeXTraitCond": rows(["ID", "TraitCondID", "TraitNodeID"], ["1", "1", "20"]),
        "TraitCurrency": rows(["ID", "Type", "SourcedMax"], ["3820", "2", "51"]),
        "TraitCurrencySource": rows(
            ["Requirement_lang", "ID", "TraitCurrencyID", "Amount", "PlayerLevel"],
            ["Level 10", "1", "3820", "1", "10"], ["Level 11", "2", "3820", "1", "11"]),
        "TraitTreeXTraitCurrency": rows(["ID", "_Index", "TraitTreeID", "TraitCurrencyID"],
                                        ["1", "0", "1", "3820"], ["2", "0", "2", "3820"]),
        "TraitCost": rows(["InternalName", "ID", "Amount", "TraitCurrencyID", "CurveID"],
                          ["Tree cost", "4039", "1", "3820", "0"]),
        "TraitTreeXTraitCost": rows(["ID", "TraitTreeID", "TraitCostID"], ["1", "1", "4039"], ["2", "2", "4039"]),
        "CharBaseInfo": rows(["ID", "RaceID", "ClassID", "OtherFactionRaceID"],
                             ["1", "1", "1", "0"], ["2", "1", "2", "0"], ["3", "2", "1", "0"]),
    }


def write_dataset(directory: Path, tables) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, (header, data) in tables.items():
        with (directory / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in data:
                writer.writerow([row.get(c, "") for c in header])
    return directory


def row_for(tables, table, **fields):
    header, _ = tables[table]
    return {c: fields.get(c, "") for c in header}


def drop_column(tables, table, column):
    header, data = tables[table]
    tables[table] = ([c for c in header if c != column], [{k: v for k, v in r.items() if k != column} for r in data])


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, tables, name="ds", version=None):
        return validate_dataset(Dataset(write_dataset(self.root / name, tables), version))

    def codes(self, report, severity=None):
        return sorted({i.code for i in report.issues if severity in (None, i.severity)})

    def find(self, report, code):
        return [i for i in report.issues if i.code == code]


def table_csv_bytes(header, data) -> bytes:
    """One table as CSV bytes, as a download would deliver it."""
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in data:
        writer.writerow([row.get(c, "") for c in header])
    return buf.getvalue().encode("utf-8")
