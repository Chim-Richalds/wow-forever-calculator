"""Tests for wowcalc.backgrounds (talent tree background images). Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fixtures import TempCase  # noqa: E402
from wowcalc.backgrounds import BackgroundCache, BackgroundError, file_name, import_folder  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SPECS = [("Druid", "Feral Combat"), ("Druid", "Restoration"), ("Shaman", "Restoration"), ("Mage", "Frost")]


class BackgroundTests(TempCase):
    def setUp(self):
        super().setUp()
        self.cache = BackgroundCache(self.root / "cache")
        self.src = self.root / "downloads"
        self.src.mkdir()

    def test_file_names(self):
        self.assertEqual(file_name("Druid", "Feral Combat"), "druid-feral-combat.png")

    def test_the_three_name_styles_are_matched(self):
        for name in ("Talents background - druid Feral Combat.png", "Talents_background_-_mage_Frost.png", "shaman restoration.png"):
            (self.src / name).write_bytes(PNG)
        added, skipped = import_folder(self.cache, self.src, SPECS)
        self.assertEqual(sorted(added), ["druid-feral-combat.png", "mage-frost.png", "shaman-restoration.png"])
        self.assertEqual(skipped, [])
        self.assertTrue(self.cache.has("Druid", "Feral Combat"))
        self.assertIn("imported from", self.cache.manifest()["mage-frost.png"]["source"])

    def test_unclear_names_and_non_images_are_skipped(self):
        (self.src / "restoration.png").write_bytes(PNG)               # Druid or Shaman?
        (self.src / "mage frost.png").write_bytes(b"<html>not an image</html>")
        (self.src / "notes.txt").write_text("x")
        added, skipped = import_folder(self.cache, self.src, SPECS)
        self.assertEqual(added, [])
        self.assertEqual(len(skipped), 2)
        self.assertTrue(any("not a PNG" in s for s in skipped))

    def test_only_plain_cached_names_are_read(self):
        self.cache.add("Mage", "Frost", PNG, "test")
        self.assertEqual(self.cache.read("mage-frost.png"), PNG)
        for bad in ("../manifest.json", "mage-frost.png/../x", "manifest.json", "MAGE.png"):
            self.assertIsNone(self.cache.read(bad), bad)

    def test_a_missing_folder_is_an_error(self):
        with self.assertRaises(BackgroundError):
            import_folder(self.cache, self.root / "nowhere", SPECS)


if __name__ == "__main__":
    unittest.main()
