"""Offline tests for wowcalc.sheetbase. Run: python -m unittest discover -s tests"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase  # noqa: E402
from wowcalc import sheetbase  # noqa: E402
from wowcalc.sheetbase import (  # noqa: E402
    HEADER, SheetBase, SheetBaseError, convert_raw, load_sheet_base, parse_panel, problems_in,
)

# Panel text copied from the game (October 2026).
SHAMAN_SPELL = ("Spell Damage0Arcane Damage0Fire Damage0Frost Damage0Holy Damage0Nature Damage0Shadow Damage0Healing0"
                "Spell Hit0.00%Hit Chance0.00%Spell Crit8.09%Crit Chance4.32%Spell Penetration0Mana per 5 Sec.0")
SHAMAN_MELEE = "Attack Power276Hit Chance0.00%Crit Chance4.32%Haste0.00%Expertise0.00%"
SHAMAN_DEFENSE = "Armor104Damage Reduction1.86%Defense300Dodge Chance4.34%Total Avoidance9.34%"
HUNTER_DEFENSE = ("Armor38Damage Reduction7.27%Defense5Dodge Chance4.13%Parry Chance5.00%Total Avoidance14.13%"
                  "Boss Avoidance12.33%Boss Miss Chance4.40%Boss Crit Chance5.60%Boss Crush Chance15.00%")
HUNTER_RANGED = "Ranged Attack Power352Ranged Hit Chance0.00%Ranged Crit Chance0.75%Haste0.00%"
MAGE_SPELL = ("Frost Damage0Spell Damage0Arcane Damage0Fire Damage0Frost Damage0Holy Damage0Nature Damage0Shadow Damage0"
              "Healing0Spell Hit0.00%Hit Chance0.00%Spell Crit7.56%Crit Chance5.26%Spell Penetration0Mana per 5 Sec.0")


def raw(columns):
    """A raw wide export from [(heading, spell, melee, defense, ranged), ...]."""
    rows = [[c[0] for c in columns], ["Spell" if c[1] else "" for c in columns], [c[1] for c in columns],
            ["Melee"] * len(columns), [c[2] for c in columns], ["Defense"] * len(columns), [c[3] for c in columns],
            ["Ranged" if c[4] else "" for c in columns], [c[4] for c in columns]]
    return rows


FULL = [("Orc Shaman 60", SHAMAN_SPELL, SHAMAN_MELEE, SHAMAN_DEFENSE, ""),
        ("Orc Shaman 1", SHAMAN_SPELL, SHAMAN_MELEE, SHAMAN_DEFENSE, ""),
        ("Human Hunter 60", SHAMAN_SPELL, SHAMAN_MELEE, HUNTER_DEFENSE, HUNTER_RANGED),
        ("Human Hunter 1", SHAMAN_SPELL, SHAMAN_MELEE, HUNTER_DEFENSE, HUNTER_RANGED)]


def tidy_base(columns=FULL) -> SheetBase:
    return SheetBase([dict(zip(HEADER, r)) for r in convert_raw(raw(columns))])


class ParsePanelTests(unittest.TestCase):
    def test_melee_panel(self):
        self.assertEqual(parse_panel(SHAMAN_MELEE), [
            ("Attack Power", Decimal(276), False), ("Hit Chance", Decimal("0.00"), True), ("Crit Chance", Decimal("4.32"), True),
            ("Haste", Decimal("0.00"), True), ("Expertise", Decimal("0.00"), True)])

    def test_the_5_in_mana_per_5_sec_is_not_read_as_a_value(self):
        got = {l: (v, p) for l, v, p in parse_panel(SHAMAN_SPELL)}
        self.assertEqual(got["Mana per 5 Sec."], (Decimal(0), False))
        self.assertEqual(got["Spell Crit"], (Decimal("8.09"), True))
        self.assertEqual(got["Crit Chance"], (Decimal("4.32"), True))  # the spell panel's own second crit line

    def test_a_stray_leading_school_is_dropped(self):
        labels = [l for l, _, _ in parse_panel(MAGE_SPELL)]
        self.assertEqual(labels.count("Frost Damage"), 1)
        self.assertEqual(labels[0], "Spell Damage")

    def test_the_stray_school_is_only_dropped_before_spell_damage(self):
        with self.assertRaises(SheetBaseError):
            parse_panel("Frost Damage0Frost Damage0")  # a genuine duplicate is an error, not silently merged

    def test_thousands_separators_and_negatives(self):
        got = parse_panel("Armor1,234Defense-5")
        self.assertEqual([(l, v) for l, v, _ in got], [("Armor", Decimal(1234)), ("Defense", Decimal(-5))])

    def test_unreadable_text_is_an_error(self):
        with self.assertRaises(SheetBaseError):
            parse_panel("Armor104 ??? Defense300")

    def test_defense_panel_with_and_without_parry(self):
        self.assertNotIn("Parry Chance", [l for l, _, _ in parse_panel(SHAMAN_DEFENSE)])
        self.assertIn("Parry Chance", [l for l, _, _ in parse_panel(HUNTER_DEFENSE)])


class ConvertTests(unittest.TestCase):
    def test_headings_are_split_into_race_class_level(self):
        tidy = convert_raw(raw([("Night Elf Druid 60", "", SHAMAN_MELEE, SHAMAN_DEFENSE, ""),
                                ("Skyborne H Druid 1", "", SHAMAN_MELEE, SHAMAN_DEFENSE, "")]))
        self.assertEqual({(r[0], r[1], r[2]) for r in tidy}, {("Druid", "Night Elf", "60"), ("Druid", "Skyborne H", "1")})

    def test_sections_come_from_the_row_they_were_in(self):
        base = tidy_base()
        figs = base.at("Hunter", "Human", 60)
        self.assertEqual(figs[("melee", "Attack Power")].value, 276)
        self.assertEqual(figs[("ranged", "Ranged Attack Power")].value, 352)
        self.assertEqual(figs[("defense", "Parry Chance")].value, Decimal("5.00"))
        self.assertTrue(figs[("defense", "Parry Chance")].percent)
        self.assertFalse(figs[("melee", "Attack Power")].percent)

    def test_a_missing_section_is_absent_not_zero(self):
        figs = tidy_base().at("Shaman", "Orc", 60)
        self.assertNotIn(("defense", "Parry Chance"), figs)
        self.assertNotIn(("ranged", "Ranged Attack Power"), figs)

    def test_a_bad_heading_is_an_error(self):
        with self.assertRaises(SheetBaseError):
            convert_raw(raw([("Orc Necromancer 60", "", SHAMAN_MELEE, SHAMAN_DEFENSE, "")]))

    def test_a_repeated_column_is_an_error(self):
        with self.assertRaises(SheetBaseError):
            convert_raw(raw([("Orc Shaman 60", "", SHAMAN_MELEE, SHAMAN_DEFENSE, "")] * 2))

    def test_too_few_rows(self):
        with self.assertRaises(SheetBaseError):
            convert_raw([["Orc Shaman 60"]])


class LookupTests(unittest.TestCase):
    def test_only_the_recorded_levels_are_answered(self):
        base = tidy_base()
        self.assertEqual(base.levels("Shaman", "Orc"), [1, 60])
        with self.assertRaises(SheetBaseError) as ctx:
            base.at("Shaman", "Orc", 30)
        self.assertIn("not estimated", str(ctx.exception))

    def test_unknown_combination(self):
        with self.assertRaises(KeyError):
            tidy_base().at("Shaman", "Human", 60)

    def test_duplicate_rows_are_refused(self):
        row = dict(zip(HEADER, ["Shaman", "Orc", "60", "melee", "Attack Power", "276", ""]))
        with self.assertRaises(SheetBaseError):
            SheetBase([row, dict(row)])

    def test_problems_flag_missing_levels_and_figures(self):
        self.assertEqual(problems_in(tidy_base()), [])
        short = tidy_base(FULL[:1])
        self.assertTrue(any("levels [60]" in p for p in problems_in(short)))
        no_dodge = tidy_base([("Orc Shaman 60", "", SHAMAN_MELEE, "Armor104Defense300", ""),
                              ("Orc Shaman 1", "", SHAMAN_MELEE, "Armor104Defense300", "")])
        self.assertTrue(any("Dodge Chance" in p for p in problems_in(no_dodge)))


class FileTests(TempCase):
    def test_round_trip_through_the_files_and_cli(self):
        raw_path = self.root / "raw.tsv"
        raw_path.write_text("\n".join("\t".join(r) for r in raw(FULL)) + "\n", encoding="utf-8")
        out = self.root / "sheet_base.tsv"
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sheetbase.main(["convert", str(raw_path), "--out", str(out)]), 0)
        base = load_sheet_base(out)
        self.assertEqual(base.at("Shaman", "Orc", 60)[("melee", "Attack Power")].value, 276)
        with redirect_stdout(buf):
            self.assertEqual(sheetbase.main(["check", "--file", str(out), "--base-stats", str(self.root / "none.tsv")]), 0)
        self.assertIn("no problems found", buf.getvalue())

    def test_a_wrong_header_is_refused(self):
        path = self.root / "x.tsv"
        path.write_text("a\tb\n1\t2\n", encoding="utf-8")
        with self.assertRaises(SheetBaseError):
            load_sheet_base(path)

    def test_a_short_row_is_refused(self):
        path = self.root / "x.tsv"
        path.write_text("\t".join(HEADER) + "\nShaman\tOrc\t60\n", encoding="utf-8")
        with self.assertRaises(SheetBaseError):
            load_sheet_base(path)

    def test_a_missing_file(self):
        with self.assertRaises(SheetBaseError):
            load_sheet_base(self.root / "nope.tsv")

    def test_convert_reports_a_bad_input_and_exits_two(self):
        raw_path = self.root / "raw.tsv"
        raw_path.write_text("Orc Necromancer 60\n" * 9, encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sheetbase.main(["convert", str(raw_path), "--out", str(self.root / "o.tsv")]), 2)


# --------------------------------------------------------------------------
# The real file (skipped when absent)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_FILE = REAL_ROOT / "external" / "sheet_base.tsv"
REAL_STATS = REAL_ROOT / "external" / "base_stats.tsv"


@unittest.skipUnless(REAL_FILE.is_file(), f"no sheet figures file at {REAL_FILE}")
class RealFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = load_sheet_base(REAL_FILE)

    def test_it_has_no_problems(self):
        self.assertEqual(problems_in(self.base), [])
        self.assertEqual(len(self.base.combinations()), 56)

    def test_it_covers_the_same_combinations_as_the_base_stats(self):
        if not REAL_STATS.is_file():
            self.skipTest("no base stats file")
        from wowcalc.basestats import load_base_stats
        self.assertEqual(self.base.combinations(), set(load_base_stats(REAL_STATS).combos()))

    def test_figures_match_the_in_game_readings_taken_before_talents(self):
        druid = self.base.at("Druid", "Skyborne H", 60)
        self.assertEqual(druid[("melee", "Crit Chance")].value, Decimal("4.21"))   # Nature's Majesty reading
        self.assertEqual(druid[("defense", "Dodge Chance")].value, Decimal("4.15"))  # Feral Swiftness reading
        self.assertEqual(druid[("melee", "Hit Chance")].value, 0)                   # Nature's Reach reading
        self.assertEqual(self.base.at("Hunter", "Human", 60)[("defense", "Parry Chance")].value, 5)  # Deflection reading
        self.assertEqual(self.base.at("Shaman", "Orc", 60)[("melee", "Attack Power")].value, 276)    # Mental Dexterity reading

    def test_defense_is_five_per_level(self):
        for (cls, race, level), figs in self.base._figures.items():
            self.assertEqual(figs[("defense", "Defense")].value, 5 * level, (cls, race, level))

    def test_only_hunters_have_ranged_attack_power(self):
        for (cls, race, level), figs in self.base._figures.items():
            self.assertEqual(("ranged", "Ranged Attack Power") in figs, cls == "Hunter", (cls, race, level))

    def test_only_casters_have_a_spell_panel(self):
        for (cls, race, level), figs in self.base._figures.items():
            self.assertEqual(("spell", "Spell Crit") in figs, cls not in ("Warrior", "Rogue"), (cls, race, level))


if __name__ == "__main__":
    unittest.main()
