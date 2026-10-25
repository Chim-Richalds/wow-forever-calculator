"""Offline tests for wowcalc.update. Run: python -m unittest discover -s tests"""

import contextlib
import io
import json
import re
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import TempCase, base_tables, row_for, table_csv_bytes, write_dataset  # noqa: E402
from wowcalc import rules as R  # noqa: E402
from wowcalc import transport, update as U  # noqa: E402
from wowcalc.builds import Config  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.fetch import BASE_TABLES, EXTENDED_TABLES, TABLES, fetch_build, promote_staged  # noqa: E402
from wowcalc.validate import validate_dataset  # noqa: E402

V_OLD, V_NEW, V_MID = "1.60.1.70338", "1.60.1.70425", "1.60.1.70374"
CFG = Config()
NOW = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)
WAGO = re.compile(r"https://wago\.tools/db2/(\w+)/csv\?build=([\d.]+)")


class Remote:
    """Fake network: Blizzard's version server and Wago's table exports."""

    def __init__(self, latest, builds=None):
        self.latest = latest
        self.builds = builds or {}
        self.fail = {}  # (version, table) -> HTTP status
        self.tact_error = None
        self.table_requests = []

    def _body(self, url):
        m = WAGO.match(url)
        version, table = m.group(2), m.group(1)
        self.table_requests.append((version, table))
        code = self.fail.get((version, table))
        if code or version not in self.builds:
            raise urllib.error.HTTPError(url, code or 404, "Not Found", {}, None)
        header, rows = self.builds[version][table]
        return table_csv_bytes(header, rows)

    def text(self, url):
        if "patch.battle.net" in url:
            if self.tact_error:
                raise self.tact_error
            build = self.latest.rsplit(".", 1)[1]
            return ("Region!STRING:0|BuildConfig!HEX:16|CDNConfig!HEX:16|KeyRing!HEX:16|BuildId!DEC:4|"
                    "VersionsName!String:0|ProductConfig!HEX:16\n## seqn = 1\n"
                    f"us|a|b||{build}|{self.latest}|c\n")
        return self._body(url).decode("utf-8")

    def bytes(self, url):
        return transport.Response(self._body(url), "text/csv")


def rule(rid="r1", spell=5000, index=0, effect_id=1, status="verified", **extra):
    return {"rule_id": rid, "talent_spell_id": spell, "effect_id": effect_id, "effect_index": index,
            "effect_type": 6, "aura_id": 108, "base_points": 2, "verification_status": status, **extra}


def edit_effect(t):
    t["SpellEffect"][1][0]["EffectBasePointsF"] = "9"


def add_empty_tree(t):
    t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3"))


def break_ranks(t):
    t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"


