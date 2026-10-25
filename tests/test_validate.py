"""Offline tests for wowcalc.dataset and wowcalc.validate.
Run: python -m unittest discover -s tests
"""

import contextlib
import copy
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from fixtures import (  # noqa: E402
    TempCase, base_tables, drop_column, row_for, write_dataset,
)
from wowcalc.dataset import Dataset, DatasetError, list_builds, open_dataset  # noqa: E402
from wowcalc.fetch import EXTENDED_TABLES  # noqa: E402
from wowcalc.validate import (  # noqa: E402
    GateResult, Thresholds, evaluate_gates, main, validate_dataset,
)

V_OLD, V_NEW = "1.60.1.70338", "1.60.1.70425"


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------

class DatasetTests(TempCase):
    def test_rows_columns_counts_skip_blank_rows(self):
        d = self.root / "d"
        d.mkdir()
        (d / "T.csv").write_text("ID,Name\n1,a\n,\n\n2,b\n", encoding="utf-8")
        ds = Dataset(d)
        self.assertEqual(ds.columns("T"), ["ID", "Name"])
        self.assertEqual(ds.row_count("T"), 2)  # streamed count
        self.assertEqual(len(ds.rows("T")), 2)  # parsed rows agree

    def test_bom_and_quoted_newline(self):
        d = self.root / "d"
        d.mkdir()
        (d / "T.csv").write_bytes(b'\xef\xbb\xbfID,Text\n1,"a\nb"\n')
        ds = Dataset(d)
        self.assertEqual(ds.columns("T")[0], "ID")
        self.assertEqual(ds.rows("T")[0]["Text"], "a\nb")

    def test_missing_table(self):
        with self.assertRaises(DatasetError):
            Dataset(self.root).rows("Nope")

    def test_no_header(self):
        d = self.root / "d"
        d.mkdir()
        (d / "T.csv").write_text("", encoding="utf-8")
        with self.assertRaises(DatasetError):
            Dataset(d).columns("T")

    def test_list_builds_and_open(self):
        for sub, v in (("raw", V_OLD), ("staging", V_NEW)):
            (self.root / sub / v).mkdir(parents=True)
        (self.root / "raw" / "junk").mkdir()
        self.assertEqual([b.version for b in list_builds(self.root / "raw")], [V_OLD])
        self.assertEqual(open_dataset(self.root, V_NEW).directory, self.root / "staging" / V_NEW)
        self.assertEqual(open_dataset(self.root, V_OLD).directory, self.root / "raw" / V_OLD)
        with self.assertRaises(DatasetError):
            open_dataset(self.root, "1.60.1.1")


# --------------------------------------------------------------------------
# validate_dataset
# --------------------------------------------------------------------------

