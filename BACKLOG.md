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
- [ ] **Hit and spell hit.** The Hit row merges melee and spell hit when they are equal. Confirm with a talent that raises only one of them.

## Modelling more talents  [offline]

The full list is `docs/unmodelled_effects.tsv` (regenerate it after each change). Of 469 talents, 23 are fully modelled and 13 partly. What is left, by type:

- [ ] Auras decoded for the sheet but still conditional: 54, 290, 55, 49, 47, 51 (abilities with a duration, a single form).
- [ ] Undecoded auras that look like they touch the sheet: 232 (9 effects: Iron Will, Silent Resolve, Surefooted, Unyielding Faith), 79 (16, looks like a damage percent), 77 (16), 332 (10), 10 (9), 87 (5), 33 (4). Read the tooltips first; never assume the meaning from the aura number.
- [ ] Spell modifiers (auras 107 and 108, about 300 effects): they change particular spells (cost, damage, cast time), not the sheet. Needs a different engine and a model of the spells.
- [ ] Scripts (auras 4, 42, 109, 190 effects): the data holds numbers only; the tooltip says what they mean.
- [ ] Instant effects (no aura, 74).
- [ ] Hunter Bestial Discipline: the pet Focus part.
- [ ] Spell damage restricted to some schools (aura 174 with a school mask other than all schools).

## Interface  [offline]

- [ ] Show Spell power and Healing rows at levels with no exported panel when the base is unknown (today they appear only if a talent contributes).
- [ ] Two talent icons are missing (357 of 359 cached). Find which and why (possibly encrypted in the game files).
- [ ] Per-class defaults (race, level) remembered between visits.

## Talent tree backgrounds  [needs-source]

- [ ] Searches in wow.export keep returning the modern game's assets. Find the Classic talent frame art for this build, then add an importer like the icon one (`src/wowcalc/icons.py`) and draw it behind each specialization tree. The names are already in `data/external/spec_names.tsv`.

## Data upkeep  [offline]

- [ ] When a new build appears: `python -m wowcalc.update prepare` and `apply`, then `python -m wowcalc.diff`, then re-run `python -m wowcalc.rates fit` and the data checks in the UI. Spec names are tied to spells, so a moved talent shows up in the Data tab's "Specialization names" check.
- [ ] Per-level stat rates (`data/external/PlayerExpectedStat.csv`) were taken from build 1.60.1.70450; refetch when the build changes.

## Done recently (for context)

- Local UI with saved builds, not-modelled warnings and a data/confidence tab.
- Talent icons (357 of 359), the resizable divider, specialization names.
- Spell power and healing rows; spell power and healing from Intellect or Spirit talents.
- Parry row appears only where it applies; Hit row merges melee and spell hit.
- Mana regeneration while casting, as a percentage only.