class UpdateCase(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"

    def install(self, version=V_OLD, tables=None):
        write_dataset(self.data / "raw" / version, tables or base_tables())

    def remote(self, mutate=None, version=V_NEW, latest=None):
        t = base_tables()
        if mutate:
            mutate(t)
        return Remote(latest or version, {version: t})

    def prepare(self, remote, version=None, **kw):
        return U.prepare_update(self.data, CFG, version, fetch_text=remote.text, fetch_bytes=remote.bytes,
                                now=NOW, **kw)

    def apply(self, **kw):
        return U.apply_update(self.data, now=NOW, **kw)

    def active(self):
        state = U.read_state(self.data)
        return state["active"] if state else None

    def put_rules(self, rules):
        R.save_rules(self.data / "rules" / "effect-rules.json", {"schema_version": 1, "client_build": V_OLD, "rules": rules})

    def adopt_rules(self, rules):
        self.put_rules(rules)
        path = self.data / "rules" / "effect-rules.json"
        doc, results = R.adopt_fingerprints(R.load_rules(path), U.open_dataset(self.data, V_OLD), V_OLD)
        assert all(r.outcome == "adopted" for r in results), results
        R.save_rules(path, doc)

    def rules(self):
        return {r["rule_id"]: r for r in R.load_rules(self.data / "rules" / "effect-rules.json")["rules"]}


class ActivePointerTests(UpdateCase):
    def test_nothing_installed(self):
        self.assertIsNone(U.get_active(self.data))

    def test_inferred_from_newest_installed_when_no_pointer(self):
        self.install(V_OLD)
        self.install(V_NEW)
        self.assertEqual(U.get_active(self.data), V_NEW)

    def test_pointer_wins_over_newest_installed(self):
        self.install(V_OLD)
        self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "test", now=NOW)
        self.assertEqual(U.get_active(self.data), V_OLD)

    def test_pointer_to_missing_build_is_an_error(self):
        self.install(V_OLD)
        U._write_active(self.data, V_NEW, None, "test", now=NOW)
        with self.assertRaises(U.UpdateError):
            U.get_active(self.data)

    def test_corrupt_pointer_is_an_error(self):
        self.data.mkdir()
        (self.data / "active.json").write_text("{nope")
        with self.assertRaises(U.UpdateError):
            U.get_active(self.data)

    def test_history_accumulates_and_no_temp_file_left(self):
        self.install(V_OLD); self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "apply", now=NOW)
        U._write_active(self.data, V_NEW, V_OLD, "apply", acknowledged=["x"], invalidated=["r1"], now=NOW)
        state = U.read_state(self.data)
        self.assertEqual([h["build"] for h in state["history"]], [V_OLD, V_NEW])
        self.assertEqual(state["history"][1]["rulesInvalidated"], ["r1"])
        self.assertEqual(sorted(p.name for p in self.data.iterdir() if p.is_file()), ["active.json"])


