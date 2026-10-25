"""Look at what a talent does, and try it in a stat table.

    python -m wowcalc.talentinfo "Mental Strength" "Heart of the Wild" --base 120
    python -m wowcalc.talentinfo sheet --class Priest --race Human --level 60 \\
        --talent "Mental Strength=3" --adjust intellect=20

The first form prints, for each talent: where it sits, each effect (decoded or why
not), the value at every rank, and the description rendered for every rank so it can be
compared with the in-game tooltip. With --base N it also shows what a stat of N becomes
at each rank.

The second form builds a character stat table. It does not check that the talents form
a legal build (use wowcalc.allocation for that); it says so when they do not.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from .allocation import check
from .basestats import load_base_stats
from .rates import RatesError, load_rates
from .sheetbase import SheetBaseError, load_sheet_base
from .dataset import Dataset, DatasetError
from .effects import STAT_PERCENT, EffectBook, render_tooltip
from .fetch import TABLES
from .stats import SHEET_STATS, apply_percents, build_sheet, ranked
from .trees import ClassTree, Node, TreeModel, load_model


def spell_names(ds: Dataset) -> dict[str, str]:
    cols = ds.columns("SpellName")
    i_id, i_name = cols.index("ID"), cols.index("Name_lang")
    return {v[i_id]: v[i_name] for v in ds.iter_values("SpellName") if len(v) > max(i_id, i_name)}


def find_talents(model: TreeModel, names: dict[str, str], query: str, class_name: str | None = None) -> list[tuple[ClassTree, Node]]:
    """Placed talents whose name is `query` (exact, ignoring case); if none, those that contain it."""
    q = query.strip().lower()
    candidates = [(t, n) for t in model.trees.values() for n in t.nodes.values()
                  if n.spell_id and (class_name is None or t.class_name.lower() == class_name.lower())]
    exact = [(t, n) for t, n in candidates if names.get(n.spell_id, "").lower() == q]
    if exact:
        return exact
    return [(t, n) for t, n in candidates if q in names.get(n.spell_id, "").lower()]


def describe(ds: Dataset, tree: ClassTree, node: Node, label: str, book: EffectBook, spells: dict[str, dict], base: int | None) -> list[str]:
    effects = book.decode(node)
    lines = [f"{label} ({tree.class_name}): node {node.id}, spell {node.spell_id}, definition {node.entries[0].definition_id}, "
             f"max ranks {node.max_ranks}"]
    for e in effects:
        values = ", ".join(f"{r}: {v:g}" for r, v in e.by_rank.items()) or "-"
        stats = f" -> {'/'.join(e.stats or e.figures)}" if (e.stats or e.figures) else ""
        note = f"  [{e.note}]" if e.note else ""
        lines.append(f"  effect {e.effect_index}: aura {e.aura}, {e.kind}{stats}{note}")
        lines.append(f"      value by rank: {values}")
    text = spells.get(node.spell_id, {}).get("Description_lang", "")
    if text:
        lines.append("  description by rank (compare with the in-game tooltip):")
        for rank in range(1, node.max_ranks + 1):
            rendered, complete = render_tooltip(text, effects, rank)
            lines.append(f"      rank {rank}: {rendered}" + ("" if complete else "   [not fully rendered]"))
    if base is not None:
        for e in effects:
            if e.kind == STAT_PERCENT and e.modelled:
                ladder = [apply_percents(base, [e.by_rank[r]]) for r in sorted(e.by_rank)]
                lines.append(f"  with a base of {base} {'/'.join(e.stats)}, ranks 0..{node.max_ranks}: {base} > " + " > ".join(map(str, ladder)))
    return lines


def _load(args) -> tuple[Dataset, TreeModel, str] | None:
    from .update import UpdateError, get_active
    try:
        build = args.build or get_active(args.data_dir)
    except UpdateError as exc:
        print(f"ERROR: {exc}")
        return None
    if build is None:
        print("No build is installed.")
        return None
    raw = args.data_dir / "raw" / build
    missing = [t for t in TABLES if not (raw / f"{t}.csv").is_file()]
    if missing:
        print(f"Build {build} lacks {len(missing)} table(s) ({', '.join(missing[:3])}...). Run: python -m wowcalc.update complete")
        return None
    ds = Dataset(raw, build)
    return ds, load_model(ds), build


def _parse_pairs(items: list[str], what: str) -> dict[str, int] | None:
    out: dict[str, int] = {}
    for item in items:
        name, sep, value = item.rpartition("=")
        try:
            if not sep or not name.strip():
                raise ValueError
            out[name.strip()] = int(value)
        except ValueError:
            print(f"ERROR: {what} must look like NAME=NUMBER, got {item!r}")
            return None
    return out


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Inspect talents and try them in a stat table.")
    ap.add_argument("command_or_talent", nargs="*", help="talent names, or 'sheet' followed by options")
    ap.add_argument("--class", dest="class_name")
    ap.add_argument("--race")
    ap.add_argument("--level", type=int, default=60)
    ap.add_argument("--base", type=int, help="show what a stat of this size becomes at each rank")
    ap.add_argument("--talent", action="append", default=[], metavar="NAME=RANKS")
    ap.add_argument("--adjust", action="append", default=[], metavar="STAT=AMOUNT",
                    help="stats or armor you add by hand, e.g. agility=20 or armor=1500 (armor on your gear)")
    ap.add_argument("--shield", action="store_true", help="the character wears a shield (shows block)")
    ap.add_argument("--attacker-level", type=int, help="with --shield: also show block chance against an attacker of this level")
    ap.add_argument("--vs-player", action="store_true", help="the attacker is a player (0.2%% per level instead of 0.5%% for a mob)")
    ap.add_argument("--build")
    ap.add_argument("--data-dir", type=Path, default=repo_root / "data")
    args = ap.parse_args(argv)

    words = args.command_or_talent
    sheet_mode = bool(words) and words[0] == "sheet"
    if not words or (sheet_mode and len(words) > 1):
        ap.print_usage()
        return 2

    loaded = _load(args)
    if loaded is None:
        return 2
    ds, model, build = loaded
    names = spell_names(ds)
    spells = {r["ID"]: r for r in ds.rows("Spell")}
    book = EffectBook.for_model(ds, model)

    if not sheet_mode:
        status = 0
        for query in words:
            matches = find_talents(model, names, query, args.class_name)
            if not matches:
                print(f"No talent matches {query!r}.")
                status = 1
                continue
            for tree, node in matches:
                print("\n".join(describe(ds, tree, node, names.get(node.spell_id, node.spell_id), book, spells, args.base)) + "\n")
        return status

    if not args.class_name or not args.race:
        print("sheet needs --class and --race")
        return 2
    wanted = _parse_pairs(args.talent, "--talent")
    adjust = _parse_pairs(args.adjust, "--adjust")
    if wanted is None or adjust is None:
        return 2
    try:
        tree = model.by_name(args.class_name)
    except KeyError:
        print(f"No class {args.class_name!r}. Classes: {', '.join(sorted(t.class_name for t in model.trees.values()))}")
        return 2
    stats_file = args.data_dir / "external" / "base_stats.tsv"
    if not stats_file.is_file():
        print(f"No base stats file at {stats_file}. See: python -m wowcalc.basestats")
        return 2
    table = load_base_stats(stats_file)
    if table.problems or not table.rows:
        print("The base stats file has problems; run: python -m wowcalc.basestats")
        return 2

    sheet_base, sheet_base_note = None, ""
    figures_file = args.data_dir / "external" / "sheet_base.tsv"
    if figures_file.is_file():
        try:
            sheet_base = load_sheet_base(figures_file)
        except SheetBaseError as exc:
            sheet_base_note = f"{figures_file.name} could not be read ({exc})"
    else:
        sheet_base_note = f"no {figures_file.name}; crit, hit, dodge, parry and attack power show talent bonuses only"

    rates, rates_note = None, ""
    rates_file = args.data_dir / "external" / "stat_rates.json"
    if rates_file.is_file():
        try:
            rates = load_rates(rates_file)
        except RatesError as exc:
            rates_note = str(exc)
    else:
        rates_note = f"no {rates_file.name}; extra stats do not change crit, dodge or attack power (python -m wowcalc.rates fit)"

    talents, allocation = [], {}
    for query, ranks in wanted.items():
        matches = find_talents(model, names, query, args.class_name)
        if len(matches) != 1:
            print(f"{query!r}: {'no talent' if not matches else 'more than one talent'} matches in {tree.class_name}")
            return 2
        _, node = matches[0]
        allocation[node.id] = ranks
        talents.append(ranked(book, node, names.get(node.spell_id, node.spell_id), ranks))
    try:
        sheet = build_sheet(table, tree.class_name, args.race, args.level, talents, adjust, sheet_base, rates, args.shield)
    except (KeyError, ValueError) as exc:
        print(f"ERROR: {exc.args[0] if exc.args else exc}")
        return 2

    verdict = check(tree, allocation, level=args.level)
    print(f"Build {build}: {sheet.race_name} {sheet.class_name}, level {sheet.level}")
    if not verdict:
        print("NOTE: these talents are not a legal build on their own:")
        for problem in verdict.problems:
            print(f"  - {problem}")
    print(f"\n{'stat':10} {'base':>6} {'adjust':>7} {'talents':>8} {'total':>7}")
    for stat in ("health", "mana") + SHEET_STATS:
        line = sheet.lines[stat]
        if line.base is None:
            print(f"{stat:10} {'-':>6}")
            continue
        mark = "" if line.base_exact else "  (base estimated)"
        change = f"{line.talent_change:+d}" if (line.contributions or line.talent_change) else ""
        why = "  <- " + ", ".join(f"{label} {pct:+g}%" for label, pct in line.contributions) if line.contributions else (
            "  <- from extra " + ("Stamina" if stat == "health" else "Intellect") if line.talent_change else "")
        print(f"{stat:10} {line.base:>6} {line.adjustment:>+7d} {change:>8} {line.total:>7}{mark}{why}")
    shown = [f for f in sheet.figures.values() if f.contributions or f.base is not None or f.from_talent_stats or f.from_adjustments]
    uncertain = False
    if shown:
        print(f"\n{'figure':12} {'base':>8} {'talents':>8} {'adjust':>8} {'total':>8}")
        for f in shown:
            base = "-" if f.base is None else f"{float(f.base):.2f}%"
            total = "-" if f.total is None else f"{float(f.total):.2f}%"
            talents_part = f"{float(f.talent_total):+.2f}%" if (f.contributions or f.from_talent_stats) else ""
            adjust_part = f"{float(f.from_adjustments):+.2f}%" if f.from_adjustments else ""
            why = [f"{label} {float(pts):+g}" for label, pts in f.contributions]
            if f.from_talent_stats:
                why.append(f"extra stats {float(f.from_talent_stats):+.2f}")
            uncertain = uncertain or f.uncertainty >= Decimal("0.005")
            err = f"  (+/-{float(f.uncertainty):.2f})" if f.uncertainty >= Decimal("0.005") else ""
            print(f"{f.figure:12} {base:>8} {talents_part:>8} {adjust_part:>8} {total:>8}{err}" + ("  <- " + ", ".join(why) if why else ""))
    ap = sheet.attack_power
    if ap.contributions or ap.base is not None or ap.from_talent_stats or ap.from_adjustments:
        base = "-" if ap.base is None else str(ap.base)
        total = "-" if ap.total is None else str(ap.total)
        talents_part = f"{ap.talent_total:+d}" if (ap.contributions or ap.from_talent_stats) else ""
        adjust_part = f"{ap.from_adjustments:+d}" if ap.from_adjustments else ""
        why = [f"{label} ({float(pct):g}% of {stat})" for label, stat, pct in ap.contributions]
        if ap.from_talent_stats:
            why.append(f"extra Strength/Agility {ap.from_talent_stats:+d}")
        print(f"{'attack power':12} {base:>8} {talents_part:>8} {adjust_part:>8} {total:>8}" + ("  <- " + ", ".join(why) if why else ""))
    for name, line in (("spell power", sheet.spell_power), ("healing", sheet.healing)):
        if line.base is not None and (line.contributions or line.item):
            why = [f"{label} ({float(pct):g}% of {stat})" for label, stat, pct in line.contributions]
            print(f"{name:12} {line.base:>8} {(f'{line.from_talents:+d}' if line.contributions else ''):>8} {(f'{line.item:+d}' if line.item else ''):>8} {line.total:>8}" + ("  <- " + ", ".join(why) if why else ""))
    arm = sheet.armor
    if arm.base is not None:
        parts = [f"{arm.base} base"]
        if arm.from_talent_stats:
            parts.append(f"{arm.from_talent_stats:+d} from the talents' extra Agility")
        if arm.from_adjustments:
            parts.append(f"{arm.from_adjustments:+d} from adjusted Agility")
        if arm.item_armor:
            parts.append(f"{arm.item_armor:+d} gear")
        dr = "" if arm.damage_reduction is None else f", damage reduction {float(arm.damage_reduction):.2f}%"
        print(f"{'armor':12} {arm.total:>8}{dr}  ({'; '.join(parts)})")
    block = sheet.figures.get("block")
    if args.attacker_level is not None and block is not None and block.total is not None and rates is not None:
        a = rates.assumed
        shift = a.block_against(args.attacker_level, sheet.level, args.vs_player) - a.block_base_chance
        vs = max(Decimal(0), block.total + Decimal(shift.numerator) / Decimal(shift.denominator))
        kind = "player" if args.vs_player else "mob"
        print(f"{'block vs lvl':12} {float(vs):>7.2f}%  (against a level {args.attacker_level} {kind}; level-difference direction inferred)")
    elif args.attacker_level is not None:
        print("\nNOTE: --attacker-level needs --shield and a class that can block")
    if sheet.block_value_from_strength is not None:
        print(f"{'block value':12} {sheet.block_value_from_strength:>8}  (from Strength only; the shield's own block value is not included)")
    for note in [sheet_base_note, rates_note, *sheet.notes]:
        if note:
            print(f"\nNOTE: {note}")
    if uncertain:
        print("\nThe +/- values come from the fitted stat rates, which the panel only fixes to a range.")
    if sheet.not_modelled:
        print("\nPart of these talents is not in the table:")
        for item in sheet.not_modelled:
            print(f"  - {item.label} (rank {item.ranks}): " + "; ".join(f"effect {i} {k} ({why})" for i, k, why in item.parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
