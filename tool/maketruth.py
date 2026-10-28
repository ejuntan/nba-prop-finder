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
devig = [{"o": o, "u": u, "po": M.devig(o, u)[0], "pu": M.devig(o, u)[1],
          "ev_o": M.ev(0.55, o), "kelly_o": M.kelly(0.55, o),
          "a2p": M.american_to_prob(o), "p2a": M.prob_to_american(0.5349)}
         for o, u in odds]
with open(os.path.join(DIR, "truth.json"), "w") as fh:
    json.dump({"calib": {s: cal[s] for s in cal}, "cases": cases,
               "odds": devig}, fh)
print(f"truth: {len(cases)} distribution cases, {len(devig)} odds cases")
