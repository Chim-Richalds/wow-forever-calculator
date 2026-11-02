"""Tests for wowcalc.combat (per-cast damage and healing). Run: python -m unittest discover -s tests"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wowcalc.combat import BOSS, SAME_LEVEL, CombatBook, _amount, spell_numbers  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.effects import EffectBook  # noqa: E402
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.spells import Catalogue, SpellBook, resolve  # noqa: E402
from wowcalc.trees import load_model  # noqa: E402


class AmountTests(unittest.TestCase):
    def test_per_level_growth_is_capped_at_the_maximum_level(self):
        levels = {"1": {"BaseLevel": "56", "MaxLevel": "60"}}
        row = {"SpellID": "1", "EffectBasePointsF": "100", "EffectRealPointsPerLevel": "2"}
        self.assertEqual(_amount(row, 58, levels), 104)
        self.assertEqual(_amount(row, 70, levels), 108)       # capped at 60
        self.assertEqual(_amount({**row, "EffectRealPointsPerLevel": ""}, 70, levels), 100)


VERSION = "1.60.1.70707-hf202610311059"
REAL = Path(os.environ.get("WOWCALC_DATA", REPO / "data")) / "raw" / VERSION
COMPLETE = REAL.is_dir() and all((REAL / f"{t}.csv").is_file() for t in TABLES)


@unittest.skipUnless(COMPLETE, f"a complete {VERSION} is not at {REAL}")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = Dataset(REAL, VERSION)
        model = load_model(ds)
        book = EffectBook.for_model(ds, model)
        cls.catalogue = Catalogue.build(ds, model, book)
        cls.mods = resolve(ds, model, book, cls.catalogue)
        cls.combat = CombatBook(ds, SpellBook(ds))

    def numbers(self, cls_name, target=SAME_LEVEL, spell_power=0, crit=0.0, hit=0.0):
        rows, _ = spell_numbers(self.combat, self.catalogue.by_class[cls_name], self.mods, cls_name, 60, {},
                                spell_power, spell_power, crit, hit, target, {})
        return {n.spell: n for n in rows}

    def test_frostbolt_range_and_spell_power(self):
        plain, powered = self.numbers("Mage")["Frostbolt"], self.numbers("Mage", spell_power=100)["Frostbolt"]
        self.assertLess(plain.direct_min, plain.direct_max)
        self.assertAlmostEqual(powered.direct_min - plain.direct_min, 81.4, places=1)   # coefficient 0.814

    def test_boss_misses_more_and_heals_always_land(self):
        same, boss = self.numbers("Priest")["Mind Blast"], self.numbers("Priest", BOSS)["Mind Blast"]
        self.assertEqual((same.hit_chance, boss.hit_chance), (96.0, 83.0))
        self.assertEqual(self.numbers("Priest", BOSS)["Greater Heal"].hit_chance, 100.0)

    def test_over_time_spells_count_their_ticks(self):
        corruption = self.numbers("Warlock")["Corruption"]
        self.assertEqual((corruption.ticks, corruption.direct_max), (6, 0))
        self.assertGreater(corruption.periodic_total, 0)

    def test_channels_take_their_duration_and_hellfire_does_not_count_the_burn_on_you(self):
        missiles = self.numbers("Mage")["Arcane Missiles"]
        self.assertAlmostEqual(missiles.per_second, missiles.expected / 5, places=3)
        hellfire = self.numbers("Warlock")["Hellfire"]
        self.assertAlmostEqual(hellfire.per_second, hellfire.expected / 15, places=3)

    def test_crit_raises_the_expected_amount(self):
        a, b = self.numbers("Mage")["Fireball"], self.numbers("Mage", crit=20.0)["Fireball"]
        self.assertGreater(b.expected, a.expected)


if __name__ == "__main__":
    unittest.main()
