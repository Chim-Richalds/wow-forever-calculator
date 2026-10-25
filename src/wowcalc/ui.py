"""The local web page: pick a character, spend talent points, and read what each talent is worth.

    PYTHONPATH=src python3 -m wowcalc.ui            # then open http://127.0.0.1:8765
    PYTHONPATH=src python3 -m wowcalc.ui --port 9000 --build 1.60.1.70450

Nothing leaves your computer: the server listens on 127.0.0.1 only, uses the same data files
and the same calculation code as `wowcalc.talentinfo`, and writes just one file of its own,
`data/ui/builds.json` (your saved builds).

The value of a talent is the sheet with it minus the sheet without it, for every stat and
sheet figure ("current ranks"), and what one more rank would add ("next rank"). Both change
only that talent's own ranks; they do not ask whether the rest of the build would still be
legal without it.

`Workbench` holds the data and answers every question as plain dictionaries, so it can be
tested without a network. `serve()` wraps it in a small standard-library HTTP server.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .allocation import can_add, can_remove, check, spent
from .basestats import load_base_stats
from .dataset import Dataset
from .effects import EffectBook, render_tooltip
from .fetch import TABLES
from .icons import IconCache, icon_ids
from .rates import RatesError, load_rates
from .specs import SpecError, load_spec_names, name_trees
from .sheetbase import SheetBaseError, load_sheet_base, problems_in
from .stats import ADJUSTABLE, SHEET_STATS, RankedTalent, Sheet, build_sheet
from .talentinfo import spell_names
from .trees import ClassTree, Node, load_model

PAGE = Path(__file__).with_name("ui_page.html")
MAX_BODY = 1_000_000
HOSTS = ("127.0.0.1", "localhost", "[::1]")
NAME_OK = re.compile(r"^[^\x00-\x1f\x7f]{1,60}$")

# What the numbers rest on. Shown on the Data tab so nothing looks more certain than it is.
CONFIDENCE = [
    ("Talent stat percentages", "verified", "Rounding down was checked in game (Mental Strength, Heart of the Wild)."),
    ("Mana regeneration while casting from talents", "inferred", "Six talents say they allow N% of mana regeneration to continue while casting; the percent is read from the data and added up. The base is taken as 0%. How much mana that is depends on Spirit and is not calculated yet."),
    ("Spell power and healing from Intellect or Spirit talents", "inferred", "Which stat and which schools are read from the effect data and checked against four tooltips (Spiritual Guidance, Mental Quickness, Natural Instinct, Champion of the Light). The rounding is not checked in game; Spiritual Guidance's damage values (1, 3, 5, 6, 8) look unlike its healing values and are worth a look."),
    ("Crit, hit, dodge and parry from talents", "verified", "Five oracle talents were read in game (Nature's Majesty, Nature's Reach, Feral Swiftness, Deflection, Mental Dexterity)."),
    ("Base crit, dodge, attack power and armor at levels 1 and 60", "verified", "Read from your character panel exports."),
    ("Crit and spell crit per Agility and Intellect", "verified", "The client's PlayerExpectedStat table reproduces the panel at levels 1 and 60."),
    ("Crit, dodge, attack power and armor between levels 1 and 60", "inferred", "The class constants are the same at both exported levels, so they are extended in a straight line. Each value is within about 0.01."),
    ("Armor = 2 per Agility, damage reduction formula", "verified", "Matches every panel row."),
    ("Mana = base mana + 1 per Intellect to 20, then 15", "verified", "The client's BaseMana reproduces 73 of 74 panel rows; Gnome mana x1.05 was found from the data."),
    ("Health = class constant + 10 per Stamina", "verified", "Fitted to the panel at level 60. Between levels it is an estimate."),
    ("Undead health x1.05", "doubtful", "Fits every Undead row, but Tauren was expected to have it. Not yet checked in game."),
    ("Base stats between levels 1 and 60", "estimated", "Straight line between the two supplied levels; marked 'base estimated'."),
    ("Extra Agility/Strength raising crit, dodge and attack power", "unverified", "Fitted from the panel; a before/after reading of a stat talent in game would confirm it."),
    ("Block chance 5% base with a shield; Strength gives block value 1 per 20", "confirmed by you", "From the game's own text; the exported panels had no items."),
    ("Block chance against a higher-level attacker", "confirmed by you", "0.5% per level against a mob, 0.2% per level against a player."),
    ("Caster attack power per Strength", "reference", "foreverdb's 1 per Strength is used for changes; the panel suggested 2."),
]


class UiError(Exception):
    """A request that cannot be answered; `status` is the HTTP code to send."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def num(value):
    """A JSON-friendly number: whole numbers stay whole, everything else is rounded to 4 places."""
    if value is None:
        return None
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else round(float(value), 4)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class Request:
    class_name: str
    race: str
    level: int
    alloc: dict[str, int]
    adjust: dict[str, int]
    shield: bool
    attacker_level: int | None
    vs_player: bool
    values: bool


