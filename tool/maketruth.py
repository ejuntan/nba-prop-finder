"""Truth file for the page's own maths test: scipy's answers, for jsc to match."""
import json
import os

import numpy as np

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import model as M

DIR = os.path.dirname(os.path.abspath(__file__))
cal = M.load()
rng = np.random.default_rng(7)
cases = []
for stat in ("pts", "reb", "ast", "pra", "fg3m", "blk", "stl"):
    c = cal[stat]
    for mu in (0.3, 0.8, 1.6, 3.2, 5.5, 9.0, 14.0, 22.0, 31.0, 47.0):
        for off in (-6, -2, -0.5, 0, 0.5, 2, 6):
            line = round(max(mu + off, 0.5) * 2) / 2
            po, pu, push = M.p_over(mu, c, line)
            cases.append({"stat": stat, "mu": mu, "line": line,
                          "p_over": po, "p_push": push,
                          "sd": float(M.sd(mu, c)),
                          "fair": M.fair_line(mu, c),
                          "q50": M.quantiles(mu, c, (0.5,))[0]})
odds = [(-110, -110), (-115, -105), (120, -145), (-250, 200), (-1000, 600)]


def to_decimal(a):
    """The page shows decimal odds; this is the conversion it uses."""
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


devig = [{"o": o, "u": u, "po": M.devig(o, u)[0], "pu": M.devig(o, u)[1],
          "ev_o": M.ev(0.55, o), "kelly_o": M.kelly(0.55, o),
          "a2p": M.american_to_prob(o), "p2a": M.prob_to_american(0.5349),
          "dec_o": to_decimal(o), "dec_u": to_decimal(u),
          "p2d": 1 / 0.5349}
         for o, u in odds]
# The parlay maths: a normal distribution and a Gaussian copula. The page
# combines correlated legs with these, so they get the same treatment as
# the negative binomial -- scipy says what the answer is.
from scipy.stats import multivariate_normal, norm

zs = [-6.0, -3.5, -2.0, -1.0, -0.4, 0.0, 0.3, 1.0, 2.5, 4.0, 6.0]
normal = {"cdf": [{"z": z, "p": float(norm.cdf(z))} for z in zs],
          "ppf": [{"p": q, "z": float(norm.ppf(q))}
                  for q in (1e-6, 1e-3, 0.02, 0.1, 0.35, 0.5, 0.62,
                            0.9, 0.995, 1 - 1e-6)]}

# P(both legs land) for two legs of probability pa, pb correlated rho.
joint = []
for pa, pb in ((0.55, 0.55), (0.6, 0.45), (0.8, 0.3), (0.5, 0.5),
               (0.72, 0.68)):
    for rho in (-0.4, 0.0, 0.25, 0.6, 0.9):
        za, zb = norm.ppf(1 - pa), norm.ppf(1 - pb)
        # P(X > za, Y > zb) by symmetry of the bivariate normal
        p = float(multivariate_normal(mean=[0, 0],
                                      cov=[[1, rho], [rho, 1]]).cdf([-za, -zb]))
        joint.append({"pa": pa, "pb": pb, "rho": rho, "p": p})

with open(os.path.join(DIR, "truth.json"), "w") as fh:
    json.dump({"calib": {s: cal[s] for s in cal}, "cases": cases,
               "odds": devig, "normal": normal, "joint": joint}, fh)
print(f"truth: {len(cases)} distribution cases, {len(devig)} odds cases, "
      f"{len(normal['cdf']) + len(normal['ppf'])} normal, "
      f"{len(joint)} parlay cases")
