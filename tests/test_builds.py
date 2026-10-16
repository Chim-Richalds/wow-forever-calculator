"""Offline tests for wowcalc.builds. Run: python -m unittest discover -s tests"""

import http.server
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wowcalc.builds import (  # noqa: E402
    Availability, BuildInfo, BuildSourceError, Config, Status,
    check_availability, check_for_updates, default_fetch, detect_latest,
    installed_builds, list_wago_builds, parse_bpsv,
)

CFG = Config()

TACT_SAMPLE = (
    "Region!STRING:0|BuildConfig!HEX:16|CDNConfig!HEX:16|KeyRing!HEX:16|"
    "BuildId!DEC:4|VersionsName!String:0|ProductConfig!HEX:16\n"
    "## seqn = 4081170\n"
    "us|aa|bb||70425|1.60.1.70425|cc\n"
    "eu|aa|bb||70425|1.60.1.70425|cc\n"
)
GOOD_CSV = "TitleText_lang,ID,TraitSystemID\n,1058,0\n,1082,0\n"

def _entry(version, created="2026-01-01 00:00:00"):
    return {"product": "wow_classic_beta", "version": version, "created_at": created,
            "build_config": "cfg_" + version, "product_config": "p", "cdn_config": "c", "is_bgdl": False}


# Shape matches the real response: dict keyed by product, array NOT in date order
# (5.5.0 entries first), plus a malformed entry that must be skipped.
BUILDS_SAMPLE = json.dumps({
    "wow_classic_beta": [
        _entry("5.5.0.62071", "2025-07-18 01:19:04"),
        _entry("1.60.1.70374"),
        _entry("1.60.1.70425", "2026-10-16 00:50:05"),
        _entry("1.60.1.70338"),
        {"product": "wow_classic_beta", "version": "garbage"},
        "not-a-dict",
    ],
    "wow": [_entry("12.0.0.99999")],
})


def make_fetch(tact=TACT_SAMPLE, wago=GOOD_CSV, builds=BUILDS_SAMPLE):
    """Fake fetcher. A value that is an Exception instance is raised."""
    def fetch(url):
        if "patch.battle.net" in url:
            payload = tact
        elif "api/builds" in url:
            payload = builds
        else:
            payload = wago
        if isinstance(payload, Exception):
            raise payload
        return payload
    return fetch


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def make_data_dir(*versions):
    tmp = tempfile.TemporaryDirectory()
    for v in versions:
        (Path(tmp.name) / "raw" / v).mkdir(parents=True)
    return tmp


class ParseTests(unittest.TestCase):
    def test_bpsv(self):
        cols, rows, seqn = parse_bpsv(TACT_SAMPLE)
        self.assertEqual(cols[0], "Region")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["BuildId"], "70425")
        self.assertEqual(seqn, 4081170)

    def test_version_line(self):
        self.assertEqual(BuildInfo.from_version("1.60.1.70425").version_line, "1.60.1")

    def test_bad_version(self):
        with self.assertRaises(ValueError):
            BuildInfo.from_version("1.60.1")


class DetectTests(unittest.TestCase):
    def test_detects_configured_region(self):
        info = detect_latest(CFG, make_fetch())
        self.assertEqual((info.version, info.build_id, info.region), ("1.60.1.70425", 70425, "us"))

    def test_missing_region(self):
        with self.assertRaises(BuildSourceError):
            detect_latest(Config(region="kr"), make_fetch())

    def test_network_error(self):
        with self.assertRaises(BuildSourceError):
            detect_latest(CFG, make_fetch(tact=urllib.error.URLError("down")))

    def test_build_id_mismatch_rejected(self):
        bad = TACT_SAMPLE.replace("|70425|", "|99999|", 1)
        with self.assertRaises(BuildSourceError):
            detect_latest(CFG, make_fetch(tact=bad))


