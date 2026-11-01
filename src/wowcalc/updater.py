"""One-click update for the page: check, fetch, validate, install, then reload.

The page starts a job (`UpdateJob.start`) and polls `snapshot()`. The job runs the same
steps as `python -m wowcalc.update prepare` and `apply`, with the same safety rules:

  - a build that fails validation is never installed (state "blocked");
  - new warnings stop the job until the person confirms (state "needs_ack"), and a
    second start with acknowledge=True installs it;
  - anything else is installed, and `on_installed(version)` lets the server load it.

The job runs in a thread because a download takes half a minute, or a few minutes when new
server hotfixes have to be read. Only one runs
at a time; the last result is kept so the page can show it after it reloads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .builds import Config
from .diff import format_report
from .update import AcknowledgementRequired, NothingToDo, UpdateError, apply_update, get_active, prepare_update

IDLE, RUNNING, CURRENT, NEEDS_ACK, BLOCKED, INSTALLED, FAILED = (
    "idle", "running", "current", "needs_ack", "blocked", "installed", "failed")
SUMMARY_LINES = 60


class UpdateJob:
    def __init__(self, data_dir: Path, on_installed: Callable[[str], None], config: Config | None = None,
                 prepare: Callable = prepare_update, apply: Callable = apply_update):
        self.data_dir = Path(data_dir)
        self.on_installed = on_installed
        self.config = config or Config()
        self._prepare, self._apply = prepare, apply
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {"state": IDLE}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._state, "log": list(self._state.get("log", []))}

    def _set(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)

    def _log(self, line: str) -> None:
        with self._lock:
            self._state.setdefault("log", []).append(line)

    def start(self, acknowledge: bool = False) -> dict:
        """Start a job. With acknowledge=True, install the build waiting for confirmation."""
        with self._lock:
            if self._state["state"] == RUNNING:
                return {**self._state, "log": list(self._state.get("log", []))}
            version = self._state.get("version") if acknowledge and self._state["state"] == NEEDS_ACK else None
            if acknowledge and version is None:
                raise UpdateError("Nothing is waiting for confirmation.")
            self._state = {"state": RUNNING, "started": _now(), "log": [], "from": None, "version": version}
            self._thread = threading.Thread(target=self._run, args=(version, acknowledge), daemon=True)
            self._thread.start()
        return self.snapshot()

    def wait(self, timeout: float | None = None) -> None:
        """For tests and the CLI: block until the running job ends."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, version: str | None, acknowledge: bool) -> None:
        try:
            active = get_active(self.data_dir)
            self._set(**{"from": active})
            self._log("Checking for a newer build…" if version is None else f"Checking {version} again…")
            try:
                a = self._prepare(self.data_dir, self.config, version=version,
                                  progress=lambda table, msg: self._log(f"{table}: {msg}"))
            except NothingToDo as exc:
                self._set(state=CURRENT, message=str(exc), finished=_now())
                return
            summary = format_report(a.changes, limit=8).strip().splitlines() if a.changes else []
            if len(summary) > SUMMARY_LINES:
                summary = summary[:SUMMARY_LINES] + [f"… {len(summary) - SUMMARY_LINES} more lines (python -m wowcalc.diff --full)"]
            self._set(version=a.version, summary=summary)
            if a.blocked:
                self._set(state=BLOCKED, blockers=list(a.gates.blockers), finished=_now(),
                          message=f"Build {a.version} failed validation and was not installed.")
                return
            if a.needs_ack and not acknowledge:
                self._set(state=NEEDS_ACK, warnings=list(a.needs_ack), finished=_now(),
                          message=f"Build {a.version} passed validation with warnings. Install it anyway?")
                return
            self._log(f"Installing {a.version}…")
            try:
                self._apply(self.data_dir, a.version, acknowledge=acknowledge)
            except AcknowledgementRequired as exc:  # the files changed between the check and now
                self._set(state=NEEDS_ACK, warnings=list(exc.items), finished=_now(),
                          message=f"Build {a.version} needs confirmation.")
                return
            self._log("Loading the new build…")
            try:
                self.on_installed(a.version)
            except Exception as exc:  # noqa: BLE001 - installed on disk, but this server could not load it
                self._set(state=FAILED, finished=_now(), message=(
                    f"Build {a.version} was installed, but this page could not load it ({exc}). "
                    "Restart the calculator; to go back, run: python -m wowcalc.update rollback"))
                return
            self._set(state=INSTALLED, finished=_now(),
                      message=f"Installed {a.version}" + (f" (was {active})." if active else "."))
        except (UpdateError, OSError, ValueError) as exc:
            self._set(state=FAILED, message=str(exc), finished=_now())
        except Exception as exc:  # noqa: BLE001 - a bug must reach the page, not die in a thread
            self._set(state=FAILED, message=f"{type(exc).__name__}: {exc}", finished=_now())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
