"""Offline tests for wowcalc.updater (the page's one-click update). Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wowcalc.update import AcknowledgementRequired, NothingToDo, UpdateError  # noqa: E402
from wowcalc.updater import BLOCKED, CURRENT, FAILED, IDLE, INSTALLED, NEEDS_ACK, UpdateJob  # noqa: E402


def assessment(version="1.60.1.70800", blockers=(), warnings=()):
    return SimpleNamespace(version=version, blocked=bool(blockers), needs_ack=tuple(warnings),
                           gates=SimpleNamespace(blockers=tuple(blockers)), changes=None)


class UpdateJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.prepared, self.applied, self.loaded = [], [], []
        self.result = assessment()
        self.load_error = None

    def prepare(self, data_dir, config, version=None, progress=None):
        self.prepared.append(version)
        if progress:
            progress("TraitTree", "17 rows")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def apply(self, data_dir, version, acknowledge=False):
        self.applied.append((version, acknowledge))
        if self.result.needs_ack and not acknowledge:
            raise AcknowledgementRequired(self.result.needs_ack)

    def installed(self, version):
        if self.load_error:
            raise self.load_error
        self.loaded.append(version)

    def run_job(self, acknowledge=False, job=None):
        job = job or UpdateJob(self.data, self.installed, prepare=self.prepare, apply=self.apply)
        job.start(acknowledge)
        job.wait(5)
        return job, job.snapshot()

    def test_starts_idle(self):
        self.assertEqual(UpdateJob(self.data, self.installed).snapshot()["state"], IDLE)

    def test_a_clean_build_is_installed_and_loaded(self):
        _, s = self.run_job()
        self.assertEqual((s["state"], s["version"]), (INSTALLED, "1.60.1.70800"))
        self.assertEqual((self.applied, self.loaded), ([("1.60.1.70800", False)], ["1.60.1.70800"]))
        self.assertIn("TraitTree: 17 rows", s["log"])

    def test_nothing_newer(self):
        self.result = NothingToDo("Build 1.60.1.70707 is already active.")
        _, s = self.run_job()
        self.assertEqual((s["state"], s["message"]), (CURRENT, "Build 1.60.1.70707 is already active."))
        self.assertEqual((self.applied, self.loaded), ([], []))

    def test_a_blocked_build_is_never_installed(self):
        self.result = assessment(blockers=["TraitNode: required column ID is missing"])
        _, s = self.run_job()
        self.assertEqual((s["state"], s["blockers"]), (BLOCKED, ["TraitNode: required column ID is missing"]))
        self.assertEqual(self.applied, [])

    def test_warnings_wait_for_confirmation_then_install(self):
        self.result = assessment(warnings=["New warning: 3 orphaned spell ids"])
        job, s = self.run_job()
        self.assertEqual((s["state"], s["warnings"]), (NEEDS_ACK, ["New warning: 3 orphaned spell ids"]))
        self.assertEqual(self.applied, [])
        _, s = self.run_job(acknowledge=True, job=job)
        self.assertEqual(s["state"], INSTALLED)
        self.assertEqual(self.prepared, [None, "1.60.1.70800"])      # the confirmed build, not a new check
        self.assertEqual(self.applied, [("1.60.1.70800", True)])

    def test_confirming_with_nothing_waiting_is_refused(self):
        with self.assertRaises(UpdateError):
            UpdateJob(self.data, self.installed, prepare=self.prepare, apply=self.apply).start(acknowledge=True)

    def test_a_failed_download_reports_and_installs_nothing(self):
        self.result = UpdateError("Download failed, nothing was kept. Spell: HTTP 503")
        _, s = self.run_job()
        self.assertEqual(s["state"], FAILED)
        self.assertIn("HTTP 503", s["message"])
        self.assertEqual(self.applied, [])

    def test_a_build_that_installs_but_does_not_load_says_so(self):
        self.load_error = ValueError("bad table")
        _, s = self.run_job()
        self.assertEqual(s["state"], FAILED)
        self.assertIn("was installed", s["message"])
        self.assertIn("rollback", s["message"])


if __name__ == "__main__":
    unittest.main()
