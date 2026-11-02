"""Build detection and data-availability checks.

Two independent questions, answered by two independent sources:

1. Detect: what is the newest build Blizzard is serving for our product?
   (TACT `versions` endpoint, official, current build only.)
2. Availability: has Wago.Tools exported that build's tables yet?
   (Probe one small table; the same endpoint the fetch step uses.)

All network access goes through an injectable `fetch(url) -> str` so the logic
is testable offline and a hosted deployment can substitute its own client.

Run as a script for a status report:

    python -m wowcalc.builds            (with PYTHONPATH=src)
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from . import transport

Fetch = Callable[[str], str]

TACT_URL = "http://{region}.patch.battle.net:1119/{product}/versions"
WAGO_BUILDS_URL = "https://wago.tools/api/builds"
WAGO_TABLE_URL = "https://wago.tools/db2/{table}/csv?build={version}&useHotfixes=1"
# A dataset ID: the game build, plus "-hf<YYYYMMDDHHMM>" when it includes the server's hotfixes
# up to that time (UTC, as first seen by Wago). Hotfixes change talents without a new build.
VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+\.(\d+))(?:-hf(\d{12}))?$")
WAGO_HOTFIXES_URL = "https://wago.tools/hotfixes"  # ?search=<text>&page=<n>; 25 rows a page, newest first
HOTFIX_ADDED, HOTFIX_DELETED = 1, 2  # Wago's status codes (3 Invalidate, 4 NotPublic are not applied)
TIMEOUT = 20


# --------------------------------------------------------------------------
# Configuration and data types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Which game product we track. Never hard-code these in logic."""

    # WoW Forever's own product. It replaced wow_classic_beta, which stopped at 1.60.1.70532
    # (2026-10-22) while wowf carried on (70245 from 2026-10-13, 70707 on 2026-10-30).
    product: str = "wowf"
    region: str = "us"
    probe_table: str = "TraitTree"  # small table used to test availability


class BuildSourceError(Exception):
    """Raised when the build source cannot give a usable answer."""


@dataclass(frozen=True)
class BuildInfo:
    version: str  # e.g. "1.60.1.70425" or "1.60.1.70707-hf202610311059"
    build_id: int  # e.g. 70425
    region: str = ""
    seqn: int | None = None
    hotfix: str = ""  # YYYYMMDDHHMM of the newest hotfix included, or "" for none

    @property
    def base(self) -> str:
        """The game build without the hotfix part, e.g. "1.60.1.70707"."""
        return self.version.split("-hf")[0]

    @property
    def order(self) -> tuple[int, str]:
        """Sort key: build first, then hotfix time (no hotfixes sorts first)."""
        return (self.build_id, self.hotfix)

    def with_hotfix(self, stamp: str) -> "BuildInfo":
        return BuildInfo.from_version(f"{self.base}-hf{stamp}" if stamp else self.base, self.region, self.seqn)

    @property
    def version_line(self) -> str:
        """First three components, e.g. "1.60.1"."""
        return ".".join(self.version.split(".")[:3])

    @classmethod
    def from_version(cls, version: str, region: str = "", seqn: int | None = None) -> "BuildInfo":
        match = VERSION_RE.match(version.strip())
        if not match:
            raise ValueError(f"Not a valid version string: {version!r}")
        return cls(version.strip(), int(match.group(2)), region, seqn, match.group(3) or "")


class Availability(Enum):
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"  # source answered: data is not there
    UNKNOWN = "unknown"  # could not tell (network error, odd response)


class Status(Enum):
    UP_TO_DATE = "up_to_date"
    UPDATE_READY = "update_ready"  # newer build, data confirmed available
    UPDATE_PENDING_DATA = "update_pending_data"  # newer build, data not exported yet
    UPDATE_UNVERIFIED = "update_unverified"  # newer build, availability unknown
    DETECTION_FAILED = "detection_failed"


@dataclass(frozen=True)
class UpdateCheck:
    status: Status
    latest: BuildInfo | None
    installed: BuildInfo | None
    availability: Availability | None
    line_changed: bool
    message: str
    newer_builds: tuple[str, ...] = ()  # published builds after the installed one, oldest first


# --------------------------------------------------------------------------
# HTTP (stdlib only)
# --------------------------------------------------------------------------

def default_fetch(url: str, retries: int = 1) -> str:
    """GET `url` and return the body as text (see transport.get for retry rules)."""
    return transport.get(url, timeout=TIMEOUT, retries=retries).body.decode(
        "utf-8", errors="replace"
    )


# --------------------------------------------------------------------------
# Detection: Blizzard TACT
# --------------------------------------------------------------------------