class AvailabilityTests(unittest.TestCase):
    def check(self, wago):
        return check_availability("1.60.1.70425", CFG, make_fetch(wago=wago))

    def test_good(self):
        self.assertIs(self.check(GOOD_CSV), Availability.AVAILABLE)

    def test_404(self):
        self.assertIs(self.check(http_error(404)), Availability.NOT_AVAILABLE)

    def test_server_error_is_unknown(self):
        self.assertIs(self.check(http_error(503)), Availability.UNKNOWN)

    def test_network_error_is_unknown(self):
        self.assertIs(self.check(urllib.error.URLError("down")), Availability.UNKNOWN)

    def test_timeout_is_unknown(self):
        self.assertIs(self.check(TimeoutError()), Availability.UNKNOWN)

    def test_html_is_unknown_not_missing(self):
        self.assertIs(self.check("<!DOCTYPE html><html>challenge</html>"), Availability.UNKNOWN)

    def test_header_only_is_not_available(self):
        self.assertIs(self.check("TitleText_lang,ID,TraitSystemID\n"), Availability.NOT_AVAILABLE)

    def test_empty_body_is_not_available(self):
        self.assertIs(self.check(""), Availability.NOT_AVAILABLE)

    def test_wrong_columns_not_available(self):
        self.assertIs(self.check("foo,bar\n1,2\n"), Availability.NOT_AVAILABLE)


class InstalledTests(unittest.TestCase):
    def test_scans_and_sorts(self):
        tmp = make_data_dir("1.60.1.70425", "1.60.1.70338")
        (Path(tmp.name) / "raw" / "not-a-build").mkdir()
        self.assertEqual([b.build_id for b in installed_builds(Path(tmp.name))], [70338, 70425])
        tmp.cleanup()

    def test_missing_dir(self):
        self.assertEqual(installed_builds(Path("/nonexistent/xyz")), [])


class UpdateCheckTests(unittest.TestCase):
    def run_check(self, installed, **kw):
        tmp = make_data_dir(*installed)
        try:
            return check_for_updates(CFG, Path(tmp.name), make_fetch(**{k: v for k, v in kw.items() if k != "manual"}),
                                     manual_version=kw.get("manual"))
        finally:
            tmp.cleanup()

    def test_up_to_date(self):
        self.assertIs(self.run_check(["1.60.1.70425"]).status, Status.UP_TO_DATE)

    def test_installed_newer_than_latest_is_up_to_date(self):
        self.assertIs(self.run_check(["1.60.1.70500"]).status, Status.UP_TO_DATE)

    def test_update_ready(self):
        r = self.run_check(["1.60.1.70338"])
        self.assertIs(r.status, Status.UPDATE_READY)
        self.assertEqual(r.latest.build_id, 70425)
        self.assertFalse(r.line_changed)

    def test_update_pending_data(self):
        r = self.run_check(["1.60.1.70338"], wago=http_error(404))
        self.assertIs(r.status, Status.UPDATE_PENDING_DATA)

    def test_update_unverified(self):
        r = self.run_check(["1.60.1.70338"], wago=urllib.error.URLError("down"))
        self.assertIs(r.status, Status.UPDATE_UNVERIFIED)

    def test_detection_failed(self):
        r = self.run_check(["1.60.1.70338"], tact=urllib.error.URLError("down"))
        self.assertIs(r.status, Status.DETECTION_FAILED)
        self.assertIsNone(r.latest)

    def test_no_dataset_installed(self):
        r = self.run_check([])
        self.assertIs(r.status, Status.UPDATE_READY)
        self.assertIsNone(r.installed)

    def test_version_line_change_flagged(self):
        r = self.run_check(["1.59.0.69000"])
        self.assertTrue(r.line_changed)
        self.assertIn("version line changed", r.message)

    def test_manual_version_skips_detection(self):
        r = self.run_check(["1.60.1.70338"], tact=urllib.error.URLError("down"), manual="1.60.1.70425")
        self.assertIs(r.status, Status.UPDATE_READY)

    def test_manual_version_invalid(self):
        r = self.run_check(["1.60.1.70338"], manual="garbage")
        self.assertIs(r.status, Status.DETECTION_FAILED)


