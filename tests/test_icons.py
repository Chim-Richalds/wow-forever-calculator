"""Offline tests for wowcalc.icons. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from wowcalc import icons  # noqa: E402
from wowcalc.icons import IconCache, IconError, fetch_icons, icon_ids, import_folder, sniff  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"data"
JPG = b"\xff\xd8\xff\xe0" + b"data"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


class Sniff(unittest.TestCase):
    def test_real_images_are_recognised(self):
        self.assertEqual((sniff(PNG), sniff(JPG), sniff(WEBP), sniff(b"GIF89a")), ("png", "jpg", "webp", "gif"))

    def test_everything_else_is_refused(self):
        for data in (b"", b"<html>login</html>", b"<svg onload=x>", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"\x89PNG", b"MZ" + b"x" * 10):
            self.assertIsNone(sniff(data), data)

    def test_oversize_is_refused(self):
        self.assertIsNone(sniff(PNG + b"x" * icons.MAX_BYTES))


class CacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cache = IconCache(self.root / "icons")


class CacheTests(CacheCase):
    def test_add_read_and_manifest(self):
        self.cache.add("133", PNG, "https://x.test/133.png")
        self.assertEqual(self.cache.read("133"), (PNG, "image/png"))
        entry = json.loads((self.root / "icons" / "manifest.json").read_text())["icons"]["133"]
        self.assertEqual((entry["file"], entry["source"], entry["bytes"]), ("133.png", "https://x.test/133.png", len(PNG)))
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(self.cache.ids(), {"133"})
        self.assertEqual(self.cache.verify(), [])

    def test_a_non_image_is_never_kept(self):
        with self.assertRaisesRegex(IconError, "not a png"):
            self.cache.add("133", b"<html>", "x")
        self.assertFalse(self.cache.has("133"))
        self.assertEqual(list((self.root / "icons").glob("*.part")) if (self.root / "icons").exists() else [], [])

    def test_ids_must_be_plain_numbers(self):
        for bad in ("", "0", "01", "../x", "1/2", "1.png", "-5", "12345678901"):
            with self.assertRaises(IconError, msg=bad):
                self.cache.add(bad, PNG, "x")
            self.assertIsNone(self.cache.find(bad), bad)

    def test_replacing_with_another_type_leaves_one_file(self):
        self.cache.add("5", PNG, "a")
        self.cache.add("5", JPG, "b")
        self.assertEqual([p.name for p in (self.root / "icons").glob("5.*")], ["5.jpg"])
        self.assertEqual(self.cache.read("5")[1], "image/jpeg")

    def test_a_file_that_was_swapped_for_junk_is_not_served(self):
        self.cache.add("7", PNG, "a")
        (self.root / "icons" / "7.png").write_bytes(b"<script>alert(1)</script>")
        self.assertIsNone(self.cache.read("7"))
        self.assertEqual(self.cache.verify(), ["7: changed since it was recorded"])

    def test_verify_reports_missing_files(self):
        self.cache.add("7", PNG, "a")
        (self.root / "icons" / "7.png").unlink()
        self.assertEqual(self.cache.verify(), ["7: file missing"])

    def test_empty_cache(self):
        self.assertEqual((self.cache.ids(), self.cache.read("1"), self.cache.verify()), (set(), None, []))


class ImportTests(CacheCase):
    def test_import_takes_numbered_images_and_reports_the_rest(self):
        src = self.root / "src"
        src.mkdir()
        (src / "10.png").write_bytes(PNG)
        (src / "11.jpg").write_bytes(JPG)
        (src / "spell_fire.png").write_bytes(PNG)
        (src / "12.png").write_bytes(b"nope")
        (src / "sub").mkdir()
        added, skipped = import_folder(self.cache, src)
        self.assertEqual(added, ["10", "11"])
        self.assertEqual(len(skipped), 2)   # the unmatched name and the non-image; the folder is walked
        self.assertTrue(any("spell_fire.png" in s for s in skipped) and any("12" in s for s in skipped))

    def test_icon_names_are_matched_through_a_listfile(self):
        listfile = self.root / "list.csv"
        listfile.write_text("133;interface/icons/inv_sword_01.blp\n134;Interface\\Icons\\Spell_Fire.blp\n"
                            "135;interface/icons/dup.blp\n136;interface/icons/dup.blp\n137;sound/x.ogg\nbad;line\n", encoding="utf-8")
        names = icons.load_listfile(listfile)
        self.assertEqual(names, {"inv_sword_01": "133", "spell_fire": "134"})   # dup dropped, non-icon ignored
        src = self.root / "src" / "interface" / "icons"
        src.mkdir(parents=True)
        for n in ("INV_Sword_01.png", "spell_fire.png", "dup.png", "unknown.png"):
            (src / n).write_bytes(PNG)
        (src / "notes.txt").write_text("x")
        added, skipped = import_folder(self.cache, self.root / "src", names)
        self.assertEqual(sorted(added), ["133", "134"])
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all("listfile" in s for s in skipped))
        _, without = import_folder(IconCache(self.root / "other"), self.root / "src")
        self.assertTrue(all("--listfile" in s for s in without))

    def test_only_needed_icons_are_kept(self):
        src = self.root / "src"
        src.mkdir()
        for n in ("10.png", "11.png"):
            (src / n).write_bytes(PNG)
        added, _ = import_folder(self.cache, src, None, {"11"})
        self.assertEqual((added, self.cache.ids()), (["11"], {"11"}))

    def test_paths_by_id(self):
        listfile = self.root / "l.csv"
        listfile.write_text("133;Interface\\Icons\\Inv_Sword_01.blp\n9;sound/a.ogg\n", encoding="utf-8")
        self.assertEqual(icons.load_paths(listfile), {"133": "interface/icons/inv_sword_01.blp"})

    def test_import_needs_a_folder(self):
        with self.assertRaisesRegex(IconError, "not a folder"):
            import_folder(self.cache, self.root / "nowhere")


class FetchTests(CacheCase):
    def test_fetch_substitutes_the_id_and_records_the_source(self):
        seen = []

        def get(url, timeout=0):
            seen.append(url)
            return icons.transport.Response(PNG if url.endswith("/1.png") else b"<html>", "image/png")

        added, failed = fetch_icons(self.cache, {"1", "2"}, "https://img.test/{id}.png", get)
        self.assertEqual((added, seen), (["1"], ["https://img.test/1.png", "https://img.test/2.png"]))
        self.assertEqual(len(failed), 1)
        self.assertIn("2:", failed[0])
        self.assertEqual(json.loads((self.root / "icons" / "manifest.json").read_text())["icons"]["1"]["source"], "https://img.test/1.png")

    def test_network_errors_are_reported_not_raised(self):
        def get(url, timeout=0):
            raise OSError("down")

        self.assertEqual(fetch_icons(self.cache, {"3"}, "https://img.test/{id}", get), ([], ["3: down"]))

    def test_the_template_must_be_https_and_have_an_id(self):
        for bad in ("https://img.test/icon.png", "http://img.test/{id}", "ftp://x/{id}", ""):
            with self.assertRaises(IconError, msg=bad):
                fetch_icons(self.cache, {"1"}, bad, lambda *a, **k: None)


class FakeDs:
    def __init__(self, tables):
        self.tables = tables

    def has_table(self, name):
        return name in self.tables

    def rows(self, name):
        return self.tables[name]


class Entry:
    def __init__(self, definition_id, spell_id):
        self.definition_id, self.spell_id = definition_id, spell_id


class Node:
    def __init__(self, id, entry):
        self.id, self.entries = id, (entry,) if entry else ()


class Tree:
    def __init__(self, nodes):
        self.nodes = {n.id: n for n in nodes}


class IconIdTests(unittest.TestCase):
    def tree(self):
        return Tree([Node("1", Entry("20", "30")), Node("2", Entry("21", "31")), Node("3", Entry("22", "32")),
                     Node("4", None), Node("5", Entry("23", "33"))])

    def test_override_beats_the_spell_icon_and_unknowns_are_absent(self):
        ds = FakeDs({
            "TraitDefinition": [{"ID": "20", "OverrideIcon": "0"}, {"ID": "21", "OverrideIcon": "999"},
                                {"ID": "22", "OverrideIcon": ""}, {"ID": "23"}],
            "SpellMisc": [{"SpellID": "30", "SpellIconFileDataID": "111"}, {"SpellID": "31", "SpellIconFileDataID": "222"},
                          {"SpellID": "32", "SpellIconFileDataID": "0"}],
        })
        self.assertEqual(icon_ids(ds, self.tree()), {
            "1": ("111", "SpellMisc.SpellIconFileDataID"), "2": ("999", "TraitDefinition.OverrideIcon")})

    def test_first_spell_misc_row_wins_and_a_missing_table_is_fine(self):
        ds = FakeDs({"TraitDefinition": [], "SpellMisc": [{"SpellID": "30", "SpellIconFileDataID": "5"}, {"SpellID": "30", "SpellIconFileDataID": "6"}]})
        self.assertEqual(icon_ids(ds, self.tree())["1"][0], "5")
        self.assertEqual(icon_ids(FakeDs({"TraitDefinition": []}), self.tree()), {})


if __name__ == "__main__":
    unittest.main()
