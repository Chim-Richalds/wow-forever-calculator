"""Update orchestrator: the backend for the one-click update.

    status     what is active, what is retained, what is available
    prepare    fetch (if needed), validate, apply the safety gates, diff, check
               rules. Changes nothing the app reads: the build waits in staging.
    apply      install a prepared build and make it active. Refuses if a gate
               blocks it, or if there are warnings and they were not acknowledged.
    rollback   make a previously installed build active again.
    complete   add tables to an installed build that was downloaded before they
               were part of the standard fetch. Additive; never rewrites a file.

The *active pointer* (data/active.json) is the single commit point. The app reads
exactly the build it names. Older builds stay on disk, so rollback is a pointer
switch, never a rebuild. Without a pointer file, the newest installed build is
treated as active (the layout this project started with).

apply is ordered so a crash is always safe:
    1. promote staging -> raw/<build>          (idempotent; build only retained)
    2. update rule statuses                    (conservative: may over-flag, never under-flag)
    3. write active.json                       (the commit)

CLI (PYTHONPATH=src):
    python -m wowcalc.update status
    python -m wowcalc.update prepare [--version V]
    python -m wowcalc.update apply [--version V] [--yes]
    python -m wowcalc.update rollback [--to V]
    python -m wowcalc.update complete [--version V]
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import rules as rules_mod
from .builds import (
    Availability, BuildInfo, BuildSourceError, Config, Fetch, check_availability,
    default_fetch, detect_latest, latest_hotfix,
)
from .dataset import DatasetError, list_builds, open_dataset
from .diff import ChangeReport, compare_datasets, format_report
from .fetch import (
    TABLES, FetchBytes, FetchError, _default_fetch_bytes, complete_build, fetch_build, promote_staged, write_json_atomic,
)
from .validate import (
    GateResult, Thresholds, ValidationReport, _print_report, evaluate_gates, validate_dataset,
)

ACTIVE_FILE = "active.json"
HOTFIX_CHECKS_FILE = "hotfix-checks.json"  # build -> newest hotfix time already checked


def latest_target(config: Config, fetch_text: Fetch = default_fetch) -> BuildInfo:
    """The newest build Blizzard serves, with the newest server hotfix Wago has seen for it."""
    build = detect_latest(config, fetch_text)
    return build.with_hotfix(latest_hotfix(build, fetch_text))


def _hotfix_checks(data_dir: Path) -> dict:
    path = Path(data_dir) / HOTFIX_CHECKS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}


def _already_checked(data_dir: Path, target: BuildInfo, active: BuildInfo | None) -> bool:
    """True if these hotfixes were downloaded before and changed none of our tables."""
    return bool(active and target.hotfix and active.base == target.base
                and _hotfix_checks(data_dir).get(target.base, "") >= target.hotfix)


def _record_checked(data_dir: Path, target: BuildInfo) -> None:
    checks = _hotfix_checks(data_dir)
    checks[target.base] = target.hotfix
    write_json_atomic(Path(data_dir) / HOTFIX_CHECKS_FILE, checks)


def _same_tables(data_dir: Path, staged: str, active: str) -> bool:
    """Do the staged build's tables hash the same as the active build's (every table we use)?"""
    def hashes(path: Path) -> dict:
        try:
            tables = json.loads(path.read_text(encoding="utf-8")).get("tables", {})
        except (OSError, ValueError):
            return {}
        return {t: e.get("sha256") for t, e in tables.items()}
    new = hashes(Path(data_dir) / "staging" / staged / "manifest.json")
    old = hashes(Path(data_dir) / "manifests" / f"{active}.json")
    return bool(new) and all(new.get(t) and new.get(t) == old.get(t) for t in TABLES)


class UpdateError(Exception):
    """The requested step cannot proceed. Nothing was changed."""


class NothingToDo(UpdateError):
    """Already in the requested state."""


class AcknowledgementRequired(UpdateError):
    def __init__(self, items: tuple[str, ...]):
        self.items = items
        super().__init__("Acknowledgement required for:\n  - " + "\n  - ".join(items))


# --------------------------------------------------------------------------
# Active pointer
# --------------------------------------------------------------------------

def read_state(data_dir: Path) -> dict | None:
    path = Path(data_dir) / ACTIVE_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise UpdateError(f"{path} is not valid JSON: {exc}") from exc


def get_active(data_dir: Path) -> str | None:
    """The build the app should read, or None if nothing is installed."""
    data_dir = Path(data_dir)
    state = read_state(data_dir)
    if state and state.get("active"):
        active = state["active"]
        if not (data_dir / "raw" / active).is_dir():
            raise UpdateError(f"Active build {active} is not installed under {data_dir / 'raw'}")
        return active
    installed = list_builds(data_dir / "raw")
    return installed[-1].version if installed else None


def _write_active(data_dir: Path, version: str, previous: str | None, via: str,
                  acknowledged=(), invalidated=(), now: datetime | None = None) -> None:
    state = read_state(data_dir) or {"history": []}
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    state.setdefault("history", []).append({
        "build": version, "previous": previous, "via": via, "at": stamp,
        "acknowledged": list(acknowledged), "rulesInvalidated": list(invalidated),
    })
    state["active"] = version
    state["updatedAt"] = stamp
    write_json_atomic(Path(data_dir) / ACTIVE_FILE, state)


def missing_tables(data_dir: Path, version: str) -> tuple[str, ...]:
    """Tables an installed build does not have (it is a "legacy" build)."""
    raw = Path(data_dir) / "raw" / version
    return tuple(t for t in TABLES if not (raw / f"{t}.csv").is_file())


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------

@dataclass
class Assessment:
    version: str
    baseline: str | None
    source: str  # "staging" (newly fetched) or "raw" (already retained)
    candidate: ValidationReport
    gates: GateResult
    changes: ChangeReport | None
    rule_checks: list
    hotfix_warnings: tuple[str, ...] = ()  # talent rows with a hotfix status the calculator does not apply

    @property
    def blocked(self) -> bool:
        return self.gates.blocked

    @property
    def invalidated_rules(self) -> list:
        return [c for c in self.rule_checks if c.invalidates]

    @property
    def needs_ack(self) -> tuple[str, ...]:
        items = list(self.gates.needs_ack)
        inv = self.invalidated_rules
        if inv:
            names = ", ".join(c.rule_id for c in inv[:6]) + (f", +{len(inv) - 6} more" if len(inv) > 6 else "")
            items.append(f"{len(inv)} verified rule(s) will be marked needs_reverification: {names}")
        items.extend(self.hotfix_warnings)
        return tuple(items)


def _default_rules_path(data_dir: Path) -> Path:
    return Path(data_dir) / "rules" / "effect-rules.json"


def assess(
    data_dir: Path,
    version: str,
    baseline: str | None,
    rules_path: Path | None = None,
    thresholds: Thresholds = Thresholds(),
) -> Assessment:
    data_dir = Path(data_dir)
    try:
        candidate_ds = open_dataset(data_dir, version)
        baseline_ds = open_dataset(data_dir, baseline) if baseline else None
        candidate = validate_dataset(candidate_ds)
        baseline_report = validate_dataset(baseline_ds) if baseline_ds else None
        gates = evaluate_gates(candidate, baseline_report, thresholds)
        changes = None
        if baseline_ds is not None and not candidate.missing_tables:  # a legacy baseline may lack newer tables
            changes = compare_datasets(baseline_ds, candidate_ds, baseline_report, candidate)
        doc = rules_mod.load_rules(rules_path or _default_rules_path(data_dir))
        checks = rules_mod.check_rules(doc, candidate_ds) if doc and not candidate.missing_tables else []
    except DatasetError as exc:
        raise UpdateError(str(exc)) from exc
    source = "staging" if (data_dir / "staging" / version).is_dir() else "raw"
    warnings = tuple(_hotfix_status_warnings(data_dir, version, candidate_ds))
    return Assessment(version, baseline, source, candidate, gates, changes, checks, warnings)


def _hotfix_status_warnings(data_dir: Path, version: str, ds) -> list[str]:
    """Talent data with a hotfix status other than added or deleted (Invalidate, NotPublic). The
    calculator keeps such rows as Wago exports them; what the game does with them is not known, so
    one that touches a talent needs a look before installing."""
    path = data_dir / "staging" / version / "manifest.json"
    if not path.is_file():
        path = data_dir / "manifests" / f"{version}.json"
    try:
        tables = json.loads(path.read_text(encoding="utf-8")).get("tables", {})
    except (OSError, ValueError):
        return []
    odd = {t: e.get("hotfixOtherRecords") or {} for t, e in tables.items() if e.get("hotfixOtherRecords")}
    if not odd:
        return []
    talent_spells = {r["SpellID"] for r in ds.rows("TraitDefinition")}
    curves = {r["CurveID"] for r in ds.rows("TraitDefinitionEffectPoints")} - {"", "0"}
    touching = {}
    for table, records in odd.items():
        if table.startswith("Trait"):
            hits = set(records)
        elif table in ("Spell", "SpellName"):
            hits = set(records) & talent_spells
        elif table in ("SpellEffect", "SpellMisc"):
            hits = {r["ID"] for r in ds.rows(table) if r["ID"] in records and r["SpellID"] in talent_spells}
        elif table == "CurvePoint":
            hits = {r["ID"] for r in ds.rows(table) if r["ID"] in records and r["CurveID"] in curves}
        else:
            hits = set()
        if hits:
            touching[table] = sorted(hits, key=lambda x: (len(x), x))
    return [f"{table}: {len(ids)} talent row(s) with a hotfix status the calculator does not apply "
            f"(Invalidate or NotPublic; kept as exported): {', '.join(ids[:8])}{' ...' if len(ids) > 8 else ''}"
            for table, ids in sorted(touching.items())]


def _write_reports(data_dir: Path, a: Assessment, now: datetime | None = None) -> list[Path]:
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    out = Path(data_dir) / "reports"
    written = []
    path = out / f"{a.version}.validation.json"
    write_json_atomic(path, {
        "generatedAt": stamp, "baseline": a.baseline,
        "validation": a.candidate.to_dict(), "gates": a.gates.to_dict(),
        "ruleChecks": [{"ruleId": c.rule_id, "status": c.verification_status, "state": c.state,
                        "changed": list(c.changed), "advisory": list(c.advisory)} for c in a.rule_checks],
    })
    written.append(path)
    if a.changes is not None:
        path = out / f"{a.version}.changes.json"
        write_json_atomic(path, {"generatedAt": stamp, "changes": a.changes.to_dict()})
        written.append(path)
    return written


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def prepare_update(
    data_dir: Path,
    config: Config,
    version: str | None = None,
    fetch_text: Fetch = default_fetch,
    fetch_bytes: FetchBytes = _default_fetch_bytes,
    rules_path: Path | None = None,
    thresholds: Thresholds = Thresholds(),
    now: datetime | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> Assessment:
    """Get a build ready for review. Never changes the active build."""
    data_dir = Path(data_dir)
    active = get_active(data_dir)
    active_info = BuildInfo.from_version(active) if active else None

    if version is None:
        try:
            target = latest_target(config, fetch_text)
            if _already_checked(data_dir, target, active_info):
                raise NothingToDo(f"Build {active} is current (hotfixes up to {target.hotfix} change none of the calculator's tables).")
            version = target.version
        except BuildSourceError as exc:
            raise UpdateError(f"Could not determine the latest build: {exc}") from exc
        detected = True
    else:
        detected = False
    try:
        target = BuildInfo.from_version(version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc

    if active_info and target.order == active_info.order:
        lacking = missing_tables(data_dir, version)
        hint = (f" It lacks {len(lacking)} table(s) newer builds carry; run: python -m wowcalc.update complete"
                if lacking else "")
        raise NothingToDo(f"Build {version} is already active.{hint}")
    if active_info and target.order < active_info.order:
        if detected:
            raise NothingToDo(f"Active build {active} is newer than the latest detected build {version}.")
        raise UpdateError(f"Build {version} is older than the active build {active}. Use rollback instead.")

    retained = (data_dir / "raw" / version).is_dir()
    staged = (data_dir / "staging" / version).is_dir()
    if not retained and not staged:
        if check_availability(version, config, fetch_text) is Availability.NOT_AVAILABLE:
            raise UpdateError(f"Wago has not exported data for {version} yet. Try again later.")
        try:
            result = fetch_build(version, config, data_dir, fetch=fetch_bytes, progress=progress, now=now)
        except FetchError as exc:
            raise UpdateError(str(exc)) from exc
        if not result.ok:
            failed = "; ".join(f"{f['table']}: {f['error']}" for f in result.failures)
            raise UpdateError(f"Download failed, nothing was kept. {failed}")
        if active and target.hotfix and BuildInfo.from_version(active).base == target.base and _same_tables(data_dir, version, active):
            # Hotfixes that touch none of our tables: nothing to install. Remember the check.
            shutil.rmtree(data_dir / "staging" / version)
            _record_checked(data_dir, target)
            raise NothingToDo(f"Build {active} is current (hotfixes up to {target.hotfix} change none of the calculator's tables).")

    assessment = assess(data_dir, version, active, rules_path, thresholds)
    _write_reports(data_dir, assessment, now)
    return assessment


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplyResult:
    version: str
    previous: str | None
    promoted: bool
    acknowledged: tuple[str, ...]
    rule_transitions: tuple[dict, ...]


def apply_update(
    data_dir: Path,
    version: str | None = None,
    acknowledge: bool = False,
    rules_path: Path | None = None,
    thresholds: Thresholds = Thresholds(),
    now: datetime | None = None,
) -> ApplyResult:
    data_dir = Path(data_dir)
    if version is None:
        staged = list_builds(data_dir / "staging")
        if not staged:
            raise UpdateError("Nothing is prepared. Run prepare first.")
        version = staged[-1].version
    try:
        target = BuildInfo.from_version(version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc

    active = get_active(data_dir)
    if active:
        active_info = BuildInfo.from_version(active)
        if target.order == active_info.order:
            raise NothingToDo(f"Build {version} is already active.")
        if target.order < active_info.order:
            raise UpdateError(f"Build {version} is older than the active build {active}. Use rollback instead.")

    in_staging = (data_dir / "staging" / version).is_dir()
    in_raw = (data_dir / "raw" / version).is_dir()
    if not in_staging and not in_raw:
        raise UpdateError(f"Build {version} is not prepared. Run prepare first.")

    # Re-assess from disk rather than trusting an earlier result: the files are
    # what will be installed.
    rules_path = rules_path or _default_rules_path(data_dir)
    assessment = assess(data_dir, version, active, rules_path, thresholds)
    if assessment.blocked:
        raise UpdateError("Blocked. This build must not be installed:\n  - " + "\n  - ".join(assessment.gates.blockers))
    if assessment.needs_ack and not acknowledge:
        raise AcknowledgementRequired(assessment.needs_ack)

    # Without a pointer file the newest folder in raw/ counts as active. Pin the
    # current build first, so that adding a build to raw/ can never activate it
    # by itself, even if this process dies before the pointer is written.
    if active and read_state(data_dir) is None:
        _write_active(data_dir, active, None, via="pinned", now=now)

    promoted = False
    if in_staging:
        try:
            promote_staged(version, data_dir)
        except FetchError as exc:
            raise UpdateError(str(exc)) from exc
        promoted = True

    transitions: list[dict] = []
    doc = rules_mod.load_rules(rules_path)
    if doc is not None:
        new_doc, transitions = rules_mod.apply_transitions(doc, assessment.rule_checks, version, now)
        rules_mod.save_rules(rules_path, new_doc)

    _write_active(
        data_dir, version, active, via="apply", acknowledged=assessment.needs_ack,
        invalidated=[t["ruleId"] for t in transitions], now=now,
    )
    return ApplyResult(version, active, promoted, assessment.needs_ack, tuple(transitions))


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RollbackResult:
    version: str
    previous: str
    missing_tables: tuple[str, ...] = ()  # legacy build: lacks tables newer builds carry


def rollback(data_dir: Path, to: str | None = None, now: datetime | None = None) -> RollbackResult:
    """Make a retained build active again. Rule statuses are left as they are:
    rules flagged by a newer build stay flagged until re-verified."""
    data_dir = Path(data_dir)
    active = get_active(data_dir)
    if active is None:
        raise UpdateError("No build is installed.")
    if to is None:
        state = read_state(data_dir) or {}
        previous = [h.get("previous") for h in state.get("history", []) if h.get("build") == active]
        to = previous[-1] if previous and previous[-1] else None
        if to is None:
            raise UpdateError("No previous build is recorded. Pass the build to roll back to.")
    try:
        BuildInfo.from_version(to)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    if to == active:
        raise NothingToDo(f"Build {to} is already active.")
    if not (data_dir / "raw" / to).is_dir():
        raise UpdateError(f"Build {to} is not retained under {data_dir / 'raw'}.")
    _write_active(data_dir, to, active, via="rollback", now=now)
    missing = tuple(t for t in TABLES if not (data_dir / "raw" / to / f"{t}.csv").is_file())
    return RollbackResult(to, active, missing)


# --------------------------------------------------------------------------
# complete
# --------------------------------------------------------------------------

def complete(
    data_dir: Path,
    config: Config,
    version: str | None = None,
    fetch_bytes: FetchBytes = _default_fetch_bytes,
    now: datetime | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> tuple[str, ...]:
    """Add the tables an installed build lacks. Returns the tables added.

    Defaults to the active build. Does not change which build is active.
    """
    data_dir = Path(data_dir)
    version = version or get_active(data_dir)
    if version is None:
        raise UpdateError("No build is installed.")
    try:
        result = complete_build(version, config, data_dir, fetch=fetch_bytes, now=now, progress=progress)
    except (FetchError, ValueError) as exc:
        raise UpdateError(str(exc)) from exc
    if not result.ok:
        failed = "; ".join(f"{f['table']}: {f['error']}" for f in result.failures)
        raise UpdateError(f"Download failed, nothing was added to {version}. {failed}")
    if not result.added:
        raise NothingToDo(f"Build {version} already has all {len(TABLES)} tables.")
    return result.added


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    active: str | None
    retained: tuple[str, ...]
    staged: tuple[str, ...]
    latest: str | None
    latest_error: str
    message: str
    incomplete: dict[str, tuple[str, ...]] = field(default_factory=dict)  # retained build -> tables it lacks


def status(data_dir: Path, config: Config, fetch_text: Fetch = default_fetch) -> Status:
    data_dir = Path(data_dir)
    active = get_active(data_dir)
    retained = tuple(b.version for b in list_builds(data_dir / "raw"))
    staged = tuple(b.version for b in list_builds(data_dir / "staging"))
    latest, err = None, ""
    try:
        target = latest_target(config, fetch_text)
        active_info = BuildInfo.from_version(active) if active else None
        latest = active if _already_checked(data_dir, target, active_info) else target.version
    except BuildSourceError as exc:
        err = str(exc)

    if latest is None:
        msg = f"Could not check for updates: {err}"
    elif active is None:
        msg = f"No build is installed. Latest is {latest}."
    elif BuildInfo.from_version(latest).order <= BuildInfo.from_version(active).order:
        msg = f"Active build {active} is current."
    elif latest in retained:
        msg = f"Build {latest} is installed but not active. Apply it to switch."
    elif latest in staged:
        msg = f"Build {latest} is prepared. Review it, then apply."
    else:
        msg = f"Build {latest} is available (active: {active}). Run prepare."
    incomplete = {v: m for v in retained if (m := missing_tables(data_dir, v))}
    if active in incomplete:
        msg += (f" The active build lacks {len(incomplete[active])} table(s) newer builds carry; "
                "run: python -m wowcalc.update complete")
    return Status(active, retained, staged, latest, err, msg, incomplete)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_assessment(a: Assessment) -> None:
    _print_report(a.candidate, a.gates, a.baseline)
    if a.changes is not None:
        print(format_report(a.changes, limit=6))
    if a.rule_checks:
        print("\nRules:")
        for c in a.rule_checks:
            flag = "  <- will need re-verification" if c.invalidates else ""
            changed = f" ({', '.join(c.changed)})" if c.changed else ""
            print(f"  {c.rule_id:40} {c.verification_status:20} {c.state}{changed}{flag}")


def main(argv: list[str] | None = None, fetch_text: Fetch = default_fetch,
         fetch_bytes: FetchBytes = _default_fetch_bytes) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Check for, review, apply and roll back build updates.")
    ap.add_argument("command", choices=["status", "prepare", "apply", "rollback", "complete"])
    ap.add_argument("--version", help="prepare/apply/complete: a specific build (default: latest / newest prepared / active)")
    ap.add_argument("--to", help="rollback: build to return to (default: the previous build)")
    ap.add_argument("--yes", action="store_true", help="apply: acknowledge warnings")
    ap.add_argument("--product", default=Config.product)
    ap.add_argument("--region", default=Config.region)
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--rules", type=Path, help="default: <data-dir>/rules/effect-rules.json")
    ap.add_argument("--max-row-drop", type=float, default=Thresholds.max_row_drop)
    args = ap.parse_args(argv)
    config = Config(product=args.product, region=args.region)
    thresholds = Thresholds(max_row_drop=args.max_row_drop)

    try:
        if args.command == "status":
            s = status(args.data_dir, config, fetch_text)
            print(f"Active:   {s.active or '(none)'}")
            shown = [f"{v} (lacks {len(s.incomplete[v])} table(s))" if v in s.incomplete else v for v in s.retained]
            print(f"Retained: {', '.join(shown) or '(none)'}")
            print(f"Prepared: {', '.join(s.staged) or '(none)'}")
            print(f"Latest:   {s.latest or '(unknown)'}")
            print(f"\n{s.message}")
            return 0

        if args.command == "prepare":
            a = prepare_update(
                args.data_dir, config, args.version, fetch_text=fetch_text, fetch_bytes=fetch_bytes,
                rules_path=args.rules, thresholds=thresholds,
                progress=lambda table, note: print(f"  {table}: {note}"),
            )
            _print_assessment(a)
            print(f"\nReports written to {args.data_dir / 'reports'}")
            if a.blocked:
                print("\nThis build is BLOCKED and cannot be applied.")
                return 1
            hint = " --yes" if a.needs_ack else ""
            print(f"\nReady. To install: python -m wowcalc.update apply --version {a.version}{hint}")
            return 0

        if args.command == "apply":
            r = apply_update(args.data_dir, args.version, acknowledge=args.yes,
                             rules_path=args.rules, thresholds=thresholds)
            print(f"Active build is now {r.version} (was {r.previous or 'none'}).")
            if r.promoted:
                print("Installed from staging; the previous build is retained for rollback.")
            for t in r.rule_transitions:
                print(f"  rule {t['ruleId']}: {t['from']} -> {t['to']} ({', '.join(t['changed'])})")
            return 0

        if args.command == "complete":
            added = complete(args.data_dir, config, args.version, fetch_bytes=fetch_bytes,
                             progress=lambda table, note: print(f"  {table}: {note}"))
            print(f"\nAdded {len(added)} table(s). Nothing already installed was changed.")
            return 0

        r = rollback(args.data_dir, args.to)
        print(f"Active build is now {r.version} (was {r.previous}).")
        if r.missing_tables:
            print(f"WARNING: {r.version} is a legacy build without {len(r.missing_tables)} table(s) "
                  f"newer builds carry ({', '.join(r.missing_tables[:4])}"
                  f"{', ...' if len(r.missing_tables) > 4 else ''}). Features that need them will not work on it.")
        return 0

    except NothingToDo as exc:
        print(exc)
        return 0
    except UpdateError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