class BuildStore:
    """Saved builds in one JSON file. Writes go to a temporary file first, so a crash cannot leave half a file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise UiError(500, f"cannot read {self.path.name}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".builds-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2, sort_keys=True)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def names(self) -> list[dict]:
        with self.lock:
            data = self._read()
        return [{"name": n, "class": b.get("class"), "race": b.get("race"), "level": b.get("level"),
                 "saved": b.get("saved"), "build": b.get("build")} for n, b in sorted(data.items(), key=lambda kv: kv[0].lower())]

    def get(self, name: str) -> dict:
        with self.lock:
            data = self._read()
        if name not in data:
            raise UiError(404, f"no saved build called {name!r}")
        return data[name]

    def put(self, name: str, entry: dict) -> None:
        if not NAME_OK.match(name or ""):
            raise UiError(400, "a build name is 1 to 60 characters with no control characters")
        with self.lock:
            data = self._read()
            data[name] = entry
            self._write(data)

    def delete(self, name: str) -> None:
        with self.lock:
            data = self._read()
            if name not in data:
                raise UiError(404, f"no saved build called {name!r}")
            del data[name]
            self._write(data)


class Workbench:
    """Everything the page needs, loaded once."""

    def __init__(self, data_dir: Path, build: str | None = None):
        from .update import UpdateError, get_active
        self.data_dir = Path(data_dir)
        try:
            build = build or get_active(self.data_dir)
        except UpdateError as exc:
            raise UiError(500, str(exc)) from exc
        if build is None:
            raise UiError(500, "No build is installed. Run: python -m wowcalc.update")
        raw = self.data_dir / "raw" / build
        missing = [t for t in TABLES if not (raw / f"{t}.csv").is_file()]
        if missing:
            raise UiError(500, f"Build {build} lacks {len(missing)} table(s) ({', '.join(missing[:3])}...). Run: python -m wowcalc.update complete")
        self.build = build
        self.ds = Dataset(raw, build)
        self.model = load_model(self.ds)
        self.names = spell_names(self.ds)
        self.spells = {r["ID"]: r for r in self.ds.rows("Spell")}
        self.book = EffectBook.for_model(self.ds, self.model)
        self.spec_names: dict[str, list] = {}
        self.spec_problems: list[str] = []
        spec_file = self.data_dir / "external" / "spec_names.tsv"
        try:
            self.spec_names = load_spec_names(spec_file) if spec_file.is_file() else {}
        except SpecError as exc:
            self.spec_problems.append(str(exc))
        self.icons = IconCache(self.data_dir / "icons")
        self._icon_ids: dict[str, dict] = {}

        external = self.data_dir / "external"
        stats_file = external / "base_stats.tsv"
        if not stats_file.is_file():
            raise UiError(500, f"No base stats file at {stats_file}. See: python -m wowcalc.basestats")
        self.stats = load_base_stats(stats_file)
        if self.stats.problems or not self.stats.rows:
            raise UiError(500, "The base stats file has problems; run: python -m wowcalc.basestats")

        self.load_notes: list[str] = []
        self.sheet_base = None
        sheet_file = external / "sheet_base.tsv"
        if sheet_file.is_file():
            try:
                self.sheet_base = load_sheet_base(sheet_file)
            except SheetBaseError as exc:
                self.load_notes.append(f"{sheet_file.name} could not be read ({exc})")
        else:
            self.load_notes.append(f"no {sheet_file.name}: crit, hit, dodge, parry and attack power show talent bonuses only")
        self.rates = None
        rates_file = external / "stat_rates.json"
        if rates_file.is_file():
            try:
                self.rates = load_rates(rates_file)
            except RatesError as exc:
                self.load_notes.append(str(exc))
        else:
            self.load_notes.append(f"no {rates_file.name}: extra stats do not change crit, dodge or attack power (python -m wowcalc.rates fit)")
        self.store = BuildStore(self.data_dir / "ui" / "builds.json")
        self._decoded: dict[str, list] = {}

    # ---- pieces ----
    def effects_of(self, node: Node) -> list:
        if node.id not in self._decoded:
            self._decoded[node.id] = self.book.decode(node) if node.entries else []
        return self._decoded[node.id]

    def label(self, node: Node) -> str:
        return self.names.get(node.spell_id, node.spell_id or f"node {node.id}")

    def tree_of(self, class_name: str) -> ClassTree:
        try:
            return self.model.by_name(class_name)
        except KeyError:
            raise UiError(400, f"no class {class_name!r}; choose from {', '.join(self.classes())}") from None

    def classes(self) -> list[str]:
        have = set(self.stats.classes)
        return sorted(t.class_name for t in self.model.trees.values() if t.class_name in have)

    def races(self, class_name: str) -> list[str]:
        return sorted(r for c, r in self.stats.combos() if c == class_name)

    # ---- /api/meta ----
    def meta(self) -> dict:
        levels = self.stats.levels
        return {
            "build": self.build,
            "classes": {c: self.races(c) for c in self.classes()},
            "levels": [levels[0], levels[-1]] if levels else [1, 60],
            "adjustable": list(ADJUSTABLE),
            "notes": self.load_notes,
            "has_rates": self.rates is not None,
            "per_level": bool(self.rates and self.rates.client),
        }

    def specs_of(self, tree: ClassTree) -> tuple[list, list[str]]:
        """(a name or None per specialization tree, problems)."""
        if tree.class_name not in self.spec_names:
            return [None] * len(tree.sub_trees), [] if not self.spec_names and not self.spec_problems else [f"{tree.class_name}: no names in spec_names.tsv"]
        return name_trees(tree, self.spec_names[tree.class_name], self.names)

    # ---- /api/tree ----
    def tree(self, class_name: str) -> dict:
        tree = self.tree_of(class_name)
        if tree.class_name not in self._icon_ids:
            self._icon_ids[tree.class_name] = icon_ids(self.ds, tree)
        icons = self._icon_ids[tree.class_name]
        nodes = []
        for node in sorted(tree.nodes.values(), key=lambda n: (n.y, n.x, n.id)):
            effects = self.effects_of(node)
            text = self.spells.get(node.spell_id, {}).get("Description_lang", "")
            by_rank = []
            for rank in range(1, node.max_ranks + 1):
                rendered, complete = render_tooltip(text, effects, rank) if text else ("", True)
                by_rank.append({"text": rendered, "complete": complete})
            modelled = [e for e in effects if e.modelled]
            nodes.append({
                "id": node.id, "x": node.x, "y": node.y, "name": self.label(node), "max": node.max_ranks,
                "sub": node.sub_tree, "ranks": by_rank,
                "icon": icons[node.id][0] if node.id in icons and self.icons.has(icons[node.id][0]) else None,
                "modelled": bool(modelled), "partly": bool(modelled) and len(modelled) < len(effects),
                "effects": [{"aura": e.aura, "kind": e.kind, "modelled": e.modelled, "note": e.note,
                             "stats": list(e.stats), "figures": list(e.figures)} for e in effects],
            })
        return {
            "class": tree.class_name, "grid": 600, "nodes": nodes, "specs": self.specs_of(tree)[0],
            "edges": [{"from": e.left, "to": e.right} for e in tree.edges],
            "cost_per_rank": tree.cost_per_rank,
            "anomalies": tree.anomalies,
        }

    # ---- request parsing ----
    def parse(self, body: dict) -> Request:
        if not isinstance(body, dict):
            raise UiError(400, "the request must be a JSON object")
        class_name = body.get("class")
        tree = self.tree_of(class_name if isinstance(class_name, str) else "")
        race = body.get("race")
        if race not in self.races(tree.class_name):
            raise UiError(400, f"no race {race!r} for {tree.class_name}; choose from {', '.join(self.races(tree.class_name))}")
        lo, hi = (self.stats.levels[0], self.stats.levels[-1]) if self.stats.levels else (1, 60)
        level = self._int(body.get("level", hi), "level")
        if not lo <= level <= hi:
            raise UiError(400, f"level must be between {lo} and {hi}")
        alloc_in = body.get("alloc", {})
        if not isinstance(alloc_in, dict):
            raise UiError(400, "alloc must map node ids to ranks")
        alloc = {}
        for node_id, ranks in alloc_in.items():
            if node_id not in tree.nodes:
                raise UiError(400, f"node {node_id} is not in the {tree.class_name} tree")
            ranks = self._int(ranks, f"ranks for node {node_id}")
            if not 0 <= ranks <= tree.nodes[node_id].max_ranks:
                raise UiError(400, f"node {node_id}: ranks must be 0 to {tree.nodes[node_id].max_ranks}")
            if ranks:
                alloc[node_id] = ranks
        adjust_in = body.get("adjust", {})
        if not isinstance(adjust_in, dict):
            raise UiError(400, "adjust must map stat names to numbers")
        adjust = {}
        for key, value in adjust_in.items():
            if key not in ADJUSTABLE:
                raise UiError(400, f"cannot adjust {key!r}; choose from {', '.join(ADJUSTABLE)}")
            value = self._int(value, key)
            if value:
                adjust[key] = value
        attacker = body.get("attacker_level")
        attacker = None if attacker in (None, "") else self._int(attacker, "attacker level")
        return Request(tree.class_name, race, level, alloc, adjust, bool(body.get("shield")), attacker,
                       bool(body.get("vs_player")), bool(body.get("values", True)))

    @staticmethod
    def _int(value, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise UiError(400, f"{what} must be a whole number")
        try:
            number = float(value)
        except ValueError:
            raise UiError(400, f"{what} must be a whole number") from None
        if not math.isfinite(number) or number != int(number) or abs(number) > 10**7:
            raise UiError(400, f"{what} must be a whole number")
        return int(number)

    # ---- the sheet ----
    def sheet_for(self, req: Request, alloc: dict[str, int]) -> Sheet:
        tree = self.tree_of(req.class_name)
        talents = [RankedTalent(n, tree.nodes[n].spell_id, self.label(tree.nodes[n]), r, tuple(self.effects_of(tree.nodes[n])))
                   for n, r in sorted(alloc.items()) if r > 0]
        try:
            return build_sheet(self.stats, req.class_name, req.race, req.level, talents, req.adjust,
                               self.sheet_base, self.rates, req.shield)
        except (KeyError, ValueError) as exc:
            raise UiError(400, str(exc.args[0]) if exc.args else str(exc)) from exc

    @staticmethod
    def metrics(sheet: Sheet) -> dict[str, object]:
        out: dict[str, object] = {}
        for stat in ("health", "mana") + SHEET_STATS:
            if sheet.lines[stat].total is not None:
                out[stat] = sheet.lines[stat].total
        for name, fig in sheet.figures.items():
            if fig.total is not None:
                out[name] = fig.total
        if sheet.attack_power.total is not None:
            out["attack_power"] = sheet.attack_power.total
        if sheet.armor.total is not None:
            out["armor"] = sheet.armor.total
        for name in ("spell_power", "healing"):
            line = getattr(sheet, name)
            if line.total is not None:
                out[name] = line.total
        return out

    @staticmethod
    def difference(a: dict, b: dict) -> dict:
        """a - b for every metric, leaving out the ones that did not move."""
        out = {}
        for key in a.keys() & b.keys():
            d = a[key] - b[key]
            if d:
                out[key] = num(d)
        return out

    @staticmethod
    def power_json(line) -> dict:
        return {"base": line.base, "talents": line.from_talents, "gear": line.item, "total": line.total,
                "from": [[label, stat, num(p)] for label, stat, p in line.contributions]}

    def sheet_json(self, sheet: Sheet, req: Request) -> dict:
        lines = {}
        for stat, line in sheet.lines.items():
            lines[stat] = {"base": line.base, "exact": line.base_exact, "adjust": line.adjustment,
                           "talents": line.talent_change, "total": line.total,
                           "from": [[label, pct] for label, pct in line.contributions]}
        figures = {}
        for name, fig in sheet.figures.items():
            if fig.base is None and not (fig.contributions or fig.from_talent_stats or fig.from_adjustments):
                continue
            figures[name] = {"base": num(fig.base), "talents": num(fig.talent_total), "adjust": num(fig.from_adjustments),
                             "total": num(fig.total), "plus_minus": num(fig.uncertainty),
                             "from": [[label, num(p)] for label, p in fig.contributions],
                             "from_stats": num(fig.from_talent_stats)}
        ap = sheet.attack_power
        arm = sheet.armor
        out = {
            "lines": lines, "figures": figures,
            "attack_power": {"base": ap.base, "talents": ap.talent_total, "adjust": ap.from_adjustments, "total": ap.total,
                             "from": [[label, stat, num(p)] for label, stat, p in ap.contributions]},
            "armor": {"base": arm.base, "talents": arm.from_talent_stats, "adjust": arm.from_adjustments, "gear": arm.item_armor,
                      "total": arm.total, "damage_reduction": num(arm.damage_reduction)},
            "spell_power": self.power_json(sheet.spell_power),
            "healing": self.power_json(sheet.healing),
            "block_value": sheet.block_value_from_strength,
            "notes": list(sheet.notes),
            "not_modelled": [{"label": n.label, "ranks": n.ranks, "parts": [{"effect": i, "kind": k, "why": w} for i, k, w in n.parts]}
                             for n in sheet.not_modelled],
        }
        block = sheet.figures.get("block")
        if req.attacker_level is not None and req.shield and block is not None and block.total is not None and self.rates is not None:
            a = self.rates.assumed
            shift = a.block_against(req.attacker_level, req.level, req.vs_player) - a.block_base_chance
            vs = max(Decimal(0), block.total + Decimal(shift.numerator) / Decimal(shift.denominator))
            out["block_against"] = {"level": req.attacker_level, "player": req.vs_player, "chance": num(vs)}
        return out

    # ---- /api/evaluate ----
    def evaluate(self, body: dict) -> dict:
        req = self.parse(body)
        tree = self.tree_of(req.class_name)
        verdict = check(tree, req.alloc, req.level)
        sheet = self.sheet_for(req, req.alloc)
        points_have = tree.budget.points(req.level)
        nodes = {}
        for node_id, node in tree.nodes.items():
            ranks = req.alloc.get(node_id, 0)
            add = can_add(tree, req.alloc, node_id, req.level) if ranks < node.max_ranks else None
            rem = can_remove(tree, req.alloc, node_id) if ranks else None
            nodes[node_id] = {"ranks": ranks,
                              "can_add": bool(add), "why_not": list(add.problems[:2]) if add is not None and not add else [],
                              "can_remove": bool(rem), "why_stuck": list(rem.problems[:2]) if rem is not None and not rem else []}
        out = {
            "legal": bool(verdict), "problems": list(verdict.problems),
            "spent": spent(tree, req.alloc), "budget": points_have, "nodes": nodes,
            "sheet": self.sheet_json(sheet, req),
        }
        if req.values:
            out["values"] = self.values(req, tree, sheet)
        return out

    def values(self, req: Request, tree: ClassTree, sheet: Sheet) -> list[dict]:
        base = self.metrics(sheet)
        rows = []
        for node in sorted(tree.nodes.values(), key=lambda n: (n.y, n.x, n.id)):
            if not any(e.modelled for e in self.effects_of(node)):
                continue
            ranks = req.alloc.get(node.id, 0)
            row = {"id": node.id, "name": self.label(node), "ranks": ranks, "max": node.max_ranks, "now": {}, "next": {}}
            if ranks:
                without = {k: v for k, v in req.alloc.items() if k != node.id}
                row["now"] = self.difference(base, self.metrics(self.sheet_for(req, without)))
            if ranks < node.max_ranks:
                more = dict(req.alloc)
                more[node.id] = ranks + 1
                row["next"] = self.difference(self.metrics(self.sheet_for(req, more)), base)
            rows.append(row)
        return rows

    # ---- /api/status ----
    def status(self) -> dict:
        checks = []

        def add(name: str, problems: list[str]):
            checks.append({"name": name, "ok": not problems, "problems": problems[:30]})

        add("Talent trees", self.model.all_anomalies())
        add("Base stats", list(self.stats.problems))
        if self.spec_names or self.spec_problems:
            add("Specialization names", self.spec_problems + [p for t in self.model.trees.values() for p in self.specs_of(t)[1]])
        if self.sheet_base is not None:
            add("Character panel figures", problems_in(self.sheet_base))
        if self.rates is not None:
            add("Fitted stat rates", list(self.rates.problems))
        out = {"build": self.build, "checks": checks, "notes": self.load_notes, "confidence": [
            {"item": a, "status": b, "detail": c} for a, b, c in CONFIDENCE]}
        out["icons"] = self.icon_summary()
        if self.rates is not None:
            out["references"] = {k: v for k, v in self.rates.references.items() if k in ("source", "build", "status")}
        return out

    def icon_summary(self) -> dict:
        wanted: set[str] = set()
        for tree in self.model.trees.values():
            if tree.class_name not in self._icon_ids:
                self._icon_ids[tree.class_name] = icon_ids(self.ds, tree)
            wanted.update(i for i, _ in self._icon_ids[tree.class_name].values())
        have = self.icons.ids() & wanted
        return {"wanted": len(wanted), "cached": len(have), "spellmisc": self.ds.has_table("SpellMisc")}

    # ---- saved builds ----
    def save(self, name: str, body: dict) -> dict:
        req = self.parse({**body, "values": False})
        self.store.put(name, {"class": req.class_name, "race": req.race, "level": req.level, "alloc": req.alloc,
                              "adjust": req.adjust, "shield": req.shield, "attacker_level": req.attacker_level,
                              "vs_player": req.vs_player, "build": self.build, "saved": _stamp()})
        return {"saved": name}

    def load_saved(self, name: str) -> dict:
        entry = self.store.get(name)
        out = dict(entry)
        if entry.get("build") != self.build:
            out["warning"] = f"saved with game build {entry.get('build')}; this is {self.build}. Talents that changed may not match."
        try:
            tree = self.tree_of(entry.get("class", ""))
            gone = [n for n in entry.get("alloc", {}) if n not in tree.nodes]
        except UiError:
            gone = list(entry.get("alloc", {}))
        if gone:
            out["alloc"] = {n: r for n, r in entry["alloc"].items() if n not in gone}
            out["warning"] = (out.get("warning", "") + f" {len(gone)} saved talent(s) no longer exist and were left out.").strip()
        return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def route(bench: Workbench, method: str, path: str, body: dict | None):
    """(status, JSON-able) for one API call. Raises UiError."""
    parts = [unquote(p) for p in path.strip("/").split("/")]
    if parts[:1] != ["api"]:
        raise UiError(404, "not found")
    parts = parts[1:]
    if method == "GET" and parts == ["meta"]:
        return 200, bench.meta()
    if method == "GET" and len(parts) == 2 and parts[0] == "tree":
        return 200, bench.tree(parts[1])
    if method == "GET" and parts == ["status"]:
        return 200, bench.status()
    if method == "POST" and parts == ["evaluate"]:
        return 200, bench.evaluate(body or {})
    if method == "GET" and parts == ["builds"]:
        return 200, {"builds": bench.store.names()}
    if len(parts) == 2 and parts[0] == "builds":
        if method == "GET":
            return 200, bench.load_saved(parts[1])
        if method == "PUT":
            return 200, bench.save(parts[1], body or {})
        if method == "DELETE":
            bench.store.delete(parts[1])
            return 200, {"deleted": parts[1]}
    raise UiError(404, "not found")


def host_name(header: str) -> str:
    """The host part of a Host header: '127.0.0.1:8765' -> '127.0.0.1', '[::1]:80' -> '[::1]'."""
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


def make_handler(bench: Workbench):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wowcalc"
        server_icons = bench.icons

        def log_message(self, fmt, *args):  # quiet: the console is for the person using it
            pass

        def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            return host_name(self.headers.get("Host") or "") in HOSTS

        def _handle(self, method: str):
            try:
                if not self._host_ok():
                    raise UiError(403, "this page only answers on 127.0.0.1 or localhost")
                url = urlsplit(self.path)
                if method == "GET" and url.path in ("/", "/index.html"):
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if method == "GET" and url.path.startswith("/icons/"):
                    found = self.server_icons.read(url.path[len("/icons/"):])
                    if found is None:
                        raise UiError(404, "no such icon")
                    return self._send(200, found[0], found[1])
                body = None
                if method in ("POST", "PUT"):
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        raise UiError(415, "send JSON with Content-Type: application/json")
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise UiError(413, "request too large")
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        raise UiError(400, "the request body is not valid JSON") from None
                status, payload = route(bench, method, url.path, body)
                self._send(status, payload)
            except UiError as exc:
                self._send(exc.status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001 - a bug must show up as an error message, not a dropped connection
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def serve(bench: Workbench, port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bench))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Talent calculator for build {bench.build} at {url}   (Ctrl+C to stop)")
    for note in bench.load_notes:
        print(f"NOTE: {note}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Run the local talent calculator page.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--build")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    args = ap.parse_args(argv)
    try:
        bench = Workbench(args.data_dir, args.build)
    except UiError as exc:
        print(f"ERROR: {exc.message}")
        return 2
    serve(bench, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
