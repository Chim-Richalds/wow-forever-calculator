"""Fetch stage: download one build's DB2 tables from Wago.Tools into staging.

Disk states for a build, all under `data_dir`:

    staging/<v>.partial/        download in progress (or crashed; discarded next run)
    staging/<v>/                every table downloaded and structurally checked, with
                                a manifest.json. NOT yet validated, NOT installed.
    raw/<v>/ + manifests/<v>.json
                                installed. Reached only through promote_staged().

Nothing under raw/ changes until promote_staged(). A failed or interrupted fetch
never touches installed data, and one bad table discards the whole download.

CLI (PYTHONPATH=src):
    python -m wowcalc.fetch                 fetch the latest build (detected)
    python -m wowcalc.fetch 1.60.1.70374    fetch a specific build
    python -m wowcalc.fetch --promote       ...and install it (see warning below)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import urllib.error
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import transport
from .builds import BuildInfo, Config, Status, check_for_updates

WAGO_TABLE_URL = "https://wago.tools/db2/{table}/csv?build={version}"
SOURCE = "Wago.Tools DB2 CSV exports"
DOWNLOAD_TIMEOUT = 60.0

# Tables every build has carried since the first fetch.
BASE_TABLES: tuple[str, ...] = (
    "TraitTree",
    "TraitNode",
    "TraitNodeEntry",
    "TraitNodeXTraitNodeEntry",
    "TraitDefinition",
    "TraitDefinitionEffectPoints",
    "TraitEdge",
    "CurvePoint",
    "SpellName",
    "Spell",
    "SpellEffect",
    "ChrClasses",
    "ChrRaces",
)

# Added for the calculator itself: which class owns which tree, how talents
# unlock, and how many points a character has. Builds fetched before these were
# added are "legacy": usable for diffs and rollback, but not complete.
EXTENDED_TABLES: tuple[str, ...] = (
    "SkillLineXTraitTree",
    "SkillRaceClassInfo",
    "TraitCond",
    "TraitNodeGroup",
    "TraitNodeGroupXTraitNode",
    "TraitNodeGroupXTraitCond",
    "TraitNodeXTraitCond",
    "TraitCurrency",
    "TraitCurrencySource",
    "TraitTreeXTraitCurrency",
    "TraitCost",
    "TraitTreeXTraitCost",
    "CharBaseInfo",  # which race and class combinations exist
)

TABLES: tuple[str, ...] = BASE_TABLES + EXTENDED_TABLES

FetchBytes = Callable[[str], transport.Response]
Progress = Callable[[str, str], None]


class FetchError(Exception):
    """The fetch or promote could not proceed (as opposed to a table failing)."""


@dataclass(frozen=True)
class FetchResult:
    version: str
    ok: bool
    staged_dir: Path | None
    manifest: dict
    failures: tuple[dict, ...] = field(default_factory=tuple)


def _default_fetch_bytes(url: str) -> transport.Response:
    return transport.get(url, timeout=DOWNLOAD_TIMEOUT, retries=1)


# --------------------------------------------------------------------------
# Per-table validation
# --------------------------------------------------------------------------

def validate_csv(content: bytes) -> dict:
    """Structural check of one downloaded table. Raises ValueError if unusable.

    Returns facts for the manifest. Malformed rows and duplicate headers are
    reported, not rejected: judging them is the validation stage's job.
    """
    if not content:
        raise ValueError("empty response")
    if content[:500].lstrip().startswith(b"<"):
        raise ValueError("response looks like HTML, not CSV")

    text = content.decode("utf-8-sig")  # UnicodeDecodeError is a ValueError
    try:
        # newline="" so quoted fields containing line breaks stay one record
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ValueError("missing or invalid CSV header")
        rows = 0
        malformed = 0
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            rows += 1
            if len(row) != len(header):
                malformed += 1
    except csv.Error as exc:
        raise ValueError(f"CSV parse error: {exc}") from exc

    if rows == 0:
        raise ValueError("no data rows")

    counts = Counter(header)
    return {
        "columns": header,
        "rows": rows,
        "malformedRows": malformed,
        "duplicateHeaders": sorted(name for name, n in counts.items() if n > 1),
    }


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_table(table: str, version: str, data_dir: Path, fetch: FetchBytes):
    """Download and structurally check one table.

    Returns (body, entry). On success `body` is the file content and `entry` is
    its manifest record; on failure `body` is None and `entry` records the error.
    """
    url = WAGO_TABLE_URL.format(table=table, version=version)
    try:
        response = fetch(url)
        facts = validate_csv(response.body)
    except urllib.error.HTTPError as exc:  # must precede OSError (subclass)
        exc.close()
        return None, {"url": url, "status": "failed", "error": f"HTTP {exc.code} {exc.reason}"}
    except (OSError, ValueError) as exc:
        return None, {"url": url, "status": "failed", "error": str(exc) or type(exc).__name__}
    return response.body, {
        "url": url,
        "file": (Path(Path(data_dir).name) / "raw" / version / f"{table}.csv").as_posix(),
        "sha256": _sha256_bytes(response.body),
        "bytes": len(response.body),
        **facts,
        "contentType": response.content_type,
        "status": "downloaded-needs-validation",
    }


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_build(
    version: str,
    config: Config,
    data_dir: Path,
    fetch: FetchBytes = _default_fetch_bytes,
    tables: tuple[str, ...] = TABLES,
    overwrite: bool = False,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> FetchResult:
    """Download every table for `version` into staging.

    Returns ok=True with the staged directory only if every table downloaded and
    passed validate_csv. Otherwise returns ok=False, nothing is left on disk, and
    `failures` lists every table that failed (all tables are attempted so one run
    reports everything wrong). Raises FetchError if the build is already
    installed or staged, and ValueError for a malformed version string.
    """
    BuildInfo.from_version(version)  # raises ValueError if malformed
    data_dir = Path(data_dir)
    installed = data_dir / "raw" / version
    staging_root = data_dir / "staging"
    staged = staging_root / version
    partial = staging_root / f"{version}.partial"

    if installed.exists():
        raise FetchError(f"Build {version} is already installed ({installed}).")
    if staged.exists():
        if not overwrite:
            raise FetchError(
                f"Build {version} is already staged ({staged}). "
                "Promote it, or fetch again with overwrite."
            )
        shutil.rmtree(staged)
    if partial.exists():
        shutil.rmtree(partial)  # left by an interrupted run
    partial.mkdir(parents=True)

    when = now or datetime.now(timezone.utc)
    manifest: dict = {
        "product": config.product,
        "build": version,
        "retrievedAt": when.isoformat(),
        "source": SOURCE,
        "tables": {},
    }
    failures: list[dict] = []

    try:
        for table in tables:
            body, entry = _download_table(table, version, data_dir, fetch)
            manifest["tables"][table] = entry
            if body is None:
                failures.append({"table": table, "error": entry["error"]})
                if progress:
                    progress(table, f"FAILED: {entry['error']}")
            else:
                (partial / f"{table}.csv").write_bytes(body)
                if progress:
                    progress(table, f"{entry['rows']:,} rows")

        manifest["failures"] = failures
        if failures:
            return FetchResult(version, False, None, manifest, tuple(failures))

        (partial / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(partial, staged)  # commit: the package appears all at once
        return FetchResult(version, True, staged, manifest, ())
    finally:
        if partial.exists():  # failure, or an exception mid-download
            shutil.rmtree(partial, ignore_errors=True)


# --------------------------------------------------------------------------
# Complete an installed build
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CompleteResult:
    version: str
    ok: bool
    added: tuple[str, ...] = ()
    failures: tuple[dict, ...] = ()


def _write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def complete_build(
    version: str,
    config: Config,
    data_dir: Path,
    fetch: FetchBytes = _default_fetch_bytes,
    tables: tuple[str, ...] = TABLES,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> CompleteResult:
    """Add tables an installed build does not have yet. Purely additive.

    Builds downloaded before a table was added to TABLES are "legacy". Wago's
    exports are stable for a given build (checked: refetching reproduces every
    file byte for byte), so the missing tables can be fetched later and belong to
    the same build. Existing files are never rewritten. Before adding anything,
    every recorded file is re-hashed, so a build that has been altered is refused.
    All missing tables are downloaded and checked first; only then are they moved
    in, and the manifest is rewritten last. If any download fails, nothing is added.
    """
    BuildInfo.from_version(version)
    data_dir = Path(data_dir)
    raw = data_dir / "raw" / version
    manifest_path = data_dir / "manifests" / f"{version}.json"
    if not raw.is_dir():
        raise FetchError(f"Build {version} is not installed ({raw}).")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FetchError(f"Manifest for {version} unreadable ({manifest_path}): {exc}") from exc

    entries = manifest.get("tables") or {}
    problems: list[str] = []
    for table, entry in entries.items():
        if entry.get("status") == "failed":
            continue
        path = raw / f"{table}.csv"
        if not path.is_file():
            problems.append(f"{table}: file missing")
        elif _sha256_file(path) != entry.get("sha256"):
            problems.append(f"{table}: hash mismatch")
    if problems:
        raise FetchError("Installed data failed integrity check: " + "; ".join(problems))

    # Unrecorded means not in the manifest. A file present without a record (left
    # by an interrupted run) is fetched again and replaced.
    missing = [t for t in tables if t not in entries or entries[t].get("status") == "failed"]
    if not missing:
        return CompleteResult(version, True, ())

    work = data_dir / "staging" / f"{version}.complete.partial"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        new_entries: dict[str, dict] = {}
        failures: list[dict] = []
        for table in missing:
            body, entry = _download_table(table, version, data_dir, fetch)
            if body is None:
                failures.append({"table": table, "error": entry["error"]})
                if progress:
                    progress(table, f"FAILED: {entry['error']}")
            else:
                (work / f"{table}.csv").write_bytes(body)
                new_entries[table] = entry
                if progress:
                    progress(table, f"{entry['rows']:,} rows")
        if failures:
            return CompleteResult(version, False, (), tuple(failures))

        stamp = (now or datetime.now(timezone.utc)).isoformat()
        for table, entry in new_entries.items():
            os.replace(work / f"{table}.csv", raw / f"{table}.csv")
            entry["addedAfterInstall"] = True
            entry["addedAt"] = stamp
        manifest["tables"] = {**entries, **new_entries}
        manifest.setdefault("completions", []).append({"at": stamp, "tables": list(new_entries)})
        _write_json_atomic(manifest_path, manifest)
        return CompleteResult(version, True, tuple(new_entries))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# Promote
# --------------------------------------------------------------------------

def promote_staged(version: str, data_dir: Path) -> Path:
    """Move a staged build into raw/ and publish its manifest.

    Re-hashes every staged file against the manifest first, so a file altered or
    truncated since download blocks installation. Callers are expected to run
    the validation gates before this; this function checks integrity only.
    Order is crash-safe: the manifest is published first (an orphan manifest is
    harmless), then the directory is renamed in one step.
    """
    BuildInfo.from_version(version)
    data_dir = Path(data_dir)
    staged = data_dir / "staging" / version
    final = data_dir / "raw" / version

    if not staged.is_dir():
        raise FetchError(f"Build {version} is not staged ({staged}).")
    if final.exists():
        raise FetchError(f"Build {version} is already installed ({final}).")

    manifest_file = staged / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FetchError(f"Staged manifest unreadable: {exc}") from exc

    entries = manifest.get("tables") or {}
    if not entries:
        raise FetchError("Staged manifest lists no tables.")
    problems: list[str] = []
    for table, entry in entries.items():
        path = staged / f"{table}.csv"
        if entry.get("status") == "failed":
            problems.append(f"{table}: recorded as failed")
        elif not path.is_file():
            problems.append(f"{table}: file missing")
        elif _sha256_file(path) != entry.get("sha256"):
            problems.append(f"{table}: hash mismatch")
    if problems:
        raise FetchError("Staged data failed integrity check: " + "; ".join(problems))

    manifests_dir = data_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(manifest_file, manifests_dir / f"{version}.json")
    os.replace(staged, final)
    return final


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Fetch a build's data into staging.")
    ap.add_argument("version", nargs="?", help="e.g. 1.60.1.70374 (default: latest detected)")
    ap.add_argument("--product", default=Config.product)
    ap.add_argument("--region", default=Config.region)
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing staged copy")
    ap.add_argument(
        "--promote", action="store_true",
        help="install after fetching. WARNING: only download integrity is checked; "
             "the validation gates are not implemented yet",
    )
    args = ap.parse_args()
    config = Config(product=args.product, region=args.region)

    version = args.version
    if version is None:
        check = check_for_updates(config, args.data_dir)
        print(check.message)
        if check.status is Status.UP_TO_DATE:
            return 0
        if check.status not in (Status.UPDATE_READY, Status.UPDATE_UNVERIFIED):
            return 1
        version = check.latest.version

    print(f"\nFetching {version} ...")
    try:
        result = fetch_build(
            version, config, args.data_dir, overwrite=args.overwrite,
            progress=lambda table, note: print(f"  {table}: {note}"),
        )
    except (FetchError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if not result.ok:
        print(f"\nFAILED: {len(result.failures)} table(s) failed. Nothing was kept.")
        return 1

    total = sum(t["bytes"] for t in result.manifest["tables"].values())
    print(f"\nStaged {len(result.manifest['tables'])} tables ({total:,} bytes): {result.staged_dir}")

    if args.promote:
        print("WARNING: promoting without validation gates (not implemented yet).")
        try:
            final = promote_staged(version, args.data_dir)
        except FetchError as exc:
            print(f"ERROR: {exc}")
            return 1
        print(f"Installed: {final}")
    else:
        print("Not installed yet. Re-run with --promote to install.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
