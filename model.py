"""Turning a projection into a price.

Two steps, fitted on a training season and then left alone:

1. Recalibrate the mean. The raw projection is a product of shrunk
   factors, and shrinkage pulls the extremes toward the middle. A
   log-linear fit, log(mu) = a + b*log(raw), pushes them back out. b < 1
   would mean the raw projection is already too spread; b > 1 means it is
   too timid.

2. Fit a dispersion. Counting stats in basketball are overdispersed
   relative to Poisson -- minutes vary, foul trouble happens, blowouts
   empty benches -- so the count is negative binomial. But the usual
   single-parameter form, Var = mu + mu^2/r, is the wrong shape here.
   Measured in bins of projected points, variance over mean is roughly
   flat at 3.3 from a 4-point bench player up to a 23-point starter,
   while Var = mu + mu^2/r with one r rises quadratically: fitted on the
   bulk of the rows it gives a 23-point scorer a variance of 171 where
   the data says 77. Over 29.5 then looks like a live bet when it is not.

   So the dispersion is a two-parameter power law,

       Var = mu + c * mu^p,   i.e.  r(mu) = mu^(2-p) / c

   with c and p fitted per stat by maximum likelihood. p = 1 recovers the
   quasi-Poisson shape the data actually shows (points lands at p ~ 0.8,
   rebounds and assists nearer 1.1); p = 2 would recover the textbook
   negative binomial. Where a stat comes out underdispersed the fit falls
   back to Poisson rather than pretending.

3. Recalibrate the probability. With the mean and the variance both
   right, the shape can still be wrong: the negative binomial is more
   right-skewed than a basketball box score, so its median sits too low
   and P(over) at the main number comes out a couple of points light on
   the points-based stats, while the far tails run a point or so heavy.
   A two-parameter map on the log odds cannot fix both at once -- pulling
   the middle up pushes the tail further out -- so the correction is an
   isotonic (monotone, non-parametric) curve fitted per stat on the
   training season and stored as a 41-knot table. It is monotone by
   construction, so it never reorders one prop against another.

   Two details that are not optional. The lines it is fitted over are
   drawn at random offsets rather than on a fixed grid of sigmas: a fixed
   grid leaves the raw probability clustered, and the gap it leaves
   happens to sit right around 0.5, which is where real prop lines are
   hung. Fitted on a grid, the curve found 240 stray rows in that gap and
   put a fourteen-point step in the middle of the correction. And each
   knot is shrunk back toward no correction at all by how much data stands
   under it, so a thin stretch of the curve stays close to the identity
   instead of inventing an edge.

Everything downstream -- the backtest, the slate board, the single-prop
pricer -- reads the constants from data/calib.json and nothing refits.
"""
import json
import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import nbinom, poisson
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import PoissonRegressor

DIR = __file__.rsplit("/", 1)[0]
CAL = DIR + "/data/calib.json"

# A prop is only graded if the player takes the floor, so every
# distribution here is conditional on playing. Availability is reported
# alongside it, never folded into it.
MIN_PLAYED = 4.0
# A player needs at least one game on record to have a rate at all. The old
# floor of twelve was quietly deleting October: with three games played, a
# rookie starting and going thirty-five minutes a night had no history by
# that standard and simply did not appear. He is priced now, with the rate
# shrunk hard toward the league mean and the thinness shown on his face --
# see THIN_GAMES. The backtest below measures this population, not a
# comfortable subset of it.
MIN_GAMES_SEEN = 1
# At or below this, say so wherever the number shows. The scale is the
# exponentially weighted game count, not a plain tally, and it moved when
# SEASON_CARRY dropped to 0.15: a settled player now tops out near 11
# rather than near 35, so the old threshold of 15 flagged the entire
# league. Five picks out the players who really are new.
THIN_GAMES = 5

PRETTY = {"pts": "Points", "reb": "Rebounds", "ast": "Assists",
          "fg3m": "Threes", "stl": "Steals", "blk": "Blocks",
          "tov": "Turnovers", "fga": "FG attempts", "fta": "FT attempts",
          "oreb": "Off. rebounds", "dreb": "Def. rebounds", "fgm": "FG made",
          "pra": "Pts+Reb+Ast", "pr": "Pts+Reb", "pa": "Pts+Ast",
          "ra": "Reb+Ast", "stocks": "Stl+Blk"}


# ------------------------------------------------------------ the fit

R_MIN, R_MAX = 0.25, 5e4


def r_of(mu, c):
    """Dispersion at this mean. Var = mu + c*mu^p  <=>  r = mu^(2-p)/c."""
    cc, pp = c.get("c"), c.get("p")
    if not cc:
        return None
    mu = np.maximum(np.asarray(mu, float), 1e-6)
    return np.clip(mu ** (2.0 - pp) / cc, R_MIN, R_MAX)


