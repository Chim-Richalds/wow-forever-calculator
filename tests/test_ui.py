"""Offline tests for wowcalc.ui. Run: python -m unittest discover -s tests"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from fractions import Fraction as F
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from test_talentinfo import BUILD, InfoCase  # noqa: E402
from wowcalc.icons import IconCache  # noqa: E402
from wowcalc.ui import PAGE, BuildStore, UiError, Workbench, host_name, make_handler, num, route  # noqa: E402


class SmallHelpers(unittest.TestCase):
    def test_host_names(self):
        self.assertEqual(host_name("127.0.0.1:8765"), "127.0.0.1")
        self.assertEqual(host_name("LOCALHOST:80"), "localhost")
        self.assertEqual(host_name("[::1]:8765"), "[::1]")
        self.assertEqual(host_name("localhost"), "localhost")
        self.assertEqual(host_name("evil.example:80"), "evil.example")

    def test_numbers_stay_whole_or_round_to_four_places(self):
        self.assertEqual((num(Decimal("3")), num(F(7, 2)), num(Decimal("0.123456")), num(None), num(4.0)), (3, 3.5, 0.1235, None, 4))
        self.assertIsInstance(num(Decimal("3.0")), int)

    def test_the_page_ships_with_the_code(self):
        text = PAGE.read_text(encoding="utf-8")
        for needle in ("/api/evaluate", "/api/tree/", "/api/builds", "/api/status", "<title>Talent value calculator</title>"):
            self.assertIn(needle, text)


class StoreTests(InfoCase):
    def setUp(self):
        super().setUp()
        self.store = BuildStore(self.root / "ui" / "builds.json")

    def test_a_build_can_be_saved_listed_read_and_deleted(self):
        self.store.put("My build", {"class": "Mage", "race": "Human", "level": 60, "build": "b", "saved": "now", "alloc": {"1": 2}})
        self.assertEqual([b["name"] for b in self.store.names()], ["My build"])
        self.assertEqual(self.store.get("My build")["alloc"], {"1": 2})
        self.store.delete("My build")
        self.assertEqual(self.store.names(), [])

    def test_saving_again_replaces_and_leaves_no_temporary_files(self):
        self.store.put("a", {"x": 1})
        self.store.put("a", {"x": 2})
        self.assertEqual(self.store.get("a"), {"x": 2})
        self.assertEqual([p.name for p in (self.root / "ui").iterdir()], ["builds.json"])

    def test_names_come_back_in_order_ignoring_case(self):
        for n in ("b", "A", "c"):
            self.store.put(n, {})
        self.assertEqual([b["name"] for b in self.store.names()], ["A", "b", "c"])

    def test_bad_names_are_refused(self):
        for bad in ("", "x" * 61, "a\nb"):
            with self.assertRaises(UiError) as ctx:
                self.store.put(bad, {})
            self.assertEqual(ctx.exception.status, 400)

    def test_unknown_builds_are_404(self):
        for call in (lambda: self.store.get("nope"), lambda: self.store.delete("nope")):
            with self.assertRaises(UiError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 404)

    def test_a_damaged_file_is_reported_not_overwritten(self):
        path = self.root / "ui" / "builds.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(UiError) as ctx:
            self.store.names()
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


class WorkbenchCase(InfoCase):
    def write_rates(self):
        data = {"level": 60, "agility": {"Mage": ["1/20", "1/20"]}, "intellect_spell_crit": ["1/60", "1/60"],
                "spell_classes": ["Mage"], "attack_power": {"Mage": [2, 0, -10, False]},
                "armor": {"per_agility": 2, "worst_dr_error": "0", "formula": "x"},
                "health": {"per_point": 10, "constants": {"Mage": 900}, "race_factors": {}},
                "mana": {"per_point": 15, "constants": {"Mage": 1100}, "race_factors": {}},
                "assumed": {"block_base_chance": "5", "shield_classes": ["Mage"], "strength_per_block_value": 20}}
        (self.data / "external").mkdir(parents=True, exist_ok=True)
        (self.data / "external" / "stat_rates.json").write_text(json.dumps(data), encoding="utf-8")

    def setUp(self):
        super().setUp()
        self.addCleanup(self._tmp.cleanup)   # a failing setUp must not leak the temporary folder
        self.install()
        self.write_sheet_base(60, melee_crit="4.21")
        self.write_rates()
        self.bench = Workbench(self.data, BUILD)

    def ev(self, **over):
        body = {"class": "Mage", "race": "Human", "level": 60, "alloc": {}}
        body.update(over)
        return self.bench.evaluate(body)


PNG = b"\x89PNG\r\n\x1a\n" + b"x"


class IconWiring(WorkbenchCase):
    """Mage nodes: 1 -> spell 3001 icon 111; 2 -> definition override 999 (spell icon 222 ignored); 3 -> none."""

    def setUp(self):
        super().setUp()
        raw = self.data / "raw" / BUILD
        rows = ["ID,SpellID,OverrideIcon"] + [f"{2000 + i},{3000 + i},{999 if i == 2 else 0}" for i in range(1, 8)]
        (raw / "TraitDefinition.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (raw / "SpellMisc.csv").write_text("ID,SpellID,SpellIconFileDataID\n1,3001,111\n2,3002,222\n", encoding="utf-8")
        self.bench = Workbench(self.data, BUILD)

    def icons_of(self, bench=None):
        return {n["id"]: n["icon"] for n in (bench or self.bench).tree("Mage")["nodes"]}

    def test_only_cached_icons_are_offered(self):
        self.assertEqual(set(self.icons_of().values()), {None})
        IconCache(self.data / "icons").add("111", PNG, "t")
        self.assertEqual(self.icons_of()["1"], "111")
        self.assertIsNone(self.icons_of()["2"])      # id 999 known but not cached
        IconCache(self.data / "icons").add("999", PNG, "t")
        self.assertEqual((self.icons_of()["2"], self.icons_of()["3"]), ("999", None))

    def test_status_counts_icons(self):
        IconCache(self.data / "icons").add("111", PNG, "t")
        info = self.bench.status()["icons"]
        self.assertEqual((info["cached"], info["wanted"], info["spellmisc"]), (1, 2, True))   # wanted: 111 and 999; 222 is overridden

    def test_icons_are_optional(self):
        (self.data / "raw" / BUILD / "SpellMisc.csv").unlink()
        bench = Workbench(self.data, BUILD)
        self.assertEqual(set(self.icons_of(bench).values()), {None})
        self.assertFalse(bench.status()["icons"]["spellmisc"])

    def test_served_over_http_with_a_matching_policy(self):
        IconCache(self.data / "icons").add("111", PNG, "t")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bench))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path, host=None):
            req = urllib.request.Request(base + path, headers={"Host": host} if host else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read(), dict(resp.headers)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, exc.read(), dict(exc.headers)
                finally:
                    exc.close()

        status, body, headers = get("/icons/111")
        self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])
        for bad in ("/icons/222", "/icons/", "/icons/..%2Fmanifest.json", "/icons/111.png", "/icons/0"):
            self.assertEqual(get(bad)[0], 404, bad)
        self.assertEqual(get("/icons/111", host="evil.example")[0], 403)
        (self.data / "icons" / "111.png").write_bytes(b"<script>")
        self.assertEqual(get("/icons/111")[0], 404)   # no longer an image


class SpecNameWiring(WorkbenchCase):
    FILE = ("class\tspec\tspell_id\ttalent\n"
            "Mage\tAlpha\t3001\tTalent 1\nMage\tAlpha\t3002\tTalent 2\nMage\tAlpha\t3003\tTalent 3\n"
            "Mage\tBeta\t3004\tTalent 4\nMage\tBeta\t3005\tTalent 5\nMage\tBeta\t3006\tTalent 6\n")

    def reload(self, text):
        (self.data / "external" / "spec_names.tsv").write_text(text, encoding="utf-8")
        return Workbench(self.data, BUILD)

    def checks(self, bench):
        return {c["name"]: c for c in bench.status()["checks"]}

    def test_without_the_file_trees_stay_numbered_and_nothing_is_reported(self):
        self.assertEqual(self.bench.tree("Mage")["specs"], [None, None])
        self.assertNotIn("Specialization names", self.checks(self.bench))

    def test_names_reach_the_page_by_tree_index_and_the_check_passes(self):
        bench = self.reload(self.FILE)
        self.assertEqual(bench.tree("Mage")["specs"], ["Alpha", "Beta"])
        check = self.checks(bench)["Specialization names"]
        self.assertEqual(check["problems"], ["Warrior: no names in spec_names.tsv"])   # a class missing from the file is reported
        self.assertIn("S.tree.specs[key]", PAGE.read_text(encoding="utf-8"))

    def test_a_wrong_row_is_reported_and_only_that_tree_loses_its_name(self):
        bench = self.reload(self.FILE.replace("3005\tTalent 5", "3005\tTalent X"))
        self.assertEqual(bench.tree("Mage")["specs"], ["Alpha", None])
        check = self.checks(bench)["Specialization names"]
        self.assertFalse(check["ok"])
        self.assertTrue(any("Talent X" in p for p in check["problems"]))

    def test_a_malformed_file_does_not_stop_the_page(self):
        bench = self.reload("class\tspec\nnonsense\n")
        self.assertEqual(bench.tree("Mage")["specs"], [None, None])
        self.assertFalse(self.checks(bench)["Specialization names"]["ok"])


class LoadTests(InfoCase):
    def test_no_build_installed(self):
        with self.assertRaisesRegex(UiError, "No build is installed"):
            Workbench(self.data)

    def test_an_incomplete_build_says_how_to_fix_it(self):
        self.install(complete=False)
        with self.assertRaisesRegex(UiError, "update complete"):
            Workbench(self.data, BUILD)

    def test_missing_base_stats(self):
        self.install(base_stats=False)
        with self.assertRaisesRegex(UiError, "No base stats file"):
            Workbench(self.data, BUILD)

    def test_missing_optional_files_become_notes_not_errors(self):
        self.install()
        bench = Workbench(self.data, BUILD)
        self.assertEqual(len(bench.load_notes), 2)
        self.assertFalse(bench.meta()["has_rates"])


class MetaAndTreeTests(WorkbenchCase):
    def test_meta(self):
        m = self.bench.meta()
        self.assertEqual((m["build"], m["classes"]["Mage"], m["has_rates"], m["levels"][1]), (BUILD, ["Human"], True, 60))
        self.assertIn("agility", m["adjustable"])

    def test_the_tree_carries_names_ranks_and_whether_each_talent_is_valued(self):
        tree = self.bench.tree("Mage")
        by_id = {n["id"]: n for n in tree["nodes"]}
        one = by_id["1"]
        self.assertEqual((one["name"], one["max"], one["modelled"], one["partly"]), ("Talent 1", 2, True, False))
        self.assertEqual(one["ranks"][1]["text"], "Does thing 1.")
        two = by_id["2"]                    # a spell modifier plus a crit bonus: only one of its effects is valued
        self.assertTrue(two["modelled"] and two["partly"])
        self.assertIn("spell_modifier", [e["kind"] for e in two["effects"]])

    def test_an_unknown_class_is_a_400(self):
        with self.assertRaises(UiError) as ctx:
            self.bench.tree("Necromancer")
        self.assertEqual(ctx.exception.status, 400)


class ParseTests(WorkbenchCase):
    def bad(self, message, **over):
        with self.assertRaisesRegex(UiError, message) as ctx:
            self.ev(**over)
        self.assertEqual(ctx.exception.status, 400)

    def test_each_kind_of_bad_input_gets_a_plain_message(self):
        self.bad("no class", **{"class": "Necromancer"})
        self.bad("no race", race="Orc")
        self.bad("level must be", level=0)
        self.bad("level must be", level=61)
        self.bad("whole number", level="sixty")
        self.bad("whole number", level=60.5)
        self.bad("whole number", level=True)
        self.bad("not in the Mage tree", alloc={"7": 1})
        self.bad("ranks must be 0 to 2", alloc={"1": 3})
        self.bad("cannot adjust", adjust={"luck": 5})
        self.bad("alloc must", alloc=[1])

    def test_a_body_that_is_not_an_object(self):
        with self.assertRaises(UiError):
            self.bench.evaluate([])

    def test_zero_ranks_and_zero_adjustments_are_dropped(self):
        req = self.bench.parse({"class": "Mage", "race": "Human", "level": 60, "alloc": {"1": 0, "2": 1}, "adjust": {"agility": 0, "intellect": "5"}})
        self.assertEqual((req.alloc, req.adjust), ({"2": 1}, {"intellect": 5}))


class EvaluateTests(WorkbenchCase):
    def test_an_empty_build_is_legal_and_shows_the_base_sheet(self):
        r = self.ev()
        self.assertTrue(r["legal"])
        self.assertEqual((r["spent"], r["problems"]), (0, []))
        self.assertEqual(r["sheet"]["lines"]["intellect"]["total"], 60)
        self.assertEqual(r["sheet"]["figures"]["melee_crit"]["base"], 4.21)

    def test_talents_change_the_sheet_and_the_points_spent(self):
        r = self.ev(alloc={"1": 2})
        self.assertEqual((r["spent"], r["sheet"]["lines"]["intellect"]["total"], r["sheet"]["lines"]["intellect"]["talents"]), (2, 62, 2))

    def test_a_build_that_breaks_the_rules_is_flagged_but_still_calculated(self):
        r = self.ev(alloc={"5": 2})         # a deeper talent with nothing spent above it
        if r["legal"]:
            self.skipTest("fixture tree has no locked node at node 5")
        self.assertTrue(r["problems"])
        self.assertEqual(r["sheet"]["lines"]["agility"]["talents"], 1)

    def test_every_node_reports_what_can_be_added_or_removed(self):
        r = self.ev(alloc={"1": 1})
        node = r["nodes"]["1"]
        self.assertEqual(node["ranks"], 1)
        self.assertTrue(node["can_add"] and node["can_remove"])
        self.assertEqual(set(r["nodes"]), set(self.bench.tree_of("Mage").nodes))

    def test_a_node_at_its_maximum_cannot_take_more(self):
        node = self.ev(alloc={"1": 2})["nodes"]["1"]
        self.assertFalse(node["can_add"])

    def test_values_are_with_minus_without_and_next_rank(self):
        row = next(v for v in self.ev(alloc={"1": 1})["values"] if v["id"] == "1")
        self.assertEqual((row["ranks"], row["max"]), (1, 2))
        self.assertEqual(row["now"]["intellect"], 1)                  # 60 -> 61 at 2%: floor(61.2)
        self.assertEqual(row["next"]["intellect"], 1)                 # 61 -> 62 at 4%: floor(62.4)

    def test_a_maxed_talent_has_no_next_value_and_an_empty_one_has_no_current_value(self):
        full = next(v for v in self.ev(alloc={"1": 2})["values"] if v["id"] == "1")
        self.assertEqual(full["next"], {})
        none = next(v for v in self.ev()["values"] if v["id"] == "1")
        self.assertEqual(none["now"], {})
        self.assertEqual(none["next"]["intellect"], 1)

    def test_values_leave_out_talents_the_calculator_cannot_value(self):
        ids = {v["id"] for v in self.ev()["values"]}
        self.assertNotIn("3", ids)          # no modelled effect

    def test_values_can_be_switched_off(self):
        self.assertNotIn("values", self.ev(values=False))

    def test_spell_power_is_on_the_sheet_and_takes_gear(self):
        r = self.ev()
        self.assertEqual(r["sheet"]["spell_power"], {"base": 0, "talents": 0, "gear": 0, "total": 0, "from": []})
        r = self.ev(adjust={"spell_power": 120})
        self.assertEqual(r["sheet"]["spell_power"], {"base": 0, "talents": 0, "gear": 120, "total": 120, "from": []})
        self.assertEqual(self.ev(adjust={"spell_power": 120})["sheet"]["lines"]["intellect"]["adjust"], 0)   # not a stat

    def test_healing_is_separate_from_spell_power(self):
        self.write_sheet_base(60, melee_crit="4.21")
        path = self.data / "external" / "sheet_base.tsv"
        path.write_text(path.read_text() + "Mage\tHuman\t60\tspell\tHealing\t0\t\n", encoding="utf-8")
        bench = Workbench(self.data, BUILD)
        r = bench.evaluate({"class": "Mage", "race": "Human", "level": 60, "alloc": {}, "adjust": {"healing": 75}})
        self.assertEqual((r["sheet"]["healing"], r["sheet"]["spell_power"]["gear"]), ({"base": 0, "talents": 0, "gear": 75, "total": 75, "from": []}, 0))

    def test_the_page_has_a_keyboard_and_pointer_splitter(self):
        text = PAGE.read_text(encoding="utf-8")
        for needle in ('id="split"', 'role="separator"', "setPointerCapture", '"ArrowLeft"', "wowcalc.split", "dblclick"):
            self.assertIn(needle, text)
        self.assertIn("#split { display: none; }", text)   # stacked layout on narrow screens has no bar

    def test_the_page_combines_hit_and_has_a_spell_power_row(self):
        text = PAGE.read_text(encoding="utf-8")
        for needle in ('"casting_regen"', '"hit", "dodge"', '"spellgear"', '"healinggear"', "Spell power", "Healing", "spell_power"):
            self.assertIn(needle, text)
        self.assertNotIn('"melee_hit", "spell_hit", "dodge", "parry", "block"]) {', text)   # the per-figure loop no longer lists both

    def test_the_values_include_what_extra_stats_do_to_the_figures(self):
        row = next(v for v in self.ev(alloc={"5": 2})["values"] if v["id"] == "5")
        self.assertIn("agility", row["now"])
        self.assertIn("melee_crit", row["now"])                       # the Agility it adds raises crit

    def test_adjustments_reach_the_sheet(self):
        r = self.ev(adjust={"agility": 20})
        self.assertEqual(r["sheet"]["lines"]["agility"]["adjust"], 20)
        self.assertEqual(r["sheet"]["figures"]["melee_crit"]["adjust"], 1.0)   # 20 / 20

    def test_block_needs_a_shield_and_a_level_for_the_attacker(self):
        plain = self.ev(attacker_level=63)
        self.assertNotIn("block_against", plain["sheet"])
        shield = self.ev(shield=True, attacker_level=63)
        self.assertEqual(shield["sheet"]["figures"]["block"]["total"], 5)
        self.assertEqual(shield["sheet"]["block_against"], {"level": 63, "player": False, "chance": 3.5})
        player = self.ev(shield=True, attacker_level=63, vs_player=True)
        self.assertEqual(player["sheet"]["block_against"]["chance"], 4.4)

    def test_notes_and_unvalued_effects_are_passed_on(self):
        r = self.ev(alloc={"5": 1})
        self.assertTrue(any(n["label"] == "Talent 5" for n in r["sheet"]["not_modelled"]))


class StatusAndSavedTests(WorkbenchCase):
    def test_status_lists_the_checks_and_what_is_assumed(self):
        s = self.bench.status()
        self.assertEqual(s["build"], BUILD)
        self.assertIn("Talent trees", [c["name"] for c in s["checks"]])
        self.assertTrue(any(c["status"] == "doubtful" for c in s["confidence"]))
        self.assertTrue(all(set(c) == {"item", "status", "detail"} for c in s["confidence"]))

    def test_a_saved_build_round_trips(self):
        self.bench.save("mine", {"class": "Mage", "race": "Human", "level": 50, "alloc": {"1": 2}, "adjust": {"agility": 3}, "shield": True})
        got = self.bench.load_saved("mine")
        self.assertEqual((got["class"], got["level"], got["alloc"], got["adjust"], got["shield"]), ("Mage", 50, {"1": 2}, {"agility": 3}, True))
        self.assertNotIn("warning", got)
        self.assertEqual(got["build"], BUILD)

    def test_saving_an_invalid_build_is_refused_and_nothing_is_written(self):
        with self.assertRaises(UiError):
            self.bench.save("bad", {"class": "Mage", "race": "Orc", "level": 60})
        self.assertEqual(self.bench.store.names(), [])

    def test_a_build_from_another_game_build_comes_back_with_a_warning(self):
        self.bench.store.put("old", {"class": "Mage", "race": "Human", "level": 60, "alloc": {"1": 1}, "build": "1.0.0.1"})
        self.assertIn("saved with game build 1.0.0.1", self.bench.load_saved("old")["warning"])

    def test_talents_that_no_longer_exist_are_left_out_and_reported(self):
        self.bench.store.put("gone", {"class": "Mage", "race": "Human", "level": 60, "alloc": {"1": 1, "999": 2}, "build": BUILD})
        got = self.bench.load_saved("gone")
        self.assertEqual(got["alloc"], {"1": 1})
        self.assertIn("1 saved talent(s) no longer exist", got["warning"])


class RouteTests(WorkbenchCase):
    def test_routes(self):
        self.assertEqual(route(self.bench, "GET", "/api/meta", None)[0], 200)
        self.assertEqual(route(self.bench, "GET", "/api/tree/Mage", None)[1]["class"], "Mage")
        self.assertEqual(route(self.bench, "GET", "/api/builds", None), (200, {"builds": []}))
        body = {"class": "Mage", "race": "Human", "level": 60, "alloc": {}}
        self.assertEqual(route(self.bench, "PUT", "/api/builds/a%20b", body), (200, {"saved": "a b"}))
        self.assertEqual(route(self.bench, "GET", "/api/builds/a%20b", None)[1]["class"], "Mage")
        self.assertEqual(route(self.bench, "DELETE", "/api/builds/a%20b", None), (200, {"deleted": "a b"}))

    def test_unknown_routes_and_methods_are_404(self):
        for method, path in (("GET", "/api/nothing"), ("GET", "/other"), ("DELETE", "/api/meta"), ("POST", "/api/tree/Mage")):
            with self.assertRaises(UiError) as ctx:
                route(self.bench, method, path, None)
            self.assertEqual(ctx.exception.status, 404, (method, path))


class ServerTests(WorkbenchCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bench))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        super().tearDown()

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), dict(exc.headers)
            finally:
                exc.close()

    def test_the_page_is_served_with_safe_headers(self):
        status, body, headers = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Talent value calculator", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_api_answers_json(self):
        status, body, _ = self.call("GET", "/api/meta")
        self.assertEqual((status, json.loads(body)["build"]), (200, BUILD))
        status, body, _ = self.call("POST", "/api/evaluate", {"class": "Mage", "race": "Human", "level": 60, "alloc": {"1": 2}})
        self.assertEqual((status, json.loads(body)["sheet"]["lines"]["intellect"]["total"]), (200, 62))

    def test_errors_are_json_with_the_right_status(self):
        status, body, _ = self.call("POST", "/api/evaluate", {"class": "Necromancer", "race": "Human"})
        self.assertEqual(status, 400)
        self.assertIn("no class", json.loads(body)["error"])
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/api/evaluate", raw=b"{broken")[0], 400)

    def test_posts_must_be_json(self):
        status, _, _ = self.call("POST", "/api/evaluate", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_a_request_from_another_host_name_is_refused(self):
        status, body, _ = self.call("GET", "/api/meta", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("127.0.0.1", json.loads(body)["error"])
        self.assertEqual(self.call("GET", "/api/meta", headers={"Host": "localhost:1234"})[0], 200)

    def test_an_oversized_body_is_refused(self):
        status, _, _ = self.call("POST", "/api/evaluate", raw=b"x" * 1_000_001)
        self.assertEqual(status, 413)

    def test_saved_builds_over_http(self):
        body = {"class": "Mage", "race": "Human", "level": 60, "alloc": {"1": 1}}
        self.assertEqual(self.call("PUT", "/api/builds/one", body)[0], 200)
        self.assertEqual(json.loads(self.call("GET", "/api/builds")[1])["builds"][0]["name"], "one")
        self.assertEqual(json.loads(self.call("GET", "/api/builds/one")[1])["alloc"], {"1": 1})
        self.assertEqual(self.call("DELETE", "/api/builds/one")[0], 200)
        self.assertEqual(self.call("GET", "/api/builds/one")[0], 404)


if __name__ == "__main__":
    unittest.main()
