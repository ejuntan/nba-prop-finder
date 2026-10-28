# NBA prop research

Projects every player on tonight's slate, prices any line you type against
that projection, and reports honestly how well the projections have held
up out of sample. Points, rebounds, assists, threes, steals, blocks,
turnovers, shot and free-throw attempts, and the five combination markets.

There is a browser board (`tool/index.html`), a command-line bench
(`price.py`), and a one-command daily refresh (`daily.sh`) &mdash; which
the board can also run itself, through `serve.py`, from a button in its
header.

The page is dark, full stop &mdash; it does not follow the system theme.
It has four tabs: **Board** (tonight's slate), **Sheet** (paste a
list of lines), **Player** (one player in depth) and **Method**. The board
shows eight columns by default &mdash; player, match, minutes, projection,
line, P(over), edge, EV, and the opponent's defensive rank &mdash; with
**More columns** adding standard deviation, the model's own line, both fair
prices, side, Kelly, availability and pace. The scorecard is not on the
page: it is a thing you read once, so it lives in `out/scorecard.txt`,
rewritten on every run, and is summarised below.

## The live site

The board also runs as a public website on Render, at a URL you can open
from a phone. What is deployed is a **copy**, not the tool: `site/` holds
the template and the night's data, and `site/web.py` splices them into a
page with the standard library. Nothing up there holds the API key,
nothing up there can spend a credit, and nothing up there can be made to
run a job &mdash; every route is a read.

That shape is forced by what Render's free plan actually is: no cron job,
no background worker, no persistent disk, and a free Postgres that
**expires thirty days after it is created**. A design that ran the chain
in the cloud would quietly stop working a month in. So the chain stays on
the machine that has the data and the key, and `daily.sh` ends by
publishing two files; Render redeploys on a push, so a push is the
deploy.

```bash
./.venv/bin/python publish.py          # copy tonight's board into site/
./.venv/bin/python publish.py --push   # and deploy it
```

One-time setup: create a repository on GitHub and push this one to it,
then on Render pick **New &rarr; Blueprint** and point it at the repo.
`render.yaml` describes the service, so there is nothing to configure by
hand. Afterwards, set `PUBLISH_PUSH=1` in the environment the nightly job
runs in and every run deploys itself.

Two things to know. A free instance **sleeps after fifteen minutes**
without traffic and takes about a minute to wake, so the first load of
the day is slow. And the site only changes when the chain runs and
pushes &mdash; if your machine is off, the board stays on the last night
it was given, which the header says plainly.