def _nb_nll(theta, y, mu):
    """Negative log likelihood of the two-parameter dispersion."""
    cc, pp = math.exp(theta[0]), theta[1]
    if not (0.2 <= pp <= 2.2) or not (1e-4 <= cc <= 50):
        return 1e18
    r = np.clip(mu ** (2.0 - pp) / cc, R_MIN, R_MAX)
    q = r / (r + mu)
    v = (gammaln(y + r) - gammaln(r) - gammaln(y + 1) +
         r * np.log(np.maximum(q, 1e-300)) +
         y * np.log(np.maximum(1 - q, 1e-300)))
    return -float(np.sum(v)) if np.all(np.isfinite(v)) else 1e18


def fit_stat(raw, y):
    """Log-linear mean calibration, then the dispersion that fits it.

    The calibration is a Poisson GLM on log(raw), not a least-squares line
    through log(y): half the rows in a thin stat are zeros, and a zero has
    no logarithm. Flooring them instead -- log(max(y, 0.25)) -- drags the
    low end down and inflates the slope, which reads as "the projection is
    too timid" when the truth is that the fit was."""
    ok = (raw > 0.05) & np.isfinite(raw) & np.isfinite(y)
    a, b = 0.0, 1.0
    if ok.sum() > 400:
        X = np.log(raw[ok]).reshape(-1, 1)
        g = PoissonRegressor(alpha=1e-10, fit_intercept=True, max_iter=800)
        g.fit(X, y[ok])
        b = float(np.clip(g.coef_[0], 0.5, 1.6))
        a = float(g.intercept_)
        # Re-centre so the calibrated mean reproduces the observed total.
        mu = np.exp(a + b * np.log(np.maximum(raw, 1e-6)))
        a += math.log(max(y.mean(), 1e-6) / max(mu.mean(), 1e-6))
    mu = np.maximum(np.exp(a + b * np.log(np.maximum(raw, 1e-6))), 1e-4)
    out = {"a": float(a), "b": float(b)}
    excess = float(((y - mu) ** 2 - mu).mean())
    if excess <= 1e-6:
        return {**out, "c": None, "p": None, "dispersion": "poisson",
                "var_mean_ratio": float(((y - mu) ** 2).mean() /
                                        max(mu.mean(), 1e-9))}
    # Start from the quasi-Poisson shape the binned variance suggests, then
    # let the likelihood move p.
    c0 = max(excess / max((mu ** 1.0).mean(), 1e-9), 1e-3)
    best, theta = None, None
    for p0 in (0.7, 1.0, 1.3):
        r = minimize(_nb_nll, x0=[math.log(c0), p0], args=(y, mu),
                     method="Nelder-Mead",
                     options={"xatol": 1e-4, "fatol": 1e-3, "maxiter": 2000})
        if best is None or r.fun < best:
            best, theta = r.fun, r.x
    cc, pp = float(math.exp(theta[0])), float(theta[1])
    if cc < 1e-4:
        return {**out, "c": None, "p": None, "dispersion": "poisson"}
    return {**out, "c": cc, "p": pp, "dispersion": "nb-meanvar",
            "nll": best / max(len(y), 1)}


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


SHAPE_KNOTS = np.linspace(0.0, 1.0, 41)
SPAN = (-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0)
SHAPE_DRAWS = 16        # random lines per row, to fill the grid's gaps
SHAPE_K = 1500.0        # pairs under a knot before it is trusted outright


def sample_lines(mu, c, draws=SHAPE_DRAWS, seed=11, spread=2.3):
    """Lines scattered around each projection, so no band of probability
    is left empty by the sampling itself."""
    rng = np.random.default_rng(seed)
    s = sd(mu, c)
    for _ in range(draws):
        k = rng.uniform(-spread, spread, size=len(mu))
        yield np.floor(np.maximum(mu + k * s, 0.5)) + 0.5


def fit_shape(mu, y, c):
    """The monotone map that straightens the shape."""
    P, Y = [], []
    for line in sample_lines(mu, c):
        P.append(p_over_vec(mu, c, line, shape=False))
        Y.append((y > line).astype(float))
    P, Y = np.concatenate(P), np.concatenate(Y)
    ok = np.isfinite(P) & np.isfinite(Y)
    if ok.sum() < 5000:
        return list(SHAPE_KNOTS)
    P, Y = P[ok], Y[ok]
    g = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True,
                           out_of_bounds="clip")
    g.fit(P, Y)
    out = g.predict(SHAPE_KNOTS)
    # Shrink each knot toward no correction by the weight of evidence
    # standing under it.
    half = 0.5 / (len(SHAPE_KNOTS) - 1)
    n = np.array([np.sum(np.abs(P - x) <= half) for x in SHAPE_KNOTS], float)
    w = n / (n + SHAPE_K)
    out = w * out + (1 - w) * SHAPE_KNOTS
    out[0], out[-1] = 0.0, 1.0
    return [float(v) for v in np.maximum.accumulate(out)]


