"""Talent icons: which icon each talent uses, and a local cache of the images.

Which icon: the build's own data names it by file data ID. A talent's definition
may carry its own (`TraitDefinition.OverrideIcon`); otherwise the spell's icon is
used (`SpellMisc.SpellIconFileDataID`). Neither is guessed: no ID, no icon.

The images are not part of the DB2 tables. They live in `data/icons/<id>.<ext>`
with a manifest recording where each came from and its hash. This module never
assumes a download address. You give a URL template containing `{id}` (for
example one you have checked yourself), or import a folder of images you already
extracted. Every file is checked to be a real image before it is kept.

    python -m wowcalc.icons tables               # add SpellMisc to the active build
    python -m wowcalc.icons status
    python -m wowcalc.icons list --out ids.txt   # the file data IDs the talent trees use
    python -m wowcalc.icons import FOLDER [--listfile listfile.csv]
    python -m wowcalc.icons fetch --url-template "https://example.test/{id}.png"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import transport
from .dataset import Dataset
from .trees import ClassTree

OPTIONAL_TABLES: tuple[str, ...] = ("SpellMisc",)
MANIFEST = "manifest.json"
MAX_BYTES = 512 * 1024
ID_OK = re.compile(r"^[1-9][0-9]{0,9}$")

# (extension, content type, leading bytes) - checked, never trusted from a name or header
SIGNATURES = (
    ("png", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("jpg", "image/jpeg", b"\xff\xd8\xff"),
    ("gif", "image/gif", b"GIF8"),
)
TYPES = {ext: ctype for ext, ctype, _ in SIGNATURES} | {"webp": "image/webp"}


class IconError(Exception):
    pass


def sniff(data: bytes) -> str | None:
    """The image extension if `data` really is a png/jpg/gif/webp, else None."""
    if len(data) > MAX_BYTES:
        return None
    for ext, _, magic in SIGNATURES:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def icon_ids(ds: Dataset, tree: ClassTree) -> dict[str, tuple[str, str]]:
    """node id -> (file data ID, where it came from). Nodes with no icon are absent."""
    override: dict[str, str] = {}
    for row in ds.rows("TraitDefinition"):
        value = (row.get("OverrideIcon") or "").strip()
        if ID_OK.match(value):
            override[row["ID"]] = value
    by_spell: dict[str, str] = {}
    if ds.has_table("SpellMisc"):
        for row in ds.rows("SpellMisc"):
            value = (row.get("SpellIconFileDataID") or "").strip()
            spell = (row.get("SpellID") or "").strip()
            if ID_OK.match(value) and spell and spell not in by_spell:
                by_spell[spell] = value
    found: dict[str, tuple[str, str]] = {}
    for node in tree.nodes.values():
        if not node.entries:
            continue
        entry = node.entries[0]
        if entry.definition_id in override:
            found[node.id] = (override[entry.definition_id], "TraitDefinition.OverrideIcon")
        elif entry.spell_id in by_spell:
            found[node.id] = (by_spell[entry.spell_id], "SpellMisc.SpellIconFileDataID")
    return found


class IconCache:
    """`data/icons/`: images named by file data ID, plus a manifest."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _manifest(self) -> dict:
        try:
            data = json.loads((self.folder / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"icons": {}}
        return data if isinstance(data, dict) and isinstance(data.get("icons"), dict) else {"icons": {}}

    def find(self, icon_id: str) -> Path | None:
        if not ID_OK.match(icon_id or ""):
            return None
        for ext in TYPES:
            path = self.folder / f"{icon_id}.{ext}"
            if path.is_file():
                return path
        return None

    def has(self, icon_id: str) -> bool:
        return self.find(icon_id) is not None

    def read(self, icon_id: str) -> tuple[bytes, str] | None:
        """(bytes, content type) for a cached icon, rechecking that it is still an image."""
        path = self.find(icon_id)
        if path is None:
            return None
        data = path.read_bytes()
        ext = sniff(data)
        return (data, TYPES[ext]) if ext else None

    def ids(self) -> set[str]:
        return {p.stem for p in self.folder.glob("*") if p.suffix[1:] in TYPES and ID_OK.match(p.stem)} if self.folder.is_dir() else set()

    def add(self, icon_id: str, data: bytes, source: str, now: datetime | None = None) -> Path:
        if not ID_OK.match(icon_id):
            raise IconError(f"{icon_id!r} is not a file data ID")
        ext = sniff(data)
        if ext is None:
            raise IconError(f"{icon_id}: not a png, jpg, gif or webp image (or larger than {MAX_BYTES} bytes)")
        self.folder.mkdir(parents=True, exist_ok=True)
        for other in TYPES:
            stale = self.folder / f"{icon_id}.{other}"
            if other != ext and stale.is_file():
                stale.unlink()
        target = self.folder / f"{icon_id}.{ext}"
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        manifest = self._manifest()
        manifest["icons"][icon_id] = {
            "file": target.name, "source": source, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "added": (now or datetime.now(timezone.utc)).isoformat(),
        }
        text = json.dumps(manifest, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, self.folder / MANIFEST)
        return target

    def verify(self) -> list[str]:
        """Problems: listed files that changed or vanished."""
        problems = []
        for icon_id, entry in self._manifest()["icons"].items():
            path = self.folder / str(entry.get("file", ""))
            if not path.is_file():
                problems.append(f"{icon_id}: file missing")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
                problems.append(f"{icon_id}: changed since it was recorded")
        return problems


def load_paths(path: Path) -> dict[str, str]:
    """File data ID -> icon path (lowercase, forward slashes) from a community listfile."""
    out: dict[str, str] = {}
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            ident, _, name = line.strip().partition(";")
            name = name.lower().replace("\\", "/")
            if ID_OK.match(ident) and name.startswith("interface/icons/"):
                out[ident] = name
    return out


def load_listfile(path: Path) -> dict[str, str]:
    """Icon name -> file data ID from a community listfile (`id;path` lines).

    Only paths under interface/icons/ count. A name that two files share is left
    out, because guessing which one is meant would be wrong.
    """
    names: dict[str, str] = {}
    clash: set[str] = set()
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            ident, _, name = line.strip().partition(";")
            name = name.lower().replace("\\", "/")
            if not ID_OK.match(ident) or not name.startswith("interface/icons/"):
                continue
            stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem in names and names[stem] != ident:
                clash.add(stem)
            names[stem] = ident
    for stem in clash:
        del names[stem]
    return names


def import_folder(cache: IconCache, source: Path, names: dict[str, str] | None = None, only: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Copy images from `source` and its subfolders. Returns (added ids, skipped notes).

    A file named `<id>.png` is used as is. A file named by icon (`inv_sword_01.png`)
    is matched through `names` (from `load_listfile`) when one is given.
    """
    source = Path(source)
    if not source.is_dir():
        raise IconError(f"{source} is not a folder")
    added, skipped = [], []
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        if path.suffix.lower()[1:] not in TYPES:
            continue
        stem = path.stem
        icon_id = stem if ID_OK.match(stem) else (names or {}).get(stem.lower())
        if icon_id is None:
            skipped.append(f"{path.name}: " + ("not in the listfile as an icon" if names else "name is not a file data ID (give --listfile to match by icon name)"))
            continue
        if only is not None and icon_id not in only:
            continue
        try:
            cache.add(icon_id, path.read_bytes(), f"imported from {path.name}")
            added.append(icon_id)
        except IconError as exc:
            skipped.append(str(exc))
    return added, skipped


def fetch_icons(cache: IconCache, ids, url_template: str, get=transport.get) -> tuple[list[str], list[str]]:
    """Download `ids` from a template containing {id}. Failures are reported, never fatal."""
    if "{id}" not in url_template or not url_template.lower().startswith("https://"):
        raise IconError("the URL template must start with https:// and contain {id}")
    added, failed = [], []
    for icon_id in sorted(ids, key=int):
        url = url_template.replace("{id}", icon_id)
        try:
            cache.add(icon_id, get(url, timeout=30.0).body, url)
            added.append(icon_id)
        except IconError as exc:
            failed.append(str(exc))
        except OSError as exc:
            failed.append(f"{icon_id}: {exc}")
    return added, failed


def wanted_ids(ds: Dataset, model) -> set[str]:
    out: set[str] = set()
    for tree in model.trees.values():
        out.update(i for i, _ in icon_ids(ds, tree).values())
    return out


def main(argv: list[str] | None = None) -> int:
    from .builds import Config
    from .fetch import FetchError, complete_build
    from .trees import load_model
    from .update import UpdateError, get_active

    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Talent icon cache.")
    ap.add_argument("command", choices=("tables", "status", "list", "import", "fetch"))
    ap.add_argument("folder", nargs="?", type=Path, help="for import: a folder of <fileDataID>.png/.jpg/.webp")
    ap.add_argument("--listfile", type=Path, help="community listfile (id;path): for import, match icon names; for list, write icon paths")
    ap.add_argument("--only-needed", action="store_true", help="for import: keep only icons the talent trees use")
    ap.add_argument("--out", type=Path, help="for list: write the ids to this file")
    ap.add_argument("--url-template", help="for fetch: https URL containing {id}")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--build")
    args = ap.parse_args(argv)
    try:
        build = args.build or get_active(args.data_dir)
    except UpdateError as exc:
        print(f"ERROR: {exc}")
        return 2
    if build is None:
        print("ERROR: no build is installed. Run: python -m wowcalc.update")
        return 2
    cache = IconCache(args.data_dir / "icons")
    if args.command == "tables":
        try:
            result = complete_build(build, Config(), args.data_dir, tables=OPTIONAL_TABLES,
                                    progress=lambda t, m: print(f"  {t}: {m}"))
        except (FetchError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 2
        if not result.ok:
            print("ERROR: " + "; ".join(f"{f['table']}: {f['error']}" for f in result.failures))
            return 2
        print("SpellMisc added." if result.added else "SpellMisc is already there.")
        return 0
    ds = Dataset(args.data_dir / "raw" / build, build)
    if args.command == "import":
        if args.folder is None:
            print("ERROR: give the folder to import")
            return 2
        try:
            only = wanted_ids(ds, load_model(ds)) if args.only_needed else None
            added, skipped = import_folder(cache, args.folder, load_listfile(args.listfile) if args.listfile else None, only)
        except (IconError, OSError) as exc:
            print(f"ERROR: {exc}")
            return 2
        print(f"Imported {len(added)} icon(s).")
        for note in skipped[:20]:
            print(f"  skipped: {note}")
        return 0
    model = load_model(ds)
    wanted = wanted_ids(ds, model)
    have = cache.ids()
    if args.command == "list":
        if args.listfile:
            paths = load_paths(args.listfile)
            lines = sorted(paths[i] for i in wanted if i in paths)
            absent = sorted((i for i in wanted if i not in paths), key=int)
            text = "\n".join(lines) + "\n"
            print(f"{len(lines)} icon paths found; {len(absent)} ids not in the listfile as icons" + (f": {', '.join(absent[:15])}" if absent else ""))
        else:
            text = "\n".join(sorted(wanted, key=int)) + "\n"
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"Wrote {len(text.splitlines())} lines to {args.out}")
        else:
            print(text, end="")
        return 0
    if args.command == "status":
        print(f"Build {build}: {len(wanted)} talent icon ids in the data; {len(wanted & have)} cached, {len(wanted - have)} missing.")
        if not ds.has_table("SpellMisc"):
            print("SpellMisc is not installed, so spell icons are unknown. Run: python -m wowcalc.icons tables")
        for problem in cache.verify():
            print(f"  PROBLEM {problem}")
        return 0
    if not args.url_template:
        print("ERROR: fetch needs --url-template (https://... containing {id}); this tool does not assume an address")
        return 2
    try:
        added, failed = fetch_icons(cache, wanted - have, args.url_template)
    except IconError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Fetched {len(added)} icon(s); {len(failed)} failed.")
    for note in failed[:20]:
        print(f"  failed: {note}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
