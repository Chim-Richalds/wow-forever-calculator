# Spell model: plan

Status: agreed 2026-11-02. Phases 1-3 now, phase 4 later with the combat model. Spell changes are shown below the stat sheet, in the same tab (no separate Spells tab).

## Why

The largest group of talent effects the calculator does not value changes particular spells: 275 effects on 200 talents (build 1.60.1.70707 with hotfixes). "Reduces the cast time of your Frostbolt by 0.5 sec", "Increases the critical strike chance of your Fire spells by 6%", "Reduces the cooldown of your Frost Nova by 4 sec". They never touch the character sheet, so the sheet cannot show them; they need a list of the class's spells and what each talent does to each one.

This plan stops short of damage per second. DPS needs a combat model (rotation, hit and crit tables, spell power coefficients, procs), which is a later step that builds on this one.

## What the data already says

Checked on the installed build, not assumed:

- **Two kinds of modifier.** Aura 107 adds a flat amount, aura 108 a percent (127 and 148 effects).
- **What is changed** is a small number in `EffectMiscValue_0` (the "modifier op"). 23 different ops appear. For one talent per op, the tooltip names the property the op suggests: 0 damage, 1 duration, 5 range, 6 radius, 7 crit chance, 10 cast time, 11 cooldown, 14 cost, 15 crit damage bonus, 21 global cooldown. That is a sample, not proof; phase 1 checks every effect against its text. One sample already disagrees: Arcane Instability's op 22 effect is the value its text calls "critical strike chance", yet the spells it matches are the ones with damage over time (Blizzard, Flamestrike, Pyroblast, Fireball's burn). The common ops (0, 7, 10, 11, 14, 15 and 22) cover about two thirds of the effects (174 of 275).
- **Which spells** comes from a 128-bit "spell class mask" on the effect (`EffectSpellClassMask_0..3`), matched against each spell's mask in `SpellClassOptions` within the class's spell family (`SpellClassSet`: Mage 3, Warrior 4, and so on).
- **The masks are precise once the list is limited to spells a player can learn** (`SkillLineAbility`, by class). Without that, they also match NPC spells that share the family (Atal'ai Poison, "Copy of Frostbolt"). With it:

| Talent | Tooltip names | Matched learnable spells |
|---|---|---|
| Improved Frostbolt | Frostbolt, Frost Blast | Frostbolt (Frost Blast left the spellbook in 70707) |
| Arctic Reach (range) | Frostbolt, Frost Blast, Ice Lance, Blizzard | Frostbolt, Ice Lance, Blizzard (Frost Blast left the spellbook in 70707) |
| Critical Mass | Fire spells | Blast Wave, Fire Blast, Fireball, Flamestrike, Frostfire Bolt, Pyroblast, Scorch |
| Frost Channeling | Frost spells | 12 Frost spells, armors and Ice Block included |
| Improved Slam | Slam | Slam |

- **30 effects match no learnable spell.** Most likely pet abilities and abilities granted by talents (not taught by a trainer). The catalogue below has to include those.
- **Every table needed is exported by Wago** for this build: `SpellClassOptions`, `SpellPower` (cost), `SpellCastTimes`, `SpellCooldowns`, `SpellDuration`, `SpellRange`, `SpellCategories` (global cooldown), `SpellLevels`, `SkillLineAbility`, `SpellEquippedItems`. `SpellMisc` (already fetched) holds each spell's school and its cast time, duration and range indexes.

## Design

1. **Spell catalogue per class.** Every spell the class can have: trainer spells (`SkillLineAbility` by class mask), abilities from talents (the talent spells that are not passive), and pet abilities where a talent targets them. Ranks are grouped under one name; the sheet shows the highest rank the character's level allows (`SpellLevels`).
2. **Base properties per spell**, straight from the tables: cost (mana, rage, energy, or percent of base mana), cast time, cooldown, global cooldown, duration, range, school, and the base values of its effects.
3. **Talent modifiers.** Decode aura 107/108 into (op, flat or percent, value per rank, spells matched). An op is accepted only when the talent's text names the property ("cast time", "cooldown", "mana cost"...) and, where the text names spells or a school, the matched spells agree. Anything else stays "not valued", with the reason, as today.
4. **Applying them.** Per spell and property: base, plus flat modifiers, times percent modifiers. The order and how percents combine (added or multiplied) is a game rule we have not checked; the first version adds percents and says so, and it goes on the in-game list.
5. **In the page.** A **Spells** section below the stat sheet, in the same tab: one row per spell with base and modified cost, cast time, cooldown, crit chance bonus, damage bonus, and which talents changed what. The **Talent values** tab gains per-spell lines ("Improved Frostbolt: Frostbolt cast time 3.0 → 2.5 s"). The same in `talentinfo` for the command line.
6. **Audit report.** For every modifier: the spells its mask matches next to the spells or school its text names. Mismatches are listed in the Data tab instead of being silently applied.

## Phases

Each phase ships on its own and leaves the calculator working.

1. **Catalogue and matching** (small–medium). **Done 2026-11-02:** `src/wowcalc/spells.py`, `python -m wowcalc.spells audit`, the Data tab check, `docs/spell-modifier-audit.tsv`. Result on 1.60.1.70707 with hotfixes: 176 of 275 modifiers agree with their text; 39 change a spell's own effect amounts (phase 4); 29 reach only pet, poison, totem or weapon spells (later); 47 disagree with their text in some way and are listed for review. Ops established from the texts: 0, 1, 2, 4, 5, 6, 7, 9, 10, 11, 14, 15, 16, 18, 21, 22; 3, 8 and 12 change effect amounts. Fetch the tables, build the per-class catalogue, resolve every modifier to its spells, write the audit report. No change to values yet. Tells us how many of the 275 can be matched cleanly.
2. **Simple properties** (medium). **Done 2026-11-02:** 98 modifiers that agree with their text are applied; the stat sheet lists the spells the build's talents change (base and with talents, the highest rank the level allows), the Talent values tab lists each talent's own spell changes, and `talentinfo sheet` prints them. Radius (op 6) is not shown yet (it needs the SpellRadius table). Cost, cast time, cooldown, global cooldown, range, radius, duration: numbers in, numbers out. Spells section and per-spell talent values.
3. **Damage-side properties** (medium). **Done 2026-11-02:** 73 more modifiers applied as bonuses per spell (damage, periodic damage, crit chance, crit bonus, hit chance, threat, chance not to lose casting time), each in the one form its op is stored in. With phase 2 that is 170 of 275. Crit chance, crit damage bonus, damage and periodic damage percents, shown as bonuses per spell (not yet damage numbers).
4. **Base damage numbers** (later, optional). Effect base values and how they scale with level and spell power; this is where the combat model starts.

## Open questions

These need the game, not the data. They go on the in-game list once phase 1 shows which talents they affect:

- Do percent modifiers on the same property add or multiply? Is the flat part applied before or after?
- Do modifiers reach spells their mask matches but their text does not name? (Phase 1's audit will list any.)
- Rounding of cast times and costs.

## Decisions

- Phases 1-3 now; phase 4 later, with the combat model.
- Spell changes appear below the stat sheet in the Stat sheet tab, not in a tab of their own.