class PrepareTests(UpdateCase):
    def test_fetches_stages_assesses_and_writes_reports_without_touching_active(self):
        self.install()
        a = self.prepare(self.remote())
        self.assertEqual((a.version, a.baseline, a.source, a.blocked), (V_NEW, V_OLD, "staging", False))
        self.assertTrue((self.data / "staging" / V_NEW / "manifest.json").is_file())
        self.assertFalse((self.data / "raw" / V_NEW).exists())
        self.assertEqual(U.get_active(self.data), V_OLD)
        self.assertFalse((self.data / "active.json").exists())  # prepare never writes the pointer
        reports = sorted(p.name for p in (self.data / "reports").iterdir())
        self.assertEqual(reports, [f"{V_NEW}.changes.json", f"{V_NEW}.validation.json"])
        self.assertEqual(len(set(t for _, t in self.remote().table_requests)), 0)

    def test_assessment_reports_changes_and_gates(self):
        self.install()
        a = self.prepare(self.remote(edit_effect))
        self.assertEqual(len(a.changes.effects_changed), 1)
        self.assertEqual(a.needs_ack, ())

    def test_blocked_candidate_is_reported_not_raised(self):
        self.install()
        a = self.prepare(self.remote(break_ranks))
        self.assertTrue(a.blocked)
        self.assertTrue((self.data / "reports" / f"{V_NEW}.validation.json").is_file())
        saved = json.loads((self.data / "reports" / f"{V_NEW}.validation.json").read_text())
        self.assertTrue(saved["gates"]["blocked"])

    def test_up_to_date(self):
        self.install(V_NEW)
        with self.assertRaises(U.NothingToDo):
            self.prepare(Remote(V_NEW))

    def test_active_newer_than_latest_detected(self):
        self.install(V_NEW)
        with self.assertRaises(U.NothingToDo):
            self.prepare(Remote(V_OLD))

    def test_explicit_older_version_points_to_rollback(self):
        self.install(V_NEW)
        with self.assertRaises(U.UpdateError) as ctx:
            self.prepare(Remote(V_NEW), V_OLD)
        self.assertIn("rollback", str(ctx.exception))

    def test_explicit_version_equal_to_active(self):
        self.install(V_OLD)
        with self.assertRaises(U.NothingToDo):
            self.prepare(Remote(V_NEW), V_OLD)

    def test_explicit_intermediate_version(self):
        self.install()
        remote = Remote(V_NEW, {V_MID: base_tables(), V_NEW: base_tables()})
        self.assertEqual(self.prepare(remote, V_MID).version, V_MID)

    def test_already_staged_is_reused_without_downloading(self):
        self.install()
        remote = self.remote()
        self.prepare(remote)
        before = len(remote.table_requests)
        a = self.prepare(remote)
        self.assertEqual(len(remote.table_requests), before)
        self.assertEqual(a.source, "staging")

    def test_retained_newer_build_needs_no_download(self):
        self.install(V_OLD)
        self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "rollback", now=NOW)
        remote = Remote(V_NEW)
        a = self.prepare(remote)
        self.assertEqual((a.source, remote.table_requests), ("raw", []))

    def test_data_not_exported_yet(self):
        self.install()
        with self.assertRaises(U.UpdateError) as ctx:
            self.prepare(Remote(V_NEW, {}))
        self.assertIn("not exported", str(ctx.exception))
        self.assertFalse((self.data / "staging").exists())

    def test_one_bad_table_keeps_nothing(self):
        self.install()
        remote = self.remote()
        remote.fail[(V_NEW, "SpellName")] = 503
        with self.assertRaises(U.UpdateError) as ctx:
            self.prepare(remote)
        self.assertIn("SpellName", str(ctx.exception))
        self.assertEqual(list((self.data / "staging").iterdir()), [])

    def test_detection_failure(self):
        self.install()
        remote = self.remote()
        remote.tact_error = urllib.error.URLError("down")
        with self.assertRaises(U.UpdateError) as ctx:
            self.prepare(remote)
        self.assertIn("latest build", str(ctx.exception))

    def test_invalid_version(self):
        with self.assertRaises(U.UpdateError):
            self.prepare(self.remote(), "garbage")

    def test_first_install_has_no_baseline_or_diff(self):
        a = self.prepare(self.remote())
        self.assertEqual((a.baseline, a.changes), (None, None))
        self.assertFalse(a.blocked)

    def test_rule_checks_included(self):
        self.install()
        self.adopt_rules([rule()])
        a = self.prepare(self.remote(edit_effect))
        self.assertEqual([(c.rule_id, c.state) for c in a.rule_checks], [("r1", "changed")])
        self.assertEqual(len(a.invalidated_rules), 1)
        saved = json.loads((self.data / "reports" / f"{V_NEW}.validation.json").read_text())
        self.assertEqual(saved["ruleChecks"][0]["state"], "changed")