def parse_bpsv(text: str) -> tuple[list[str], list[dict[str, str]], int | None]:
    """Parse Blizzard's pipe-separated format.

    Header line is `Name!TYPE:length|...`; a `## seqn = N` comment line carries
    the manifest sequence number.
    """
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    seqn: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            if "seqn" in line and "=" in line:
                try:
                    seqn = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            continue
        parts = line.split("|")
        if not columns:
            columns = [p.split("!", 1)[0] for p in parts]
            continue
        rows.append(dict(zip(columns, parts)))
    return columns, rows, seqn


def build_from_version_string(version: str) -> BuildInfo:
    """Manual-entry fallback: build info from a typed version like 1.60.1.70425."""
    return BuildInfo.from_version(version)


def detect_latest(config: Config, fetch: Fetch = default_fetch) -> BuildInfo:
    url = TACT_URL.format(region=config.region, product=config.product)
    try:
        text = fetch(url)
    except OSError as exc:  # URLError, HTTPError, timeouts are all OSError
        raise BuildSourceError(f"TACT request failed: {exc}") from exc

    _, rows, seqn = parse_bpsv(text)
    for row in rows:
        if row.get("Region") != config.region:
            continue
        try:
            info = BuildInfo.from_version(row["VersionsName"], config.region, seqn)
            if str(info.build_id) != row.get("BuildId", ""):
                raise ValueError(
                    f"BuildId {row.get('BuildId')!r} does not match "
                    f"VersionsName {row['VersionsName']!r}"
                )
        except (KeyError, ValueError) as exc:
            raise BuildSourceError(f"Unusable TACT row: {exc}") from exc
        return info
    raise BuildSourceError(
        f"TACT response has no row for region {config.region!r} "
        f"(product {config.product!r})"
    )


# --------------------------------------------------------------------------
# Availability: Wago.Tools
# --------------------------------------------------------------------------

def latest_hotfix(build: BuildInfo, fetch: Fetch = default_fetch) -> str:
    """YYYYMMDDHHMM of the newest server hotfix Wago has seen for this build, or "" if none.

    Read from the first page of Wago's hotfix list (newest first), searched by build number.
    The search also matches other builds whose IDs contain the number, so rows are filtered
    by their build field."""
    rows, _, _ = hotfix_page(str(build.build_id), 1, fetch)
    seen = [_stamp(r["created_at"]) for r in rows if r["build"] == build.build_id]
    return max(seen) if seen else ""


def hotfix_page(search: str, page: int, fetch: Fetch = default_fetch) -> tuple[list[dict], int, int]:
    """One page of Wago's hotfix list: (rows, last page, total rows). Raises BuildSourceError.

    The search is one phrase looked for in a line "<locale> <push> <record> <build> <table> <data>",
    so "70707 spelleffect" finds build 70707's SpellEffect hotfixes and "5 70707 spelleffect" those
    whose record ID ends in 5."""
    url = WAGO_HOTFIXES_URL + "?" + urllib.parse.urlencode({"search": search, "page": page})
    try:
        text = fetch(url)
    except OSError as exc:
        raise BuildSourceError(f"Hotfix list request failed: {exc}") from exc
    match = re.search(r'data-page="([^"]+)"', text)
    if not match:
        raise BuildSourceError("Hotfix list page has no data (the page layout may have changed)")
    try:
        listing = json.loads(html.unescape(match.group(1)))["props"]["hotfixes"]
        rows = [{"id": str(r.get("id", "")), "build": int(r["build"]), "table": str(r["table_name"]), "record": str(r["record_id"]),
                 "push": int(r.get("push_id") or 0), "status": int(r["status"]), "created_at": str(r["created_at"]),
                 "locale": str(r.get("locale") or "")}
                for r in listing["data"]]
        return rows, int(listing.get("last_page") or 1), int(listing.get("total", len(rows)))
    except (ValueError, KeyError, TypeError) as exc:
        raise BuildSourceError(f"Unusable hotfix list: {exc}") from exc


def _stamp(created_at: str) -> str:
    stamp = re.sub(r"\D", "", created_at)[:12]  # "2026-10-31 10:59:19" -> "202610311059"
    if len(stamp) != 12:
        raise BuildSourceError(f"Unusable hotfix time {created_at!r}")
    return stamp


def hotfix_statuses(build: BuildInfo, table: str, fetch: Fetch = default_fetch,
                    family: str | None = None) -> dict[str, int]:
    """Record ID -> status of its newest hotfix, for one table of a hotfixed dataset, counting
    only hotfixes up to the dataset's hotfix time. Wago's hotfixed exports apply added and changed
    rows but keep deleted ones, so the caller drops records whose status is HOTFIX_DELETED.

    `family` is the shortest table name to search by when several tables share a beginning
    ("spell" for SpellEffect, SpellMisc ...): Wago's search cannot end at a table name, so one
    read of the family serves every table in it (kept for the rest of the run)."""
    if not build.hotfix:
        return {}
    phrase = f"{build.build_id} {(family or table).lower()}"
    key = (phrase, build.hotfix)
    if key not in _HOTFIX_READS:
        _HOTFIX_READS[key] = _all_hotfix_rows(phrase, fetch)
    newest: dict[str, tuple[int, int]] = {}
    for r in _HOTFIX_READS[key]:
        if r["build"] == build.build_id and r["table"].lower() == table.lower() and _stamp(r["created_at"]) <= build.hotfix:
            if r["record"] not in newest or r["push"] > newest[r["record"]][0]:
                newest[r["record"]] = (r["push"], r["status"])
    return {record: status for record, (_, status) in newest.items()}