class ValidateDatasetTests(TempCase):
    def test_clean(self):
        r = self.report(base_tables(), version=V_OLD)
        self.assertEqual(r.issues, [])
        self.assertEqual(r.row_counts["CurvePoint"], 10)
        self.assertEqual(r.stats["placedTalents"], 3)
        self.assertEqual((r.stats["entriesNotOnAnyNode"], r.stats["definitionsUnused"]), (0, 0))

    def test_missing_table(self):
        t = base_tables(); del t["Spell"]
        r = self.report(t)
        self.assertEqual(r.missing_tables, ["Spell"])
        self.assertIn("missing_table", self.codes(r, "error"))

    def test_missing_extended_table_is_an_error(self):
        t = base_tables(); del t["TraitCond"]
        r = self.report(t)
        self.assertEqual(r.missing_tables, ["TraitCond"])
        self.assertEqual(self.find(r, "missing_table")[0].subject, "TraitCond")

    def test_missing_required_column_in_an_extended_table(self):
        t = base_tables(); drop_column(t, "TraitCond", "SpentAmountRequired")
        r = self.report(t)
        self.assertEqual(self.find(r, "missing_columns")[0].values, ("SpentAmountRequired",))
        self.assertEqual(self.find(r, "missing_columns")[0].subject, "TraitCond")

    def test_missing_core_table_does_not_crash(self):
        t = base_tables(); del t["TraitEdge"]
        self.assertIn("missing_table", self.codes(self.report(t)))

    def test_missing_required_column_skips_structure_checks(self):
        t = base_tables(); drop_column(t, "TraitNodeEntry", "MaxRanks")
        r = self.report(t)
        self.assertEqual(self.find(r, "missing_columns")[0].values, ("MaxRanks",))
        self.assertEqual(r.stats, {})  # structure not attempted

    def test_unreadable_table(self):
        t = base_tables()
        write_dataset(self.root / "ds", t)
        (self.root / "ds" / "Spell.csv").write_bytes(b"")
        r = validate_dataset(Dataset(self.root / "ds"))
        self.assertIn("unreadable_table", self.codes(r, "error"))

    def test_empty_and_duplicate_keys(self):
        t = base_tables()
        t["TraitNode"][1].append(row_for(t, "TraitNode", ID="10", TraitTreeID="1", PosX="9", PosY="9"))
        t["TraitTree"][1].append(row_for(t, "TraitTree", ID="", TitleText_lang="x"))
        r = self.report(t)
        dup = self.find(r, "duplicate_primary_key")[0]
        self.assertEqual((dup.subject, dup.values), ("TraitNode.ID", ("10",)))
        self.assertEqual(self.find(r, "empty_key")[0].subject, "TraitTree.ID")

    def test_structural_orphans_are_errors(self):
        t = base_tables()
        t["TraitNode"][1].append(row_for(t, "TraitNode", ID="30", TraitTreeID="99", PosX="5", PosY="5"))
        t["TraitNodeXTraitNodeEntry"][1].append(row_for(t, "TraitNodeXTraitNodeEntry", ID="9", TraitNodeID="77", TraitNodeEntryID="100"))
        t["TraitNodeXTraitNodeEntry"][1].append(row_for(t, "TraitNodeXTraitNodeEntry", ID="8", TraitNodeID="10", TraitNodeEntryID="888"))
        t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID="300", TraitDefinitionID="3333", MaxRanks="1"))
        t["TraitEdge"][1].append(row_for(t, "TraitEdge", ID="2", LeftTraitNodeID="10", RightTraitNodeID="66"))
        r = self.report(t)
        subjects = {i.subject for i in self.find(r, "orphan_foreign_key") if i.severity == "error"}
        self.assertEqual(subjects, {
            "TraitNode.TraitTreeID", "TraitNodeXTraitNodeEntry.TraitNodeID",
            "TraitNodeXTraitNodeEntry.TraitNodeEntryID", "TraitNodeEntry.TraitDefinitionID",
            "TraitEdge.RightTraitNodeID"})

    def test_blank_and_zero_references_are_not_orphans(self):
        t = base_tables()
        t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="3000", SpellID="0"))
        t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="3001", SpellID=""))
        self.assertEqual(self.find(self.report(t), "orphan_foreign_key"), [])

    def test_spell_orphans_are_warnings_and_placed_talents_called_out(self):
        t = base_tables()
        # unplaced definition with no spell: FK warning only
        t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="3000", SpellID="9001"))
        t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID="300", TraitDefinitionID="3000", MaxRanks="1"))
        # placed definition with no spell: also a talent warning
        t["TraitDefinition"][1][0]["SpellID"] = "9002"
        # effect for an unknown spell
        t["SpellEffect"][1].append(row_for(t, "SpellEffect", ID="4", SpellID="9003"))
        r = self.report(t)
        self.assertEqual(self.codes(r, "error"), [])
        fk = {i.subject: i.values for i in self.find(r, "orphan_foreign_key")}
        self.assertEqual(fk["TraitDefinition.SpellID"], ("9001", "9002"))
        self.assertEqual(fk["SpellEffect.SpellID"], ("9003",))
        talent = self.find(r, "talent_missing_spell_data")
        self.assertEqual([(i.subject, i.tree_id, i.values) for i in talent],
                         [("TraitDefinition:1000", "1", ("9002",))])  # not 3000: it is unplaced

    def test_invalid_max_ranks_only_for_placed_entries(self):
        t = base_tables()
        t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"
        t["TraitNodeEntry"][1][1]["MaxRanks"] = "many"
        t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID="300", TraitDefinitionID="2000", MaxRanks="-4"))
        r = self.report(t)
        self.assertEqual(sorted(i.subject for i in self.find(r, "invalid_max_ranks")),
                         ["TraitNodeEntry:100", "TraitNodeEntry:101"])

    def test_tree_integrity_warnings(self):
        t = base_tables()
        t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3"))  # empty tree
        t["TraitNode"][1].append(row_for(t, "TraitNode", ID="12", TraitTreeID="1", PosX="0", PosY="0"))  # dup position, no entry
        t["TraitEdge"][1].append(row_for(t, "TraitEdge", ID="2", LeftTraitNodeID="10", RightTraitNodeID="20"))  # cross-tree
        r = self.report(t)
        got = {(i.code, i.subject) for i in r.warnings}
        self.assertEqual(got, {
            ("empty_tree", "TraitTree:3"), ("node_without_entry", "TraitNode:12"),
            ("duplicate_position", "TraitTree:1@0,0"), ("cross_tree_edge", "TraitEdge:2")})
        self.assertEqual(self.codes(r, "error"), [])

    def test_unused_and_unplaced_are_stats_not_issues(self):
        t = base_tables()
        t["TraitNodeEntry"][1].append(row_for(t, "TraitNodeEntry", ID="300", TraitDefinitionID="2000", MaxRanks="1"))
        t["TraitDefinition"][1].append(row_for(t, "TraitDefinition", ID="4000", SpellID="5000"))
        r = self.report(t)
        self.assertEqual(r.issues, [])
        self.assertEqual((r.stats["entriesNotOnAnyNode"], r.stats["definitionsUnused"]), (1, 1))

    def test_errors_sorted_before_warnings(self):
        t = base_tables()
        t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3"))
        t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"
        r = self.report(t)
        self.assertEqual([i.severity for i in r.issues], ["error", "warning"])