class ApplyTests(UpdateCase):
    def test_success(self):
        self.install()
        self.prepare(self.remote(edit_effect))
        r = self.apply()
        self.assertEqual((r.version, r.previous, r.promoted), (V_NEW, V_OLD, True))
        self.assertEqual(self.active(), V_NEW)
        self.assertTrue((self.data / "raw" / V_NEW / "TraitTree.csv").is_file())
        self.assertTrue((self.data / "raw" / V_OLD).is_dir())  # retained for rollback
        self.assertFalse((self.data / "staging" / V_NEW).exists())
        self.assertTrue((self.data / "manifests" / f"{V_NEW}.json").is_file())
        entry = U.read_state(self.data)["history"][-1]
        self.assertEqual((entry["build"], entry["previous"], entry["via"]), (V_NEW, V_OLD, "apply"))

    def test_default_version_is_the_newest_prepared(self):
        self.install()
        self.prepare(self.remote())
        self.assertEqual(self.apply().version, V_NEW)

    def test_blocked_build_changes_nothing(self):
        self.install()
        self.prepare(self.remote(break_ranks))
        with self.assertRaises(U.UpdateError) as ctx:
            self.apply(acknowledge=True)  # acknowledging never overrides a blocker
        self.assertIn("Blocked", str(ctx.exception))
        self.assertTrue((self.data / "staging" / V_NEW).is_dir())
        self.assertFalse((self.data / "raw" / V_NEW).exists())
        self.assertFalse((self.data / "active.json").exists())

    def test_warnings_need_acknowledgement(self):
        self.install()
        self.prepare(self.remote(add_empty_tree))
        with self.assertRaises(U.AcknowledgementRequired) as ctx:
            self.apply()
        self.assertTrue(any("no nodes" in i for i in ctx.exception.items))
        self.assertFalse((self.data / "raw" / V_NEW).exists())
        self.assertFalse((self.data / "active.json").exists())
        r = self.apply(acknowledge=True)
        self.assertTrue(any("no nodes" in a for a in r.acknowledged))
        self.assertTrue(any("no nodes" in a for a in U.read_state(self.data)["history"][-1]["acknowledged"]))

    def test_gross_tampering_is_caught_by_validation(self):
        self.install()
        self.prepare(self.remote())
        (self.data / "staging" / V_NEW / "TraitTree.csv").write_bytes(b"TitleText_lang,ID\nTree A,1\n")
        with self.assertRaises(U.UpdateError) as ctx:
            self.apply()
        self.assertIn("Blocked", str(ctx.exception))
        self.assertFalse((self.data / "raw" / V_NEW).exists())

    def test_subtle_tampering_is_caught_by_the_hash_check(self):
        self.install()
        self.prepare(self.remote())
        # Same rows and columns, so validation passes; only the hash can tell.
        (self.data / "staging" / V_NEW / "TraitTree.csv").write_bytes(b"TitleText_lang,ID\nTree A tampered,1\nTree B,2\n")
        with self.assertRaises(U.UpdateError) as ctx:
            self.apply()
        self.assertIn("hash mismatch", str(ctx.exception))
        self.assertFalse((self.data / "raw" / V_NEW).exists())
        self.assertEqual(U.get_active(self.data), V_OLD)
        self.assertTrue((self.data / "staging" / V_NEW).is_dir())  # still there for inspection

    def test_not_prepared(self):
        self.install()
        with self.assertRaises(U.UpdateError):
            self.apply()
        with self.assertRaises(U.UpdateError):
            self.apply(version=V_NEW)

    def test_already_active_and_older(self):
        self.install(V_OLD)
        with self.assertRaises(U.NothingToDo):
            self.apply(version=V_OLD)
        self.install(V_NEW)
        U._write_active(self.data, V_NEW, None, "test", now=NOW)
        with self.assertRaises(U.UpdateError) as ctx:
            self.apply(version=V_OLD)
        self.assertIn("rollback", str(ctx.exception))

    def test_first_install(self):
        self.prepare(self.remote())
        r = self.apply()
        self.assertEqual((r.previous, self.active()), (None, V_NEW))

    def test_applying_a_retained_build_needs_no_promotion(self):
        self.install(V_OLD)
        self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "rollback", now=NOW)
        r = self.apply(version=V_NEW)
        self.assertEqual((r.promoted, self.active()), (False, V_NEW))

    # ---- rules ----

    def test_verified_rule_with_changed_data_needs_acknowledgement_then_is_flagged(self):
        self.install()
        self.adopt_rules([rule("r1"), rule("p1", status="pending")])
        self.prepare(self.remote(edit_effect))
        with self.assertRaises(U.AcknowledgementRequired) as ctx:
            self.apply()
        self.assertTrue(any("needs_reverification" in i and "r1" in i for i in ctx.exception.items))
        self.assertEqual(self.rules()["r1"]["verification_status"], "verified")  # nothing changed yet
        r = self.apply(acknowledge=True)
        self.assertEqual([t["ruleId"] for t in r.rule_transitions], ["r1"])
        rules = self.rules()
        self.assertEqual(rules["r1"]["verification_status"], "needs_reverification")
        self.assertEqual(rules["r1"]["history"][0]["build"], V_NEW)
        self.assertEqual(rules["p1"]["verification_status"], "pending")
        self.assertEqual(U.read_state(self.data)["history"][-1]["rulesInvalidated"], ["r1"])

    def test_verified_rule_with_unchanged_data_is_confirmed_without_acknowledgement(self):
        self.install()
        self.adopt_rules([rule("r1")])
        self.prepare(self.remote(add_empty_tree))  # unrelated warning only
        self.apply(acknowledge=True)
        self.assertEqual(self.rules()["r1"]["verification_status"], "verified")
        self.assertEqual(self.rules()["r1"]["last_confirmed_build"], V_NEW)

    def test_crash_before_pointer_write_is_safe_and_retryable(self):
        self.install()
        self.adopt_rules([rule("r1")])
        self.prepare(self.remote(edit_effect))
        real = U._write_active

        def dies_on_the_final_write(data_dir, version, previous, via, **kw):
            if via == "apply":
                raise RuntimeError("disk full")
            return real(data_dir, version, previous, via, **kw)  # the earlier pin still works
        U._write_active = dies_on_the_final_write
        try:
            with self.assertRaises(RuntimeError):
                self.apply(acknowledge=True)
        finally:
            U._write_active = real
        # The pointer did not move, but nothing was under-flagged. The new build is
        # in raw/ yet must NOT have become active just by being there.
        self.assertTrue((self.data / "raw" / V_NEW).is_dir())
        self.assertEqual(U.get_active(self.data), V_OLD)
        self.assertEqual(U.read_state(self.data)["history"][-1]["via"], "pinned")
        self.assertEqual(self.rules()["r1"]["verification_status"], "needs_reverification")
        self.assertTrue((self.data / "raw" / V_NEW).is_dir())
        # Retry completes from the retained build.
        r = self.apply(version=V_NEW)
        self.assertEqual((r.promoted, self.active()), (False, V_NEW))


