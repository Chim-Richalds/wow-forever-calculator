"""Offline tests for wowcalc.fetch and wowcalc.transport.
Run: python -m unittest discover -s tests
"""

import hashlib
import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wowcalc import transport  # noqa: E402
from wowcalc.builds import Config, installed_builds  # noqa: E402
from wowcalc.fetch import (  # noqa: E402
    TABLES, FetchError, fetch_build, promote_staged, validate_csv,
)

CFG = Config()
V = "1.60.1.70425"
GOOD = b"ID,Name\n1,alpha\n2,beta\n"
FIXED_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def ok_fetch(url):
    return transport.Response(GOOD, "text/csv; charset=UTF-8")


def http_error(code=404, reason="Not Found"):
    return urllib.error.HTTPError("http://x", code, reason, {}, None)


def fetch_failing_on(bad_table, error):
    """Fetcher that succeeds except for one table, which raises/returns `error`."""
    calls = []

    def fetch(url):
        calls.append(url)
        if f"/{bad_table}/" in url:
            if isinstance(error, Exception):
                raise error
            return transport.Response(error, "text/html")
        return ok_fetch(url)

    fetch.calls = calls
    return fetch


class TempData(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"  # named "data", as in the repo
        self.data.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def fetch(self, fetch=ok_fetch, tables=("TraitTree", "SpellName"), **kw):
        return fetch_build(V, CFG, self.data, fetch=fetch, tables=tables, now=FIXED_NOW, **kw)


class ValidateCsvTests(unittest.TestCase):
    def test_good(self):
        facts = validate_csv(GOOD)
        self.assertEqual(facts["columns"], ["ID", "Name"])
        self.assertEqual(facts["rows"], 2)
        self.assertEqual(facts["malformedRows"], 0)
        self.assertEqual(facts["duplicateHeaders"], [])

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            validate_csv(b"")

    def test_rejects_html(self):
        for body in (b"<!DOCTYPE html><html></html>", b"  \n<html>x</html>"):
            with self.assertRaises(ValueError):
                validate_csv(body)

    def test_rejects_single_column_header(self):
        with self.assertRaises(ValueError):
            validate_csv(b"ID\n1\n")

    def test_rejects_header_only(self):
        with self.assertRaises(ValueError):
            validate_csv(b"ID,Name\n")

    def test_rejects_invalid_utf8(self):
        with self.assertRaises(ValueError):
            validate_csv(b"ID,Name\n1,\xff\xfe\n")

    def test_bom_stripped(self):
        self.assertEqual(validate_csv(b"\xef\xbb\xbfID,Name\n1,a\n")["columns"], ["ID", "Name"])

    def test_malformed_rows_counted_not_rejected(self):
        facts = validate_csv(b"ID,Name\n1,a\n2\n3,c,extra\n")
        self.assertEqual((facts["rows"], facts["malformedRows"]), (3, 2))

    def test_duplicate_headers_reported(self):
        self.assertEqual(validate_csv(b"ID,Name,Name\n1,a,b\n")["duplicateHeaders"], ["Name"])

    def test_quoted_newline_is_one_row(self):
        facts = validate_csv(b'ID,Text\n1,"line one\nline two"\n2,plain\n')
        self.assertEqual((facts["rows"], facts["malformedRows"]), (2, 0))

    def test_blank_lines_ignored(self):
        self.assertEqual(validate_csv(b"ID,Name\n1,a\n\n\n2,b\n")["rows"], 2)


class FetchSuccessTests(TempData):
    def test_stages_files_and_manifest(self):
        r = self.fetch()
        self.assertTrue(r.ok)
        staged = self.data / "staging" / V
        self.assertEqual(r.staged_dir, staged)
        self.assertTrue((staged / "TraitTree.csv").is_file())
        self.assertFalse((self.data / "staging" / f"{V}.partial").exists())
        manifest = json.loads((staged / "manifest.json").read_text())
        self.assertEqual(manifest["build"], V)
        self.assertEqual(manifest["product"], "wow_classic_beta")
        self.assertEqual(manifest["retrievedAt"], FIXED_NOW.isoformat())
        self.assertEqual(manifest["failures"], [])

    def test_manifest_entries_are_accurate(self):
        self.fetch()
        entry = json.loads((self.data / "staging" / V / "manifest.json").read_text())["tables"]["TraitTree"]
        self.assertEqual(entry["sha256"], hashlib.sha256(GOOD).hexdigest())
        self.assertEqual(entry["bytes"], len(GOOD))
        self.assertEqual(entry["rows"], 2)
        self.assertEqual(entry["file"], f"data/raw/{V}/TraitTree.csv")  # final location
        self.assertEqual(entry["url"], f"https://wago.tools/db2/TraitTree/csv?build={V}")
        self.assertEqual(entry["contentType"], "text/csv; charset=UTF-8")
        self.assertEqual(entry["status"], "downloaded-needs-validation")

    def test_does_not_touch_installed_data(self):
        self.fetch()
        self.assertFalse((self.data / "raw").exists())
        self.assertEqual(installed_builds(self.data), [])

    def test_default_table_list_is_thirteen(self):
        self.assertEqual(len(TABLES), 13)
        r = fetch_build(V, CFG, self.data, fetch=ok_fetch, now=FIXED_NOW)
        self.assertEqual(len(r.manifest["tables"]), 13)

    def test_progress_reported(self):
        seen = []
        self.fetch(progress=lambda t, n: seen.append((t, n)))
        self.assertEqual([t for t, _ in seen], ["TraitTree", "SpellName"])
        self.assertEqual(seen[0][1], "2 rows")


class FetchFailureTests(TempData):
    def assert_clean(self):
        staging = self.data / "staging"
        self.assertEqual(list(staging.iterdir()) if staging.exists() else [], [])

    def test_http_404_fails_and_leaves_nothing(self):
        f = fetch_failing_on("SpellName", http_error(404))
        r = self.fetch(fetch=f)
        self.assertFalse(r.ok)
        self.assertIsNone(r.staged_dir)
        self.assertEqual(r.failures[0], {"table": "SpellName", "error": "HTTP 404 Not Found"})
        self.assertEqual(r.manifest["tables"]["SpellName"]["status"], "failed")
        self.assert_clean()

    def test_html_response_fails(self):
        r = self.fetch(fetch=fetch_failing_on("TraitTree", b"<html>blocked</html>"))
        self.assertFalse(r.ok)
        self.assertIn("HTML", r.failures[0]["error"])
        self.assert_clean()

    def test_network_error_fails(self):
        r = self.fetch(fetch=fetch_failing_on("TraitTree", urllib.error.URLError("down")))
        self.assertFalse(r.ok)
        self.assert_clean()

    def test_timeout_fails(self):
        r = self.fetch(fetch=fetch_failing_on("TraitTree", TimeoutError("timed out")))
        self.assertFalse(r.ok)
        self.assertIn("timed out", r.failures[0]["error"])

    def test_all_tables_attempted_and_all_failures_reported(self):
        def fetch(url):
            raise http_error(503, "Unavailable")
        r = self.fetch(fetch=fetch, tables=("A", "B", "C"))
        self.assertEqual([f["table"] for f in r.failures], ["A", "B", "C"])

    def test_one_failure_among_good_tables_discards_everything(self):
        f = fetch_failing_on("TraitNode", http_error(404))
        r = self.fetch(fetch=f, tables=("TraitTree", "TraitNode", "SpellName"))
        self.assertFalse(r.ok)
        self.assertEqual(len(f.calls), 3)  # kept going after the failure
        self.assert_clean()

    def test_unexpected_exception_cleans_partial(self):
        def fetch(url):
            raise RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            self.fetch(fetch=fetch)
        self.assert_clean()


class FetchPreconditionTests(TempData):
    def test_already_installed_refused(self):
        (self.data / "raw" / V).mkdir(parents=True)
        with self.assertRaises(FetchError):
            self.fetch()

    def test_already_staged_refused_without_overwrite(self):
        self.fetch()
        with self.assertRaises(FetchError):
            self.fetch()

    def test_overwrite_replaces_staged(self):
        self.fetch()
        marker = self.data / "staging" / V / "stale.txt"
        marker.write_text("old")
        self.assertTrue(self.fetch(overwrite=True).ok)
        self.assertFalse(marker.exists())

    def test_stale_partial_removed(self):
        stale = self.data / "staging" / f"{V}.partial"
        stale.mkdir(parents=True)
        (stale / "junk.csv").write_text("x")
        self.assertTrue(self.fetch().ok)
        self.assertFalse(stale.exists())

    def test_bad_version_rejected(self):
        with self.assertRaises(ValueError):
            fetch_build("garbage", CFG, self.data, fetch=ok_fetch)

    def test_failed_fetch_does_not_remove_installed_or_other_staged(self):
        (self.data / "raw" / "1.60.1.70338").mkdir(parents=True)
        other = self.data / "staging" / "1.60.1.70374"
        other.mkdir(parents=True)
        self.fetch(fetch=fetch_failing_on("TraitTree", http_error(404)))
        self.assertTrue((self.data / "raw" / "1.60.1.70338").is_dir())
        self.assertTrue(other.is_dir())


class PromoteTests(TempData):
    def setUp(self):
        super().setUp()
        self.fetch()

    def test_promote_installs(self):
        final = promote_staged(V, self.data)
        self.assertEqual(final, self.data / "raw" / V)
        self.assertTrue((final / "TraitTree.csv").is_file())
        self.assertFalse((final / "manifest.json").exists())  # published separately
        self.assertTrue((self.data / "manifests" / f"{V}.json").is_file())
        self.assertFalse((self.data / "staging" / V).exists())
        self.assertEqual([b.version for b in installed_builds(self.data)], [V])

    def test_promoted_manifest_is_the_fetch_manifest(self):
        promote_staged(V, self.data)
        manifest = json.loads((self.data / "manifests" / f"{V}.json").read_text())
        self.assertEqual(manifest["build"], V)
        self.assertEqual(set(manifest["tables"]), {"TraitTree", "SpellName"})

    def test_hash_mismatch_blocks_and_changes_nothing(self):
        (self.data / "staging" / V / "TraitTree.csv").write_bytes(b"ID,Name\n9,tampered\n")
        with self.assertRaises(FetchError) as ctx:
            promote_staged(V, self.data)
        self.assertIn("hash mismatch", str(ctx.exception))
        self.assertTrue((self.data / "staging" / V).is_dir())
        self.assertFalse((self.data / "raw").exists())
        self.assertFalse((self.data / "manifests" / f"{V}.json").exists())

    def test_missing_file_blocks(self):
        (self.data / "staging" / V / "SpellName.csv").unlink()
        with self.assertRaises(FetchError) as ctx:
            promote_staged(V, self.data)
        self.assertIn("file missing", str(ctx.exception))

    def test_not_staged(self):
        with self.assertRaises(FetchError):
            promote_staged("1.60.1.70374", self.data)

    def test_already_installed(self):
        (self.data / "raw" / V).mkdir(parents=True)
        with self.assertRaises(FetchError):
            promote_staged(V, self.data)

    def test_unreadable_manifest(self):
        (self.data / "staging" / V / "manifest.json").write_text("{not json")
        with self.assertRaises(FetchError):
            promote_staged(V, self.data)

    def test_orphan_manifest_does_not_block_retry(self):
        (self.data / "manifests").mkdir()
        (self.data / "manifests" / f"{V}.json").write_text("{}")  # left by a crash
        promote_staged(V, self.data)
        self.assertEqual(json.loads((self.data / "manifests" / f"{V}.json").read_text())["build"], V)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.end_headers()
                self.wfile.write(b"ID,Name\n1,a\n")

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_returns_bytes_and_content_type(self):
        r = transport.get(self.base + "/ok")
        self.assertEqual(r.body, b"ID,Name\n1,a\n")
        self.assertEqual(r.content_type, "text/csv")

    def test_http_error_propagates_with_code(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            transport.get(self.base + "/missing")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_end_to_end_fetch_against_local_server(self):
        """fetch_build through the real transport (URL template swapped to localhost)."""
        import wowcalc.fetch as fetch_mod
        original = fetch_mod.WAGO_TABLE_URL
        fetch_mod.WAGO_TABLE_URL = self.base + "/{table}?build={version}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp) / "data"
                r = fetch_build(V, CFG, data, tables=("TraitTree",), now=FIXED_NOW)
                self.assertTrue(r.ok)
                self.assertEqual((data / "staging" / V / "TraitTree.csv").read_bytes(), b"ID,Name\n1,a\n")
        finally:
            fetch_mod.WAGO_TABLE_URL = original


if __name__ == "__main__":
    unittest.main()
