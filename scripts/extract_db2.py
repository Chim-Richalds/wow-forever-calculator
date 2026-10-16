
import csv
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = os.environ.get("WOW_BUILD", "1.60.1.70338")
BASE_URL = "https://wago.tools/db2"

TABLES = [
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
]

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw" / BUILD
MANIFEST_DIR = ROOT / "data" / "manifests"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    manifest = {
        "product": "wow_classic_beta",
        "build": BUILD,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Wago.Tools DB2 CSV exports",
        "tables": {},
    }

    failures = []

    for table in TABLES:
        url = f"{BASE_URL}/{table}/csv?build={BUILD}"
        destination = OUT_DIR / f"{table}.csv"
        temporary = destination.with_suffix(".csv.tmp")

        print(f"Downloading {table}...")

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "WoWForeverCalculator/0.1"},
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
                content_type = response.headers.get(
                    "Content-Type", ""
                )

            # Reject obvious HTML/error pages.
            prefix = content[:500].lstrip().lower()
            if (
                not content
                or b"<html" in prefix
                or b"<!doctype html" in prefix
            ):
                raise ValueError("Response does not look like CSV data")

            # Verify that the payload can be parsed as CSV.
            text = content.decode("utf-8-sig")
            reader = csv.reader(text.splitlines())
            header = next(reader, None)

            if not header or len(header) < 2:
                raise ValueError("Missing or invalid CSV header")

            temporary.write_bytes(content)
            temporary.replace(destination)

            manifest["tables"][table] = {
                "url": url,
                "file": str(destination.relative_to(ROOT)),
                "sha256": sha256_file(destination),
                "bytes": len(content),
                "columns": header,
                "contentType": content_type,
                "status": "downloaded-needs-validation",
            }

            print(
                f"  Saved {destination} "
                f"({len(content):,} bytes)"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
            StopIteration,
        ) as exc:
            temporary.unlink(missing_ok=True)
            failures.append({"table": table, "error": str(exc)})
            manifest["tables"][table] = {
                "url": url,
                "status": "failed",
                "error": str(exc),
            }
            print(f"  FAILED: {exc}", file=sys.stderr)

    manifest["failures"] = failures
    manifest_path = MANIFEST_DIR / f"{BUILD}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nManifest: {manifest_path}")
    print(f"Successful downloads: {len(TABLES) - len(failures)}")
    print(f"Failed downloads: {len(failures)}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