_HOTFIX_READS: dict[tuple[str, str], list[dict]] = {}   # (search phrase, hotfix time) -> rows, for one run


def clear_hotfix_reads() -> None:
    """Forget shared hotfix list reads (start and end of each download run)."""
    _HOTFIX_READS.clear()
_PAGE_READ_LIMIT = 4   # an answer of up to this many pages is read page by page, then checked


def _all_hotfix_rows(phrase: str, fetch: Fetch) -> list[dict]:
    """Every row of Wago's hotfix list matching `phrase`.

    Rows of one push share a timestamp and the list is sorted by it alone, so its pages are not
    stable: paging can skip some rows and repeat others. A short answer is read page by page and
    kept if the rows add up to the total Wago reports; otherwise the search is narrowed by the end
    of the record ID ("7 70707 spell", then "17 70707 spell", ...) and each part read the same way.
    If the rows still do not add up, this fails rather than return an incomplete list."""
    def key(r):
        return (r["id"], r["table"], r["record"], r["push"], r["locale"], r["status"])

    def read(suffix: str) -> list[dict]:
        q = f"{suffix} {phrase}" if suffix else phrase
        rows, last, total = hotfix_page(q, 1, fetch)
        if last > 1 and last <= _PAGE_READ_LIMIT:
            found = {key(r): r for r in rows}
            for page in range(2, last + 1):
                more, _, _ = hotfix_page(q, page, fetch)
                found.update({key(r): r for r in more})
            if len(found) == total:
                return list(found.values())
        elif last <= 1:
            if len(rows) != total:
                raise BuildSourceError(f"hotfix list for {q!r}: read {len(rows)} rows but Wago reports {total}; "
                                       "not using an incomplete list")
            return rows
        if len(suffix) >= 9:
            raise BuildSourceError(f"hotfix list for {phrase!r} cannot be read in full")
        found = {key(r): r for r in rows if r["record"] == suffix}   # a record ID equal to the suffix: no narrower search finds it
        for d in "0123456789":
            found.update({key(r): r for r in read(d + suffix)})
        if len(found) != total:
            raise BuildSourceError(f"hotfix list for {q!r}: read {len(found)} rows but Wago reports {total}; "
                                   "not using an incomplete list")
        return list(found.values())

    return read("")


def check_availability(version: str, config: Config, fetch: Fetch = default_fetch) -> Availability:
    """Is the probe table exported by Wago for this build, with real content?"""
    url = WAGO_TABLE_URL.format(table=config.probe_table, version=BuildInfo.from_version(version).base)
    try:
        body = fetch(url)
    except urllib.error.HTTPError as exc:  # must precede OSError (subclass)
        exc.close()
        return Availability.NOT_AVAILABLE if exc.code in (404, 410) else Availability.UNKNOWN
    except OSError:
        return Availability.UNKNOWN

    if body.lstrip().startswith("<"):
        # HTML instead of CSV: error page or bot challenge. We cannot say the
        # data is missing, only that we didn't get an answer.
        return Availability.UNKNOWN

    reader = csv.reader(io.StringIO(body))
    header = next(reader, None)
    if not header or "ID" not in header:
        return Availability.NOT_AVAILABLE
    has_row = any(any(cell.strip() for cell in row) for row in reader)
    return Availability.AVAILABLE if has_row else Availability.NOT_AVAILABLE


# --------------------------------------------------------------------------
# History: Wago.Tools build list
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WagoBuild:
    version: str
    build_id: int
    created_at: str
    build_config: str
    is_bgdl: bool

    @property
    def version_line(self) -> str:
        return ".".join(self.version.split(".")[:3])