# --------------------------------------------------------------------------
# evaluate_gates
# --------------------------------------------------------------------------

class GateTests(TempCase):
    def pair(self, mutate):
        base = base_tables()
        cand = copy.deepcopy(base)
        mutate(cand)
        return self.report(cand, "cand"), self.report(base, "base")

    def test_identical_is_clear(self):
        c, b = self.pair(lambda t: None)
        g = evaluate_gates(c, b)
        self.assertEqual((g.blockers, g.needs_ack), ((), ()))

    def test_no_baseline_error_blocks_warning_needs_ack(self):
        t = base_tables()
        t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"
        t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3"))
        g = evaluate_gates(self.report(t), None)
        self.assertTrue(g.blocked)
        self.assertEqual(len(g.needs_ack), 1)

    def test_baseline_error_is_grandfathered(self):
        def bad(t):
            t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"
        base = base_tables(); bad(base)
        cand = base_tables(); bad(cand)
        g = evaluate_gates(self.report(cand, "c"), self.report(base, "b"))
        self.assertFalse(g.blocked)
        self.assertEqual(g.carried, 1)

    def test_new_error_blocks(self):
        def bad(t):
            t["TraitNodeEntry"][1][0]["MaxRanks"] = "0"
        c, b = self.pair(bad)
        g = evaluate_gates(c, b)
        self.assertTrue(g.blocked)
        self.assertIn("TraitNodeEntry:100".split(":")[1], g.blockers[0])

    def test_new_values_on_known_error_block(self):
        def with_orphans(t, *ids):
            for i, spell in enumerate(ids):
                t["TraitNode"][1].append(row_for(t, "TraitNode", ID=str(50 + i), TraitTreeID=spell, PosX=str(i), PosY="7"))
        base = base_tables(); with_orphans(base, "90")
        cand = base_tables(); with_orphans(cand, "90", "91")
        g = evaluate_gates(self.report(cand, "c"), self.report(base, "b"))
        self.assertTrue(g.blocked)
        self.assertIn("[new: 91]", g.blockers[0])

    def test_fewer_orphans_than_baseline_is_fine(self):
        # Same row counts in both builds; the baseline edge points at a missing
        # node, the candidate's edge has been repaired.
        base = base_tables()
        base["TraitEdge"][1].append(row_for(base, "TraitEdge", ID="2", LeftTraitNodeID="11", RightTraitNodeID="66"))
        cand = base_tables()
        cand["TraitEdge"][1].append(row_for(cand, "TraitEdge", ID="2", LeftTraitNodeID="11", RightTraitNodeID="10"))
        b = self.report(base, "b")
        self.assertEqual(b.errors[0].values, ("66",))  # baseline really has the orphan
        g = evaluate_gates(self.report(cand, "c"), b)
        self.assertEqual((g.blockers, g.needs_ack), ((), ()))

    def test_new_warning_needs_ack_known_warning_does_not(self):
        def warn(t):
            t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3"))
        c, b = self.pair(warn)
        g = evaluate_gates(c, b)
        self.assertFalse(g.blocked)
        self.assertEqual(len(g.needs_ack), 1)
        base = base_tables(); warn(base)
        cand = base_tables(); warn(cand)
        g2 = evaluate_gates(self.report(cand, "c2"), self.report(base, "b2"))
        self.assertEqual(g2.needs_ack, ())

    def test_new_spell_orphans_on_known_warning_need_ack_with_values(self):
        base = base_tables()
        base["SpellEffect"][1].append(row_for(base, "SpellEffect", ID="4", SpellID="9001"))
        cand = base_tables()
        for i, s in enumerate(("9001", "9002")):
            cand["SpellEffect"][1].append(row_for(cand, "SpellEffect", ID=str(4 + i), SpellID=s))
        g = evaluate_gates(self.report(cand, "c"), self.report(base, "b"))
        self.assertFalse(g.blocked)
        self.assertIn("[new: 9002]", g.needs_ack[0])

    def test_row_drop_threshold(self):
        def drop(n):
            def f(t):
                t["CurvePoint"] = (t["CurvePoint"][0], t["CurvePoint"][1][:n])
            return f
        c, b = self.pair(drop(7))  # 10 -> 7 = 30% loss
        g = evaluate_gates(c, b)
        self.assertTrue(g.blocked)
        self.assertIn("CurvePoint dropped from 10 to 7", g.blockers[0])
        c, b = self.pair(drop(9))  # 10% loss
        self.assertFalse(evaluate_gates(c, b).blocked)
        c, b = self.pair(drop(7))
        self.assertFalse(evaluate_gates(c, b, Thresholds(max_row_drop=0.5)).blocked)

    def test_growth_is_fine(self):
        def grow(t):
            t["CurvePoint"][1].extend(row_for(t, "CurvePoint", ID=str(100 + i), CurveID="8", OrderIndex=str(i)) for i in range(50))
        c, b = self.pair(grow)
        self.assertEqual(evaluate_gates(c, b).blockers, ())

    def test_removed_optional_column_needs_ack(self):
        c, b = self.pair(lambda t: drop_column(t, "TraitNode", "Flags"))
        g = evaluate_gates(c, b)
        self.assertFalse(g.blocked)
        self.assertIn("TraitNode: column(s) removed or renamed: Flags", g.needs_ack)

    def test_added_column_is_silent(self):
        def add(t):
            header, data = t["TraitNode"]
            t["TraitNode"] = (header + ["Extra"], [dict(r, Extra="x") for r in data])
        c, b = self.pair(add)
        g = evaluate_gates(c, b)
        self.assertEqual((g.blockers, g.needs_ack), ((), ()))

    def test_removed_required_column_blocks(self):
        c, b = self.pair(lambda t: drop_column(t, "TraitNode", "PosX"))
        self.assertTrue(evaluate_gates(c, b).blocked)

    def test_missing_table_blocks_even_if_baseline_also_lacks_it(self):
        base = base_tables(); del base["Spell"]
        cand = base_tables(); del cand["Spell"]
        g = evaluate_gates(self.report(cand, "c"), self.report(base, "b"))
        self.assertTrue(g.blocked)
        self.assertEqual(len([x for x in g.blockers if "Spell is missing" in x]), 1)  # not duplicated

    def test_blocked_property_and_dict(self):
        g = GateResult(("x",), ("y",), 2)
        self.assertTrue(g.blocked)
        self.assertEqual(g.to_dict(), {"blocked": True, "blockers": ["x"], "needsAcknowledgement": ["y"], "carriedFromBaseline": 2})


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class CliTests(TempCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.root / "data"), *args])
        return code, out.getvalue()

    def place(self, sub, version, mutate=None):
        t = base_tables()
        if mutate:
            mutate(t)
        write_dataset(self.root / "data" / sub / version, t)

    def test_nothing_staged(self):
        code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("Nothing is staged", out)

    def test_clean_candidate_passes_and_writes_report(self):
        self.place("raw", V_OLD)
        self.place("staging", V_NEW)
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("CLEAR", out)
        report = json.loads((self.root / "data" / "reports" / f"{V_NEW}.validation.json").read_text())
        self.assertEqual(report["baseline"], V_OLD)
        self.assertFalse(report["gates"]["blocked"])
        self.assertEqual(report["validation"]["version"], V_NEW)

    def test_blocked_candidate_returns_one(self):
        self.place("raw", V_OLD)
        self.place("staging", V_NEW, lambda t: t["TraitNodeEntry"][1][0].update(MaxRanks="0"))
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)

    def test_warning_prints_ack_but_exit_zero(self):
        self.place("raw", V_OLD)
        self.place("staging", V_NEW, lambda t: t["TraitTree"][1].append(row_for(t, "TraitTree", ID="3")))
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Needs acknowledgement", out)

    def test_no_baseline_when_nothing_installed(self):
        self.place("staging", V_NEW)
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("(none", out)

    def test_candidate_never_its_own_baseline(self):
        self.place("raw", V_OLD)
        code, out = self.run_cli(V_OLD)
        self.assertIn("(none", out)

    def test_no_write_flag(self):
        self.place("staging", V_NEW)
        self.run_cli("--no-write")
        self.assertFalse((self.root / "data" / "reports").exists())

    def test_unknown_version(self):
        self.place("raw", V_OLD)
        code, out = self.run_cli("1.60.1.1")
        self.assertEqual(code, 2)

    def test_explicit_baseline_and_threshold(self):
        self.place("raw", V_OLD)
        self.place("staging", V_NEW, lambda t: t.update(CurvePoint=(t["CurvePoint"][0], t["CurvePoint"][1][:7])))
        self.assertEqual(self.run_cli(V_NEW, "--baseline", V_OLD)[0], 1)
        self.assertEqual(self.run_cli(V_NEW, "--baseline", V_OLD, "--max-row-drop", "0.5")[0], 0)


