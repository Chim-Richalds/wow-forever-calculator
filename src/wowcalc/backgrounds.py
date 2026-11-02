"""Talent tree background art: one image per specialization, kept in a local cache.

The images are the original-style talent backgrounds ("Talents background - mage Frost.png")
from Wowpedia's category of talent interface backgrounds, chosen by the project owner. They come
from a later version of the game than WoW Forever, which uses the same 27 specializations. Like the
talent icons they are game art: they live in `data/backgrounds/` (not in git), with a manifest
recording where each came from and its hash, and every file is checked against the SHA-1 the wiki
publishes and checked to be a PNG before it is kept.

    python -m wowcalc.backgrounds import FOLDER   # images you saved from the wiki (any of the names below)
    python -m wowcalc.backgrounds urls            # where to download each one
    python -m wowcalc.backgrounds fetch           # download through the wiki's API (its image host may refuse)
    python -m wowcalc.backgrounds status

Files are matched by name (and accepted as PNG or WebP by their contents: browsers get WebP from
the wiki even when the name says .png): "Talents background - mage Frost.png", "Talents_background_-_mage_Frost.png"
or simply "mage frost.png" all give Mage / Frost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import transport

WIKI_API = "https://wowpedia.fandom.com/api.php"
CATEGORY = "Category:Talents_interface_backgrounds"
TITLE = re.compile(r"^File:Talents background - (?P<cls>[a-z ]+?) (?P<spec>[A-Z][A-Za-z ]+)\.png$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TYPES = {"png": "image/png", "webp": "image/webp"}   # the wiki's image host sends WebP to browsers, under a .png name


def sniff(body: bytes) -> str | None:
    """'png', 'webp' or None, from the file's first bytes (not its name)."""
    if body.startswith(PNG_MAGIC):
        return "png"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return None
MAX_BYTES = 2_000_000


class BackgroundError(Exception):
    pass


def file_name(class_name: str, spec: str, kind: str = "png") -> str:
    """'Druid', 'Feral Combat' -> 'druid-feral-combat.png' (or .webp)."""
    return re.sub(r"[^a-z0-9]+", "-", f"{class_name} {spec}".lower()).strip("-") + "." + kind


class BackgroundCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.manifest_path = self.dir / "manifest.json"

    def manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def stored(self, class_name: str, spec: str) -> str | None:
        """The cached file name for this specialization, whichever image type it is, or None."""
        for kind in TYPES:
            if (self.dir / file_name(class_name, spec, kind)).is_file():
                return file_name(class_name, spec, kind)
        return None

    def has(self, class_name: str, spec: str) -> bool:
        return self.stored(class_name, spec) is not None

    def read(self, name: str) -> tuple[bytes, str] | None:
        """(the cached file `name`, its media type), or None. Rejects any other name."""
        match = re.fullmatch(r"[a-z0-9-]+\.(png|webp)", name)
        if not match:
            return None
        path = self.dir / name
        return (path.read_bytes(), TYPES[match.group(1)]) if path.is_file() else None

    def add(self, class_name: str, spec: str, body: bytes, source: str) -> str:
        kind = sniff(body)
        if kind is None:
            raise BackgroundError(f"{class_name} {spec}: not a PNG or WebP image")
        self.dir.mkdir(parents=True, exist_ok=True)
        for other in TYPES:       # one image per specialization: drop one of another type
            if other != kind:
                (self.dir / file_name(class_name, spec, other)).unlink(missing_ok=True)
        name = file_name(class_name, spec, kind)
        tmp = self.dir / (name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, self.dir / name)
        manifest = self.manifest()
        manifest[name] = {"class": class_name, "spec": spec, "source": source, "bytes": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(),
                          "retrievedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.manifest_path)
        return name


def wiki_files(get=transport.get) -> dict[tuple[str, str], dict]:
    """(class, spec) -> {"url", "sha1", "title"} for the original-style backgrounds in the category."""
    q = {"action": "query", "list": "categorymembers", "cmtitle": CATEGORY, "cmlimit": "500", "cmtype": "file", "format": "json"}
    members = json.loads(get(WIKI_API + "?" + urllib.parse.urlencode(q)).body)["query"]["categorymembers"]
    wanted = {}
    for m in members:
        match = TITLE.match(m["title"])
        if match and " pet" not in match["cls"] and "(unused)" not in m["title"]:
            wanted[m["title"]] = (match["cls"].title(), match["spec"])
    out = {}
    titles = list(wanted)
    for i in range(0, len(titles), 40):
        q = {"action": "query", "prop": "imageinfo", "iiprop": "url|sha1|mime", "format": "json", "titles": "|".join(titles[i:i + 40])}
        pages = json.loads(get(WIKI_API + "?" + urllib.parse.urlencode(q)).body)["query"]["pages"]
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            if page.get("title") in wanted and info.get("url"):
                out[wanted[page["title"]]] = {"url": info["url"], "sha1": info.get("sha1", ""), "title": page["title"]}
    return out


def fetch(cache: BackgroundCache, specs: list[tuple[str, str]], get=transport.get) -> tuple[list[str], list[str]]:
    """Download the background of each (class, spec). Returns (added files, problems)."""
    files = wiki_files(get)
    added, problems = [], []
    for class_name, spec in specs:
        found = files.get((class_name, spec))
        if found is None:
            problems.append(f"{class_name} {spec}: no background in {CATEGORY}")
            continue
        try:
            body = get(found["url"]).body
            if len(body) > MAX_BYTES:
                raise BackgroundError("larger than expected")
            if found["sha1"] and hashlib.sha1(body).hexdigest() != found["sha1"]:
                raise BackgroundError("does not match the wiki's SHA-1")
            added.append(cache.add(class_name, spec, body, found["url"]))
        except (OSError, BackgroundError) as exc:
            problems.append(f"{class_name} {spec}: {exc}")
    return added, problems


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def import_folder(cache: BackgroundCache, folder: Path, specs: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Copy the PNGs in `folder` whose names name one specialization. Returns (added, skipped notes)."""
    folder = Path(folder)
    if not folder.is_dir():
        raise BackgroundError(f"{folder} is not a folder")
    wanted = {_words(f"{c} {s}"): (c, s) for c, s in specs}
    added, skipped = [], []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".png", ".webp"):
            continue
        name = _words(path.stem).removeprefix("talents background ").strip()
        hits = [cs for key, cs in wanted.items() if name == key or name.endswith(" " + key)]
        if len(hits) != 1:
            skipped.append(f"{path.name}: names no single specialization")
            continue
        try:
            added.append(cache.add(*hits[0], path.read_bytes(), f"imported from {path.name}"))
        except BackgroundError as exc:
            skipped.append(f"{path.name}: {exc}")
    return added, skipped


def main(argv: list[str] | None = None) -> int:
    from .specs import SpecError, load_spec_names
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Talent tree background images.")
    ap.add_argument("command", choices=("import", "urls", "fetch", "status"))
    ap.add_argument("folder", nargs="?", type=Path, help="for import: the folder holding the images")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    args = ap.parse_args(argv)
    try:
        names = load_spec_names(args.data_dir / "external" / "spec_names.tsv")
    except SpecError as exc:
        print(f"ERROR: {exc}")
        return 2
    specs = [(cls, spec) for cls, items in sorted(names.items()) for spec, _ in items]
    cache = BackgroundCache(args.data_dir / "backgrounds")
    if args.command == "import":
        if args.folder is None:
            print("ERROR: give the folder to import")
            return 2
        try:
            added, skipped = import_folder(cache, args.folder, specs)
        except BackgroundError as exc:
            print(f"ERROR: {exc}")
            return 2
        print(f"Imported {len(added)} background(s); {len([s for s in specs if cache.has(*s)])} of {len(specs)} now cached.")
        for note in skipped:
            print(f"  skipped: {note}")
        return 0
    if args.command == "urls":
        files = wiki_files()
        for cls, spec in specs:
            found = files.get((cls, spec))
            print(f"{cls} {spec}: {found['url'].split('/revision/')[0] if found else 'not found in the category'}")
        return 0
    if args.command == "fetch":
        added, problems = fetch(cache, specs)
        print(f"Downloaded {len(added)} of {len(specs)} backgrounds into {cache.dir}.")
        for p in problems:
            print(f"  PROBLEM {p}")
        return 0 if not problems else 1
    have = [s for s in specs if cache.has(*s)]
    print(f"{len(have)} of {len(specs)} specialization backgrounds cached in {cache.dir}.")
    for cls, spec in specs:
        if not cache.has(cls, spec):
            print(f"  missing: {cls} {spec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
