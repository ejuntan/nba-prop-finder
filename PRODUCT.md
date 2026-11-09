# NBA Prop Research

## What it is

A statistics tool for tonight's NBA player props. It reports **records**:
how often a player has cleared a given number, cut several ways. It is not
a prediction model and does not display projections — that was removed on
purpose.

## Who reads it

One person, the author, on a phone in the afternoon and a laptop in the
evening, between the slate going up and the first tip-off. Nobody is
onboarded, nobody needs the product explained in the interface, and there
is no funnel. The surface mode is **Operate**.

## Product truth

- **The line.** The book's number where one is quoted (marked `book`),
  otherwise the median of the player's own last twenty (marked `median`) —
  against which he cleared it half the time by construction.
- **The cuts.** Last 5/10/20, home or away, against tonight's opponent, and
  the games a team-mate who is out tonight also missed. A "without" split
  needs at least two games alongside that man before it means anything.
- **The honesty constraints.** A record is tonight's number carried
  backwards over games played against different numbers. Small samples are
  marked rather than hidden; `beyondNoise` is the test. Nothing on the page
  claims an edge.
- **Odds are decimal**, everywhere a person reads them. American is stored
  and graded, converted once at the boundary.
- **Parlays** are priced through a Gaussian copula on correlations measured
  from date-matched game logs, not by multiplying legs.

## Sections

Home (one flat board, strongest record first) · Trends · Parlay · Slip ·
Player. Explicitly out of scope, by the author's decision: alternate lines,
a cheat-sheet tab, a method page, SGP as its own section, team spreads and
totals, and a dedicated injury view.

## Constraints

Static HTML built by `tool/build.py`, served from Render's free tier. No
cron, no workers, no disk: the chain runs locally and a push is the deploy.
