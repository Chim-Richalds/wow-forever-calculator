"""Read-only access to one build's CSV tables (staged or installed)."""

from __future__ import annotations

import csv
from pathlib import Path

from .builds import VERSION_RE, BuildInfo


class DatasetError(Exception):
    """A table is missing, unreadable, or has no header."""


def _is_blank(values) -> bool:
    return not any(isinstance(v, str) and v.strip() for v in values)


class Dataset:
    """Lazily loads tables from a directory of `<Table>.csv` files.

    Row counting and header reading stream the file; `rows` caches the parsed
    rows. A "row" is any record with at least one non-blank cell, the same
    definition the fetch stage uses for its manifest.
    """

    def __init__(self, directory: Path, version: str | None = None):
        self.directory = Path(directory)
        self.version = version
        self._rows: dict[str, list[dict[str, str]]] = {}
        self._columns: dict[str, list[str]] = {}
        self._counts: dict[str, int] = {}

    def path(self, table: str) -> Path:
        return self.directory / f"{table}.csv"

    def has_table(self, table: str) -> bool:
        return self.path(table).is_file()

    def _open(self, table: str):
        path = self.path(table)
        if not path.is_file():
            raise DatasetError(f"Missing table file: {path}")
        return path.open("r", encoding="utf-8-sig", newline="")

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            try:
                with self._open(table) as handle:
                    header = next(csv.reader(handle), None)
            except (OSError, UnicodeError, csv.Error) as exc:
                raise DatasetError(f"Cannot read {self.path(table)}: {exc}") from exc
            if not header:
                raise DatasetError(f"No CSV header: {self.path(table)}")
            self._columns[table] = header
        return self._columns[table]

    def rows(self, table: str) -> list[dict[str, str]]:
        if table not in self._rows:
            try:
                with self._open(table) as handle:
                    reader = csv.DictReader(handle, restval="")
                    if not reader.fieldnames:
                        raise DatasetError(f"No CSV header: {self.path(table)}")
                    self._rows[table] = [
                        row for row in reader if not _is_blank(row.values())
                    ]
            except (OSError, UnicodeError, csv.Error) as exc:
                raise DatasetError(f"Cannot read {self.path(table)}: {exc}") from exc
        return self._rows[table]

    def iter_values(self, table: str):
        """Stream the table's rows as lists of strings, without caching.

        Skips the header and blank rows. Use for large tables that only need
        one pass; columns line up with `columns(table)`.
        """
        try:
            with self._open(table) as handle:
                reader = csv.reader(handle)
                next(reader, None)  # header
                for row in reader:
                    if not _is_blank(row):
                        yield row
        except (OSError, UnicodeError, csv.Error) as exc:
            raise DatasetError(f"Cannot read {self.path(table)}: {exc}") from exc

    def row_count(self, table: str) -> int:
        if table in self._rows:
            return len(self._rows[table])
        if table not in self._counts:
            try:
                with self._open(table) as handle:
                    reader = csv.reader(handle)
                    next(reader, None)  # header
                    self._counts[table] = sum(1 for row in reader if not _is_blank(row))
            except (OSError, UnicodeError, csv.Error) as exc:
                raise DatasetError(f"Cannot read {self.path(table)}: {exc}") from exc
        return self._counts[table]


def list_builds(directory: Path) -> list[BuildInfo]:
    """Build-named subdirectories of `directory`, oldest first."""
    directory = Path(directory)
    found: list[BuildInfo] = []
    if directory.is_dir():
        for child in directory.iterdir():
            if child.is_dir() and VERSION_RE.match(child.name):
                found.append(BuildInfo.from_version(child.name))
    return sorted(found, key=lambda b: b.build_id)


def open_dataset(data_dir: Path, version: str) -> Dataset:
    """Open `version` from staging if present there, otherwise from raw/."""
    data_dir = Path(data_dir)
    for sub in ("staging", "raw"):
        candidate = data_dir / sub / version
        if candidate.is_dir():
            return Dataset(candidate, version)
    raise DatasetError(f"Build {version} is neither staged nor installed under {data_dir}")
