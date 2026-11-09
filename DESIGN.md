# Design

A working screen first. The world shows up as type, palette, density and
one signature move; it never supplies the navigation or the controls.

## Signature move

**The strip.** Every record on this page is a count of games, so every
record shows those games: one mark each, oldest on the left, teal where the
number was cleared and red where it was not, a neutral mark for a push.
The marks a card shows are the games that card's record counted — a
headline of 8 from 8 can never sit over a row containing a miss. It is the
only element with an entrance, a 500ms clip-path wipe left to right, in the
direction the games were played, and it is suppressed under
`prefers-reduced-motion`.

## Palette

Two colour systems, kept apart on purpose:

- **Violet is the interface** — links, the selected tab, focus, selection,
  the slip badge.
- **Teal and red are the data** — a diverging pair around the line, with a
  neutral for a push.

They are separated because an accent in the same hue family as a data mark
muddies both. Measured in OKLab against protanopia, deuteranopia and
tritanopia, over/under stay 9.5 apart and neither is confusable with the
violet. Status (green, red, amber) always ships with a number or a word, so
colour never carries meaning alone.

| | dark | light |
|---|---|---|
| ground / panel / raised | `#0a0d14` `#121725` `#1b2131` | `#f6f7f9` `#ffffff` `#eef0f4` |
| ink / secondary / tertiary | `#eef2f8` `#a8b3c6` `#8b96ab` | `#111621` `#59616f` `#616977` |
| accent | `#b49bff` | `#6336c9` |
| over / under / push | `#2ec4b6` `#e8736f` `#6b7689` | `#0f766e` `#ad3a35` `#8a93a2` |
| good / bad / warn | `#58d68d` `#ff7b72` `#e3b341` | `#15774a` `#b3342b` `#7a4a00` |

Dark is the base, because the scene is an evening one. Light is its own set
of steps, not an inversion. **Every ink token clears 4.5:1 on all three of
its surfaces in both themes**, computed rather than judged. The system
chooses unless the reader overrides it, and the override is remembered.

## Type

Inter, one face, loaded with `display=swap` behind a preconnect. Figures are
measurement, so they are tabular everywhere they appear in a column.
Monospace is reserved for the refresh log, which is genuinely terminal
output, and is not used as a costume for "technical". Nothing on the page
is below 12px.

## Density and space

A 4px ladder (`--s1`…`--s6`) and three radii. Depth comes from a surface
ladder plus shadows that carry both an offset and a blur — never a
zero-offset halo. Cards are a grid of one prop each; nothing nests.

## The parts no stylesheet draws

Selection, caret, scrollbars, focus rings and the tab order are themed from
the palette. Focus is a 2px accent ring at 2px offset, on `:focus-visible`
only. A skip link precedes the masthead. A player's name is a `button`,
because it opens him.
