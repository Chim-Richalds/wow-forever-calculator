# Backlog

Items are tagged:
- **[servers]** needs the game servers (an in-game reading or check). Do these first when they come back up.
- **[offline]** can be done now from the data we hold.
- **[needs-source]** blocked on finding a trustworthy source.

Principles that apply to everything below: data-driven, versioned, traceable, never guess, mark what is not modelled, tested.

## When the servers come back up  [servers]

- [ ] **Mana regeneration.** Fill in `data/external/regen_readings.tsv` (template is there): total Spirit and Intellect, gear mana per 5 seconds, and the "not casting" and "casting" figures from the mana regen tooltip. Take Spirit at three values, Intellect at two, and Meditation at 0 and 3 ranks, at level 60 (and 1 if possible), for two different classes. Then fit the formula (is it linear in Spirit? does Intellect matter?) and turn the "Mana regen while casting" percentage into real mana per 5 seconds. Nothing in the data holds this: no regeneration table was found in the build, and no source for WoW Forever has been found. Do not borrow Classic formulas without a reading to check them against.
- [ ] **Undead or Tauren x1.05 health.** The base-stats table gives Undead the 1.05 health factor; the owner expects it to be Tauren. Check both on a character. The data cannot decide this.
- [ ] **Strength and Agility flow-through.** Read the sheet before and after a stat talent (or a buff) and compare with the calculator: attack power, crit, dodge, armor.
- [ ] **Spiritual Guidance, rank 5.** Check spell damage and healing against the calculator. The data gives damage 1, 3, 5, 6, 8% and healing 5, 10, 15, 20, 25% per rank, which looks odd. Also confirms how the result is rounded (the calculator rounds down).
- [ ] **Spell power and healing rounding** for the other three talents that use it (Mental Quickness, Natural Instinct, Champion of the Light).
- [ ] **Parry for a class that has none on the panel.** The calculator counts a parry talent from 0% for such a class. Check it on any class that gets parry from a talent.
- [ ] **Mana regen while casting base.** The calculator takes the base as 0% (the texts say the talent "allows" N%). Check without any of the six talents.
- [ ] **Block.** The 5% base, the level-difference rule and Strength to block value are taken from the game's own text, not tested. Check with a shield on a Warrior, Paladin and Shaman.
- [ ] **Specialization names.** Look through all 27 trees in the UI and confirm each name (`data/external/spec_names.tsv`), especially the two Protection trees.
- [ ] **Iron Will.** The text says it reduces Stun and Fear durations by up to 15%. The data says charmed (+3 .. +15, which looks like Classic's resist chance) and stunned (no rank values). Check the stun and fear durations with 0 and 5 ranks. Left unsupported until then.
- [ ] **Stacking of threat, damage and crowd control percents.** Each is listed per talent, not summed. Check how two combine (added or multiplied), e.g. Naturalist with Moonfury on a Nature spell.
- [ ] **Silent Resolve threat schools.** The text says "your spells"; the data says Holy and Shadow only. Check threat on a non-Holy, non-Shadow spell if a Priest has one.
- [ ] **Total health percent** (Survivalist, Improved Reincarnation): the calculator applies it after Stamina and gear and rounds down. Check health with 0 and 5 ranks of Survivalist, with and without gear Stamina.
- [ ] **Armor from Intellect** (Arcane Resilience): 25/50/75% of Intellect, rounded down. Check armor at 0 and 3 ranks with an Intellect total that is not a multiple of 4.
- [ ] **Hit and spell hit.** The Hit row merges melee and spell hit when they are equal. Confirm with a talent that raises only one of them.

## Modelling more talents  [offline]

The full list is `docs/unmodelled_effects.tsv` (regenerate it after each change and each new build: `python -m wowcalc.talentinfo unmodelled --out docs/unmodelled_effects.tsv`). Of 468 talents, 52 are fully modelled and 20 partly (build 1.60.1.70707-hf202610311059, 2026-11-02). What is left, by type:

- [ ] Auras decoded for the sheet but still conditional: 54, 290, 55, 49, 47, 51 (abilities with a duration, a single form).
- [ ] What is not valued on 1.60.1.70707-hf202610311059 (694 effects): 181 belong to abilities and forms (85 talents; kind `ability`, need a combat model), 275 change particular spells, 180 are scripts, and 58 are undecoded auras on passive talents (most common: 332 x13, 79 x6, 122 x3, 231 x3; 332 are single-spell changes such as Improved Slam, 79 and 10 the equipment- or state-dependent ones). Read the tooltips first; never assume the meaning from the aura number.
- [x] Situational damage and threat: the page has a box per situation a class's talents need (shield, two-handed weapon, Defensive Stance, Rockbiter Weapon); effects that need one are listed as "not counted: needs ..." until it is set. Weapon-scoped bonuses ("damage with wands / ranged weapons / two-handed melee weapons") are shown with that scope. A condition at the end of a sentence covers the sentence's earlier values unless they have their own (fixes Paladin Two-Handed Weapon Specialization, whose damage was counted without the weapon).
- [ ] Still not counted for lack of data: Lone Wolf (its text shows the script effect's 20%, not the damage aura's), Sharpened Claws and Furious Precision (no rank values). Forms and an active pet would need inputs too.
- [x] The passive-spell check (SpellMisc) also guards the sheet decoders. On 1.60.1.70707 all 55 counted sheet effects are on passive spells, so nothing changed.
- [ ] Spell modifiers (auras 107 and 108, about 300 effects): they change particular spells (cost, damage, cast time), not the sheet. Needs a different engine and a model of the spells.
- [ ] Scripts (auras 4, 42, 109, 190 effects): the data holds numbers only; the tooltip says what they mean.
- [ ] Instant effects (no aura, 74).
- [ ] Hunter Bestial Discipline: the pet Focus part.
- [ ] Careful Aim's aura 580 is the ranged half of its attack power from Intellect (aura 598, the melee half, is valued). Needs a ranged attack power row.
- [x] Spell damage restricted to some schools (aura 174 with a mask other than all magic schools): no talent has one on 1.60.1.70707-hf202610311059. The decoder refuses it with the reason if one appears; model it then (the school names come from Resistances, as for threat and damage).

## Interface  [offline]

- [x] Spell power and Healing rows always show, so gear can be entered; with no panel figure for the level the total stays unknown (–) instead of assuming 0.
- [x] **Talent icons: 361 of 361** on 1.60.1.70707-hf202610311059 (2026-11-02). The 11 missing ones were exported from wow.export by talent name and imported with `python -m wowcalc.icons import FOLDER --talent-names` (two files with misspelled names were imported from renamed copies). After each update, `python -m wowcalc.icons status` shows what is missing.
- [x] Per-class race and level, and the last class, are remembered in the browser (a loaded build keeps its own).
- [x] Threat, damage done, damage taken and crowd control durations are in the Values tab (each talent's own percent).

## Codebase clean-up  [offline]

Goal: less code saying the same thing, with no loss of coverage of real behaviour. Do it in small steps, and keep the full test run green after each one.

- [x] **Tests.** Checked all 901: no copied tests, and only the IPv6 tests call internals (on purpose). Merged the two groups that checked one behaviour with different inputs into table-driven tests (895 now). Other near-identical tests document distinct behaviour and stay. Further cuts would lose coverage; revisit only with a coverage tool.
- [x] **Redundant sections in `src/wowcalc/`.** One integer parser (`dataset.as_int`), one atomic JSON writer (`fetch.write_json_atomic`), one spell name helper in `diff.py`; unused imports removed. No other copied logic found.
- [x] `wowcalc.icons tables` removed: SpellMisc is a required table, and `update complete` adds it to older builds.
- [x] **One-off scripts.** Removed the 20 investigation scripts (Purification, Chain Heal, tree 1082; superseded by `wowcalc.fetch`, `validate`, `rules` and `effects`) and their outputs in `data/processed/`. Kept `scripts/probe_tables.py`, now defaulting to the active build.
- [x] **Stray files at the repo root.** Removed (two exact copies, and `icon_ids.txt`, which `python -m wowcalc.icons list` regenerates).
- [x] **Compiled files in git.** Untracked.
- [x] **Rate fit summary.** Prints the attack power rule in use, with the panel fit as a note.

## Talent tree backgrounds  [needs-source]

- [ ] Searches in wow.export keep returning the modern game's assets. Find the Classic talent frame art for this build, then add an importer like the icon one (`src/wowcalc/icons.py`) and draw it behind each specialization tree. The names are already in `data/external/spec_names.tsv`.

## Data upkeep  [offline]

- [ ] Builds come from Blizzard's `wowf` product (WoW Forever's own). Until 2026-11-01 the calculator tracked `wow_classic_beta`, which stopped at 1.60.1.70532 on 2026-10-22 while `wowf` moved on (70707 on 2026-10-30). If updates ever stop again, compare the products on https://wago.tools/api/builds.
- [ ] Server hotfixes are part of the data (since 2026-11-01): datasets are `<build>-hf<YYYYMMDDHHMM>`. Wago's hotfixed export keeps deleted rows, so deletions are applied from the hotfix list (status 2); statuses 3 (Invalidate) and 4 (NotPublic) are counted in the manifest (`hotfixOtherStatus`) but not applied: on 1.60.1.70707-hf202610311059 there are 99 such records (CurvePoint, Spell, SpellName, SpellEffect, SpellMisc) and none touches a talent spell, effect or scaling curve. The update review now asks for confirmation when one touches talent data (Trait tables, talent spells, their effects, misc rows and scaling curves). Still open: what the game does with them.
- [ ] When a new build appears: `python -m wowcalc.update prepare` and `apply`, then `python -m wowcalc.diff`, then re-run `python -m wowcalc.rates fit` and the data checks in the UI. Spec names are tied to spells, so a moved talent shows up in the Data tab's "Specialization names" check.
- [ ] Per-level stat rates (`data/external/PlayerExpectedStat.csv`): refetch when the build changes. Last checked against 1.60.1.70707 with and without hotfixes (2026-11-02): identical to the copy in use; `rates fit` unchanged.

## Done recently (for context)

- Local UI with saved builds, not-modelled warnings and a data/confidence tab.
- Talent icons (357 of 359), the resizable divider, specialization names.
- Spell power and healing rows; spell power and healing from Intellect or Spirit talents.
- Parry row appears only where it applies; Hit row merges melee and spell hit.
- Mana regeneration while casting, as a percentage only.
- Crowd control durations (aura 232), named from the build's SpellMechanic table and checked against each tooltip.
- Threat, damage done and damage taken percents (auras 10, 79, 87) for passive talents, with schools from the build's Resistances table.