class PinningTests(UpdateCase):
    def test_apply_pins_the_current_build_when_there_is_no_pointer_file(self):
        self.install()
        self.prepare(self.remote())
        self.apply()
        history = U.read_state(self.data)["history"]
        self.assertEqual([(h["build"], h["via"]) for h in history], [(V_OLD, "pinned"), (V_NEW, "apply")])

    def test_apply_does_not_pin_again_when_a_pointer_exists(self):
        self.install()
        U._write_active(self.data, V_OLD, None, "apply", now=NOW)
        self.prepare(self.remote())
        self.apply()
        self.assertEqual([h["via"] for h in U.read_state(self.data)["history"]], ["apply", "apply"])

    def test_first_install_pins_nothing(self):
        self.prepare(self.remote())
        self.apply()
        self.assertEqual([h["via"] for h in U.read_state(self.data)["history"]], ["apply"])

    def test_blocked_or_unacknowledged_apply_pins_nothing(self):
        self.install()
        self.prepare(self.remote(break_ranks))
        with self.assertRaises(U.UpdateError):
            self.apply()
        self.assertFalse((self.data / "active.json").exists())


class RollbackTests(UpdateCase):
    def applied(self):
        self.install()
        self.prepare(self.remote())
        self.apply()

    def test_rollback_and_back_again(self):
        self.applied()
        r = U.rollback(self.data, now=NOW)
        self.assertEqual((r.version, r.previous, self.active()), (V_OLD, V_NEW, V_OLD))
        self.assertTrue((self.data / "raw" / V_NEW).is_dir())  # nothing deleted
        self.assertEqual(U.read_state(self.data)["history"][-1]["via"], "rollback")
        self.assertEqual(U.rollback(self.data, now=NOW).version, V_NEW)

    def test_rollback_to_specific_build(self):
        self.install(V_OLD); self.install(V_MID); self.install(V_NEW)
        U._write_active(self.data, V_NEW, V_MID, "apply", now=NOW)
        self.assertEqual(U.rollback(self.data, to=V_OLD, now=NOW).version, V_OLD)

    def test_build_not_retained(self):
        self.applied()
        with self.assertRaises(U.UpdateError):
            U.rollback(self.data, to=V_MID)

    def test_already_active(self):
        self.applied()
        with self.assertRaises(U.NothingToDo):
            U.rollback(self.data, to=V_NEW)

    def test_no_previous_recorded(self):
        self.install(V_OLD)
        with self.assertRaises(U.UpdateError):
            U.rollback(self.data)

    def test_nothing_installed_and_invalid_version(self):
        with self.assertRaises(U.UpdateError):
            U.rollback(self.data)
        self.applied()
        with self.assertRaises(U.UpdateError):
            U.rollback(self.data, to="garbage")

    def test_rule_flags_survive_a_rollback(self):
        self.install()
        self.adopt_rules([rule("r1")])
        self.prepare(self.remote(edit_effect))
        self.apply(acknowledge=True)
        U.rollback(self.data, now=NOW)
        self.assertEqual(self.rules()["r1"]["verification_status"], "needs_reverification")