class FetchTests(unittest.TestCase):
    """default_fetch against a throwaway local server (no internet needed)."""

    @classmethod
    def setUpClass(cls):
        cls.hits = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits[self.path] = cls.hits.get(self.path, 0) + 1
                if self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"hello")

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_success(self):
        self.assertEqual(default_fetch(self.base + "/ok"), "hello")

    def test_http_error_raised_and_not_retried(self):
        before = self.hits.get("/missing", 0)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            default_fetch(self.base + "/missing")
        self.assertEqual(ctx.exception.code, 404)  # still readable after close
        ctx.exception.close()
        self.assertEqual(self.hits["/missing"] - before, 1)  # one request, no retry

    def test_connection_failure_raises_oserror(self):
        with socket.socket() as s:  # grab a free port, then close it
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(OSError):
            default_fetch(f"http://127.0.0.1:{port}/x")

    def test_404_maps_to_not_available(self):
        cfg = Config()
        result = check_availability("1.60.1.70425", cfg, lambda url: default_fetch(self.base + "/missing"))
        self.assertIs(result, Availability.NOT_AVAILABLE)


class WagoBuildListTests(unittest.TestCase):
    def test_filters_line_sorts_newest_first_skips_malformed(self):
        builds = list_wago_builds(CFG, make_fetch(), version_line="1.60.1")
        self.assertEqual([b.build_id for b in builds], [70425, 70374, 70338])

    def test_no_line_filter_keeps_other_lines(self):
        builds = list_wago_builds(CFG, make_fetch())
        self.assertEqual([b.version for b in builds][-1], "5.5.0.62071")
        self.assertEqual(len(builds), 4)  # garbage + non-dict skipped

    def test_other_products_ignored(self):
        builds = list_wago_builds(CFG, make_fetch())
        self.assertNotIn("12.0.0.99999", [b.version for b in builds])

    def test_fields_carried(self):
        newest = list_wago_builds(CFG, make_fetch(), version_line="1.60.1")[0]
        self.assertEqual(newest.created_at, "2026-10-16 00:50:05")
        self.assertEqual(newest.build_config, "cfg_1.60.1.70425")
        self.assertFalse(newest.is_bgdl)

    def test_unknown_product_raises(self):
        with self.assertRaises(BuildSourceError):
            list_wago_builds(Config(product="nope"), make_fetch())

    def test_bad_json_raises(self):
        with self.assertRaises(BuildSourceError):
            list_wago_builds(CFG, make_fetch(builds="<html>"))

    def test_network_error_raises(self):
        with self.assertRaises(BuildSourceError):
            list_wago_builds(CFG, make_fetch(builds=urllib.error.URLError("down")))


class NewerBuildsTests(unittest.TestCase):
    def run_check(self, installed, **kw):
        tmp = make_data_dir(*installed)
        try:
            return check_for_updates(CFG, Path(tmp.name), make_fetch(**kw))
        finally:
            tmp.cleanup()

    def test_lists_builds_between_installed_and_latest(self):
        r = self.run_check(["1.60.1.70338"])
        self.assertEqual(r.newer_builds, ("1.60.1.70374", "1.60.1.70425"))

    def test_no_dataset_lists_all_in_line(self):
        r = self.run_check([])
        self.assertEqual(r.newer_builds, ("1.60.1.70338", "1.60.1.70374", "1.60.1.70425"))

    def test_up_to_date_has_none(self):
        self.assertEqual(self.run_check(["1.60.1.70425"]).newer_builds, ())

    def test_list_failure_does_not_change_status(self):
        r = self.run_check(["1.60.1.70338"], builds=urllib.error.URLError("down"))
        self.assertIs(r.status, Status.UPDATE_READY)
        self.assertEqual(r.newer_builds, ())


if __name__ == "__main__":
    unittest.main()