def fit(df, stats, out=CAL):
    cal = {}
    for s in stats:
        raw = df[f"proj_{s}"].to_numpy(float)
        y = df[s].to_numpy(float)
        c = fit_stat(raw, y)
        c["n"] = int(len(y))
        c["shape"] = fit_shape(mean(raw, c), y, c)
        cal[s] = c
    with open(out, "w") as fh:
        json.dump(cal, fh, indent=2, sort_keys=True)
    return cal


def load(path=CAL):
    with open(path) as fh:
        return json.load(fh)


# ------------------------------------------------------- distributions

def mean(raw, c):
    """Calibrated mean from the raw factor product."""
    raw = np.maximum(np.asarray(raw, float), 1e-6)
    return np.maximum(np.exp(c["a"] + c["b"] * np.log(raw)), 1e-4)


def dist(mu, c):
    """A frozen distribution for one projected mean."""
    mu = float(max(mu, 1e-4))
    r = r_of(mu, c)
    if r is None:
        return poisson(mu)
    r = float(r)
    return nbinom(r, r / (r + mu))


def sd(mu, c):
    """Standard deviation of the priced distribution."""
    mu = np.asarray(mu, float)
    r = r_of(mu, c)
    return np.sqrt(mu if r is None else mu + mu ** 2 / r)


def shape(p, c):
    """Apply the fitted monotone map."""
    tab = c.get("shape")
    if not tab:
        return np.asarray(p, float)
    return np.interp(np.asarray(p, float), SHAPE_KNOTS, np.asarray(tab, float))


def p_over_vec(mu, c, line, shape=True):
    """P(over) for arrays of means and lines, in one shot."""
    mu = np.maximum(np.asarray(mu, float), 1e-4)
    line = np.asarray(line, float)
    r = r_of(mu, c)
    lo = np.floor(line)
    if r is None:
        p = poisson.sf(lo, mu)
    else:
        p = nbinom.sf(lo, r, r / (r + mu))
    return globals()["shape"](p, c) if shape else p


def p_over(mu, c, line):
    """(P over, P under, P push). Half-point lines never push."""
    over = float(p_over_vec(mu, c, line))
    if not float(line).is_integer():
        return over, 1.0 - over, 0.0
    # An integer line can push, and the shape map is fitted on P(over), so
    # the push is taken out of the remaining mass.
    raw_push = float(dist(mu, c).pmf(line))
    raw_over = float(p_over_vec(mu, c, line, shape=False))
    push = raw_push * (1 - over) / max(1 - raw_over, 1e-9)
    push = min(push, 1 - over)
    return over, 1.0 - over - push, push


def quantiles(mu, c, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    d = dist(mu, c)
    return [int(d.ppf(q)) for q in qs]


def fair_line(mu, c):
    """The line a book would hang to split the action, to the half point."""
    k = int(dist(mu, c).ppf(0.5))
    cands = np.array([k + off + 0.5 for off in range(-4, 5)])
    cands = cands[cands > 0]
    ps = p_over_vec(np.full(len(cands), mu), c, cands)
    return float(cands[int(np.argmin(np.abs(ps - 0.5)))])


# --------------------------------------------------------------- odds

def american_to_prob(o):
    o = float(o)
    return 100.0 / (o + 100.0) if o > 0 else -o / (-o + 100.0)


def prob_to_american(p):
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def to_decimal(o):
    """American to decimal.

    The books quote American and that is what gets stored, but every
    table and the whole page read in decimal: a decimal price is a
    multiplier you can apply to a stake, and its reciprocal is the
    implied probability, which makes the devig and the EV legible
    without a second conversion in your head. Only the display changes
    -- nothing recomputes from the rounded number."""
    o = float(o)
    return 1 + o / 100 if o > 0 else 1 + 100 / -o


def prob_to_decimal(p):
    """The fair decimal price of a probability."""
    return 1.0 / min(max(float(p), 1e-6), 1 - 1e-6)


def devig(over_odds, under_odds, method="power"):
    """Strip the hold. Multiplicative splits it proportionally, which
    overtaxes the favourite; the power method is closer to how books
    actually shade a two-way market."""
    a, b = american_to_prob(over_odds), american_to_prob(under_odds)
    if method == "multiplicative" or a + b <= 0:
        t = a + b
        return a / t, b / t
    # a^(1/k) + b^(1/k) rises with k, so bisect on that monotone sum.
    lo, hi = 0.01, 10.0
    for _ in range(80):
        k = 0.5 * (lo + hi)
        if a ** (1 / k) + b ** (1 / k) > 1:
            hi = k
        else:
            lo = k
    k = 0.5 * (lo + hi)
    return a ** (1 / k), b ** (1 / k)


def ev(p, odds):
    """Expected profit per unit staked at American odds."""
    o = float(odds)
    payout = o / 100.0 if o > 0 else 100.0 / -o
    return p * payout - (1 - p)


def kelly(p, odds):
    o = float(odds)
    b = o / 100.0 if o > 0 else 100.0 / -o
    f = (p * b - (1 - p)) / b
    return max(f, 0.0)


def hold(over_odds, under_odds):
    return american_to_prob(over_odds) + american_to_prob(under_odds) - 1.0