class LegacyBuildTests(UpdateCase):
    """Builds downloaded before the extended tables existed."""

    def legacy_tables(self):
        return {k: v for k, v in base_tables().items() if k in BASE_TABLES}

    def install_legacy(self, version=V_OLD):
        write_dataset(self.data / "raw" / version, self.legacy_tables())

    def test_diff_against_a_legacy_baseline_still_works(self):
        self.install_legacy()
        a = self.prepare(self.remote(edit_effect))
        self.assertFalse(a.blocked)
        self.assertIsNotNone(a.changes)
        self.assertEqual({t for t, s in a.changes.schema.items() if s["tableAdded"]}, set(EXTENDED_TABLES))
        self.assertEqual(len(a.changes.effects_changed), 1)  # the talent diff is intact

    def test_incomplete_candidate_cannot_be_applied(self):
        self.install()
        write_dataset(self.data / "staging" / V_NEW, self.legacy_tables())
        with self.assertRaises(U.UpdateError) as ctx:
            self.apply(acknowledge=True)
        self.assertIn("is missing", str(ctx.exception))
        self.assertFalse((self.data / "raw" / V_NEW).exists())

    def test_download_missing_extended_tables_is_rejected_and_keeps_nothing(self):
        self.install()
        remote = self.remote()
        for table in EXTENDED_TABLES:
            remote.fail[(V_NEW, table)] = 400  # how Wago answers for a table it does not export
        with self.assertRaises(U.UpdateError) as ctx:
            self.prepare(remote)
        self.assertIn("SkillLineXTraitTree", str(ctx.exception))
        self.assertEqual(list((self.data / "staging").iterdir()), [])

    def test_rollback_to_a_legacy_build_works_but_warns(self):
        self.install_legacy()
        self.prepare(self.remote())
        self.apply()
        r = U.rollback(self.data, now=NOW)
        self.assertEqual((r.version, self.active()), (V_OLD, V_OLD))
        self.assertEqual(r.missing_tables, EXTENDED_TABLES)

    def test_rollback_to_a_complete_build_has_no_warning(self):
        self.install()
        self.prepare(self.remote())
        self.apply()
        self.assertEqual(U.rollback(self.data, now=NOW).missing_tables, ())

    def test_cli_prints_the_legacy_warning(self):
        self.install_legacy()
        remote = self.remote()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            U.main(["--data-dir", str(self.data), "prepare"], fetch_text=remote.text, fetch_bytes=remote.bytes)
            U.main(["--data-dir", str(self.data), "apply"], fetch_text=remote.text, fetch_bytes=remote.bytes)
            code = U.main(["--data-dir", str(self.data), "rollback"], fetch_text=remote.text, fetch_bytes=remote.bytes)
        self.assertEqual(code, 0)
        self.assertIn(f"WARNING: {V_OLD} is a legacy build without {len(EXTENDED_TABLES)} table(s)", out.getvalue())


