# Combat model: plan (spell model phase 4 and beyond)

Status: proposal for review (2026-11-02). Nothing below is built yet.

## Why

The calculator now says what each talent does to the sheet and to each spell (cost, cast time, crit chance, damage bonus...). It cannot yet say what a talent is worth in damage, healing or survival, which is the project's goal ("Delta = with talent - without talent", in DPS, HPS and survivability). That needs actual damage and healing numbers per spell, and then a way to turn them into a rate.

## What the data already gives (checked on 1.60.1.70707 with hotfixes)

Each damage or healing effect of a spell (SpellEffect) carries:

| Data | Example |
|---|---|
| Base amount (`EffectBasePointsF`) and its spread (`Variance`) | Frostbolt 475, +/- 7.5% |
| Spell power coefficient (`EffectBonusCoefficient`) | Shadow Bolt 0.857, Fireball 1.0, Corruption 0.2 per tick |
| Attack power coefficient (`BonusCoefficientFromAP`) | where a spell scales with attack power |
| Growth per level above the spell's level (`EffectRealPointsPerLevel`) | Frostbolt +3.2 per level |
| Ticks of a damage or heal over time: the aura's period and the spell's duration | Corruption every 3 s for 18 s |
| Weapon attacks: their own effect types | 17 "weapon damage + N" (Heroic Strike), 121 "normalized weapon damage" (Sinister Strike, Mortal Strike) |

Tooltip variables such as Frostbolt's `$<frostdamage>` turned out to be tooltip text only (a Piercing Ice multiplier, from `SpellDescriptionVariables`), not hidden scaling.

Phases 2 and 3 already give each spell's cast time, cost, cooldown and the talents' damage, crit and crit-bonus changes.

## What the data does not give

The combat rules live in the game's code, not its tables. The project's principles say unsupported mechanics are marked, never guessed, so each is listed here with how it would be handled:

| Rule | Needed for | Handling |
|---|---|---|
| Crit multipliers (spells x1.5, melee x2?) | every expected value | assumed, labelled, in-game check |
| Spell miss chance by level difference | casters | assumed, labelled, in-game check |
| Melee table (miss, dodge, parry, glancing, block) by level difference | melee | assumed, labelled, in-game check |
| Weapon damage from attack power (AP / 14 x speed; normalized speeds for effect 121) | melee and hunters | assumed, labelled, in-game check |
| How per-level growth is capped | every spell below max rank level | read against tooltips (they render the numbers), then in-game |
| Target armor and resistances | physical damage, partial resists | an input, default stated |

"Assumed" here means the published rules of the original game, marked as such everywhere they are used, in the same way the block rules are today, until a reading confirms them.

## Design

1. **Per-cast numbers first (no rotation).** For each spell at the highest rank the level allows: its damage or healing range, the part that comes from spell or attack power, the total of a damage or heal over time, and the expected amount per cast including crit chance, crit bonus and hit chance against a target of a chosen level. Then three rates that need no rotation: per cast, per second of cast time (or global cooldown), and per point of mana, rage or energy.
2. **Talent value per spell.** With and without each talent, as everywhere else: "Improved Frostbolt: Frostbolt +X damage per second of casting", "Critical Mass: Fireball +Y expected damage per cast".
3. **Weapon attacks.** Gear inputs for the weapon (minimum and maximum damage, speed, one- or two-handed, ranged), so white hits and weapon abilities get numbers too.
4. **A simple rate for the whole character.** The person enters how often they use each spell (casts per minute, or a share of their casting time); the calculator adds the per-cast values up into damage or healing per second, and the talents' value becomes "% of your DPS". No simulation, no guessed rotation.
5. **Later: simulation.** Procs, cooldown timing, resource limits and rotations, compared with the existing Forever Sim (github.com/ProfetGit/forever-sim) as a reference, not a source of truth.

Every number keeps its provenance: from the data, from an assumed rule (and which), or from an input.

## Phases

1. **Per-cast spell numbers** (medium): steps 1-2 for spells (not weapon attacks). Shown in the stat sheet's spell section and the Talent values tab.
2. **Weapon attacks** (medium): step 3.
3. **Character rate** (medium): step 4.
4. **Simulation** (large, later).

## Decisions I need

1. **Assumed combat rules:** use the original game's published rules, clearly labelled as assumed until checked in game (recommended), or show only numbers that need no such rule (per-cast amounts before hit and crit)?
2. **Default target:** a boss (level 63, higher miss chance) or a target of your own level?
3. **Scope now:** phase 1 only, then review? (recommended)