def list_wago_builds(
    config: Config,
    fetch: Fetch = default_fetch,
    version_line: str | None = None,
) -> list[WagoBuild]:
    """Builds Wago has recorded for the product, newest first (by build number).

    The response is a dict keyed by product. The product name has been reused
    across game lines (1.13.x, 5.5.x, 1.60.x), so pass `version_line` to keep
    only one. Array order in the response is not meaningful; we sort here.
    Entries without a parseable version are skipped.
    """
    try:
        body = fetch(WAGO_BUILDS_URL)
    except OSError as exc:
        raise BuildSourceError(f"Wago builds request failed: {exc}") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise BuildSourceError(f"Wago builds response is not JSON: {exc}") from exc

    entries = data.get(config.product) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise BuildSourceError(f"Wago builds response has no list for product {config.product!r}")

    found: list[WagoBuild] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            info = BuildInfo.from_version(str(entry.get("version", "")))
        except ValueError:
            continue
        if version_line is not None and info.version_line != version_line:
            continue
        found.append(WagoBuild(
            version=info.version,
            build_id=info.build_id,
            created_at=str(entry.get("created_at", "")),
            build_config=str(entry.get("build_config", "")),
            is_bgdl=bool(entry.get("is_bgdl", False)),
        ))
    return sorted(found, key=lambda b: b.build_id, reverse=True)


def _newer_builds(
    config: Config, installed: BuildInfo | None, latest: BuildInfo, fetch: Fetch
) -> tuple[str, ...]:
    """Published builds in `latest`'s version line after `installed`, oldest first.

    Best effort: any failure returns (), because this is extra detail and must
    not change the update status.
    """
    try:
        builds = list_wago_builds(config, fetch, version_line=latest.version_line)
    except BuildSourceError:
        return ()
    floor = installed.build_id if installed else -1
    chosen = [b for b in builds if floor < b.build_id <= latest.build_id]
    return tuple(b.version for b in sorted(chosen, key=lambda b: b.build_id))


# --------------------------------------------------------------------------
# Installed datasets
# --------------------------------------------------------------------------

def installed_builds(data_dir: Path) -> list[BuildInfo]:
    """Builds with a raw-data directory under `data_dir/raw`, oldest first."""
    raw = Path(data_dir) / "raw"
    found: list[BuildInfo] = []
    if raw.is_dir():
        for child in raw.iterdir():
            if child.is_dir() and VERSION_RE.match(child.name):
                found.append(BuildInfo.from_version(child.name))
    return sorted(found, key=lambda b: b.order)


# --------------------------------------------------------------------------
# Combined check
# --------------------------------------------------------------------------

def check_for_updates(
    config: Config,
    data_dir: Path,
    fetch: Fetch = default_fetch,
    manual_version: str | None = None,
) -> UpdateCheck:
    installed_list = installed_builds(data_dir)
    installed = installed_list[-1] if installed_list else None

    try:
        latest = (
            build_from_version_string(manual_version)
            if manual_version
            else detect_latest(config, fetch)
        )
    except (BuildSourceError, ValueError) as exc:
        return UpdateCheck(
            Status.DETECTION_FAILED, None, installed, None, False,
            f"Could not determine the latest build: {exc}. "
            "You can enter a build version manually.",
        )

    line_changed = installed is not None and installed.version_line != latest.version_line

    if installed is not None and latest.build_id <= installed.build_id:
        return UpdateCheck(
            Status.UP_TO_DATE, latest, installed, None, False,
            f"Installed build {installed.version} is current.",
        )

    availability = check_availability(latest.version, config, fetch)
    newer = _newer_builds(config, installed, latest, fetch)
    note = (
        f" Note: version line changed from {installed.version_line} to "
        f"{latest.version_line}; review the change report carefully."
        if line_changed and installed else ""
    )
    have = f" (installed: {installed.version})" if installed else " (no dataset installed)"

    if availability is Availability.AVAILABLE:
        return UpdateCheck(
            Status.UPDATE_READY, latest, installed, availability, line_changed,
            f"Build {latest.version} is available{have}.{note}",
            newer_builds=newer,
        )
    if availability is Availability.NOT_AVAILABLE:
        return UpdateCheck(
            Status.UPDATE_PENDING_DATA, latest, installed, availability, line_changed,
            f"Build {latest.version} is live but its data is not yet exported{have}. "
            f"Try again later.{note}",
            newer_builds=newer,
        )
    return UpdateCheck(
        Status.UPDATE_UNVERIFIED, latest, installed, availability, line_changed,
        f"Build {latest.version} is live but data availability could not be "
        f"confirmed{have}.{note}",
        newer_builds=newer,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Check for a newer game build.")
    ap.add_argument("--product", default=Config.product)
    ap.add_argument("--region", default=Config.region)
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--version", help="Skip detection and check this version, e.g. 1.60.1.70425")
    args = ap.parse_args()

    result = check_for_updates(
        Config(product=args.product, region=args.region),
        args.data_dir,
        manual_version=args.version,
    )
    print(f"status:    {result.status.value}")
    print(f"installed: {result.installed.version if result.installed else '-'}")
    print(f"latest:    {result.latest.version if result.latest else '-'}")
    if result.availability:
        print(f"wago data: {result.availability.value}")
    if result.newer_builds:
        print(f"builds since installed: {', '.join(result.newer_builds)}")
    print(result.message)


if __name__ == "__main__":
    main()