# --------------------------------------------------------------------------
# Real data (skipped when the build is not present)
# --------------------------------------------------------------------------

REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL_70338 = REAL_ROOT / "raw" / V_OLD


@unittest.skipUnless(REAL_70338.is_dir(), f"real data not found at {REAL_70338}")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = validate_dataset(Dataset(REAL_70338, V_OLD))

    def test_counts_match_first_session_report(self):
        rc = self.report.row_counts
        self.assertEqual(
            (rc["TraitTree"], rc["TraitNode"], rc["TraitNodeEntry"], rc["TraitNodeXTraitNodeEntry"],
             rc["TraitDefinition"], rc["TraitEdge"], rc["Spell"], rc["SpellEffect"]),
            (17, 558, 659, 561, 658, 98, 31863, 42569))

    def test_no_errors_and_known_warnings(self):
        legacy = {i.subject for i in self.report.errors}
        # A download made before the extended tables existed is "legacy": the only
        # errors allowed are the missing extended tables themselves.
        self.assertEqual(legacy, {t for t in EXTENDED_TABLES if not (REAL_70338 / f"{t}.csv").is_file()})
        self.assertTrue(all(i.code == "missing_table" for i in self.report.errors))
        self.assertEqual({(i.code, i.subject) for i in self.report.warnings}, {
            ("empty_tree", "TraitTree:1118"),
            ("node_without_entry", "TraitNode:104806"),
            ("orphan_foreign_key", "TraitDefinition.SpellID"),
            ("orphan_foreign_key", "SpellEffect.SpellID"),
            ("talent_missing_spell_data", "TraitDefinition:134244"),
        })

    def test_known_orphan_counts(self):
        fk = {i.subject: i for i in self.find("orphan_foreign_key")}
        self.assertEqual(len(fk["TraitDefinition.SpellID"].values), 7)
        self.assertIn("11 reference(s)", fk["SpellEffect.SpellID"].message)

    def find(self, code):
        return [i for i in self.report.issues if i.code == code]

    def test_against_itself_only_blocked_by_missing_extended_tables(self):
        g = evaluate_gates(self.report, self.report)
        self.assertEqual(g.needs_ack, ())
        self.assertTrue(all("is missing" in b for b in g.blockers))
        self.assertEqual(len(g.blockers), len(self.report.missing_tables))


if __name__ == "__main__":
    unittest.main()
