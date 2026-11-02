"""Offline tests for wowcalc.effects. Run: python -m unittest discover -s tests"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from effect_fixtures import ACTIVE, add_curve, add_effect, write_effects  # noqa: E402
from fixtures import TempCase  # noqa: E402
from wowcalc.dataset import Dataset  # noqa: E402
from wowcalc.effects import (  # noqa: E402
    ABILITY, ALL_STATS, AP_FROM_STAT, ARMOR_FROM_STAT, RAP_FROM_STAT, CC_DURATION, DAMAGE_DONE, DAMAGE_TAKEN, THREAT, SP_FROM_STAT, SCRIPT, condition_in, SHEET_ADD, SPELL_MODIFIER, STAT_PERCENT, UNSUPPORTED, EffectBook, TalentEffect, decode_stats,
    render_tooltip, decode_schools, effect_phrase, schools_label,
)
from wowcalc.fetch import TABLES  # noqa: E402
from wowcalc.trees import Node, load_model  # noqa: E402


class DecodeCase(TempCase):
    def decode(self, mutate=None, node_id="1"):
        ds = Dataset(write_effects(self.root / "ds", mutate), "test")
        model = load_model(ds)
        return EffectBook.for_model(ds, model).decode(model.by_name("Mage").nodes[node_id])

    def one(self, mutate, node_id="1"):
        effects = self.decode(mutate, node_id)
        self.assertEqual(len(effects), 1)
        return effects[0]


class StatDecodingTests(unittest.TestCase):
    def test_mask_bits(self):
        self.assertEqual(decode_stats(0, 1), (("strength",), ""))
        self.assertEqual(decode_stats(0, 8), (("intellect",), ""))
        self.assertEqual(decode_stats(0, 16), (("spirit",), ""))
        self.assertEqual(decode_stats(0, 5), (("strength", "stamina"), ""))
        self.assertEqual(decode_stats(0, 31), (ALL_STATS, ""))

    def test_unknown_mask_bits_are_refused(self):
        for mask in (32, 40, 64):
            self.assertIn("unknown stat mask", decode_stats(0, mask)[1])

    def test_the_mask_wins_when_both_fields_are_set(self):
        self.assertEqual(decode_stats(4, 2), (("agility",), ""))

    def test_index_form_when_there_is_no_mask(self):
        self.assertEqual(decode_stats(4, 0), (("spirit",), ""))  # Demonic Embrace's hidden Spirit penalty
        self.assertEqual(decode_stats(3, 0), (("intellect",), ""))
        self.assertEqual(decode_stats(-1, 0), (ALL_STATS, ""))

    def test_nothing_specified_is_not_guessed_as_strength(self):
        self.assertIn("not specified", decode_stats(0, 0)[1])

    def test_unknown_index(self):
        self.assertIn("unknown stat index 9", decode_stats(9, 0)[1])


class DecodeTests(DecodeCase):
    def stat_talent(self, values=(2, 4), **effect):
        def mutate(t):
            add_effect(t, 1, **effect)
            add_curve(t, 1, effect.get("index", 0), values)
        return mutate

    def test_a_stat_percent_talent(self):
        e = self.one(self.stat_talent())
        self.assertEqual((e.kind, e.stats, e.by_rank, e.aura, e.modelled), (STAT_PERCENT, ("intellect",), {1: 2.0, 2: 4.0}, 137, True))

    def test_the_curve_replaces_the_base_points(self):
        e = self.one(self.stat_talent(base=99))
        self.assertEqual(e.by_rank, {1: 2.0, 2: 4.0})

    def test_a_penalty_keeps_its_sign(self):
        self.assertEqual(self.one(self.stat_talent(values=(-1, -2))).by_rank, {1: -1.0, 2: -2.0})

    def test_float_formatted_ranks_still_match(self):
        def mutate(t):
            add_effect(t, 1)
            add_curve(t, 1, 0, (2, 4))
            for p in t["CurvePoint"][1]:
                p["Pos_0"] += ".0"
        self.assertEqual(self.one(mutate).by_rank, {1: 2.0, 2: 4.0})

    def test_a_mask_of_several_stats(self):
        self.assertEqual(self.one(self.stat_talent(misc1=5)).stats, ("strength", "stamina"))

    def test_an_unknown_mask_makes_the_effect_unsupported(self):
        e = self.one(self.stat_talent(misc1=32))
        self.assertEqual((e.kind, e.modelled, e.by_rank), (UNSUPPORTED, False, {}))
        self.assertIn("unknown stat mask 32", e.note)

    def test_single_rank_talent_without_scaling_uses_its_base_points(self):
        e = self.one(lambda t: add_effect(t, 4, base=10), node_id="4")
        self.assertEqual((e.kind, e.by_rank), (STAT_PERCENT, {1: 10.0}))

    def test_multi_rank_talent_without_scaling_is_unsupported(self):
        e = self.one(lambda t: add_effect(t, 1))
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("2-rank talent with no scaling data", e.note)

    def test_a_curve_missing_a_rank_is_unsupported(self):
        e = self.one(self.stat_talent(values=(2,)))
        self.assertEqual((e.kind, e.by_rank), (UNSUPPORTED, {}))
        self.assertIn("no value for rank 2", e.note)

    def test_a_curve_that_starts_at_the_wrong_rank_is_unsupported(self):
        def mutate(t):
            add_effect(t, 1)
            add_curve(t, 1, 0, (2, 4), first_rank=0)  # ranks 0 and 1: rank 2 is missing
        self.assertIn("no value for rank 2", self.one(mutate).note)

    def test_an_unfamiliar_scaling_operation_is_unsupported(self):
        def mutate(t):
            add_effect(t, 1)
            add_curve(t, 1, 0, (2, 4), operation=1)
        e = self.one(mutate)
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("scaling operation 1 is not understood", e.note)

    def test_two_scaling_links_for_one_effect_are_unsupported(self):
        def mutate(t):
            add_effect(t, 1)
            add_curve(t, 1, 0, (2, 4))
            add_curve(t, 1, 0, (3, 6))
        self.assertIn("more than one scaling link", self.one(mutate).note)

    def test_scaling_is_matched_by_effect_index(self):
        def mutate(t):
            add_effect(t, 1, index=0, misc1=8)
            add_effect(t, 1, index=1, aura=4)
            add_curve(t, 1, 0, (2, 4))
            add_curve(t, 1, 1, (10, 20))
        e0, e1 = self.decode(mutate)
        self.assertEqual((e0.effect_index, e0.by_rank), (0, {1: 2.0, 2: 4.0}))
        self.assertEqual((e1.effect_index, e1.kind, e1.by_rank), (1, SCRIPT, {1: 10.0, 2: 20.0}))

    def test_effects_come_back_in_index_order(self):
        def mutate(t):
            add_effect(t, 1, index=2, aura=4)
            add_effect(t, 1, index=0, aura=4)
            add_effect(t, 1, index=1, aura=4)
            for i in range(3):
                add_curve(t, 1, i, (1, 2))
        self.assertEqual([e.effect_index for e in self.decode(mutate)], [0, 1, 2])

    def test_other_auras_are_classified_not_modelled(self):
        for aura, kind in ((108, SPELL_MODIFIER), (107, SPELL_MODIFIER), (4, SCRIPT), (42, SCRIPT), (109, SCRIPT),
                           (111, SCRIPT), (112, SCRIPT)):
            e = self.one(lambda t, a=aura: (add_effect(t, 4, aura=a, base=5)), node_id="4")
            self.assertEqual((e.kind, e.modelled), (kind, False), aura)

    def test_additive_sheet_auras(self):
        for aura, figures in ((47, ("parry",)), (49, ("dodge",)), (54, ("melee_hit",)), (55, ("spell_hit",)),
                              (290, ("melee_crit", "spell_crit"))):
            def mutate(t, a=aura):
                add_effect(t, 1, aura=a, misc0=0, misc1=0)
                add_curve(t, 1, 0, (2, 4))
            e = self.one(mutate)
            self.assertEqual((e.kind, e.figures, e.by_rank, e.modelled), (SHEET_ADD, figures, {1: 2.0, 2: 4.0}, True), aura)

    def test_an_additive_aura_with_no_rank_values_is_unsupported(self):
        e = self.one(lambda t: add_effect(t, 1, aura=47, misc0=0, misc1=0))  # 2-rank talent, no curve
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))

    def test_attack_power_from_a_stat(self):
        def mutate(t):
            add_effect(t, 1, aura=598, misc0=3, misc1=0)
            add_curve(t, 1, 0, (33, 67))
        e = self.one(mutate)
        self.assertEqual((e.kind, e.stats, e.by_rank, e.modelled), (AP_FROM_STAT, ("intellect",), {1: 33.0, 2: 67.0}, True))

    def test_attack_power_from_an_unknown_stat_is_refused(self):
        def mutate(t):
            add_effect(t, 1, aura=598, misc0=9, misc1=0)
            add_curve(t, 1, 0, (33, 67))
        e = self.one(mutate)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))
        self.assertIn("unknown source stat", e.note)

    def test_spell_damage_and_healing_from_a_stat(self):
        # as read from the data: Champion of the Light (174, school mask 126, stat in misc1), Natural Instinct (175, stat in misc0)
        def damage(t):
            add_effect(t, 1, aura=174, misc0=126, misc1=3)
            add_curve(t, 1, 0, (10, 20))
        e = self.one(damage)
        self.assertEqual((e.kind, e.stats, e.figures, e.by_rank, e.modelled), (SP_FROM_STAT, ("intellect",), ("spell_power",), {1: 10.0, 2: 20.0}, True))

        def healing(t):
            add_effect(t, 1, aura=175, misc0=4, misc1=0)
            add_curve(t, 1, 0, (5, 10))
        e = self.one(healing)
        self.assertEqual((e.kind, e.stats, e.figures, e.modelled), (SP_FROM_STAT, ("spirit",), ("healing",), True))

    def test_spell_damage_for_one_school_is_not_modelled(self):
        def mutate(t):
            add_effect(t, 1, aura=174, misc0=4, misc1=3)    # mask 4 = a single school
            add_curve(t, 1, 0, (10, 20))
        e = self.one(mutate)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))
        self.assertIn("some schools", e.note)

    def test_spell_power_from_an_unknown_stat_or_without_values_is_refused(self):
        for aura, m0, m1 in ((174, 126, 9), (175, 9, 0)):
            def mutate(t, aura=aura, m0=m0, m1=m1):
                add_effect(t, 1, aura=aura, misc0=m0, misc1=m1)
                add_curve(t, 1, 0, (1, 2))
            e = self.one(mutate)
            self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False), aura)
            self.assertIn("unknown source stat", e.note)
        e = self.one(lambda t: add_effect(t, 1, aura=175, misc0=3, misc1=0))   # 2 ranks, no curve
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))

    def test_an_unlisted_aura_says_so(self):
        e = self.one(lambda t: add_effect(t, 4, aura=77), node_id="4")
        self.assertEqual((e.kind, e.note), (UNSUPPORTED, "aura 77 is not decoded yet"))

    def test_an_effect_with_no_aura(self):
        e = self.one(lambda t: add_effect(t, 4, aura=0), node_id="4")
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("no aura", e.note)

    def test_no_effect_rows_or_no_entry(self):
        self.assertEqual(self.decode(None, "4"), [])
        ds = Dataset(write_effects(self.root / "empty"), "t")
        self.assertEqual(EffectBook(ds, set(), set()).decode(Node("x", "T", 0, 0, (), -1)), [])

    def test_for_model_covers_every_tree(self):
        def mutate(t):
            add_effect(t, 1, base=5); add_effect(t, 7, base=7)  # node 7 is the Warrior's
            add_curve(t, 1, 0, (2, 4))
        ds = Dataset(write_effects(self.root / "two", mutate), "t")
        model = load_model(ds)
        book = EffectBook.for_model(ds, model)
        self.assertEqual(book.decode(model.by_name("Warrior").nodes["7"])[0].by_rank, {1: 7.0})
        self.assertEqual(book.decode(model.by_name("Mage").nodes["1"])[0].by_rank, {1: 2.0, 2: 4.0})


class ConditionTests(unittest.TestCase):
    def test_durations_are_abilities_not_permanent_bonuses(self):
        for text in ("Stun the target for $24394d.", "Lasts $d.", "Increases chance to block by 30% for $d, and deals damage."):
            self.assertEqual(condition_in(text), "an ability that lasts a limited time", text)

    def test_a_while_clause_is_a_condition(self):
        self.assertEqual(condition_in("Increases your critical strike chance while in Bear Form, Dire Bear Form, or Cat Form by 6%."),
                         "only while in Bear Form")

    def test_off_hand_only_effects(self):
        self.assertEqual(condition_in("Increases your chance to hit with off-hand attacks by 10%."), "only for off-hand attacks")
        self.assertEqual(condition_in("Improves your chance to hit with Off Hand weapons"), "only for off-hand attacks")

    def test_a_while_clause_about_another_effect_does_not_count(self):
        text = "Increases your movement speed while in Cat Form by 30%, and increases your chance to Dodge by 4%."
        self.assertEqual(condition_in(text, ("dodge",)), "")  # the dodge was checked in game and is not form-limited
        self.assertEqual(condition_in(text, ("speed",)), "only while in Cat Form")
        self.assertEqual(condition_in(text), "only while in Cat Form")  # without keywords the whole text is examined

    def test_keywords_that_match_nothing_fall_back_to_the_whole_text(self):
        self.assertEqual(condition_in("Works only while stealthed.", ("parry",)), "only while stealthed")

    def test_a_duration_counts_wherever_it_is(self):
        self.assertEqual(condition_in("Stun the target for $24394d, and increases your chance to crit by 100%.", ("crit",)),
                         "an ability that lasts a limited time")

    def test_plain_permanent_text_has_no_condition(self):
        for text in ("Increases your Parry chance by 5%.", "Increases your chance to Block attacks with your shield by 5% and grants you a 100% chance to generate Rage when you Block.",
                     "Improves your chance to get a critical strike with melee attacks by 5%.", ""):
            self.assertEqual(condition_in(text), "", text)


class ConditionalDecodeTests(DecodeCase):
    def with_text(self, text, aura=47):
        def mutate(t):
            add_effect(t, 1, aura=aura, misc0=0, misc1=0)
            add_curve(t, 1, 0, (2, 4))
            for spell in t["Spell"][1]:
                if spell["ID"] == "3001":
                    spell["Description_lang"] = text
        return self.one(mutate)

    def test_a_timed_ability_is_not_a_sheet_bonus(self):
        e = self.with_text("Command your pet to Stun the target for $24394d.", aura=290)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))
        self.assertIn("not always on", e.note)
        self.assertIn("limited time", e.note)

    def test_a_form_only_bonus_is_not_a_sheet_bonus(self):
        e = self.with_text("Increases your critical strike chance while in Cat Form by 6%.", aura=290)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))
        self.assertIn("only while in Cat Form", e.note)

    def test_regeneration_while_casting_is_a_figure_not_a_condition(self):
        e = self.with_text("Allows $s1% of your Mana regeneration to continue while casting.", aura=134)
        self.assertEqual((e.kind, e.figures, e.by_rank, e.modelled), (SHEET_ADD, ("casting_regen",), {1: 2.0, 2: 4.0}, True))

    def test_regeneration_while_casting_without_rank_values_is_refused(self):
        def mutate(t):
            add_effect(t, 1, aura=134, misc0=0, misc1=0)       # 2-rank talent, no curve
        e = self.one(mutate)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))

    def test_a_permanent_bonus_is_kept(self):
        e = self.with_text("Increases your Parry chance by 2%.")
        self.assertEqual((e.kind, e.modelled), (SHEET_ADD, True))

    def test_block_and_melee_crit_are_decoded(self):
        self.assertEqual(self.with_text("Increases your chance to Block by 2%.", aura=51).figures, ("block",))
        self.assertEqual(self.with_text("Improves melee critical strikes by 2%.", aura=52).figures, ("melee_crit",))

    def test_a_total_health_percent(self):
        e = self.with_text("Increases your total Health by $s1%.", aura=133)
        self.assertEqual((e.kind, e.stats, e.by_rank), (STAT_PERCENT, ("health",), {1: 2.0, 2: 4.0}))
        self.assertEqual(self.with_text("Increases your total Mana by $s1%.", aura=133).kind, UNSUPPORTED)

    def test_armor_from_a_stat(self):
        def mutate(text, stat=3):
            def m(t):
                add_effect(t, 1, aura=268, misc0=stat, misc1=0)
                add_curve(t, 1, 0, (25, 50))
                for spell in t["Spell"][1]:
                    if spell["ID"] == "3001":
                        spell["Description_lang"] = text
            return m
        e = self.one(mutate("Increases your Armor by an amount equal to $s1% of your Intellect."))
        self.assertEqual((e.kind, e.stats, e.by_rank, e.modelled), (ARMOR_FROM_STAT, ("intellect",), {1: 25.0, 2: 50.0}, True))

    def test_armor_from_a_stat_the_text_does_not_name_is_refused(self):
        def m(t):
            add_effect(t, 1, aura=268, misc0=3, misc1=0)
            add_curve(t, 1, 0, (25, 50))
            for spell in t["Spell"][1]:
                if spell["ID"] == "3001":
                    spell["Description_lang"] = "Increases your Armor by an amount equal to $s1% of your Spirit."
        e = self.one(m)
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("does not say so", e.note)

    def test_ranged_attack_power_from_a_stat(self):
        def m(text):
            def mutate(t):
                add_effect(t, 1, aura=580, misc0=3, misc1=0)
                add_curve(t, 1, 0, (20, 40))
                for spell in t["Spell"][1]:
                    if spell["ID"] == "3001":
                        spell["Description_lang"] = text
            return mutate
        e = self.one(m("Increases your Attack Power by $s1% of your Intellect."))
        self.assertEqual((e.kind, e.stats, e.by_rank), (RAP_FROM_STAT, ("intellect",), {1: 20.0, 2: 40.0}))
        self.assertEqual(self.one(m("Increases your Attack Power by $s1% of your Agility.")).kind, UNSUPPORTED)

    def test_a_sheet_bonus_on_an_ability_is_refused(self):
        def mutate(t):
            add_effect(t, 1, aura=47, misc0=0, misc1=0)
            add_curve(t, 1, 0, (2, 4))
            t["SpellMisc"][1][0]["Attributes_0"] = ACTIVE
        e = self.one(mutate)
        self.assertEqual((e.kind, e.modelled, e.by_rank), (ABILITY, False, {1: 2.0, 2: 4.0}))   # values kept for reference
        self.assertIn("an ability or form", e.note)

    def test_without_spell_flags_the_text_check_alone_decides(self):
        def mutate(t):
            add_effect(t, 1, aura=47, misc0=0, misc1=0)
            add_curve(t, 1, 0, (2, 4))
            del t["SpellMisc"]
        self.assertEqual(self.one(mutate).kind, SHEET_ADD)

    def test_the_condition_guard_does_not_touch_stat_percent_talents(self):
        def mutate(t):
            add_effect(t, 1)
            add_curve(t, 1, 0, (2, 4))
            for spell in t["Spell"][1]:
                if spell["ID"] == "3001":
                    spell["Description_lang"] = "Increases Intellect by $s1% while in Bear Form."
        self.assertEqual(self.one(mutate).kind, STAT_PERCENT)


class RenderTests(unittest.TestCase):
    def effects(self, **by_index):
        return [TalentEffect("s", i, 4, SCRIPT, (), vals) for i, vals in by_index.items()]

    def eff(self, values):
        return [TalentEffect("s", i, 4, SCRIPT, (), v) for i, v in enumerate(values)]

    def test_substitutes_each_effect_value_for_the_rank(self):
        text, complete = render_tooltip("Intellect by $s1%, Stamina by $s2%.", self.eff([{1: 2, 2: 4}, {1: 4, 2: 8}]), 2)
        self.assertEqual((text, complete), ("Intellect by 4%, Stamina by 8%.", True))

    def test_m_and_s_are_equivalent(self):
        self.assertEqual(render_tooltip("by $m1%", self.eff([{3: 6}]), 3)[0], "by 6%")

    def test_the_sign_is_dropped_because_the_text_says_reduce(self):
        self.assertEqual(render_tooltip("Reduces by $s1%.", self.eff([{1: -5}]), 1)[0], "Reduces by 5%.")

    def test_fractions_are_kept(self):
        self.assertEqual(render_tooltip("$s1 sec", self.eff([{1: 2.5}]), 1)[0], "2.5 sec")

    def test_whitespace_is_normalised(self):
        self.assertEqual(render_tooltip("a\r\n  b   $s1", self.eff([{1: 1}]), 1)[0], "a b 1")

    def test_what_it_cannot_render_is_flagged(self):
        text, complete = render_tooltip("Reduces by ${$s1/-1000} sec.", self.eff([{1: 1000}]), 1)
        self.assertFalse(complete)
        self.assertIn("${", text)
        self.assertEqual(render_tooltip("by $s3%", self.eff([{1: 1}]), 1), ("by $s3%", False))   # no such effect
        self.assertFalse(render_tooltip("by $s1%", self.eff([{1: 1}]), 2)[1])                      # no value at that rank

    def test_plain_text_is_complete(self):
        self.assertEqual(render_tooltip("Does a thing.", [], 1), ("Does a thing.", True))


# --------------------------------------------------------------------------
# Real data (skipped unless a complete build is present)
# --------------------------------------------------------------------------

class MechanicDurationTests(DecodeCase):
    """Aura 232: the duration of one kind of crowd control on you (EffectMiscValue_0 = SpellMechanic ID)."""

    def with_mechanic(self, text, mechanic=12, values=(-5, -10), drop_names=False):
        def mutate(t):
            add_effect(t, 1, aura=232, base=values[-1] if values else -10, misc0=mechanic, misc1=0)
            if values:
                add_curve(t, 1, 0, values)
            for spell in t["Spell"][1]:
                if spell["ID"] == "3001":
                    spell["Description_lang"] = text
            if drop_names:
                del t["SpellMechanic"]
        return self.one(mutate)

    def test_a_reduction_named_in_the_text_is_decoded(self):
        e = self.with_mechanic("Reduces the duration of Stun effects on you by $s1%.")
        self.assertEqual((e.kind, e.figures, e.by_rank, e.modelled), (CC_DURATION, ("stunned",), {1: -5.0, 2: -10.0}, True))

    def test_the_name_comes_from_the_builds_table(self):
        e = self.with_mechanic("Reduces the duration of Fear effects on you by $s1%.", mechanic=5)
        self.assertEqual(e.figures, ("fleeing",))

    def test_without_the_names_table_the_mechanic_is_named_by_number(self):
        e = self.with_mechanic("Reduces the duration of Stun effects on you by $s1%.", drop_names=True)
        self.assertEqual((e.kind, e.figures), (CC_DURATION, ("mechanic 12",)))

    def test_a_mechanic_the_text_does_not_name_is_refused(self):
        e = self.with_mechanic("Reduces the duration of Fear effects on you by $s1%.", mechanic=12)
        self.assertEqual((e.kind, e.modelled), (UNSUPPORTED, False))
        self.assertIn("does not mention", e.note)

    def test_positive_values_on_a_reduction_are_refused(self):
        e = self.with_mechanic("Reduces the duration of Stun effects on you by $s1%.", values=(3, 6))
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("values are positive", e.note)

    def test_a_mechanic_without_a_tooltip_check_is_refused(self):
        e = self.with_mechanic("Reduces the duration of Bleed effects on you by $s1%.", mechanic=15)
        self.assertEqual(e.kind, UNSUPPORTED)
        self.assertIn("no tooltip check", e.note)

    def test_no_rank_values_is_refused_and_names_the_mechanic(self):
        e = self.with_mechanic("Reduces the duration of Stun effects on you by $s1%.", values=())
        self.assertEqual((e.kind, e.by_rank), (UNSUPPORTED, {}))
        self.assertIn("stunned duration", e.note)


class SchoolPercentTests(DecodeCase):
    """Auras 10 (threat), 79 (damage done), 87 (damage taken): a percent for the schools in a bit mask."""

    def school(self, text, aura=10, mask=16, values=(-10, -20), index=0, target=1, attrs=None, drop=None):
        def mutate(t):
            add_effect(t, 1, index=index, aura=aura, base=values[-1], misc0=mask, misc1=0, target=target)
            add_curve(t, 1, index, values)
            for spell in t["Spell"][1]:
                if spell["ID"] == "3001":
                    spell["Description_lang"] = text
            if attrs:
                t["SpellMisc"][1][0]["Attributes_0"] = attrs
            if drop:
                del t[drop]
        return self.one(mutate)

    def test_a_school_threat_reduction_is_decoded(self):
        e = self.school("Reduces the threat caused by your Frost spells by $s1%.")
        self.assertEqual((e.kind, e.figures, e.what, e.by_rank, e.modelled), (THREAT, ("Frost",), "Frost", {1: -10.0, 2: -20.0}, True))

    def test_damage_done_and_taken(self):
        e = self.school("Increases the damage done by your Arcane and Nature spells by $m1%.", aura=79, mask=72, values=(2, 4))
        self.assertEqual((e.kind, e.figures, e.what), (DAMAGE_DONE, ("Nature", "Arcane"), "Nature, Arcane"))
        e = self.school("Reduces all damage taken by $s1%.", aura=87, mask=127)
        self.assertEqual((e.kind, e.what), (DAMAGE_TAKEN, "all schools"))

    def test_the_value_is_matched_to_its_own_phrase(self):
        # Effect 1's phrase is the part after effect 0's value: the mana cost clause does not count.
        e = self.school("Reduces the mana cost of your Fire spells by $s1% and reduces the threat caused by your Frost spells by $s2%.", index=1)
        self.assertEqual((e.kind, e.figures), (THREAT, ("Frost",)))

    def test_an_ability_or_form_is_refused(self):
        e = self.school("Reduces the threat caused by your Frost spells by $s1%.", attrs=ACTIVE)
        self.assertEqual(e.kind, ABILITY)
        self.assertIn("an ability or form", e.note)

    def test_without_spell_flags_nothing_is_assumed(self):
        e = self.school("Reduces the threat caused by your Frost spells by $s1%.", drop="SpellMisc")
        self.assertIn("cannot tell whether it is always on", e.note)

    def test_an_effect_on_someone_else_is_refused(self):
        e = self.school("Increases your pet's damage by $s1%.", aura=79, mask=127, values=(5, 10), target=5)
        self.assertIn("not to you", e.note)

    def test_text_that_disagrees_is_refused(self):
        cases = [
            ("Reduces the threat caused by your Fire spells by $s1%.", 10, 16, (-10, -20), "the text names Fire but the data says Frost"),
            ("Increases the threat caused by your Frost spells by $s1%.", 10, 16, (-10, -20), "says it increases but the values are negative"),
            ("Reduces the mana cost of your Frost spells by $s1%.", 10, 16, (-10, -20), "not about threat"),
            ("Reduces all damage you deal by $s1%.", 87, 127, (-10, -20), "not about damage taken"),
            ("Increases all damage taken by $s1%.", 79, 127, (2, 4), "not about damage done"),
            ("Increases all damage you deal by $s1% while in Cat Form.", 79, 127, (2, 4), "only while in Cat Form"),
            ("Increases all damage you deal by $s1% in Battle Stance.", 79, 127, (2, 4), "only in Battle Stance"),
            ("Increases your damage with daggers by $s1%.", 79, 126, (2, 4), "particular equipment (daggers)"),
            ("Increases all damage by $s1% while Rockbiter Weapon is active and while Rockbiter Weapon is not active.", 79, 127, (2, 4), "both requires and excludes"),
            ("Increases all damage you deal by an amount.", 79, 127, (2, 4), "does not show this effect's value"),
        ]
        for text, aura, mask, values, why in cases:
            e = self.school(text, aura=aura, mask=mask, values=values)
            self.assertEqual(e.kind, UNSUPPORTED, text)
            self.assertIn(why, e.note, text)

    def test_school_masks(self):
        names = dict(enumerate(["Physical", "Holy", "Fire", "Nature", "Frost", "Shadow", "Arcane"]))
        self.assertEqual(decode_schools(28, names), (("Fire", "Nature", "Frost"), ""))
        self.assertIn("unknown school bits", decode_schools(128, names)[1])
        self.assertIn("Resistances", decode_schools(16, {})[1])
        every = set(names.values())
        self.assertEqual(schools_label(tuple(names.values()), every), "all schools")
        self.assertEqual(schools_label(tuple(names[i] for i in range(1, 7)), every), "all magic schools")

    def test_situations_and_weapon_scopes_are_recorded(self):
        e = self.school("Increases all damage you deal by $s1% while a shield is equipped.", aura=79, mask=1, values=(2, 4))
        self.assertEqual((e.kind, e.what, e.requires), (DAMAGE_DONE, "Physical", (("shield", True),)))
        e = self.school("Increases all threat generated in Defensive stance by an additional $s1% while a shield is equipped.", mask=127, values=(5, 10))
        self.assertEqual(e.requires, (("defensive_stance", True), ("shield", True)))
        e = self.school("Increases your damage with Wands by $s1%.", aura=79, mask=126, values=(13, 25))
        self.assertEqual((e.what, e.requires), ("with wands, all magic schools", ()))

    def test_a_condition_at_the_end_of_a_sentence_covers_the_earlier_value(self):
        text = "Increases the physical damage you deal by $s1%, and reduces all threat you generate by $m2% while a two-handed melee weapon is equipped."
        e = self.school(text, aura=79, mask=1, values=(2, 4))
        self.assertEqual(e.requires, (("two_hand", True),))
        # ... but not when the earlier value has its own condition (Spirit Weapons)
        text = "Reduces all threat generated by $s1% while Rockbiter Weapon is not active, and increases all threat generated by $s2% while Rockbiter Weapon is active."
        self.assertEqual(self.school(text, values=(-10, -20)).requires, (("rockbiter", False),))

    def test_effect_phrase(self):
        text = "Reduces the cast time of your Hammer of Wrath by ${$m1/-1000}.1 sec, and reduces all threat you generate by $m2% while Righteous Fury is not active."
        phrase, segment, verb, _ = effect_phrase(text, 1)
        self.assertEqual((phrase, verb), ("reduces all threat you generate by ", "reduc"))
        self.assertIn("while Righteous Fury", segment)
        self.assertIsNone(effect_phrase(text, 2))


PINNED = "1.60.1.70450"
REAL_ROOT = Path(os.environ.get("WOWCALC_DATA", REPO / "data"))
REAL = REAL_ROOT / "raw" / PINNED
COMPLETE = REAL.is_dir() and all((REAL / f"{t}.csv").is_file() for t in TABLES)

# Tooltips copied from the game, rank 1 to 5 (October 2026).
HEART_OF_THE_WILD = [
    f"Increases your Intellect by {2*r}%. In addition, while in Bear Form or Dire Bear Form your Stamina is "
    f"increased by {4*r}% and while in Cat Form your Strength is increased by {2*r}%." for r in range(1, 6)]
MENTAL_STRENGTH = [f"Increases your total Intellect by {2*r}%." for r in range(1, 6)]


@unittest.skipUnless(COMPLETE, f"a complete {PINNED} is not at {REAL}")
class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = Dataset(REAL, PINNED)
        cls.model = load_model(cls.ds)
        cls.book = EffectBook.for_model(cls.ds, cls.model)
        cls.spells = {r["ID"]: r for r in cls.ds.rows("Spell")}
        cols = cls.ds.columns("SpellName")
        i, n = cols.index("ID"), cols.index("Name_lang")
        cls.names = {v[i]: v[n] for v in cls.ds.iter_values("SpellName")}

    def talent(self, name, class_name):
        tree = self.model.by_name(class_name)
        found = [n for n in tree.nodes.values() if self.names.get(n.spell_id) == name]
        self.assertEqual(len(found), 1, name)
        return found[0]

    def rendered(self, node):
        effects = self.book.decode(node)
        text = self.spells[node.spell_id]["Description_lang"]
        return [render_tooltip(text, effects, r) for r in range(1, node.max_ranks + 1)]

    def test_heart_of_the_wild_matches_the_in_game_tooltips_exactly(self):
        node = self.talent("Heart of the Wild", "Druid")
        e0, e1, e2 = self.book.decode(node)
        self.assertEqual((e0.kind, e0.stats, e0.by_rank), (STAT_PERCENT, ("intellect",), {r: 2.0 * r for r in range(1, 6)}))
        self.assertEqual((e1.kind, e2.kind), (SCRIPT, SCRIPT))  # the bear and cat parts are game code
        self.assertEqual(self.rendered(node), [(t, True) for t in HEART_OF_THE_WILD])

    def test_mental_strength_matches_the_in_game_tooltips_exactly(self):
        node = self.talent("Mental Strength", "Priest")
        (e,) = self.book.decode(node)
        self.assertEqual((e.kind, e.stats, e.by_rank), (STAT_PERCENT, ("intellect",), {r: 2.0 * r for r in range(1, 6)}))
        self.assertEqual(self.rendered(node), [(t, True) for t in MENTAL_STRENGTH])

    def test_the_known_stat_talents_decode_to_the_right_stats(self):
        # Names repeat across classes (a Paladin "Toughness" is armor, a Shaman's is stamina),
        # so each is looked up in its own class. Hunter's Lightning Reflexes sits on two nodes.
        expect = [("Divine Strength", "Paladin", "strength"), ("Lightning Reflexes", "Hunter", "agility"),
                  ("Toughness", "Shaman", "stamina"), ("Sacred Duty", "Paladin", "stamina"),
                  ("Arcane Mind", "Mage", "intellect"), ("Divine Intellect", "Paladin", "intellect"),
                  ("Ancestral Knowledge", "Shaman", "intellect"), ("Living Spirit", "Druid", "spirit")]
        for name, class_name, stat in expect:
            nodes = [n for n in self.model.by_name(class_name).nodes.values() if self.names.get(n.spell_id) == name]
            self.assertTrue(nodes, f"{name} not found in {class_name}")
            for node in nodes:
                modelled = [e for e in self.book.decode(node) if e.modelled]
                self.assertTrue(modelled, f"{name} ({class_name}) has no modelled effect")
                self.assertEqual(modelled[0].stats, (stat,), name)

    # In-game readings (October 2026): sheet before > after each rank.
    def figures(self, name, class_name):
        node = self.talent(name, class_name)
        return {e.effect_index: (e.kind, e.figures, e.by_rank) for e in self.book.decode(node) if e.kind == SHEET_ADD}

    def test_natures_majesty_adds_crit_to_melee_and_spell(self):
        # spell crit 7.85 > 9.85 > 11.85 and melee crit 4.21 > 6.21 > 8.21
        self.assertEqual(self.figures("Nature's Majesty", "Druid"),
                         {0: (SHEET_ADD, ("melee_crit", "spell_crit"), {1: 2.0, 2: 4.0})})

    def test_natures_reach_adds_melee_and_spell_hit(self):
        # spell hit 0 > 2 > 4 and melee hit 0 > 2 > 4; its range change is a spell modifier
        self.assertEqual(self.figures("Nature's Reach", "Druid"),
                         {1: (SHEET_ADD, ("melee_hit",), {1: 2.0, 2: 4.0}), 2: (SHEET_ADD, ("spell_hit",), {1: 2.0, 2: 4.0})})

    def test_feral_swiftness_adds_dodge(self):
        # dodge 4.15 > 6.15 > 8.15; the movement speed part is game code
        self.assertEqual(self.figures("Feral Swiftness", "Druid"), {0: (SHEET_ADD, ("dodge",), {1: 2.0, 2: 4.0})})

    def test_deflection_adds_parry_and_differs_by_class(self):
        # Hunter parry 5 > 6 > 7 > 8 > 9 > 10
        self.assertEqual(self.figures("Deflection", "Hunter"), {0: (SHEET_ADD, ("parry",), {r: float(r) for r in range(1, 6)})})
        self.assertEqual(self.figures("Deflection", "Rogue"), {0: (SHEET_ADD, ("parry",), {1: 2.0, 2: 4.0, 3: 6.0})})

    def test_mental_dexterity_takes_attack_power_from_intellect(self):
        # The data says 33/67/100 (the rank-2 text copied from the game said 66; the readings below say 67)
        (e, _script) = self.book.decode(self.talent("Mental Dexterity", "Shaman"))
        self.assertEqual((e.kind, e.stats, e.by_rank), (AP_FROM_STAT, ("intellect",), {1: 33.0, 2: 67.0, 3: 100.0}))

    def test_every_sheet_bonus_talent_says_so_in_its_own_words(self):
        """Evidence for each decoded aura: the talent text names the figure the aura is taken to raise,
        and the rank values match the number in the text at the top rank."""
        words = {"parry": "parry", "dodge": "dodge", "block": "block", "melee_hit": "hit", "spell_hit": "hit",
                 "melee_crit": "crit", "spell_crit": "crit", "casting_regen": "while casting"}
        checked = 0
        for tree in self.model.trees.values():
            for node in tree.nodes.values():
                for e in self.book.decode(node):
                    if e.kind != SHEET_ADD:
                        continue
                    text = self.spells[node.spell_id]["Description_lang"].lower()
                    for figure in e.figures:
                        self.assertIn(words[figure], text, (tree.class_name, self.names[node.spell_id], figure))
                    checked += 1
        self.assertGreaterEqual(checked, 25)

    def cc(self, name, class_name):
        return {e.effect_index: (e.kind, e.figures, e.by_rank) for e in self.book.decode(self.talent(name, class_name)) if e.aura == 232}

    def test_crowd_control_durations(self):
        # Mechanic names from the build's SpellMechanic table; each matches the talent's text.
        r3 = {1: -5.0, 2: -10.0, 3: -15.0}
        self.assertEqual(self.cc("Silent Resolve", "Priest"), {1: (CC_DURATION, ("stunned",), r3), 2: (CC_DURATION, ("fleeing",), r3),
                                                               3: (CC_DURATION, ("silenced",), r3)})
        r10 = {1: -10.0, 2: -20.0, 3: -30.0}
        self.assertEqual(self.cc("Surefooted", "Hunter"), {0: (CC_DURATION, ("ensnared",), r10), 1: (CC_DURATION, ("rooted",), r10)})
        r15 = {1: -15.0, 2: -30.0}
        self.assertEqual(self.cc("Unyielding Faith", "Paladin"), {0: (CC_DURATION, ("fleeing",), r15), 1: (CC_DURATION, ("disoriented",), r15)})

    def test_threat_and_damage_percents(self):
        # (talent, class, effect index) -> (kind, schools shown, top-rank value). Each school list matches the tooltip.
        expected = {
            ("Frost Channeling", "Mage", 1): (THREAT, "Frost", -30.0),
            ("Burning Soul", "Mage", 1): (THREAT, "Fire", -30.0),
            ("Subtlety", "Druid", 0): (THREAT, "Nature, Arcane", -30.0),
            ("Silent Resolve", "Priest", 0): (THREAT, "Holy, Shadow", -30.0),  # text: "your spells"
            ("Natural Grace", "Shaman", 0): (THREAT, "all magic schools", -15.0),
            ("Suppression", "Warlock", 2): (THREAT, "all schools", -20.0),
            ("Naturalist", "Druid", 1): (DAMAGE_DONE, "all schools", 5.0),
            ("Moonfury", "Druid", 0): (DAMAGE_DONE, "Nature, Arcane", 10.0),
            ("Darkness", "Priest", 0): (DAMAGE_DONE, "Shadow", 10.0),
            ("Searing Light", "Priest", 0): (DAMAGE_DONE, "Holy", 5.0),
            ("Spell Warding", "Priest", 0): (DAMAGE_TAKEN, "all magic schools", -10.0),
            ("Elemental Warding", "Shaman", 0): (DAMAGE_TAKEN, "Fire, Nature, Frost", -10.0),
            ("Molten Skin", "Warlock", 0): (DAMAGE_TAKEN, "all schools", -10.0),
        }
        for (name, class_name, index), (kind, what, top) in expected.items():
            e = [x for x in self.book.decode(self.talent(name, class_name)) if x.effect_index == index][0]
            self.assertEqual((e.kind, e.what, e.by_rank[max(e.by_rank)]), (kind, what, top), name)

    def test_conditional_threat_and_damage_talents_are_not_counted(self):
        for name, class_name, why in (("Shadowform", "Priest", "an ability or form"), ("Death Wish", "Warrior", "an ability or form"),
                                      ("Power Infusion", "Priest", "an ability or form"), ("Bestial Wrath", "Hunter", "an ability or form"),
                                      ("Instrument of Law", "Paladin", "Righteous Fury"),
                                      ("Lone Wolf", "Hunter", "does not show this effect's value")):
            effects = [e for e in self.book.decode(self.talent(name, class_name)) if e.aura in (10, 79, 87)]
            self.assertTrue(effects, name)
            for e in effects:
                self.assertEqual(e.kind, ABILITY if why == "an ability or form" else UNSUPPORTED, name)
                self.assertIn(why, e.note, name)

    def test_situational_threat_and_damage_talents_say_when_they_count(self):
        # (talent, class) -> (kind, label, requirements): a situation the person sets, or the weapon it covers
        expected = {
            ("Bastion", "Warrior"): (DAMAGE_DONE, "Physical", (("shield", True),)),
            ("Defiance", "Warrior"): (THREAT, "all schools", (("defensive_stance", True), ("shield", True))),
            ("Spirit Weapons", "Shaman"): (THREAT, "all schools", (("rockbiter", False),)),
            ("Two-Handed Weapon Specialization", "Warrior"): (DAMAGE_DONE, "with two-handed melee weapons, Physical", ()),
            ("Wand Specialization", "Mage"): (DAMAGE_DONE, "with wands, all magic schools", ()),
        }
        for (name, class_name), want in expected.items():
            effects = [e for e in self.book.decode(self.talent(name, class_name)) if e.aura in (10, 79)]
            self.assertEqual([(e.kind, e.what, e.requires) for e in effects], [want], name)

    def test_iron_will_data_disagrees_with_its_text(self):
        # Text: "Stun and Fear" durations. Data: charmed at +3..+15 (Classic's resist chance) and stunned with no rank values.
        e0, e1 = self.book.decode(self.talent("Iron Will", "Warrior"))
        self.assertEqual((e0.kind, e1.kind), (UNSUPPORTED, UNSUPPORTED))
        self.assertIn("charmed", e0.note)
        self.assertIn("positive", e0.note)
        self.assertIn("no scaling data", e1.note)

    def test_activated_abilities_and_conditional_bonuses_are_not_counted(self):
        # All four carry a crit / block / hit aura but are not permanent, unconditional bonuses: two are
        # abilities (the game's passive flag is clear), two are passive with a condition in the text.
        for name, class_name, kind, why in (("Intimidation", "Hunter", ABILITY, "an ability or form"), ("Holy Shield", "Paladin", ABILITY, "an ability or form"),
                                            ("Sharpened Claws", "Druid", UNSUPPORTED, "only while"), ("Furious Precision", "Warrior", UNSUPPORTED, "off-hand")):
            node = self.talent(name, class_name)
            effects = [e for e in self.book.decode(node) if e.aura in (290, 51, 52, 54)]
            self.assertTrue(effects, name)
            for e in effects:
                self.assertEqual((e.kind, e.modelled), (kind, False), name)
                self.assertIn(why, e.note, name)

    def test_shield_specialization_and_cruelty_are_decoded(self):
        spec = [e for e in self.book.decode(self.talent("Shield Specialization", "Warrior")) if e.kind == SHEET_ADD]
        self.assertEqual([(e.figures, e.by_rank) for e in spec], [(("block",), {r: float(r) for r in range(1, 6)})])
        cruelty = [e for e in self.book.decode(self.talent("Cruelty", "Warrior")) if e.kind == SHEET_ADD]
        self.assertEqual([e.figures for e in cruelty], [("melee_crit",)])
        self.assertEqual(cruelty[0].by_rank[5], 5.0)

    def test_demonic_embrace_lost_its_hidden_spirit_penalty_in_this_build(self):
        # Build 70338 had a second effect (Spirit by plain index, -1%); the 70450 change
        # report lists it as removed. The index form itself is covered by decode_stats tests.
        node = self.talent("Demonic Embrace", "Warlock")
        effects = self.book.decode(node)
        self.assertEqual([e.effect_index for e in effects], [0])
        self.assertEqual((effects[0].kind, effects[0].stats), (STAT_PERCENT, ("stamina",)))

    def test_every_placed_talent_decodes_without_error_and_most_are_classified(self):
        kinds = {}
        for tree in self.model.trees.values():
            for node in tree.nodes.values():
                for e in self.book.decode(node):
                    kinds[e.kind] = kinds.get(e.kind, 0) + 1
        self.assertGreaterEqual(kinds.get(STAT_PERCENT, 0), 11)
        self.assertGreater(kinds.get(SPELL_MODIFIER, 0), 100)


if __name__ == "__main__":
    unittest.main()