## Setup

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./daily.sh
```

The first run pulls three seasons of box scores (~2,700 games, a few
minutes). After that `daily.sh` takes about a minute and a half, most of it
the backtest.

## Using it

```bash
open tool/index.html                          # the board
./.venv/bin/python price.py "jokic"           # every market, one player
./.venv/bin/python price.py "jokic" pra 44.5 -115 -105
./.venv/bin/python price.py "jokic" reb 12.5 --last 20
./.venv/bin/python price.py scan out/lines.csv
./.venv/bin/python price.py board pts 25
./.venv/bin/python price.py def GS reb        # one defence, or `def all reb`
```

### The Refresh button

`tool/index.html` has the night's numbers baked into it, which is what
makes it portable &mdash; and also why a button inside it cannot refresh
anything on its own. Refreshing means running `daily.sh`: a couple of
minutes of Python ending in a rebuilt page. `serve.py` is the small local
server that lets the page start that run and watch it:

```bash
./.venv/bin/python serve.py          # then http://127.0.0.1:8765
```

**Auto** is on by default and spends nothing. Once a minute it asks the
server what the board on disk was built from; when that stamp differs
from the one baked into the open page, the page reloads itself &mdash;
which picks up the scheduled nightly run, or a refresh started in
another tab, not just one started here. If the slate on screen is for a
night already played, it rebuilds it, free, at most once an hour.
Spending is always a click: nothing automatic ever buys lines. A reload
is deferred while you are typing in a field or have anything in the
Sheet box, and a **Reload** button appears instead. The choice is
remembered per browser.

**Refresh** reruns the whole chain and streams its output into a panel
under the tabs; the page reloads itself only on a clean exit, so a failed
run leaves the old board up and the log on screen. **+ lines** is the one
click that spends money &mdash; it buys new prices for the least-sampled
games, 15 credits &mdash; and the button beside it tells you how many
credits the account had left when it was last told. Left unticked, a
refresh costs nothing: it reprices tonight against whatever lines are
already stored.

Three things guard that button, because a refresh can spend money. The
server binds to `127.0.0.1`; the POST that starts a run needs a token
minted at startup and injected into the page as it is served, carried in a
custom header so a cross-origin preflight has to pass first (and this
server refuses them); and the `Host` header is checked, so a DNS-rebinding
page cannot read the token out of the page. Opened straight off disk the
button disables itself and says what to run &mdash; it does not pretend.

Only one run happens at a time. `daily.sh` takes a `data/.running`
directory as a lock and exits 3 if one is already there, so a click
during the scheduled 06:30 run does nothing rather than interleaving
writes into the same SQLite files.

`price.py scan` takes a CSV of `name, stat, line, over_odds, under_odds`
and ranks what it finds by expected value. The board's **Line sheet** tab
does the same thing from pasted text, and prices it in the browser.

The **Player** tab takes a typed name &mdash; partial, surname or initials,
so `sga`, `joki` and `kd` all land &mdash; with arrow keys and Enter to pick
from the suggestions, and clicking any name on the board or the sheet jumps
straight to it. For whichever market is selected it shows a column chart of his
last 5, 10, 15 or 20 games &mdash; one bar per game, oldest to newest, grown
from zero, with the line drawn across them and each bar taking the colour of
the side it landed on. The count sits above it (*3 over / 7 under of the
last 10 at 51.5*), because that is the question being asked and nobody
should have to total the bars to answer it. Hovering a bar gives the
opponent, the date, the number and the minutes. Under it is the same ten
games as a table, and then the opposing defence — defensive rating, pace and what it concedes in that market per
100 possessions, each with its rank among the thirty. Every game in the log
carries that opponent's rank too, so a 34-point night against the 29th
defence does not read like a 34-point night against the 2nd. Rank 1 always
means stingiest, so green is the soft matchup.

Positions are ESPN's three &mdash; guard, forward, centre. Five would halve
each sample for a distinction that is mostly nominal. The three share the
possessions the defence actually faced, so they add back to the
all-positions figure exactly; whatever the three do not account for is
carried in a fourth, hidden bucket rather than dropped, because ESPN ships
24 team-games in three seasons whose totals row disagrees with its own
athlete list, and losing those silently would make the split quietly wrong.

Two windows are worth comparing and both are offered: the last 10 games,
which crosses the season break and is a real sample, and the current
season, which in October is three games deep and is what the narrative is
built on. Game counts and date ranges are shown so the thin one is obvious.

One caution on reading those ranks: they are today's rating, not the rating
on the night each game was played, so they are matchup context rather than
a record. And the model's own opponent factor — shown beside them — is the
shrunk, exponentially weighted version, deliberately milder than the raw
ranking. It is the one that actually moves the number.

## What it does

The projection is a product of pieces you can inspect one at a time:

    mean = per-minute rate x minutes x pace x opponent defence x home/away

**Rates** are exponentially weighted over the player's past games, shrunk
toward the league by how many minutes stand behind them.

**Minutes** are the whole game. The model keeps minutes *conditional on
playing* separate from the chance of playing at all, because a prop that
is not graded is a refund rather than a loss. Each team's expected minutes
are then adjusted to add to 240 — with the correction weighted by role, so
a 38-minute starter barely moves and the bench absorbs it. A player ruled
out on the ESPN injury report frees his minutes for everyone behind him,
which is where most of a night's real prop value lives.

**Pace and defence are kept apart.** A team that concedes a lot because it
plays fast is not the same as one that concedes a lot per possession, and a
model that cannot tell them apart chases the wrong teams all season.

**The distribution** is negative binomial with a fitted mean–variance power
law, `Var = μ + c·μ^p`. The textbook one-parameter version is the wrong
shape for basketball: measured in bins, variance over mean is roughly flat
at 3.3 from a four-point bench player to a 23-point starter, while the
one-parameter form gives that starter a variance of 171 where the data says
77 — which would make Over 29.5 look live when it is not. A monotone
isotonic correction on `P(over)`, fitted on the training season, straightens
out the remaining skew.

## The scorecard

Constants are fitted on **2024-25 only**. Everything below is 2025-26 and
2026-27, 25,724 player-games those constants never saw. Features are built
in one causal pass; `tests.py` proves it by rebuilding them from truncated
data and checking that nothing before the cut moves (it does not, to zero
difference).

**Calibration**, over 5.4M (row, line) pairs — the part that matters, since
a book's line already sits near the mean and what pays is knowing the shape
around it:

| Model said | n | Predicted | Actual |
|---|---:|---:|---:|
| 0–10% | 1,212k | 5.4% | 5.3% |
| 10–20% | 750k | 14.6% | 14.3% |
| 20–30% | 528k | 24.5% | 24.4% |
| 30–40% | 478k | 34.8% | 34.2% |
| 40–50% | 455k | 45.0% | 45.2% |
| 50–60% | 411k | 55.1% | 55.3% |
| 60–70% | 387k | 65.0% | 65.1% |
| 70–80% | 378k | 75.0% | 75.4% |
| 80–90% | 398k | 85.3% | 84.9% |
| 90–100% | 456k | 94.3% | 93.9% |

Brier 0.1473 against 0.2407 for quoting the base rate — 38.8% skill. Every
decile is inside 0.6 points.

**Accuracy** against the baseline a bettor has for free:

| Market | Model MAE | Last-10 MAE | Better by |
|---|---:|---:|---:|
| Points | 4.751 | 4.837 | 1.8% |
| Rebounds | 1.920 | 1.982 | 3.1% |
| Assists | 1.392 | 1.424 | 2.2% |
| Pts+Reb+Ast | 6.175 | 6.320 | 2.3% |
| Threes | 0.932 | 0.946 | 1.4% |
| Turnovers | 0.922 | 0.953 | 3.3% |
| Blocks | 0.536 | 0.539 | 0.5% |

**What the ablation says.** Switching off pace, opponent defence and
home/away together costs 0.15% of points MAE and 0.30% on Pts+Reb+Ast. Put
plainly: nearly all of the model's edge over a ten-game average comes from
separating minutes from per-minute rate. The matchup factors are close to
decoration. They stay in because they are free and correctly signed, not
because they earn their keep.

## Real lines

```bash
echo 'export ODDS_API_KEY=your-key' >> ~/.zprofile   # not in this repo
source ~/.zprofile
./.venv/bin/python fetch_odds.py      # tonight's slate
./.venv/bin/python edges.py           # lines against projections, ranked
```

The key is read from the environment and never stored, printed or passed
on a command line. `daily.sh` runs both stages when the variable is set
and skips them silently when it is not — everything else works without it.

**Credits, and what they buy.** Measured: **five credits per event** —
one per market, one region. A full NBA night averages 7.5 games and the
season runs 166 of them, so logging everything is about 1,120 credits a
month against a free tier of 500. The budget sets the sampling:

| Games a night | Credits/night | Per month | Free tier |
|---|---:|---:|---|
| 3 | 15 | 450 | fits |
| 4 | 20 | 600 | over |
| 8 (a full slate) | 37 | 1,120 | over |

So `daily.sh` passes `--budget 15` (override with `ODDS_BUDGET`; **0
spends nothing at all**, and any budget too small to pay for one whole
game buys nothing rather than rounding up to one), and
`fetch_odds.py` spends it on the clubs sampled least so far — coverage
spreads across the league over weeks rather than piling onto whoever tips
earliest. `--only-missing` skips events quoted in the last twelve hours,
and `fetch_odds.py quota` is free and worth running first.

Closing line value wants two fetches a night, which doubles the cost and
does not fit the free tier at a useful number of games. The single fetch
is timed late instead — half an hour before the first tip — so the lines
logged are near closing and the graded results are clean.

**A quote belongs to a game, not to a player.** Lines are matched to the
slate on the player *and* the game they were quoted for. Matching on the
player alone looks harmless and is not: on any night when a club was not
sampled, the last number stored for one of its players &mdash; Saturday's,
perhaps &mdash; would appear on tonight's board as the market, and a
projection that has since moved three points would read as a twenty-point
edge. A night with no quotes of its own shows no market at all, and
`edges.py` deletes the previous night's file rather than let the board
bake it in.

**Two different edges, kept apart.** Against the book's own devigged
two-way price, and against the *consensus* — the median no-vig
probability of every book quoting the same number, where three or more
are. The consensus is a far better estimate of the truth than any single
book, so `edge_cons` is the honest figure and `edge_book` is the one that
flatters a model which happens to be wrong in the same direction as one
soft line. Where only one book is quoting, the consensus column says so
rather than quietly reusing the first answer.

**A ceiling on believable edge.** The backtest says this model beats a
ten-game average by one to three per cent of projection error and is
calibrated to within a point. Nothing in it supports beating a four-book
consensus by twenty-five points of probability, so `edges.py` flags any
claim over ten per cent as a bug report on the model rather than ranking
it as a bet. It takes less disagreement than you would think to get
there: a gap of under one standard deviation between the line and the
projection is worth twenty-five points of probability near the middle of
a distribution, which is why the test is on the edge and not on the gap.

Every run prints two standing sanity checks. **Agreement:** correlation,
bias and MAE against the posted lines — on the first full slate, 0.975
and 1.44 points across 557 markets, which is a model broadly in step with
the market. **Side balance:** how often it lands on the over. That one is
currently failing, and it matters more than the agreement figure.

On the first real slate the model preferred the over in **69%** of
markets, and in every market type: points 71%, assists 73%, threes 67%,
PRA 67%, rebounds 59%. Against an efficient market a calibrated model
should land either side about half the time. Its projections sit about a
point above the lines on points (and so about 1.5 on PRA, which inherits
it), while rebounds, assists and threes are within a quarter of a point.

The backtest says the model is unbiased against *actual outcomes* —
points bias +0.07 over 25,000 test games — so either the market shades
points lines down, which books do, or the model is high here. Nothing in
this repository can yet tell you which, and until the logged lines have
been graded against results, that question is open and every points or
PRA over on the board inherits it.

On the board, a market line fills in automatically: **Line** and the
prices are the book's, **Mkt** is the consensus, **Over** is the model,
and the gap between the last two is the edge. **Price** and **Book**,
under More columns, are the best price being offered on the side the model
likes and who is offering it. Type over a line to price a different
number; the toolbar odds take over when you do.

Every snapshot is kept, timestamped. That is the point — the difference
between the first line a book hangs and the last before tip is closing
line value.

**Daily, unattended.** `com.tanejun.nba-daily.plist` runs the whole chain
at 06:30 local, which on a UTC+8 machine is 18:30 Eastern — half an hour
before the first tip, so the injury report is current and the lines are
near closing. Install it with:

```bash
cp com.tanejun.nba-daily.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.tanejun.nba-daily.plist
```

It deliberately carries no key: `fetch_odds.py` reads the gitignored
`.env`, so the key lives in one place.

## Grading

```bash
./.venv/bin/python grade.py
```

Every slate is logged to `data/slate_log.sqlite3` before it is
overwritten, because grading a line later needs the projection that was
live when the line was quoted — and the causal features cannot reproduce
it, carrying neither the injury report nor the 240-minute budget. The
number that was actually shown is the number that gets judged.

`grade.py` joins logged lines to logged projections to final box scores
and reports, in descending order of how much it matters: **calibration at
real market lines** (when the model said 60% at a number a book hung, did
it land 60%?), **return** on the model's preferred side bucketed by the
edge it claimed (if the edge is real, return rises with it), and **closing
line value** where a prop was quoted more than once. Pushes return the
stake rather than counting as losses, nothing is graded until the game is
final, and every table carries its sample size.

The arithmetic is proved in `tests.py` against a real completed box
score — a winning bet, a losing one on each side, and a push — so it is
known to be right before the first real result lands.

## What this cannot tell you

**It has not yet been scored against a market.** Until `fetch_odds.py` has
been running for a while, there is no closing prop number in this
repository, and the model has never been measured against one.
Calibration says the probabilities are honest; it says nothing about
whether they beat a book, which is a different and much harder bar. The
backtest's "naive-line test" — hanging the line at a last-ten average — shows
60–74% win rates, and that number is worthless as an edge estimate: a
ten-game average is a terrible line, and a real book is nowhere near that
soft. Until you have logged closing numbers and measured CLV, treat every
edge the tool reports as unproven.

**Thin records are priced, and labelled.** The only history requirement is
one game. A rookie who has never played a prior season gets a projection
built from what he has done, shrunk hard toward the league mean, and carries
a *thin record* flag wherever his number appears. The scorecard measures
this population rather than a comfortable subset of it, and splits
calibration by how deep the player's own record is: at 3&ndash;8 games the
probabilities are still honest (gap &minus;0.2 points) but carry noticeably
less skill than a settled player's (Brier skill 0.34 against 0.40 at 30+
games). An earlier floor of twelve games was quietly deleting October
&mdash; sixteen players a night, including rookies starting and playing
thirty-five minutes.

**October minutes are the weak point.** With two or three games on the
board, the minutes estimate is the shakiest input in the model, and minutes
are what the whole projection scales with. The board reports each player's
own recent minutes and the budget multiplier applied to them so you can see
when it is guessing.

**Availability is never priced in.** It sits beside the number, not inside
it.

**Players ruled out stay on the board**, greyed, with their status, their
usual minutes and no price &mdash; every pricing column reads as a dash,
the Sheet refuses them, and `price.py` says so and prints his record
instead. Who is missing is the thing that moves everyone else's minutes,
so a board that silently deletes him tells you nothing about the night.
They are excluded from the +EV filter, and a checkbox hides them. A
reserve who plays under eight minutes is not listed: his absence is not
news.

## Files

| File | What it does |
|---|---|
| `fetch.py` | ESPN public API -> `data/nba.sqlite3`. Stages `games`, `boxes`, `injuries`, all resumable. |
| `defense.py` | Defensive ratings, pace and per-100-possession rates allowed, ranked, over three windows &mdash; overall and split by the position of the player who scored it. |
| `features.py` | One causal pass: per-minute rates, minutes, pace, opponent defence, home/away. Writes the frame and the current state. |
| `model.py` | Mean calibration, the mean–variance dispersion, the isotonic correction, devig, EV and Kelly. |
| `tune.py` | Hyper-parameter sweep, scored on the training season only. |
| `backtest.py` | Walk-forward scorecard: accuracy, ablation, calibration, fair-line test. Writes `data/calib.json`. |
| `project.py` | Tonight's slate: the minutes budget, the injury report, a projection per player. |
| `fetch_odds.py` | The Odds API -> `data/odds.sqlite3`. Every snapshot kept and timestamped, so CLV becomes measurable. |
| `edges.py` | Lines against projections: edge versus the book, edge versus the consensus, best price per market. |
| `grade.py` | Logged lines + logged projections + final box scores: calibration at market lines, return by claimed edge, CLV. |
| `price.py` | The bench — one prop, a pasted sheet, or the night's biggest numbers. |
| `site/web.py` | The public copy: template + data spliced into one page, gzipped, read-only. No key, no writes, no pipeline. |
| `publish.py` | Copies the night's template and data into `site/`, and optionally commits and pushes, which is what deploys them. |
| `serve.py` | Serves the board on localhost so its Refresh button can run `daily.sh`, streams the run's output back, and gates the one endpoint that can spend money. |
| `export_tool.py`, `tool/build.py` | The board's data and the standalone page. |
| `tests.py` | The leak test, the pricing invariants, the credit budget and the server's refusals. |
| `tool/mathtest.js` | Checks the page's own negative binomial against scipy, 2,480 cases. |

The chart's two colours are a diverging pair &mdash; a cool pole above the
line, a warm one below &mdash; rather than green-for-good and red-for-bad,
since which side you want depends on which side you backed. They were
validated against the three dark surfaces they actually render on: worst
colour-vision-deficient separation 19.2 and normal-vision 29.0 in OKLab
(floors are 8 and 15), both clearing 3:1 contrast. Both sides carry a text
label in the legend, so the colour never has to say it alone.

## When ESPN will not answer

Two failure modes are worth knowing, because both were found by running
the chain repeatedly rather than once a day.

A single calendar date sometimes comes back as an **empty 200** &mdash;
not an error status, not JSON, just nothing &mdash; for hours at a time,
while every other day in the same window answers normally. That used to
raise out of the first stage and, under `set -e`, end the whole run, so
one unavailable past date meant no board. A day that cannot be read is
now skipped, left out of `days_done` so the next run retries it, and
reported; but if more than half the window fails the run still stops,
because that is an outage and nothing downstream should be built on it.

The scoreboard endpoint also dislikes width: ten days at once provokes
those empty responses, three does not. That stage runs at `DAY_WORKERS`,
separate from the ten used for box scores. And a day crawled in the last
`FRESH_MINUTES` is not crawled again, so several runs in an hour do not
re-fetch the same thirteen days &mdash; a schedule does not change that
fast.

## Dates

Every date in the database is the **US Eastern** date the game tips on,
and so is "today". Both halves of that matter, and getting either wrong
produces a board that looks plausible and is not the right slate.

A tip at 8pm Eastern or later is already tomorrow in UTC, so storing
ESPN's UTC date splits one night across two: the 7:30 games under one
date, the 8pm-and-later ones under the next, sitting alongside the
following evening's early game. And `date.today()` is the machine's own
date, which on a laptop in UTC+8 is a day ahead of Eastern for the whole
of an NBA evening &mdash; so the tool would ask for the wrong day and then
get half of it.

`fetch.py` also crawls eight days ahead. ESPN groups its scoreboard by its
own notion of the date, so a late Eastern tip comes back from the *next*
day's query; stopping the crawl at today leaves tonight's late games
stale.

## Data

ESPN's public NBA API. `stats.nba.com` and `cdn.nba.com` both answer 403
from this machine, which is why nothing here uses them. ESPN files the
All-Star game under the regular season with rosters called WORLD and
STARS; `features.py` drops any side with fewer than 50 games, so a
40-point exhibition never enters a player's rate.