class CompleteTests(UpdateCase):
    def install_legacy(self, version=V_OLD):
        """A build installed the real way (fetch + promote), but with only the base tables."""
        remote = Remote(version, {version: base_tables()})
        fetch_build(version, CFG, self.data, fetch=remote.bytes, tables=BASE_TABLES, now=NOW)
        promote_staged(version, self.data)
        return remote

    def complete(self, remote, version=None, **kw):
        return U.complete(self.data, CFG, version, fetch_bytes=remote.bytes, now=NOW, **kw)

    def test_completes_the_active_build_by_default_without_changing_it(self):
        remote = self.install_legacy()
        added = self.complete(remote)
        self.assertEqual(added, EXTENDED_TABLES)
        self.assertEqual(U.missing_tables(self.data, V_OLD), ())
        self.assertEqual(U.get_active(self.data), V_OLD)
        self.assertFalse((self.data / "active.json").exists())  # completing never touches the pointer

    def test_completed_build_validates_clean(self):
        remote = self.install_legacy()
        self.complete(remote)
        report = validate_dataset(Dataset(self.data / "raw" / V_OLD, V_OLD))
        self.assertEqual((report.missing_tables, report.errors), ([], []))

    def test_complete_a_retained_build_that_is_not_active(self):
        old = self.install_legacy(V_OLD)
        new = Remote(V_NEW, {V_NEW: base_tables()})
        self.install(V_NEW)
        U._write_active(self.data, V_NEW, V_OLD, "apply", now=NOW)
        self.assertEqual(self.complete(old, V_OLD), EXTENDED_TABLES)
        self.assertEqual(U.rollback(self.data, now=NOW).missing_tables, ())  # no legacy warning any more

    def test_nothing_to_do_when_complete(self):
        remote = self.install_legacy()
        self.complete(remote)
        with self.assertRaises(U.NothingToDo):
            self.complete(remote)

    def test_failed_download_adds_nothing(self):
        remote = self.install_legacy()
        remote.fail[(V_OLD, "TraitCurrency")] = 503
        with self.assertRaises(U.UpdateError) as ctx:
            self.complete(remote)
        self.assertIn("TraitCurrency", str(ctx.exception))
        self.assertEqual(U.missing_tables(self.data, V_OLD), EXTENDED_TABLES)

    def test_nothing_installed(self):
        with self.assertRaises(U.UpdateError):
            self.complete(Remote(V_OLD, {}))

    def test_build_without_a_manifest_is_refused_clearly(self):
        self.install()  # written directly, so there is no manifest
        with self.assertRaises(U.UpdateError) as ctx:
            self.complete(Remote(V_OLD, {V_OLD: base_tables()}))
        self.assertIn("Manifest", str(ctx.exception))

    def test_status_flags_incomplete_builds_and_how_to_fix_it(self):
        remote = self.install_legacy()
        s = U.status(self.data, CFG, remote.text)
        self.assertEqual(s.incomplete, {V_OLD: EXTENDED_TABLES})
        self.assertIn("python -m wowcalc.update complete", s.message)
        self.complete(remote)
        self.assertEqual(U.status(self.data, CFG, remote.text).incomplete, {})

    def test_prepare_for_the_already_active_incomplete_build_points_to_complete(self):
        remote = self.install_legacy()
        with self.assertRaises(U.NothingToDo) as ctx:
            self.prepare(remote)
        self.assertIn("already active", str(ctx.exception))
        self.assertIn(f"lacks {len(EXTENDED_TABLES)} table(s)", str(ctx.exception))
        self.assertIn("complete", str(ctx.exception))

    def test_prepare_for_a_complete_active_build_has_no_hint(self):
        self.install()
        with self.assertRaises(U.NothingToDo) as ctx:
            self.prepare(Remote(V_OLD))
        self.assertNotIn("lacks", str(ctx.exception))

    def test_cli_status_and_complete(self):
        remote = self.install_legacy()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            U.main(["--data-dir", str(self.data), "status"], fetch_text=remote.text, fetch_bytes=remote.bytes)
            code = U.main(["--data-dir", str(self.data), "complete"], fetch_text=remote.text, fetch_bytes=remote.bytes)
            again = U.main(["--data-dir", str(self.data), "complete"], fetch_text=remote.text, fetch_bytes=remote.bytes)
        text = out.getvalue()
        self.assertIn(f"{V_OLD} (lacks {len(EXTENDED_TABLES)} table(s))", text)
        self.assertIn("SkillLineXTraitTree: 2 rows", text)
        self.assertIn(f"Added {len(EXTENDED_TABLES)} table(s)", text)
        self.assertIn(f"already has all {len(TABLES)} tables", text)
        self.assertEqual((code, again), (0, 0))


class StatusTests(UpdateCase):
    def status(self, remote):
        return U.status(self.data, CFG, remote.text)

    def test_nothing_installed(self):
        self.assertIn("No build is installed", self.status(Remote(V_NEW)).message)

    def test_current(self):
        self.install(V_NEW)
        self.assertIn("is current", self.status(Remote(V_NEW)).message)

    def test_update_available(self):
        self.install(V_OLD)
        s = self.status(Remote(V_NEW))
        self.assertIn("Run prepare", s.message)
        self.assertEqual((s.active, s.latest, s.retained), (V_OLD, V_NEW, (V_OLD,)))

    def test_prepared(self):
        self.install(V_OLD)
        self.prepare(self.remote())
        s = self.status(Remote(V_NEW))
        self.assertIn("is prepared", s.message)
        self.assertEqual(s.staged, (V_NEW,))

    def test_installed_but_not_active(self):
        self.install(V_OLD); self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "rollback", now=NOW)
        self.assertIn("installed but not active", self.status(Remote(V_NEW)).message)

    def test_detection_failure(self):
        self.install(V_OLD)
        remote = Remote(V_NEW)
        remote.tact_error = urllib.error.URLError("down")
        s = self.status(remote)
        self.assertIn("Could not check", s.message)
        self.assertIsNone(s.latest)


class CliTests(UpdateCase):
    def run_cli(self, remote, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = U.main(["--data-dir", str(self.data), *args], fetch_text=remote.text, fetch_bytes=remote.bytes)
        return code, out.getvalue()

    def test_status(self):
        self.install()
        code, out = self.run_cli(Remote(V_NEW), "status")
        self.assertEqual(code, 0)
        self.assertIn("Active:   " + V_OLD, out)

    def test_prepare_then_apply_then_rollback(self):
        self.install()
        remote = self.remote(edit_effect)
        code, out = self.run_cli(remote, "prepare")
        self.assertEqual(code, 0)
        self.assertIn("TraitTree: 2 rows", out)
        self.assertIn("Ready. To install", out)
        self.assertEqual(self.run_cli(remote, "apply")[0], 0)
        self.assertEqual(self.active(), V_NEW)
        code, out = self.run_cli(remote, "rollback")
        self.assertEqual((code, self.active()), (0, V_OLD))
        self.assertIn(V_OLD, out)

    def test_prepare_blocked_returns_one(self):
        self.install()
        code, out = self.run_cli(self.remote(break_ranks), "prepare")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)

    def test_apply_requires_yes_for_warnings(self):
        self.install()
        remote = self.remote(add_empty_tree)
        code, out = self.run_cli(remote, "prepare")
        self.assertIn("apply --version " + V_NEW + " --yes", out)
        code, out = self.run_cli(remote, "apply")
        self.assertEqual(code, 1)
        self.assertIn("Acknowledgement required", out)
        self.assertEqual(self.run_cli(remote, "apply", "--yes")[0], 0)

    def test_nothing_to_do_is_not_an_error(self):
        self.install(V_NEW)
        code, out = self.run_cli(Remote(V_NEW), "prepare")
        self.assertEqual(code, 0)
        self.assertIn("already active", out)

    def test_error_returns_one(self):
        self.install()
        self.assertEqual(self.run_cli(Remote(V_NEW, {}), "prepare")[0], 1)

    def test_rules_cli_default_build_follows_the_active_pointer(self):
        self.install(V_OLD); self.install(V_NEW)
        U._write_active(self.data, V_OLD, None, "rollback", now=NOW)
        self.assertEqual(R._default_build(self.data), V_OLD)


if __name__ == "__main__":
    unittest.main()
